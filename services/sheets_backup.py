import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.sheets import SheetsService

logger = logging.getLogger(__name__)

_TS_FMT = "%Y-%m-%d_%H-%M-%S"


def _sanitize(s: str) -> str:
    return re.sub(r"[^\w\-]", "_", s)


def backup_sheet(
    svc: "SheetsService",
    spreadsheet_id: str,
    sheet_name: str,
    backup_dir: str,
    max_days: int,
) -> str:
    """Read all rows from sheet and save as JSON backup. Cleans up old backups after saving.

    Returns absolute path to the created backup file.
    """
    rows: list[list[Any]] = svc.read_sheet(spreadsheet_id, sheet_name)

    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now()
    filename = (
        f"{_sanitize(spreadsheet_id)}__{_sanitize(sheet_name)}__{ts.strftime(_TS_FMT)}.json"
    )
    out_path = out_dir / filename

    payload = {
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": sheet_name,
        "timestamp": ts.isoformat(),
        "row_count": len(rows),
        "rows": rows,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("sheets_backup created file=%s rows=%d", filename, len(rows))

    cleanup_old_backups(backup_dir, max_days)
    return str(out_path)


def restore_sheet(
    svc: "SheetsService",
    backup_path: str,
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
) -> dict:
    """Restore sheet from a backup JSON file via clear_and_write.

    If spreadsheet_id / sheet_name are omitted, the values from the backup are used.
    Returns {"spreadsheet_id", "sheet_name", "rows"}.
    """
    with open(backup_path, encoding="utf-8") as f:
        payload = json.load(f)

    target_id = spreadsheet_id or payload["spreadsheet_id"]
    target_sheet = sheet_name or payload["sheet_name"]
    rows: list[list[Any]] = payload["rows"]

    svc.clear_and_write(target_id, target_sheet, rows)
    logger.info(
        "sheets_backup restored file=%s spreadsheet=%s sheet=%s rows=%d",
        backup_path, target_id, target_sheet, len(rows),
    )
    return {"spreadsheet_id": target_id, "sheet_name": target_sheet, "rows": len(rows)}


def list_backups(
    backup_dir: str,
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
) -> list[dict]:
    """List backup files sorted by creation time (newest first).

    Optionally filter by spreadsheet_id and/or sheet_name.
    Returns list of metadata dicts (no row data).
    """
    backup_path = Path(backup_dir)
    if not backup_path.exists():
        return []

    result = []
    for f in sorted(backup_path.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with f.open(encoding="utf-8") as fp:
                meta = json.load(fp)
            sid = meta.get("spreadsheet_id", "")
            sname = meta.get("sheet_name", "")
            if spreadsheet_id and sid != spreadsheet_id:
                continue
            if sheet_name and sname != sheet_name:
                continue
            result.append({
                "file": f.name,
                "spreadsheet_id": sid,
                "sheet_name": sname,
                "timestamp": meta.get("timestamp"),
                "row_count": meta.get("row_count", len(meta.get("rows", []))),
                "size_bytes": f.stat().st_size,
            })
        except Exception as exc:
            logger.warning("sheets_backup unreadable file=%s: %s", f.name, exc)

    return result


def cleanup_old_backups(backup_dir: str, max_days: int) -> int:
    """Delete *.json backup files whose mtime is older than max_days.

    Returns number of deleted files.
    """
    backup_path = Path(backup_dir)
    if not backup_path.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=max_days)
    deleted = 0

    for f in backup_path.glob("*.json"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
                logger.info("sheets_backup expired deleted file=%s", f.name)
                deleted += 1
        except Exception as exc:
            logger.warning("sheets_backup cleanup error file=%s: %s", f.name, exc)

    return deleted
