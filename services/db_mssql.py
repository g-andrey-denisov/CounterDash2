import pyodbc
from flask import current_app, g


def get_db() -> pyodbc.Connection:
    if "db_mssql" not in g:
        cfg = current_app.config
        dsn = (
            f"DRIVER={{{cfg['MSSQL_DRIVER']}}};"
            f"SERVER={cfg['MSSQL_HOST']},{cfg['MSSQL_PORT']};"
            f"DATABASE={cfg['MSSQL_DB']};"
            f"UID={cfg['MSSQL_USER']};"
            f"PWD={cfg['MSSQL_PASSWORD']};"
        )
        g.db_mssql = pyodbc.connect(dsn, autocommit=True)
    return g.db_mssql


def close_db(e=None):
    db = g.pop("db_mssql", None)
    if db is not None:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)
