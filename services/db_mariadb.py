import pymysql
import pymysql.cursors
from flask import current_app, g


def get_db() -> pymysql.connections.Connection:
    if "db_mariadb" not in g:
        cfg = current_app.config
        g.db_mariadb = pymysql.connect(
            host=cfg["MARIADB_HOST"],
            port=cfg["MARIADB_PORT"],
            user=cfg["MARIADB_USER"],
            password=cfg["MARIADB_PASSWORD"],
            database=cfg["MARIADB_DB"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
    return g.db_mariadb


def close_db(e=None):
    db = g.pop("db_mariadb", None)
    if db is not None:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)
