from flask import Flask

from config import Config
from services import db_mariadb, db_mssql
from services.logging_setup import init_logging
from api.resource import bp as resource_bp
from api.moesk import bp as moesk_bp
from api.backup import bp as backup_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    app.json.ensure_ascii = False

    init_logging(app)
    db_mariadb.init_app(app)
    db_mssql.init_app(app)

    app.register_blueprint(resource_bp)
    app.register_blueprint(moesk_bp)
    app.register_blueprint(backup_bp)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(
        host=app.config["HOST"],
        port=app.config["PORT"],
        debug=app.config["DEBUG"],
    )
