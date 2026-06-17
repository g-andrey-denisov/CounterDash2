import os
from dotenv import load_dotenv

load_dotenv(override=True)


class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() in ("true", "1", "yes")
    HOST = os.getenv("FLASK_HOST", "127.0.0.1")
    PORT = int(os.getenv("FLASK_PORT", 5000))

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_MAX_DAYS = int(os.getenv("LOG_MAX_DAYS", 30))

    DAILY_REPORT_LIMIT = int(os.getenv("DAILY_REPORT_LIMIT", 50))
    MONTHLY_REPORT_LIMIT = int(os.getenv("MONTHLY_REPORT_LIMIT", 24))

    MARIADB_HOST = os.getenv("MARIADB_HOST", "127.0.0.1")
    MARIADB_PORT = int(os.getenv("MARIADB_PORT", 3306))
    MARIADB_DB = os.getenv("MARIADB_DB", "resource")
    MARIADB_USER = os.getenv("MARIADB_USER", "resource")
    MARIADB_PASSWORD = os.getenv("MARIADB_PASSWORD", "")

    MSSQL_DRIVER = os.getenv("MSSQL_DRIVER", "SQL Server")
    MSSQL_HOST = os.getenv("MSSQL_HOST", "127.0.0.1")
    MSSQL_PORT = int(os.getenv("MSSQL_PORT", 1433))
    MSSQL_DB = os.getenv("MSSQL_DB", "RMon4Dev")
    MSSQL_USER = os.getenv("MSSQL_USER", "flask_agent")
    MSSQL_PASSWORD = os.getenv("MSSQL_PASSWORD", "")

    GOOGLE_SA_KEY_PATH = os.getenv("GOOGLE_SA_KEY_PATH", "")
    GOOGLE_SHEETS_RESOURCE_ID = os.getenv("GOOGLE_SHEETS_RESOURCE_ID", "")
    GOOGLE_SHEETS_RESOURCE_SHEET = os.getenv("GOOGLE_SHEETS_RESOURCE_SHEET", "resource")
    GOOGLE_SHEETS_MOESK_ID = os.getenv("GOOGLE_SHEETS_MOESK_ID", "")
    GOOGLE_SHEETS_MOESK_SHEET = os.getenv("GOOGLE_SHEETS_MOESK_SHEET", "moesk")
    GOOGLE_SHEETS_AKRON_ID = os.getenv("GOOGLE_SHEETS_AKRON_ID", "")
    GOOGLE_SHEETS_AKRON_SHEET = os.getenv("GOOGLE_SHEETS_AKRON_SHEET", "akron")
    GOOGLE_SHEETS_ENGINEERING_ID = os.getenv("GOOGLE_SHEETS_ENGINEERING_ID", "")

    SHEETS_BACKUP_DIR = os.getenv("SHEETS_BACKUP_DIR", "backups/sheets")
    SHEETS_BACKUP_MAX_DAYS = int(os.getenv("SHEETS_BACKUP_MAX_DAYS", 32))
