"""Idempotently create the dedicated Plane role/database without argv secrets."""

import os

import psycopg
from psycopg import sql


host = os.environ["PLANE_PG_HOST"]
port = os.environ["PLANE_PG_PORT"]
role = os.environ["PLANE_PG_APP_USER"]
role_password = os.environ["PLANE_PG_APP_PASSWORD"]
database = os.environ["PLANE_PG_DATABASE"]
admin_user = os.environ["PLANE_PG_ADMIN_USER"]
admin_password = os.environ.get("PLANE_PG_ADMIN_PASSWORD", "")


with psycopg.connect(
    host=host,
    port=port,
    user=admin_user,
    password=admin_password,
    dbname="postgres",
    autocommit=True,
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(sql.Identifier(role), sql.Literal(role_password))
            )

        cursor.execute(
            "SELECT datname FROM pg_database WHERE datname = %s",
            (database,),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
        else:
            cursor.execute(
                """
                SELECT pg_get_userbyid(datdba)
                FROM pg_database
                WHERE datname = %s
                """,
                (database,),
            )
            owner = cursor.fetchone()[0]
            if owner != role:
                raise SystemExit(
                    f"Database {database} is owned by {owner}; refusing to alter it."
                )

with psycopg.connect(
    host=host,
    port=port,
    user=role,
    password=role_password,
    dbname=database,
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone()[0] != role:
            raise SystemExit("Dedicated Plane database login verification failed.")

print("Dedicated Plane PostgreSQL role and database verified.")
