import pyodbc
from flask import current_app, g


def _build_dsn(cfg: dict) -> str:
    driver = cfg["MSSQL_DRIVER"]
    host = cfg["MSSQL_HOST"]
    port = cfg["MSSQL_PORT"]
    db = cfg["MSSQL_DB"]
    user = cfg["MSSQL_USER"]
    pwd = cfg["MSSQL_PASSWORD"]

    if driver.lower() == "freetds":
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={host};PORT={port};"
            f"DATABASE={db};"
            f"UID={user};PWD={pwd};"
            "TDS_Version=7.4;"
        )
    # Windows legacy "SQL Server" driver
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={host},{port};"
        f"DATABASE={db};"
        f"UID={user};PWD={pwd};"
    )


def get_db() -> pyodbc.Connection:
    if "db_mssql" not in g:
        g.db_mssql = pyodbc.connect(_build_dsn(current_app.config), autocommit=True)
    return g.db_mssql


def close_db(e=None):
    db = g.pop("db_mssql", None)
    if db is not None:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)
