"""Blueprint /api/engineering — посуточные показания в таблицу «Инженерия» (Google Sheets).

Каждый лист соответствует одному календарному месяцу, формат имени: «{Месяц} {Год}»
(напр. «Июнь 2026»). Если нужный лист отсутствует, он создаётся дублированием
последнего существующего листа:
  - ячейки показателей (столбцы K-Q, V-AB, AG-AM, AR-AX, BC-BI, строки 6+) очищаются;
  - D1 обновляется именем месяца;
  - строка 2 заполняется датами по пяти ISO-неделям.

Источник данных: таблица consumption БД Ресурс (MariaDB).
Пишется SUM(ConsumptionDelta) × КТР (b_count) за сутки. Дни позже вчера — пустые.
Числовой формат ячеек нового листа: до 2 знаков после запятой; целые — без дробной части.
"""

import logging
import re
from datetime import date, timedelta

from flask import Blueprint, current_app, jsonify, request

from services.db_mariadb import get_db

bp = Blueprint("engineering", __name__, url_prefix="/api/v1/engineering")
logger = logging.getLogger(__name__)

_RU_MONTHS = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

_RU_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]  # 0=Monday

# 0-based начальные индексы столбцов для каждой из 5 недель:
# K=10, V=21, AG=32, AR=43, BC=54
_WEEK_START_COLS = [10, 21, 32, 43, 54]

# Regex для поиска серийного номера: первая последовательность ≥6 цифр в ячейке B
_SERIAL_RE = re.compile(r"\d{6,}")

# Строка, с которой начинаются данные (1-indexed)
_DATA_ROW_START = 6
# Нижняя граница строк данных (достаточно для всех показателей)
_DATA_ROW_END = 80


# ---------------------------------------------------------------------------
# Helpers: даты и столбцы
# ---------------------------------------------------------------------------

def _col_letter(idx: int) -> str:
    """0-based индекс столбца → буква(ы) A1-нотации."""
    result = ""
    n = idx
    while True:
        result = chr(65 + (n % 26)) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


def _month_week_start(year: int, month: int) -> date:
    """Дата понедельника ISO-недели, содержащей 1-е число месяца."""
    first = date(year, month, 1)
    return first - timedelta(days=first.weekday())


def _date_col_map(year: int, month: int) -> dict[date, int]:
    """Маппинг дата → 0-based индекс столбца для всех 35 дней пяти недель."""
    start = _month_week_start(year, month)
    mapping: dict[date, int] = {}
    for week_num, week_col in enumerate(_WEEK_START_COLS):
        for day_offset in range(7):
            d = start + timedelta(weeks=week_num, days=day_offset)
            mapping[d] = week_col + day_offset
    return mapping


def _date_cell_text(d: date) -> str:
    """Формат ячейки даты строки 2: 'пн|30 .03'."""
    wd = _RU_WEEKDAYS[d.weekday()]
    return f"{wd}|{d.day} .{d.month:02d}"


def _parse_serial(cell: str) -> str | None:
    """Первый 6+-значный номер в тексте ячейки — серийный номер; иначе None."""
    m = _SERIAL_RE.search(str(cell))
    return m.group() if m else None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _find_counter(serial: str) -> int | None:
    """counter_id по серийному номеру или None."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT Obj_Id_Counter FROM counter"
            " WHERE CAST(SerialNumber AS UNSIGNED) = CAST(%s AS UNSIGNED)"
            " LIMIT 1",
            (serial,),
        )
        row = cur.fetchone()
        return row["Obj_Id_Counter"] if row else None


def _fetch_ktr(serial: str) -> int:
    """КТР из b_count по серийному номеру. Если счётчик не в b_count — возвращает 1."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT Ktr FROM b_count"
            " WHERE SerialNumber = CAST(%s AS UNSIGNED)"
            " LIMIT 1",
            (serial,),
        )
        row = cur.fetchone()
        return int(row["Ktr"]) if row else 1


def _cell_val(raw: float, ktr: int) -> float | int:
    """Расход × КТР, округлённый до 2 знаков. Целые числа — без дробной части."""
    v = round(raw * ktr, 2)
    return int(v) if v == int(v) else v


def _fetch_readings_by_day(
    counter_id: int, date_from: date, date_to: date
) -> dict[date, float]:
    """Сумма ConsumptionDelta за каждый день периода [date_from, date_to].

    Возвращает dict: date → суммарный расход за сутки (только для дней с данными).
    """
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT SUM(ConsumptionDelta) AS value,
                   DATE(UpdateTime)      AS day
            FROM consumption
            WHERE Obj_Id_Counter = %s
              AND DATE(UpdateTime) BETWEEN %s AND %s
            GROUP BY DATE(UpdateTime)
            ORDER BY day
            """,
            (counter_id, date_from, date_to),
        )
        rows = cur.fetchall()
    return {r["day"]: float(r["value"]) for r in rows}


# ---------------------------------------------------------------------------
# Sheets helpers
# ---------------------------------------------------------------------------

def _get_spreadsheet_id() -> str:
    sid = current_app.config.get("GOOGLE_SHEETS_ENGINEERING_ID", "")
    if not sid:
        raise RuntimeError("GOOGLE_SHEETS_ENGINEERING_ID не настроен")
    return sid


def _spreadsheet_sheets(svc, spreadsheet_id: str) -> list[dict]:
    """Возвращает список {'title': ..., 'sheetId': ...} в порядке вкладок."""
    meta = svc._svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return [
        {"title": s["properties"]["title"], "sheetId": s["properties"]["sheetId"]}
        for s in meta.get("sheets", [])
    ]


def _find_sheet_name(svc, spreadsheet_id: str, year: int, month: int) -> str | None:
    """Найти имя существующего листа для периода."""
    canonical = f"{_RU_MONTHS[month]} {year}"
    titles = {s["title"] for s in _spreadsheet_sheets(svc, spreadsheet_id)}
    return canonical if canonical in titles else None


def _prev_sheet_title(svc, spreadsheet_id: str, year: int, month: int) -> str | None:
    """Найти последний лист в таблице, кроме листа нужного периода."""
    canonical = f"{_RU_MONTHS[month]} {year}"
    sheets = _spreadsheet_sheets(svc, spreadsheet_id)
    candidates = [s["title"] for s in sheets if s["title"] != canonical]
    return candidates[-1] if candidates else None


def _duplicate_and_prepare(
    svc, spreadsheet_id: str, source_title: str, new_title: str, year: int, month: int
) -> None:
    """Дублировать лист source_title → new_title и подготовить шапку + пустые данные."""
    sheets = _spreadsheet_sheets(svc, spreadsheet_id)
    source_id = next(s["sheetId"] for s in sheets if s["title"] == source_title)

    dup_resp = svc._svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"duplicateSheet": {
            "sourceSheetId": source_id,
            "insertSheetIndex": len(sheets),
            "newSheetName": new_title,
        }}]},
    ).execute()
    new_sheet_id = dup_resp["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
    logger.info("engineering: дублирован лист %r → %r (sheetId=%d)", source_title, new_title, new_sheet_id)

    # --- Собираем batch-обновление ---
    data_entries: list[dict] = []

    # D1 → имя месяца
    data_entries.append({
        "range": f"'{new_title}'!D1",
        "values": [[_RU_MONTHS[month]]],
    })

    # Строка 2: даты по 5 неделям (пн|27 .04 и т.п.)
    week_start = _month_week_start(year, month)
    for week_num, week_col in enumerate(_WEEK_START_COLS):
        week_dates = []
        for day_offset in range(7):
            d = week_start + timedelta(weeks=week_num, days=day_offset)
            week_dates.append(_date_cell_text(d))
        col_a = _col_letter(week_col)
        col_b = _col_letter(week_col + 6)
        data_entries.append({
            "range": f"'{new_title}'!{col_a}2:{col_b}2",
            "values": [week_dates],
        })

    # Очистка ячеек данных (строки _DATA_ROW_START .. _DATA_ROW_END)
    n_rows = _DATA_ROW_END - _DATA_ROW_START + 1
    empty_block = [[""] * 7 for _ in range(n_rows)]
    for week_col in _WEEK_START_COLS:
        col_a = _col_letter(week_col)
        col_b = _col_letter(week_col + 6)
        data_entries.append({
            "range": f"'{new_title}'!{col_a}{_DATA_ROW_START}:{col_b}{_DATA_ROW_END}",
            "values": empty_block,
        })

    svc._svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data_entries},
    ).execute()

    # Формат 0.## — до 2 знаков после запятой, незначащие нули не показываются
    format_requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": new_sheet_id,
                    "startRowIndex": _DATA_ROW_START - 1,  # 0-based
                    "endRowIndex": _DATA_ROW_END,
                    "startColumnIndex": week_col,
                    "endColumnIndex": week_col + 7,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "0.##"},
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        }
        for week_col in _WEEK_START_COLS
    ]
    svc._svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": format_requests},
    ).execute()
    logger.info("engineering: лист %r подготовлен (D1 + строка 2 + очистка + формат)", new_title)


def _read_col_b(svc, spreadsheet_id: str, sheet_name: str) -> list[tuple[int, str]]:
    """Вернуть список (row_1indexed, text) для строк _DATA_ROW_START+ столбца B."""
    resp = svc._svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!B{_DATA_ROW_START}:B{_DATA_ROW_END}",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = resp.get("values", [])
    result = []
    for i, row in enumerate(rows, _DATA_ROW_START):
        text = row[0].strip() if row else ""
        if text:
            result.append((i, text))
    return result


# ---------------------------------------------------------------------------
# Основная функция синхронизации
# ---------------------------------------------------------------------------

def _sync_to_sheet(year: int, month: int) -> dict:
    from services.sheets import get_sheets_service
    svc = get_sheets_service()
    spreadsheet_id = _get_spreadsheet_id()

    # Найти или создать лист
    sheet_name = _find_sheet_name(svc, spreadsheet_id, year, month)
    sheet_created = False
    if sheet_name is None:
        new_title = f"{_RU_MONTHS[month]} {year}"
        prev = _prev_sheet_title(svc, spreadsheet_id, year, month)
        if prev is None:
            return {"error": "Нет листа-шаблона для создания нового"}
        _duplicate_and_prepare(svc, spreadsheet_id, prev, new_title, year, month)
        sheet_name = new_title
        sheet_created = True

    # Маппинг: дата → 0-based индекс столбца (35 дней пяти недель)
    col_map = _date_col_map(year, month)
    all_dates = sorted(col_map.keys())
    date_from, date_to = all_dates[0], all_dates[-1]

    # Данные записываются только до вчерашнего дня включительно
    yesterday = date.today() - timedelta(days=1)
    db_date_to = min(date_to, yesterday)

    # Читаем столбец B (строки 6+)
    b_rows = _read_col_b(svc, spreadsheet_id, sheet_name)

    # Кэши: serial → counter_id / ktr
    serial_cache: dict[str, int | None] = {}
    ktr_cache: dict[str, int] = {}

    updates: list[dict] = []
    stats = {
        "rows_processed": 0,
        "rows_no_counter": 0,
        "cells_written": 0,
    }

    for row_idx, cell_text in b_rows:
        serial = _parse_serial(cell_text)
        if serial is None:
            continue

        # counter_id и КТР из кэша или из БД
        if serial not in serial_cache:
            serial_cache[serial] = _find_counter(serial)
        counter_id = serial_cache[serial]

        if counter_id is None:
            stats["rows_no_counter"] += 1
            logger.warning(
                "engineering: счётчик не найден serial=%s row=%d cell=%r",
                serial, row_idx, cell_text,
            )
            continue

        if serial not in ktr_cache:
            ktr_cache[serial] = _fetch_ktr(serial)
        ktr = ktr_cache[serial]

        stats["rows_processed"] += 1

        # Показания за период одним запросом (только до вчера)
        by_day = _fetch_readings_by_day(counter_id, date_from, db_date_to)

        # Формируем строки по неделям
        for week_num, week_col in enumerate(_WEEK_START_COLS):
            week_vals = []
            for i in range(7):
                d = all_dates[week_num * 7 + i]
                if d > yesterday:
                    week_vals.append("")          # будущее — не заполнять
                else:
                    raw = by_day.get(d, 0.0)
                    week_vals.append(_cell_val(raw, ktr))
            col_a = _col_letter(week_col)
            col_b = _col_letter(week_col + 6)
            updates.append({
                "range": f"'{sheet_name}'!{col_a}{row_idx}:{col_b}{row_idx}",
                "values": [week_vals],
            })
            stats["cells_written"] += sum(1 for v in week_vals if v != "")

    if updates:
        # Записываем всё одним batch-запросом
        svc._svc.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    logger.info(
        "engineering: sheet=%r year=%d month=%d rows=%d cells=%d no_counter=%d",
        sheet_name, year, month,
        stats["rows_processed"], stats["cells_written"], stats["rows_no_counter"],
    )
    return {
        "sheet_name": sheet_name,
        "sheet_created": sheet_created,
        **stats,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@bp.route("/daily")
def daily():
    """
    GET /api/engineering/daily[?year=YYYY&month=M][&sync=y]

    Посуточные накопительные показания счётчиков (БД Ресурс) в таблицу «Инженерия».
    Лист выбирается по имени «{Месяц} {Год}» (или «D- {Месяц} {Год}» в dev-режиме).
    Если лист не существует — создаётся копированием последнего листа в таблице.

    sync=y — записать данные в Google Sheets.
    """
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except ValueError:
        return jsonify({"error": "year и month должны быть числами"}), 400

    if not (1 <= month <= 12):
        return jsonify({"error": "month должен быть от 1 до 12"}), 400

    sync = request.args.get("sync", "n").strip().lower() in ("y", "yes", "true", "1")

    result: dict = {
        "year": year,
        "month": month,
        "month_name": _RU_MONTHS[month],
        "target_sheet": f"{_RU_MONTHS[month]} {year}",
    }

    if sync:
        try:
            result["sheets_sync"] = _sync_to_sheet(year, month)
        except Exception as exc:
            logger.error("engineering daily sheets sync: %s", exc, exc_info=True)
            result["sheets_sync"] = {"error": str(exc)}

    return jsonify(result)
