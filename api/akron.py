"""Blueprint /api/akron — посуточный отчёт АКРОН + ЭХО-Р-02.

Два счётчика БД Ресурс (MariaDB) выводятся в один лист Google Sheets «бок о бок»:
левая половина — АКРОН, правая — ЭХО-Р-02. Данные те же, что и в /api/resource,
но:
  - серийники без ведущих нулей (в поиске и в выводе);
  - отдельный лист (GOOGLE_SHEETS_AKRON_*);
  - выгрузка вставкой сверху (новые сутки — со 2-й строки, сдвигая вниз),
    дедуп по дате (повторный запуск идемпотентен);
  - сортировка обратная (новые сутки сверху).
"""

import logging
from datetime import date, datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from services.db_mariadb import get_db

bp = Blueprint("akron", __name__, url_prefix="/api/akron")
logger = logging.getLogger(__name__)

# Серийники по умолчанию (тестовые/боевые) — переопределяются query-параметрами.
_DEFAULT_AKRON = "14331"
_DEFAULT_EHOR = "9899"

_RU_MONTHS = [
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]

# Заголовок листа (16 столбцов: 8 АКРОН + 8 ЭХО-Р-02).
_AKRON_SHEET_HEADER = [
    "Год", "Месяц", "Месяц_ИД", "Дата показаний",
    "Названия счетчика", "Номер счетчика", "Показания", "За сутки",
    "Год(ЭХО-Р-02)", "Месяц(ЭХО-Р-02)", "Месяц_ИД(ЭХО-Р-02)", "Дата показаний(ЭХО-Р-02)",
    "Названия счетчика(ЭХО-Р-02)", "Номер счетчика(ЭХО-Р-02)", "Показания(ЭХО-Р-02)", "За сутки(ЭХО-Р-02)",
]
# Дедуп по дате — столбец D (индекс 3, «Дата показаний» АКРОН).
# Ключ — канонизированная дата (без времени), чтобы повторная синхронизация
# обновляла сутки, а не плодила дубли при изменившемся времени последнего показания.
_AKRON_KEY_COL = 3
# Столбцы «Дата показаний» (АКРОН — D, ЭХО-Р — L): формат дата-время.
_DATE_COLS = [3, 11]
_DATE_FORMAT = "dd.mm.yyyy hh:mm:ss"

# Защита истории: строки с датой ДО этого порога (≤ 31.10.2024, т.е. до
# ноября 2024) не трогаются при синхронизации/дедупе — их не обновляем и не
# вставляем. Заморожённая закрытая история. 01.11.2024 и новее — управляются.
_PROTECT_BEFORE = date(2024, 11, 1)

# Понятные имена для вывода (переопределяют counter.Name из БД).
# В БД: 14331 → "(СВ.сч.41.05) 00014331-21 [АКРОН, Очистные]",
#       9899  → "ЭХО-Р-02 9899 временно отключен".
# В отчёте выводим короткие "АКРОН-01" и "ЭХО-Р-02".
_AKRON_NAME = "АКРОН-01"
_EHOR_NAME = "ЭХО-Р-02"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _round(val) -> float | str:
    if val is None or val == "-":
        return "-"
    try:
        return round(float(val), 1)
    except (TypeError, ValueError):
        return "-"


def _is_num(v) -> bool:
    return isinstance(v, (int, float))


def _strip_serial(s) -> str:
    """Серийный номер без ведущих нулей (для поиска и вывода)."""
    s = str(s).strip()
    return s.lstrip("0") or "0"


def _cell(v):
    """Значение для ячейки листа: '-'/None → пустая строка."""
    return "" if v in (None, "-") else v


def _date_cell(ts, fallback: date) -> str:
    """Ячейка «Дата показаний» как ISO-строка (Google распознаёт её как дату-время).

    Отображение задаёт числовой формат столбца (dd.mm.yyyy hh:mm:ss).
    С показанием → время показания, без показания → дата дня (00:00:00).
    """
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(ts, date):
        return ts.strftime("%Y-%m-%d")
    return fallback.strftime("%Y-%m-%d")


def _date_key(cell) -> str:
    """Канонический ключ дедупа — дата YYYY-MM-DD (формато-независимо).

    Понимает и DD.MM.YYYY (как Google отдаёт отформатированную дату-время),
    и YYYY-MM-DD (как мы записываем / ISO). Так старые текстовые строки и
    новые дата-значения матчатся по одному ключу.
    """
    s = str(cell).strip().lstrip("'").strip()
    head = s[:10]
    if len(head) == 10 and head[2] == "." and head[5] == ".":
        d, m, y = head.split(".")
        return f"{y}-{m}-{d}"
    return head


def _cell_date(cell) -> date | None:
    """Дата строки (по столбцу «Дата показаний») или None, если не разобрать."""
    try:
        return date.fromisoformat(_date_key(cell))
    except ValueError:
        return None


def _is_protected(d: date | None) -> bool:
    """True для заморожённой истории (дата < _PROTECT_BEFORE)."""
    return d is not None and d < _PROTECT_BEFORE


def _parse_date(param: str | None, fallback: date) -> date:
    if not param:
        return fallback
    try:
        return date.fromisoformat(param)
    except ValueError:
        return fallback


def _parse_period(
    raw_from: str | None,
    raw_to: str | None,
    today: date,
    default_days: int = 50,
) -> tuple[date, date]:
    """Те же умолчания, что и в /api/resource."""
    has_from = bool(raw_from and raw_from.strip())
    has_to = bool(raw_to and raw_to.strip())

    if not has_from and not has_to:
        return date(today.year, today.month, 1), today
    if has_from and not has_to:
        d_from = _parse_date(raw_from, today)
        return d_from, d_from + timedelta(days=default_days)
    if has_to and not has_from:
        d_to = _parse_date(raw_to, today)
        return d_to - timedelta(days=default_days), d_to
    return _parse_date(raw_from, today), _parse_date(raw_to, today)


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def _fetch_counter(serial: str) -> dict | None:
    """Счётчик по серийнику (ведущие нули игнорируются через CAST AS UNSIGNED)."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT Obj_Id_Counter AS counter_id,"
            "       SerialNumber   AS serial_number,"
            "       Name           AS name"
            " FROM counter"
            " WHERE CAST(SerialNumber AS UNSIGNED) = CAST(%s AS UNSIGNED)"
            " LIMIT 1",
            (serial,),
        )
        return cur.fetchone()


def _last_before(counter_id: int, target_date: date) -> dict | None:
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT Consumption AS value, UpdateTime AS timestamp"
            " FROM consumption"
            " WHERE Obj_Id_Counter = %s AND DATE(UpdateTime) < %s"
            " ORDER BY UpdateTime DESC LIMIT 1",
            (counter_id, target_date),
        )
        return cur.fetchone()


def _day_readings(counter_id: int, date_from: date, date_to: date) -> list[dict]:
    """Последнее показание на каждый день периода."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT cn.Consumption      AS value,
                   DATE(cn.UpdateTime) AS day,
                   cn.UpdateTime       AS timestamp
            FROM consumption cn
            JOIN (
                SELECT Obj_Id_Counter,
                       DATE(UpdateTime) AS d,
                       MAX(UpdateTime)  AS max_ts
                FROM consumption
                WHERE Obj_Id_Counter = %s
                  AND DATE(UpdateTime) BETWEEN %s AND %s
                GROUP BY Obj_Id_Counter, DATE(UpdateTime)
            ) t ON cn.Obj_Id_Counter = t.Obj_Id_Counter
                AND cn.UpdateTime = t.max_ts
            ORDER BY day
            """,
            (counter_id, date_from, date_to),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# per-counter daily series
# ---------------------------------------------------------------------------

def _counter_daily(
    serial: str, date_from: date, date_to: date,
    name_override: str | None = None,
) -> tuple[dict, dict]:
    """Вернуть (meta, day_map).

    meta    = {"serial", "name", "found"}
    day_map = {date: {"value", "delta", "ts"}} для каждого дня периода (даже без данных).
    name_override — если задано, имя в отчёте берётся из него, иначе из БД (counter.Name).
    """
    requested = _strip_serial(serial)
    counter = _fetch_counter(serial)
    if counter is None:
        meta = {"serial": requested, "name": name_override or "", "found": False}
        day_map = {}
        cur_d = date_from
        while cur_d <= date_to:
            day_map[cur_d] = {"value": "-", "delta": "-", "ts": None}
            cur_d += timedelta(days=1)
        return meta, day_map

    cid = counter["counter_id"]
    meta = {
        "serial": _strip_serial(counter["serial_number"]),
        "name": name_override or counter["name"],
        "found": True,
    }

    prev_row = _last_before(cid, date_from)
    prev_value: float | None = None
    if prev_row and _is_num(prev_row["value"]):
        prev_value = _round(prev_row["value"])

    rows = _day_readings(cid, date_from, date_to)
    by_day = {r["day"]: r for r in rows}

    day_map: dict[date, dict] = {}
    cur_d = date_from
    while cur_d <= date_to:
        row = by_day.get(cur_d)
        if row:
            val = _round(row["value"])
            ts = row["timestamp"]
            delta = (
                _round(val - prev_value)
                if (_is_num(val) and prev_value is not None) else "-"
            )
            if _is_num(val):
                prev_value = val
        else:
            val, delta, ts = "-", "-", None
        day_map[cur_d] = {"value": val, "delta": delta, "ts": ts}
        cur_d += timedelta(days=1)

    return meta, day_map


def _half(meta: dict, day: dict, d: date) -> list:
    """8 столбцов одного счётчика для строки листа."""
    return [
        d.year, _RU_MONTHS[d.month], d.month, _date_cell(day["ts"], d),
        meta["name"], meta["serial"],
        _cell(day["value"]), _cell(day["delta"]),
    ]


# ---------------------------------------------------------------------------
# Google Sheets push
# ---------------------------------------------------------------------------

def _push_sheet(rows_desc: list[list]) -> dict:
    """Выгрузить строки (отсортированы по дате убыв.) с дедупом по дате.

    Существующие сутки обновляются на месте, новые — вставляются сверху
    (со 2-й строки, сдвигая имеющиеся данные вниз).
    """
    spreadsheet_id = current_app.config.get("GOOGLE_SHEETS_AKRON_ID", "")
    if not spreadsheet_id:
        return {"error": "GOOGLE_SHEETS_AKRON_ID не настроен"}

    # Защита истории: не трогаем строки с датой < _PROTECT_BEFORE — их не
    # обновляем и не вставляем (закрытая история заморожена).
    protected = sum(1 for r in rows_desc if _is_protected(_cell_date(r[_AKRON_KEY_COL])))
    rows_desc = [r for r in rows_desc if not _is_protected(_cell_date(r[_AKRON_KEY_COL]))]

    if not rows_desc:
        return {"appended": 0, "updated": 0, "protected": protected}

    sheet_name = current_app.config.get("GOOGLE_SHEETS_AKRON_SHEET", "akron")

    from services.sheets import get_sheets_service
    svc = get_sheets_service()
    svc.ensure_header(spreadsheet_id, sheet_name, _AKRON_SHEET_HEADER)
    # Столбцы дат — настоящий формат дата-время (идемпотентно).
    svc.set_columns_datetime_format(spreadsheet_id, sheet_name, _DATE_COLS, _DATE_FORMAT)

    existing = svc.read_sheet(spreadsheet_id, sheet_name)
    header_rows = 1 if existing else 0
    data_rows = existing[header_rows:]
    key_to_idx = {
        _date_key(r[_AKRON_KEY_COL]): i
        for i, r in enumerate(data_rows)
        if len(r) > _AKRON_KEY_COL
    }

    to_update: list[tuple[int, list]] = []
    to_insert: list[list] = []
    for row in rows_desc:
        key = _date_key(row[_AKRON_KEY_COL])
        if key in key_to_idx:
            sheet_row = key_to_idx[key] + header_rows + 1
            to_update.append((sheet_row, row))
        else:
            to_insert.append(row)

    # Обновления — по текущим позициям (до вставки, чтобы индексы не «поехали»).
    for sheet_row, row in to_update:
        svc._update_row(spreadsheet_id, sheet_name, sheet_row, row)

    # Новые сутки — блоком сверху (rows_desc уже отсортированы новые→старые).
    if to_insert:
        svc.insert_rows_top(spreadsheet_id, sheet_name, to_insert)

    logger.info(
        "akron sheets spreadsheet=%s sheet=%s inserted=%d updated=%d protected=%d",
        spreadsheet_id, sheet_name, len(to_insert), len(to_update), protected,
    )
    result = {"appended": len(to_insert), "updated": len(to_update)}
    if protected:
        result["protected"] = protected
    return result


# ---------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------

@bp.route("/daily")
def daily():
    """
    GET /api/akron/daily[?akron=<SN>&ehor=<SN>][&date_from=&date_to=][&sync=y]

    Посуточный отчёт по двум счётчикам (АКРОН + ЭХО-Р-02), бок о бок.
    Все дни периода присутствуют (без данных → «-»). Период ограничен
    DAILY_REPORT_LIMIT днями. sync=y — выгрузка в Google Sheets.
    """
    akron_serial = (request.args.get("akron") or _DEFAULT_AKRON).strip()
    ehor_serial = (request.args.get("ehor") or _DEFAULT_EHOR).strip()
    sync = request.args.get("sync", "n").strip().lower() in ("y", "yes", "true", "1")

    today = date.today()
    date_from, date_to = _parse_period(
        request.args.get("date_from"), request.args.get("date_to"), today
    )
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    limit: int = current_app.config.get("DAILY_REPORT_LIMIT", 50)
    capped = False
    min_from = date_to - timedelta(days=limit - 1)
    if date_from < min_from:
        date_from = min_from
        capped = True

    ak_meta, ak_days = _counter_daily(
        akron_serial, date_from, date_to, name_override=_AKRON_NAME
    )
    eh_meta, eh_days = _counter_daily(
        ehor_serial, date_from, date_to, name_override=_EHOR_NAME
    )

    # Дни периода по убыванию даты (новые сверху).
    all_dates = sorted(ak_days, reverse=True)

    days = []
    rows_desc = []
    for d in all_dates:
        ak = ak_days[d]
        eh = eh_days[d]
        days.append({
            "req_date": d.isoformat(),
            "akron_value": ak["value"],
            "akron_delta": ak["delta"],
            "ehor_value": eh["value"],
            "ehor_delta": eh["delta"],
        })
        rows_desc.append(_half(ak_meta, ak, d) + _half(eh_meta, eh, d))

    result: dict = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "akron": ak_meta,
        "ehor": eh_meta,
        "days": days,
    }
    if capped:
        result["note"] = f"Период ограничен до {limit} суток"
    if not ak_meta["found"]:
        result["error"] = f"АКРОН '{akron_serial}' не найден"
    if not eh_meta["found"]:
        result["error"] = f"ЭХО-Р '{ehor_serial}' не найден"

    if sync:
        try:
            result["sheets_sync"] = _push_sheet(rows_desc)
        except Exception as exc:
            logger.error("akron daily sheets sync: %s", exc)
            result["sheets_sync"] = {"error": str(exc)}

    return jsonify(result)
