import logging
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_instance: "SheetsService | None" = None


def get_sheets_service() -> "SheetsService":
    global _instance
    if _instance is None:
        from flask import current_app
        key_path = current_app.config.get("GOOGLE_SA_KEY_PATH", "")
        if not key_path:
            raise RuntimeError("GOOGLE_SA_KEY_PATH is not configured")
        _instance = SheetsService(key_path)
    return _instance


class SheetsService:
    """Google Sheets writer backed by a service account.

    Params key_cols: list of column indices forming the composite upsert key.
    If omitted — simple append without deduplication.
    """

    def __init__(self, key_path: str) -> None:
        creds = Credentials.from_service_account_file(key_path, scopes=_SCOPES)
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert_rows(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        rows: list[list[Any]],
        key_cols: list[int] | None = None,
    ) -> dict[str, int]:
        """Write rows to the sheet, updating existing rows that share the same key.

        Returns {"appended": N, "updated": N}.
        """
        if not rows:
            return {"appended": 0, "updated": 0}

        if key_cols is None:
            self._append(spreadsheet_id, sheet_name, rows)
            logger.info(
                "sheets append spreadsheet=%s sheet=%s rows=%d",
                spreadsheet_id, sheet_name, len(rows),
            )
            return {"appended": len(rows), "updated": 0}

        existing = self._read_all(spreadsheet_id, sheet_name)
        # Row 1 is treated as a header when the sheet is non-empty.
        header_rows = 1 if existing else 0
        data_rows = existing[header_rows:]

        key_to_data_idx = {
            self._make_key(r, key_cols): i for i, r in enumerate(data_rows)
        }

        to_update: list[tuple[int, list[Any]]] = []
        to_append: list[list[Any]] = []

        for row in rows:
            key = self._make_key(row, key_cols)
            if key in key_to_data_idx:
                # +1 for 1-indexing, +header_rows to skip header
                sheet_row = key_to_data_idx[key] + header_rows + 1
                to_update.append((sheet_row, row))
            else:
                to_append.append(row)

        for sheet_row, row in to_update:
            self._update_row(spreadsheet_id, sheet_name, sheet_row, row)

        if to_append:
            self._append(spreadsheet_id, sheet_name, to_append)

        logger.info(
            "sheets upsert spreadsheet=%s sheet=%s appended=%d updated=%d",
            spreadsheet_id, sheet_name, len(to_append), len(to_update),
        )
        return {"appended": len(to_append), "updated": len(to_update)}

    def read_sheet(self, spreadsheet_id: str, sheet_name: str) -> list[list[Any]]:
        """Return all rows from the sheet (including header if present)."""
        return self._read_all(spreadsheet_id, sheet_name)

    def clear_and_write(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        rows: list[list[Any]],
    ) -> None:
        """Clear the sheet and write all rows from A1."""
        self._svc.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=sheet_name,
        ).execute()
        if rows:
            self._svc.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_name}!A1",
                valueInputOption="USER_ENTERED",
                body={"values": rows},
            ).execute()
        logger.info(
            "sheets rewrite spreadsheet=%s sheet=%s rows=%d",
            spreadsheet_id, sheet_name, len(rows),
        )

    def insert_rows_top(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        rows: list[list[Any]],
        after_header: bool = True,
    ) -> None:
        """Insert rows at the top (row 2 by default), shifting existing rows down.

        With after_header=True the block is inserted starting at row 2, leaving
        the header (row 1) in place. rows are written in the given order, so the
        first element ends up topmost — pass them newest-first for reverse order.
        """
        if not rows:
            return
        sheet_id = self._sheet_id(spreadsheet_id, sheet_name)
        start_index = 1 if after_header else 0  # 0-based row index
        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": start_index,
                        "endIndex": start_index + len(rows),
                    },
                    "inheritFromBefore": False,
                }
            }]},
        ).execute()
        self._svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A{start_index + 1}",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()
        logger.info(
            "sheets insert-top spreadsheet=%s sheet=%s rows=%d",
            spreadsheet_id, sheet_name, len(rows),
        )

    def set_columns_datetime_format(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        col_indices: list[int],
        pattern: str = "dd.mm.yyyy hh:mm:ss",
    ) -> None:
        """Apply a DATE_TIME number format to whole columns (skipping the header row).

        Makes ISO datetime values written with USER_ENTERED display in the given
        pattern while remaining real date-time values (sortable, right-aligned).
        Idempotent — safe to call on every sync.
        """
        if not col_indices:
            return
        sheet_id = self._sheet_id(spreadsheet_id, sheet_name)
        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,  # keep header (row 1) untouched
                        "startColumnIndex": c,
                        "endColumnIndex": c + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "DATE_TIME", "pattern": pattern}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
            for c in col_indices
        ]
        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

    def ensure_header(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        header: list[str],
    ) -> None:
        """Create sheet tab if missing, then write header row if the sheet is empty."""
        self._ensure_sheet_exists(spreadsheet_id, sheet_name)
        existing = self._read_all(spreadsheet_id, sheet_name)
        if not existing:
            self._append(spreadsheet_id, sheet_name, [header])
            logger.info(
                "sheets header written spreadsheet=%s sheet=%s",
                spreadsheet_id, sheet_name,
            )

    def _ensure_sheet_exists(self, spreadsheet_id: str, sheet_name: str) -> None:
        """Create the sheet tab if it does not exist in the spreadsheet."""
        meta = self._svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
        if sheet_name not in titles:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
            ).execute()
            logger.info(
                "sheets tab created spreadsheet=%s sheet=%s",
                spreadsheet_id, sheet_name,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sheet_id(self, spreadsheet_id: str, sheet_name: str) -> int:
        """Numeric sheetId of a tab by title (needed for batchUpdate ops)."""
        meta = self._svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == sheet_name:
                return s["properties"]["sheetId"]
        raise ValueError(f"sheet {sheet_name!r} not found in spreadsheet")

    @staticmethod
    def _make_key(row: list[Any], key_cols: list[int]) -> tuple:
        result = []
        for c in key_cols:
            s = str(row[c]).strip() if c < len(row) else ""
            # Apostrophe prefix forces Sheets to store as text, but Sheets drops it on read-back
            if s.startswith("'"):
                s = s[1:]
            # Normalize pure-numeric strings: Sheets strips leading zeros on read-back
            result.append(s.lstrip("0") or "0" if s.isdigit() else s)
        return tuple(result)

    def _read_all(self, spreadsheet_id: str, sheet_name: str) -> list[list[Any]]:
        try:
            resp = (
                self._svc.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=sheet_name)
                .execute()
            )
            return resp.get("values", [])
        except HttpError as exc:
            logger.error("sheets read error spreadsheet=%s sheet=%s: %s", spreadsheet_id, sheet_name, exc)
            raise

    def _append(self, spreadsheet_id: str, sheet_name: str, rows: list[list[Any]]) -> None:
        self._svc.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=sheet_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    def _update_row(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        row_num: int,
        row: list[Any],
    ) -> None:
        self._svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()
