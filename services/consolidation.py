import logging
from dataclasses import dataclass, field
from datetime import date, datetime

logger = logging.getLogger(__name__)


@dataclass
class SheetSchema:
    """Column layout for a Google Sheet to be consolidated.

    col_year      : column index containing the year (int)
    col_month_id  : column index containing the month number 1-12 (int)
    col_date      : column index containing the date string (DD.MM.YYYY or YYYY-MM-DD)
    col_serial    : column index containing the meter/device identifier (grouping key)
    cols_sum      : column indices whose values are summed across the group
    cols_from_last: column indices whose values are taken from the last row of the group;
                    all other columns come from the first row;
                    col_date is always replaced with the first-of-month ISO string
    sum_precision : {col_idx: decimal_places}; columns not listed default to 2 decimal places
    """
    col_year: int
    col_month_id: int
    col_date: int
    col_serial: int
    cols_sum: list
    cols_from_last: list
    sum_precision: dict = field(default_factory=dict)


# Default schema for the resource sheet (matches _RESOURCE_SHEET_HEADER in api/resource.py)
#   0:Год 1:Месяц 2:Месяц_ИД 3:Дата 4:Номер счётчика 5:Название
#   6:Дельта 7:Нач.показания 8:Время нач. 9:Кон.показания 10:Время кон.
#   11:Ктр 12:Тариф 13:Группа_ИД 14:Группа 15:Потребление*Ктр 16:Потребление*Ктр*Тариф
RESOURCE_SCHEMA = SheetSchema(
    col_year=0,
    col_month_id=2,
    col_date=3,
    col_serial=4,
    cols_sum=[6, 15, 16],
    cols_from_last=[9, 10, 11, 12, 13, 14],
    sum_precision={6: 1, 15: 1, 16: 2},
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _num(v):
    """Parse a number from a Sheets cell (handles Russian locale formatting)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lstrip("'").strip()
    if not s or s == "-":
        return None
    s = s.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _int_safe(v):
    """Parse an integer; tolerates '8', '8.0', '8,0'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", ".")
    try:
        return int(float(s))
    except ValueError:
        return None


def _cell(row, idx, default="-"):
    return row[idx] if len(row) > idx else default


_DATE_FMTS = ("%d.%m.%Y", "%Y-%m-%d")


def _parse_date(s):
    """Parse DD.MM.YYYY or YYYY-MM-DD; return None on failure."""
    raw = str(s).lstrip("'").strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _month_first_iso(year, month_id):
    return f"{year}-{month_id:02d}-01"


def _is_empty_row(row):
    return not any(str(c).strip() for c in row)


def _consolidate_group(rows, schema):
    """Build a single monthly row from daily rows according to the schema.

    Returns None only if year/month_id are unparseable.
    """
    rows = sorted(
        rows,
        key=lambda r: _parse_date(_cell(r, schema.col_date, "")) or date(1, 1, 1),
    )
    first, last = rows[0], rows[-1]

    year = _int_safe(_cell(first, schema.col_year))
    month_id = _int_safe(_cell(first, schema.col_month_id))
    if year is None or month_id is None:
        return None

    cols_sum_set = set(schema.cols_sum)
    cols_last_set = set(schema.cols_from_last)

    sums = {c: 0.0 for c in cols_sum_set}
    has = {c: False for c in cols_sum_set}
    for r in rows:
        for c in cols_sum_set:
            v = _num(_cell(r, c))
            if v is not None:
                sums[c] += v
                has[c] = True

    n_cols = max(len(r) for r in rows)
    result = []
    for idx in range(n_cols):
        if idx == schema.col_date:
            result.append(_month_first_iso(year, month_id))
        elif idx in cols_sum_set:
            p = schema.sum_precision.get(idx, 2)
            result.append(round(sums[idx], p) if has[idx] else "-")
        elif idx in cols_last_set:
            result.append(_cell(last, idx))
        else:
            result.append(_cell(first, idx))
    return result


def _group_data(data, schema, remove_empty=False):
    """Split data rows into keyed groups and ungrouped leftovers."""
    min_cols = max(schema.col_year, schema.col_month_id, schema.col_serial) + 1
    groups = {}
    ungrouped = []
    for row in data:
        if _is_empty_row(row):
            if not remove_empty:
                ungrouped.append(row)
            continue
        if len(row) < min_cols:
            ungrouped.append(row)
            continue
        year = _int_safe(row[schema.col_year])
        month_id = _int_safe(row[schema.col_month_id])
        if year is None or month_id is None:
            ungrouped.append(row)
            continue
        serial = str(row[schema.col_serial]).strip()
        groups.setdefault((year, month_id, serial), []).append(row)
    return groups, ungrouped


def _is_month_first(row, year, month_id, col_date):
    d = _parse_date(_cell(row, col_date, ""))
    return d == date(year, month_id, 1)


def _sort_key(row, schema):
    return (
        _parse_date(_cell(row, schema.col_date, "")) or date(1, 1, 1),
        str(_cell(row, schema.col_serial, "")),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_resource(
    sheets_service,
    spreadsheet_id,
    sheet_name,
    cutoff,
    schema,
    remove_empty_rows=False,
):
    """Return a human-readable dry-run summary without modifying the sheet."""

    all_rows = sheets_service._read_all(spreadsheet_id, sheet_name)
    if not all_rows:
        return ["Лист пуст."]

    n_empty = sum(1 for r in all_rows[1:] if _is_empty_row(r))
    groups, _ = _group_data(all_rows[1:], schema, remove_empty=remove_empty_rows)
    cutoff_ym = (cutoff.year, cutoff.month)
    lines = []
    n_cons = n_del = n_skip = 0

    for (year, month_id, serial), group in sorted(groups.items()):
        if (year, month_id) >= cutoff_ym:
            continue
        if len(group) == 1 and _is_month_first(group[0], year, month_id, schema.col_date):
            n_skip += 1
            continue
        lines.append(f"  {year}-{month_id:02d} | {serial} | {len(group)} строк → 1 помесячная")
        n_cons += 1
        n_del += len(group)

    empty_note = f", удалено пустых строк: {n_empty}" if (remove_empty_rows and n_empty) else ""
    header = (
        f"Будет консолидировано групп: {n_cons}, "
        f"удалено суточных строк: {n_del}, "
        f"пропущено (уже помесячных): {n_skip}"
        f"{empty_note}."
    )
    return [header] + lines


def consolidate_resource(
    sheets_service,
    spreadsheet_id,
    sheet_name,
    cutoff,
    schema,
    remove_empty_rows=False,
):
    """Consolidate daily rows into monthly rows for all months strictly before cutoff.

    Parameters
    ----------
    schema : SheetSchema
        Column layout description for this sheet.
    remove_empty_rows : bool
        Drop rows where every cell is blank.

    Returns
    -------
    dict: consolidated, deleted, skipped, empty_removed
    """

    all_rows = sheets_service._read_all(spreadsheet_id, sheet_name)
    if not all_rows:
        return {"consolidated": 0, "deleted": 0, "skipped": 0, "empty_removed": 0}

    header = all_rows[0]
    n_empty = sum(1 for r in all_rows[1:] if _is_empty_row(r))
    groups, ungrouped = _group_data(all_rows[1:], schema, remove_empty=remove_empty_rows)

    cutoff_ym = (cutoff.year, cutoff.month)
    keep_rows = list(ungrouped)
    consolidated_rows = []
    n_cons = n_del = n_skip = 0

    for (year, month_id, serial), group in groups.items():
        if (year, month_id) >= cutoff_ym:
            keep_rows.extend(group)
            continue
        if len(group) == 1 and _is_month_first(group[0], year, month_id, schema.col_date):
            keep_rows.extend(group)
            n_skip += 1
            continue
        row = _consolidate_group(group, schema)
        if row is None:
            keep_rows.extend(group)
            continue
        consolidated_rows.append(row)
        n_cons += 1
        n_del += len(group)

    all_data = keep_rows + consolidated_rows
    all_data.sort(key=lambda r: _sort_key(r, schema))

    sheets_service.clear_and_write(spreadsheet_id, sheet_name, [header] + all_data)

    empty_removed = n_empty if remove_empty_rows else 0
    logger.info(
        "consolidation spreadsheet=%s sheet=%s consolidated=%d deleted=%d skipped=%d empty_removed=%d",
        spreadsheet_id, sheet_name, n_cons, n_del, n_skip, empty_removed,
    )
    return {"consolidated": n_cons, "deleted": n_del, "skipped": n_skip, "empty_removed": empty_removed}
