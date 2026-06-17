import os

from flask import Blueprint, current_app, jsonify, request

from services import sheets_backup
from services.sheets import get_sheets_service

bp = Blueprint("backup", __name__, url_prefix="/api/v1/backup")


@bp.get("/list")
def list_backups():
    """List available backup files.

    Query params:
      spreadsheet_id — filter by spreadsheet (optional)
      sheet_name     — filter by sheet tab name (optional)
    """
    backup_dir = current_app.config["SHEETS_BACKUP_DIR"]
    spreadsheet_id = request.args.get("spreadsheet_id") or None
    sheet_name = request.args.get("sheet_name") or None

    items = sheets_backup.list_backups(backup_dir, spreadsheet_id, sheet_name)
    return jsonify({"backups": items, "count": len(items)})


@bp.post("/create")
def create_backup():
    """Create a backup of a Google Sheets tab.

    Body (JSON):
      spreadsheet_id — defaults to GOOGLE_SHEETS_RESOURCE_ID from config
      sheet_name     — defaults to GOOGLE_SHEETS_RESOURCE_SHEET from config
    """
    data = request.get_json(force=True, silent=True) or {}
    spreadsheet_id = (data.get("spreadsheet_id") or "").strip()
    sheet_name = (data.get("sheet_name") or "").strip()

    if not spreadsheet_id or not sheet_name:
        return jsonify({"error": "spreadsheet_id and sheet_name are required"}), 400

    svc = get_sheets_service()
    backup_dir = current_app.config["SHEETS_BACKUP_DIR"]
    max_days = current_app.config["SHEETS_BACKUP_MAX_DAYS"]

    path = sheets_backup.backup_sheet(svc, spreadsheet_id, sheet_name, backup_dir, max_days)
    return jsonify({"file": os.path.basename(path), "path": path}), 201


@bp.post("/restore")
def restore_backup():
    """Restore a Google Sheets tab from a local backup file.

    Body (JSON):
      file           — backup filename (basename only, required)
      spreadsheet_id — override target spreadsheet (optional)
      sheet_name     — override target sheet tab (optional)
    """
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get("file", "").strip()

    if not filename:
        return jsonify({"error": "file is required"}), 400

    # Guard against path traversal
    if os.sep in filename or "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "invalid filename"}), 400

    backup_dir = current_app.config["SHEETS_BACKUP_DIR"]
    backup_path = os.path.join(backup_dir, filename)

    if not os.path.isfile(backup_path):
        return jsonify({"error": "backup file not found"}), 404

    svc = get_sheets_service()
    result = sheets_backup.restore_sheet(
        svc,
        backup_path,
        spreadsheet_id=data.get("spreadsheet_id") or None,
        sheet_name=data.get("sheet_name") or None,
    )
    return jsonify(result)


@bp.post("/cleanup")
def cleanup():
    """Manually trigger cleanup of expired backup files.

    Query param:
      max_days — override retention period (optional, defaults to SHEETS_BACKUP_MAX_DAYS)
    """
    max_days_raw = request.args.get("max_days")
    max_days = (
        int(max_days_raw) if max_days_raw and max_days_raw.isdigit()
        else current_app.config["SHEETS_BACKUP_MAX_DAYS"]
    )
    backup_dir = current_app.config["SHEETS_BACKUP_DIR"]
    deleted = sheets_backup.cleanup_old_backups(backup_dir, max_days)
    return jsonify({"deleted": deleted, "max_days": max_days})
