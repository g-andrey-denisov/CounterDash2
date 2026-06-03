import logging
import logging.handlers
from pathlib import Path


def init_logging(app) -> None:
    log_dir = Path(app.root_path) / "logs"
    log_dir.mkdir(exist_ok=True)

    level = getattr(logging, app.config.get("LOG_LEVEL", "INFO"), logging.INFO)
    backup_count = app.config.get("LOG_MAX_DAYS", 30)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_dir / "app.log",
        when="midnight",
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    # Избегаем дублирования хэндлеров при hot-reload Flask
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(console_handler)

    # Подавляем избыточный вывод werkzeug в продакшене
    if not app.debug:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app.logger.info("Logging initialized (level=%s, keep=%d days)", app.config["LOG_LEVEL"], backup_count)
