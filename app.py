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


def create_table(metadata: Metadata) -> bool:
    """
    Create table as specified in metadata.

    If table already exists, and sql_up is up-to-date, do nothing. If
    sql_up has been changed, run the stored sql_drop, and create table
    as specified in new sql_up.

    Returns whether the table was created or not.
    """
    cmd = f"SELECT up, down FROM Tables WHERE table_name = %s"
    metadata.table_name = metadata.table_name.lower()
    cursor.execute(cmd, (metadata.table_name,))
    table_sql = cursor.fetchone()
    if not table_sql:
        # Execute create table
        cursor.execute(metadata.sql_up)

        # Store metadata
        cmd = f"INSERT INTO Tables(table_name, up, down) VALUES (%s, %s, %s)"
        cursor.execute(cmd, (metadata.table_name, metadata.sql_up, metadata.sql_down))

        return True
    elif table_sql[0] != metadata.sql_up:
        # Re-create
        cursor.execute(table_sql[1])  # old sql_down
        cursor.execute(metadata.sql_up)

        # Store new metadata
        cmd = f"UPDATE Tables SET up = %s, down = %s WHERE table_name = %s"
        cursor.execute(cmd, (metadata.sql_up, metadata.sql_down, metadata.table_name))

        return True

    return False


def send_hasura_api_query(query: object):
    return requests.post(
        "http://graphql-engine:8080/v1/metadata",
        headers={
            "X-Hasura-Admin-Secret": os.environ.get("HASURA_GRAPHQL_ADMIN_SECRET")
        },
        json=query
    )


# The below functions are used to adhere to Hasura's relationship nomenclature
# https://hasura.io/docs/latest/schema/postgres/using-existing-database/
# Possibly use the `inflect` module if they aren't sufficient
def plural(s: str) -> str:
    return s if s.endswith("s") else s + "s"


def singular(s: str) -> str:
    return s if not s.endswith("s") else s[:-1]


def infer_relationships(table_name: str) -> list[object]:
    """
    Use pg_suggest_relationships to infer any relations from foreign keys
    in the given table. Returns an array containing queries to track each
    relationship.

    See https://hasura.io/docs/latest/api-reference/metadata-api/relationship/
    """
    res = send_hasura_api_query({
        "type": "pg_suggest_relationships",
        "version": 1,
        "args": {
            "omit_tracked": True,
            "tables": [table_name]
        }
    })

    queries = []
    for rel in res.json()["relationships"]:
        if rel["type"] == "object":
            queries.append({
                "type": "pg_create_object_relationship",
                "args": {
                    "source": "default",
                    "table": rel["from"]["table"]["name"],
                    "name": singular(rel["to"]["table"]["name"]),
                    "using": {
                        "foreign_key_constraint_on": rel["from"]["columns"]
                    }
                }
            })
        elif rel["type"] == "array":
            queries.append({
                "type": "pg_create_array_relationship",
                "args": {
                    "source": "default",
                    "table": rel["from"]["table"]["name"],
                    "name": plural(rel["to"]["table"]["name"]),
                    "using": {
                        "foreign_key_constraint_on": {
                            "table": rel["to"]["table"]["name"],
                            "columns": rel["to"]["columns"]
                        }
                    }
                }
            })

    return queries


@app.post("/insert")
def insert(metadata: Metadata, payload: list[Any]):
    try:
        created = create_table(metadata)
    except (Exception, Error) as error:
        print("Error while creating PostgreSQL table:", error)
        connection.rollback()
        return {"status": "error", "error": str(error)}

    try:
        # Remove old data
        cmd = f'TRUNCATE {metadata.table_name} CASCADE'
        cursor.execute(cmd)

        # Insert new data
        values = [tuple(row[col] for col in metadata.columns) for row in payload]
        metadata.columns = [f'"{col}"' for col in metadata.columns]
        cmd = f'INSERT INTO {metadata.table_name}({", ".join(metadata.columns)}) VALUES ({", ".join(["%s"] * len(metadata.columns))})'
        cursor.executemany(cmd, values)
    except (Exception, Error) as error:
        print("Error while inserting into PostgreSQL table:", error)
        connection.rollback()
        return {"status": "error", "error": str(error)}

    connection.commit()

    # Run Hasura actions - must be done after transaction committed
    if created:
        # Track table
        send_hasura_api_query({
            "type": "pg_track_table",
            "args": {
                "source": "default",
                "schema": "public",
                "name": metadata.table_name.lower()
            }
        })

        # Allow anonymous access
        send_hasura_api_query({
            "type": "pg_create_select_permission",
            "args": {
                "source": "default",
                "table": metadata.table_name.lower(),
                "role": "anonymous",
                "permission": {
                    "columns": "*",
                    "filter": {},
                    "allow_aggregations": True
                }
            }
        })

        # Track relationships
        send_hasura_api_query({
            "type": "bulk",
            "args": infer_relationships(metadata.table_name.lower())
        })

    return {"status": "success"}


if __name__ == '__main__':
    port = os.environ.get('PORT') or "8000"
    uvicorn.run(app, host="0.0.0.0", port=int(port))
