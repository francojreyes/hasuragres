from typing import Any
import psycopg2
from psycopg2 import Error
import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import requests


class Metadata(BaseModel):
    table_name: str
    sql_up: str         # SQL to set UP table and related data types/indexes
    sql_down: str       # SQL to tear DOWN a table (should be the opp. of up)
    columns: list[str]  # list of column names that require insertion


connection = None
cursor = None
load_dotenv()
try:
    connection = psycopg2.connect(user=os.environ.get('POSTGRES_USER'),
                                  password=os.environ.get('POSTGRES_PASSWORD'),
                                  host="postgres",
                                  port=os.environ.get('POSTGRES_PORT'),
                                  database=os.environ.get('POSTGRES_DB'))
    cursor = connection.cursor()
    app = FastAPI(port=os.environ.get('PORT'))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost", "http://scraper"],
        # only allow from specific places, this service executes arbitrary SQL
        allow_methods=["*"],
        allow_headers=["*"],
    )
except (Exception, Error) as error:
    print("Error while connecting to PostgreSQL", error)
    if connection:
        connection.close()
        if cursor:
            cursor.close()
        print("PostgreSQL connection is closed")
    exit(1)


def create_table(metadata: Metadata, hasura_actions: list):
    """
    Create table as specified in metadata.

    If table already exists, and sql_up is up-to-date, do nothing. If
    sql_up has been changed, run the stored sql_drop, and create table
    as specified in new sql_up.

    Also tracks the table on Hasura.
    """
    cmd = f"SELECT up, down FROM Tables WHERE table_name = %s"
    metadata.table_name = metadata.table_name.lower()
    cursor.execute(cmd, (metadata.table_name,))
    table_sql = cursor.fetchone()
    if not table_sql:
        # Execute create table
        cursor.execute(metadata.sql_up)

        # Track on Hasura
        hasura_actions.append({
            "type": "pg_track_table",
            "args": {
                "source": "default",
                "schema": "public",
                "name": metadata.table_name.lower()
            }
        })

        # Store metadata
        cmd = f"INSERT INTO Tables(table_name, up, down) VALUES (%s, %s, %s)"
        cursor.execute(cmd, (metadata.table_name, metadata.sql_up, metadata.sql_down))
    elif table_sql[0] != metadata.sql_up:
        # Re-create
        cursor.execute(table_sql[1])  # old sql_down
        cursor.execute(metadata.sql_up)

        # Store new metadata
        cmd = f"UPDATE Tables SET up = %s, down = %s WHERE table_name = %s"
        cursor.execute(cmd, (metadata.sql_up, metadata.sql_down, metadata.table_name))


@app.post("/insert")
def insert(metadata: Metadata, payload: list[Any]):
    # Accumulate Hasura actions such as track table, since they need to be run
    # after committing the transaction
    hasura_actions = []

    try:
        create_table(metadata, hasura_actions)
    except (Exception, Error) as error:
        print("Error while creating PostgreSQL table:", error)
        connection.rollback()
        return {"status": "error", "error": str(error)}

    values = [tuple(row[col] for col in metadata.columns) for row in payload]
    metadata.columns = [f'"{col}"' for col in metadata.columns]
    cmd = f'INSERT INTO {metadata.table_name}({", ".join(metadata.columns)}) VALUES ({", ".join(["%s"] * len(metadata.columns))}) ON CONFLICT (id) DO UPDATE SET {", ".join([f"{col}=EXCLUDED.{col}" for col in metadata.columns])}'
    try:
        cursor.executemany(cmd, values)
        connection.commit()
    except (Exception, Error) as error:
        print("Error while inserting into PostgreSQL table:", error)
        connection.rollback()
        return {"status": "error", "error": str(error)}

    # Run Hasura actions
    for action in hasura_actions:
        requests.post(
            "http://graphql-engine:8080/v1/metadata",
            headers={
                "X-Hasura-Admin-Secret": os.environ.get("HASURA_GRAPHQL_ADMIN_SECRET")
            },
            json=action
        )

    return {"status": "success"}


if __name__ == '__main__':
    port = os.environ.get('PORT') or "8000"
    uvicorn.run(app, host="0.0.0.0", port=int(port))