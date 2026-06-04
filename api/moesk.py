import calendar
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from services.db_mssql import get_db
from services.sheets import get_sheets_service

bp = Blueprint("moesk", __name__, url_prefix="/api/moesk")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google Sheets — MOESK monthly tab
# ---------------------------------------------------------------------------

_MOESK_HEADER = [
    "Год", "Месяц", "Месяц_ИД", "№",
    "Название", "Адрес", "Объект", "Корпус",
    "Дата", "Прибор учета", "Наименование точки учета", "Номер счетчика",
    "Номер договора", "Вид энергии", "КТТ",
    "Показания на начало периода", "Показания на конец периода",
    "Расход, кВт*ч", "Расход с учетом КТТ и КТН, кВт*ч",
    "Тариф", "Начисления с НДС, руб.",
]
# Dedup ключ: Дата(8) + Номер счетчика(11) + Наименование точки учета(10)
_MOESK_KEY_COLS = [8, 11, 10]
# Индекс столбца U (0-based) — формула начислений
_MOESK_FORMULA_COL = 20

_MONTH_ABBR = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}

# A+  — текущие/суточные показания (для /reading, /period, /daily)
# MA+ — архивные помесячные снимки  (для /monthly)
_CODES_A  = ("A+0",  "A+1",  "A+2",  "A+3")
_CODES_MA = ("MA+0", "MA+1", "MA+2", "MA+3")

# Единый маппинг: оба семейства кодов → имена полей
_CODE_KEYS: dict[str, str] = {
    "A+0": "total",  "A+1": "tariff_1",  "A+2": "tariff_2",  "A+3": "tariff_3",
    "MA+0": "total", "MA+1": "tariff_1", "MA+2": "tariff_2", "MA+3": "tariff_3",
}

# --- helpers ----------------------------------------------------------------

def _round(val) -> float | str:
    if val is None:
        return "-"
    try:
        return round(float(val), 1)
    except (TypeError, ValueError):
        return "-"


def _is_num(v) -> bool:
    return isinstance(v, (int, float))


def _fmt_ts(ts) -> str:
    if ts is None:
        return "-"
    return ts.isoformat() if isinstance(ts, datetime) else str(ts)


def _delta(sv, ev) -> float | str:
    if _is_num(sv) and _is_num(ev):
        return _round(ev - sv)
    return "-"


def _parse_date(param: str | None, fallback: date) -> date:
    if not param:
        return fallback
    try:
        return date.fromisoformat(param)
    except ValueError:
        return fallback


def _add_months(y: int, m: int, n: int) -> tuple[int, int]:
    total = y * 12 + m - 1 + n
    return total // 12, total % 12 + 1


def _month_count(ym_from: tuple[int, int], ym_to: tuple[int, int]) -> int:
    return ym_to[0] * 12 + ym_to[1] - ym_from[0] * 12 - ym_from[1] + 1


def _normalize_serial(raw: str) -> str:
    """Return serial zfilled to 8, preserving non-numeric serials as-is."""
    s = str(raw).strip()
    return s.zfill(8) if s.isdigit() else s


# --- DB queries: device lookup ----------------------------------------------

_DEVICE_SELECT = """
    SELECT
        v.ValueData                AS serial_raw,
        t.IdLogicDevice,
        ld.Name                    AS ld_name,
        ld.IdLogicDeviceType,
        ld.OrderNum,
        s.Id                       AS station_id,
        s.Name                     AS station_name,
        s.Address                  AS station_address,
        s.IdEso,
        e.Name                     AS eso_name,
        f.Name                     AS filial_name,
        r.Name                     AS region_name
    FROM dbo.Value v
    JOIN dbo.Tag t          ON v.IdDeviceTag   = t.Id
    JOIN dbo.DeviceTag dt   ON t.IdDeviceTag   = dt.Id
    JOIN dbo.LogicDevice ld ON t.IdLogicDevice = ld.Id
    JOIN dbo.Station s      ON ld.IdStation    = s.Id
    LEFT JOIN dbo.Eso e     ON s.IdEso         = e.Id
    LEFT JOIN dbo.Filial f  ON e.IdFilial      = f.Id
    LEFT JOIN dbo.Region r  ON f.IdRegion      = r.Id
    WHERE dt.Code = 'serial'
"""

_PROPS_SQL = """
    SELECT lpt.Code, ldp.Value
    FROM dbo.LogicDeviceProperty ldp
    JOIN dbo.LogicDevicePropertyType lpt ON ldp.IdLogDevicePropType = lpt.Id
    WHERE ldp.IdLogicDevice = ?
      AND lpt.Code IN ('kC', 'kV', 'AgrNo', 'MeasuringpointName', 'MeasuringpointCode', 'PriborType')
"""


def _row_to_device(row, description: list) -> dict:
    cols = [d[0] for d in description]
    device = dict(zip(cols, row))
    raw = device.get("serial_raw") or ""
    device["serial_number"] = _normalize_serial(raw)
    return device


_ESO_ROW_SQL = """
WITH serial_devs AS (
    SELECT DISTINCT t.IdLogicDevice
    FROM dbo.Value v
    JOIN dbo.Tag t        ON v.IdDeviceTag = t.Id
    JOIN dbo.DeviceTag dt ON t.IdDeviceTag = dt.Id
    WHERE dt.Code = 'serial'
),
all_devs AS (
    SELECT ld.Id AS ld_id,
           s.IdEso, ld.IdLogicDeviceType,
           s.Id  AS st_id,
           ld.OrderNum
    FROM serial_devs sd
    JOIN dbo.LogicDevice ld ON ld.Id = sd.IdLogicDevice
    JOIN dbo.Station s      ON s.Id  = ld.IdStation
),
numbered AS (
    SELECT ld_id,
           (ROW_NUMBER() OVER (
               PARTITION BY IdEso, IdLogicDeviceType
               ORDER BY st_id, OrderNum
           ) - 1) * 4 + 1 AS eso_row
    FROM all_devs
)
SELECT eso_row FROM numbered WHERE ld_id = ?
"""


def _attach_props(device: dict, cur) -> None:
    cur.execute(_PROPS_SQL, (device["IdLogicDevice"],))
    device["props"] = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute(
        "SELECT TOP 1 Name FROM dbo.LogDevProp WHERE IdLogicDevice = ?",
        (device["IdLogicDevice"],),
    )
    rows = cur.fetchall()  # fetchall дренирует курсор, освобождая соединение
    device["meter_name"] = rows[0][0] if rows else None

    try:
        cur2 = get_db().cursor()
        cur2.execute(_ESO_ROW_SQL, (device["IdLogicDevice"],))
        rows2 = cur2.fetchall()
        device["eso_row"] = rows2[0][0] if rows2 else None
    except Exception as _e:
        log.error("eso_row query failed ld=%s: %s", device["IdLogicDevice"], _e)
        device["eso_row"] = None


def _serial_variants(serial: str) -> tuple[str, str]:
    """Return (original-stripped, leading-zeros-stripped) for IN-list matching."""
    s = serial.strip()
    stripped = s.lstrip("0") or "0"
    return s, stripped


def _fetch_device(serial: str) -> dict | None:
    db = get_db()
    cur = db.cursor()
    s, stripped = _serial_variants(serial)
    cur.execute(
        _DEVICE_SELECT + "  AND LTRIM(RTRIM(v.ValueData)) IN (?, ?)",
        (s, stripped),
    )
    row = cur.fetchone()
    if row is None:
        return None
    device = _row_to_device(row, cur.description)
    _attach_props(device, cur)
    return device


def _fetch_all_devices() -> list[dict]:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        _DEVICE_SELECT + """
        GROUP BY
            v.ValueData, t.IdLogicDevice, ld.Name, ld.IdLogicDeviceType, ld.OrderNum,
            s.Id, s.Name, s.Address, s.IdEso,
            e.Name, f.Name, r.Name
        ORDER BY r.Name, f.Name, s.Name, ld.OrderNum
        """,
    )
    rows = cur.fetchall()
    descr = cur.description
    devices = []
    for row in rows:
        device = _row_to_device(row, descr)
        _attach_props(device, cur)
        devices.append(device)
    return devices


# --- response builders ------------------------------------------------------

def _props_block(device: dict) -> dict:
    """Shared properties sub-dict (kC, contract, point-of-account)."""
    props = device.get("props", {})
    try:
        kc = float(props["kC"]) if props.get("kC") is not None else None
    except (ValueError, TypeError):
        kc = None
    return {
        "ktt": _round(kc),
        "agr_no": props.get("AgrNo"),
        "point_name": props.get("MeasuringpointName"),
        "meter_name": device.get("meter_name"),
    }


def _device_info(device: dict) -> dict:
    """Fields for the /devices endpoint."""
    return {
        "serial_number": device["serial_number"],
        "order_num": device.get("eso_row"),
        "name": device.get("ld_name"),
        "station": device.get("station_name"),
        "station_address": device.get("station_address"),
        "eso": device.get("eso_name"),
        "filial": device.get("filial_name"),
        "region": device.get("region_name"),
        **_props_block(device),
    }


def _device_meta(device: dict) -> dict:
    """Full metadata block for report endpoints."""
    return {
        "serial_number": device["serial_number"],
        "order_num": device.get("eso_row"),
        "name": device.get("ld_name"),
        "station": device.get("station_name"),
        "station_address": device.get("station_address"),
        "region": device.get("region_name"),
        "filial": device.get("filial_name"),
        "eso": device.get("eso_name"),
        **_props_block(device),
    }


def _get_kc(device: dict) -> float | None:
    try:
        val = device.get("props", {}).get("kC")
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


# --- DB queries: readings ---------------------------------------------------

def _cte(codes: tuple[str, ...], order: str, cond: str) -> str:
    in_list = ", ".join(f"'{c}'" for c in codes)
    return f"""
WITH ranked AS (
    SELECT dt.Code, v.Datetime, v.ValueFloat,
           ROW_NUMBER() OVER (PARTITION BY dt.Code ORDER BY v.Datetime {order}) AS rn
    FROM dbo.Value v
    JOIN dbo.Tag t        ON v.IdDeviceTag = t.Id
    JOIN dbo.DeviceTag dt ON t.IdDeviceTag = dt.Id
    WHERE t.IdLogicDevice = ?
      AND dt.Code IN ({in_list})
      AND {cond}
)
SELECT Code, Datetime, ValueFloat FROM ranked WHERE rn = 1
"""


def _q(ld_id: int, order: str, cond: str, params: tuple,
       codes: tuple[str, ...] = _CODES_MA) -> dict[str, dict]:
    db = get_db()
    cur = db.cursor()
    cur.execute(_cte(codes, order, cond), (ld_id, *params))
    return {r[0]: {"ts": r[1], "val": r[2]} for r in cur.fetchall()}


def _latest(ld_id: int, codes: tuple[str, ...] = _CODES_A) -> dict[str, dict]:
    """Absolute latest reading for each code — no upper bound."""
    return _q(ld_id, "DESC", "1 = 1", (), codes)


def _last_on_or_before(ld_id: int, dt: datetime,
                       codes: tuple[str, ...] = _CODES_A) -> dict[str, dict]:
    return _q(ld_id, "DESC", "v.Datetime <= ?", (dt,), codes)


def _last_before(ld_id: int, dt: datetime,
                 codes: tuple[str, ...] = _CODES_A) -> dict[str, dict]:
    return _q(ld_id, "DESC", "v.Datetime < ?", (dt,), codes)


def _first_in(ld_id: int, dt_from: datetime, dt_to: datetime,
              codes: tuple[str, ...] = _CODES_A) -> dict[str, dict]:
    return _q(ld_id, "ASC", "v.Datetime >= ? AND v.Datetime < ?", (dt_from, dt_to), codes)


def _last_in(ld_id: int, dt_from: datetime, dt_to: datetime,
             codes: tuple[str, ...] = _CODES_A) -> dict[str, dict]:
    return _q(ld_id, "DESC", "v.Datetime >= ? AND v.Datetime < ?", (dt_from, dt_to), codes)


def _all_in(ld_id: int, dt_from: datetime, dt_to: datetime,
            codes: tuple[str, ...] = _CODES_A) -> list[dict]:
    db = get_db()
    cur = db.cursor()
    in_list = ", ".join(f"'{c}'" for c in codes)
    cur.execute(
        f"""
        SELECT dt.Code, v.Datetime, v.ValueFloat
        FROM dbo.Value v
        JOIN dbo.Tag t        ON v.IdDeviceTag = t.Id
        JOIN dbo.DeviceTag dt ON t.IdDeviceTag = dt.Id
        WHERE t.IdLogicDevice = ?
          AND dt.Code IN ({in_list})
          AND v.Datetime >= ? AND v.Datetime < ?
        ORDER BY v.Datetime, dt.Code
        """,
        (ld_id, dt_from, dt_to),
    )
    return [{"code": r[0], "ts": r[1], "val": r[2]} for r in cur.fetchall()]


# --- response helpers -------------------------------------------------------

def _total_r(readings: dict[str, dict]) -> dict | None:
    return readings.get("A+0") or readings.get("MA+0")


def _snapshot(readings: dict[str, dict]) -> dict:
    """Total snapshot for /reading (A+ or MA+ codes)."""
    tr = _total_r(readings)
    total_val = _round(tr["val"]) if tr else "-"
    return {
        "timestamp": _fmt_ts(tr["ts"]) if tr else "-",
        "total": total_val,
    }


def _period_fields(start: dict[str, dict], end: dict[str, dict], kc: float | None) -> dict:
    """total start/end/delta/consumption_ktt (A+ or MA+ codes)."""
    ts = _total_r(start)
    te = _total_r(end)
    sv = _round(ts["val"]) if ts else "-"
    ev = _round(te["val"]) if te else "-"
    d = _delta(sv, ev)
    return {
        "timestamp_start": _fmt_ts(ts["ts"]) if ts else "-",
        "timestamp_end": _fmt_ts(te["ts"]) if te else "-",
        "total_start": sv,
        "total_end": ev,
        "total_delta": d,
        "total_consumption_ktt": _round(d * kc) if (_is_num(d) and kc is not None) else "-",
    }


# --- endpoints --------------------------------------------------------------

@bp.route("/devices")
def devices():
    """
    GET /api/moesk/devices[?serial=<SN>]

    Информация о счётчике(ах): объект, корпус/здание, станция, устройство, серийный номер.
    Без параметра serial возвращает все счётчики.
    """
    serial = request.args.get("serial", "").strip()

    if serial:
        device = _fetch_device(serial)
        if device is None:
            return jsonify({"error": f"Счётчик '{serial}' не найден"}), 404
        return jsonify(_device_info(device))

    all_devs = _fetch_all_devices()
    return jsonify([_device_info(d) for d in all_devs])


# ---------------------------------------------------------------------------
# Per-device logic helpers
# ---------------------------------------------------------------------------

def _parse_serials(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _sort_results(results: list[dict]) -> list[dict]:
    return sorted(results, key=lambda x: (x.get("order_num") or 0))


def _respond(results: list[dict], multi: bool):
    if not results:
        return jsonify({"error": "нет данных"}), 404
    results = _sort_results(results)
    return jsonify(results if multi else results[0])


def _reading_one(device: dict, date_param: str) -> dict:
    ld_id = device["IdLogicDevice"]
    if date_param:
        target = _parse_date(date_param, date.today())
        target_dt = datetime(target.year, target.month, target.day, 23, 59, 59)
        readings = _last_on_or_before(ld_id, target_dt)
        req_date = target.isoformat()
    else:
        readings = _latest(ld_id)
        tr = _total_r(readings)
        req_date = tr["ts"].date().isoformat() if tr and isinstance(tr["ts"], datetime) else date.today().isoformat()
    return {**_device_meta(device), "req_date": req_date, **_snapshot(readings)}


def _period_one(device: dict, date_from: date, date_to: date) -> dict:
    ld_id = device["IdLogicDevice"]
    kc = _get_kc(device)
    dt_from = datetime(date_from.year, date_from.month, date_from.day)
    dt_to_exc = datetime(date_to.year, date_to.month, date_to.day) + timedelta(days=1)
    start = _last_before(ld_id, dt_from) or _first_in(ld_id, dt_from, dt_to_exc)
    end = _last_in(ld_id, dt_from, dt_to_exc) or start
    return {
        **_device_meta(device),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        **_period_fields(start, end, kc),
    }


_DAY_EMPTY = {
    "timestamp_start": "-", "timestamp_end": "-",
    "total_start": "-", "total_end": "-",
    "total_delta": "-", "total_consumption_ktt": "-",
}


def _daily_one(device: dict, date_from: date, date_to: date) -> dict:
    ld_id = device["IdLogicDevice"]
    kc = _get_kc(device)
    dt_from = datetime(date_from.year, date_from.month, date_from.day)
    dt_to_exc = datetime(date_to.year, date_to.month, date_to.day) + timedelta(days=1)

    prev = _last_before(ld_id, dt_from) or _first_in(ld_id, dt_from, dt_to_exc)
    period_end_r = _last_in(ld_id, dt_from, dt_to_exc) or prev

    rows = _all_in(ld_id, dt_from, dt_to_exc)
    date_map: dict[date, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        d = r["ts"].date() if isinstance(r["ts"], datetime) else r["ts"]
        date_map[d][r["code"]] = {"ts": r["ts"], "val": r["val"]}

    prev_snap = prev
    days = []
    cur_d = date_from
    while cur_d <= date_to:
        if cur_d in date_map:
            cur_snap = date_map[cur_d]
            entry = {"req_date": cur_d.isoformat()}
            entry.update(_period_fields(prev_snap, cur_snap, kc))
            prev_snap = cur_snap
        else:
            entry = {"req_date": cur_d.isoformat(), **_DAY_EMPTY}
        days.append(entry)
        cur_d += timedelta(days=1)

    return {
        **_device_meta(device),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        **_period_fields(prev, period_end_r, kc),
        "days": days,
    }


_MONTH_EMPTY = {
    "timestamp_start": "-", "timestamp_end": "-",
    "total_start": "-", "total_end": "-",
    "total_delta": "-", "total_consumption_ktt": "-",
}


def _monthly_one(
    device: dict,
    ym_from: tuple[int, int],
    ym_to: tuple[int, int],
) -> dict:
    ld_id = device["IdLogicDevice"]
    kc = _get_kc(device)
    today = date.today()
    cur_ym = (today.year, today.month)

    dt_from = datetime(ym_from[0], ym_from[1], 1)
    next_ym = _add_months(ym_to[0], ym_to[1], 1)
    # Конец завершённого месяца M — это MA+ снимок на 1-е число M+1 (00:00:00).
    # Для последнего месяца диапазона (ym_to) этот снимок лежит ровно на границе
    # next_ym, поэтому окно расширяем ещё на месяц, иначе условие Datetime < dt_to
    # отрезало бы его и столбцы «конец/расход» оставались бы пустыми.
    after_ym = _add_months(next_ym[0], next_ym[1], 1)
    dt_to = datetime(after_ym[0], after_ym[1], 1)

    rows = _all_in(ld_id, dt_from, dt_to, _CODES_MA)

    seen: set[tuple] = set()
    first_by_month: dict[tuple[int, int], dict[str, dict]] = defaultdict(dict)
    for r in sorted(rows, key=lambda x: x["ts"]):
        ts = r["ts"]
        key = (ts.year, ts.month, r["code"])
        if key not in seen:
            seen.add(key)
            first_by_month[(ts.year, ts.month)][r["code"]] = {"ts": ts, "val": r["val"]}

    # Итоговый период: начало — последний MA+ до ym_from, конец — первый MA+ после ym_to
    year_s = _last_before(ld_id, dt_from, _CODES_MA) or first_by_month.get(ym_from, {})
    year_e = first_by_month.get(next_ym, {})
    latest_cache: dict | None = None

    def _get_latest_cached() -> dict:
        nonlocal latest_cache
        if latest_cache is None:
            latest_cache = _latest(ld_id)
        return latest_cache

    if not year_e and ym_to >= cur_ym:
        year_e = _get_latest_cached()

    months = []
    ym = ym_from
    while ym <= ym_to:
        y, m = ym
        if date(y, m, 1) > today:
            months.append({"req_year": y, "req_month": m, **_MONTH_EMPTY})
        else:
            start_r = first_by_month.get(ym, {})
            end_ym = _add_months(y, m, 1)
            end_r = first_by_month.get(end_ym, {})
            if not end_r and ym == cur_ym:
                end_r = _get_latest_cached()
            months.append({"req_year": y, "req_month": m, **_period_fields(start_r, end_r, kc)})
        ym = _add_months(y, m, 1)

    return {
        **_device_meta(device),
        "year_from": ym_from[0],
        "month_from": ym_from[1],
        "year_to": ym_to[0],
        "month_to": ym_to[1],
        "energy_type": "СУММА",
        **_period_fields(year_s, year_e, kc),
        "months": months,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp.route("/reading")
def reading():
    """
    GET /api/moesk/reading?serial=<SN>[,<SN2>...][&date=YYYY-MM-DD]

    Последнее A+ показание. Без date — абсолютно последнее в БД.
    Несколько серийников через запятую → массив, отсортированный по order_num.
    """
    raw = request.args.get("serial", "").strip()
    if not raw:
        return jsonify({"error": "Параметр 'serial' обязателен"}), 400
    date_param = request.args.get("date", "").strip()
    serials = _parse_serials(raw)
    multi = len(serials) > 1

    results = []
    for s in serials:
        device = _fetch_device(s)
        if device is None:
            if not multi:
                return jsonify({"error": f"Счётчик '{s}' не найден"}), 404
            continue
        results.append(_reading_one(device, date_param))

    return _respond(results, multi)


@bp.route("/period")
def period():
    """
    GET /api/moesk/period?serial=<SN>[,<SN2>...][&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD]

    Показания за период. Несколько серийников → массив.
    """
    raw = request.args.get("serial", "").strip()
    if not raw:
        return jsonify({"error": "Параметр 'serial' обязателен"}), 400
    today = date.today()
    date_from = _parse_date(request.args.get("date_from"), today)
    date_to = _parse_date(request.args.get("date_to"), today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    serials = _parse_serials(raw)
    multi = len(serials) > 1

    results = []
    for s in serials:
        device = _fetch_device(s)
        if device is None:
            if not multi:
                return jsonify({"error": f"Счётчик '{s}' не найден"}), 404
            continue
        results.append(_period_one(device, date_from, date_to))

    return _respond(results, multi)


@bp.route("/daily")
def daily():
    """
    GET /api/moesk/daily?serial=<SN>[,<SN2>...][&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD]

    Посуточный отчёт. Все дни периода присутствуют (без данных → «-»).
    Период ограничен DAILY_REPORT_LIMIT днями (дефолт 62).
    """
    raw = request.args.get("serial", "").strip()
    if not raw:
        return jsonify({"error": "Параметр 'serial' обязателен"}), 400
    today = date.today()
    date_from = _parse_date(request.args.get("date_from"), today)
    date_to = _parse_date(request.args.get("date_to"), today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    limit: int = current_app.config.get("DAILY_REPORT_LIMIT", 62)
    capped = False
    min_from = date_to - timedelta(days=limit - 1)
    if date_from < min_from:
        date_from = min_from
        capped = True
    serials = _parse_serials(raw)
    multi = len(serials) > 1

    results = []
    for s in serials:
        device = _fetch_device(s)
        if device is None:
            if not multi:
                return jsonify({"error": f"Счётчик '{s}' не найден"}), 404
            continue
        r = _daily_one(device, date_from, date_to)
        if capped:
            r["note"] = f"Период ограничен до {limit} суток"
        results.append(r)

    return _respond(results, multi)


def _build_sheet_row(device: dict, m: dict) -> list | None:
    """Собирает строку листа для одного месяца одного устройства (без формулы)."""
    if m.get("total_start") in (None, "-") and m.get("total_end") in (None, "-"):
        return None

    def _v(val):
        return "" if val in (None, "-") else val

    props = device.get("props", {})
    ktt = _round(_get_kc(device))
    mn = m["req_month"]
    year = m["req_year"]
    return [
        year,                                   # A  Год
        f"{mn:02d}-{_MONTH_ABBR[mn]}",          # B  Месяц
        mn,                                     # C  Месяц_ИД        ← ключ сортировки[0]
        device.get("eso_row") or "",            # D  №               ← ключ сортировки[1]
        device.get("station_name") or "",       # E  Название
        device.get("station_address") or "",    # F  Адрес
        device.get("region_name") or "",        # G  Объект
        device.get("filial_name") or "",        # H  Корпус
        f"01.{mn:02d}.{year}",                  # I  Дата            ← dedup[0]
        device.get("meter_name") or "",         # J  Прибор учета
        props.get("MeasuringpointName") or "",  # K  Точка учёта     ← dedup[2]
        device["serial_number"],                # L  Номер счетчика  ← dedup[1]
        props.get("AgrNo") or "",               # M  Номер договора
        "СУММА",                                # N  Вид энергии
        ktt,                                    # O  КТТ
        _v(m.get("total_start")),               # P  Показания начало
        _v(m.get("total_end")),                 # Q  Показания конец
        _v(m.get("total_delta")),               # R  Расход, кВт*ч
        _v(m.get("total_consumption_ktt")),     # S  Расход с КТТ и КТН
        "",                                     # T  Тариф — заполняется при синке
        # U  Начисления — добавляется по номеру строки
    ]


def _last_recorded_tariffs(data_rows: list[list]) -> tuple[dict[str, str], str]:
    """Из существующих строк листа собирает последний записанный тариф (столбец T, idx 19).

    Тариф МОЭСК в БД RMon4Dev отсутствует и вносится в лист вручную. Чтобы при
    повторной выгрузке столбец T не обнулялся, переносим последний уже записанный
    тариф: по каждому серийнику (макс. по (Год, Месяц_ИД) среди строк с непустым T)
    плюс общий запасной (последний тариф по всему листу).
    Возвращает ({серийник: тариф}, последний_общий_тариф).
    """
    best: dict[str, tuple[int, int, str]] = {}
    overall: tuple[int, int, str] | None = None
    for r in data_rows:
        tariff = str(r[19]).strip() if len(r) > 19 else ""
        if not tariff:
            continue
        try:
            ym = (int(r[0]), int(r[2]))
        except (ValueError, TypeError, IndexError):
            continue
        if overall is None or ym > overall[:2]:
            overall = (ym[0], ym[1], tariff)
        serial = str(r[11]).strip() if len(r) > 11 else ""
        if serial and (serial not in best or ym > best[serial][:2]):
            best[serial] = (ym[0], ym[1], tariff)
    by_serial = {s: v[2] for s, v in best.items()}
    return by_serial, (overall[2] if overall else "")


def _resort_moesk_sheet(svc, spreadsheet_id: str, sheet_name: str) -> None:
    """Пересортировать весь лист по (Год, Месяц_ИД, №) и пересчитать формулы столбца U."""
    all_rows = svc.read_sheet(spreadsheet_id, sheet_name)
    if len(all_rows) <= 1:
        return

    header = all_rows[:1]
    data = list(all_rows[1:])

    def _int(v: object) -> int:
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    data.sort(key=lambda r: (
        _int(r[0]) if len(r) > 0 else 0,   # Год
        _int(r[2]) if len(r) > 2 else 0,   # Месяц_ИД
        _int(r[3]) if len(r) > 3 else 0,   # №
    ))

    for i, row in enumerate(data):
        sheet_row = i + 2  # 1-indexed + 1 строка заголовка
        formula = f"=S{sheet_row}*T{sheet_row}"
        if len(row) > _MOESK_FORMULA_COL:
            row[_MOESK_FORMULA_COL] = formula
        else:
            while len(row) < _MOESK_FORMULA_COL:
                row.append("")
            row.append(formula)

    svc.clear_and_write(spreadsheet_id, sheet_name, header + data)


def _push_moesk_monthly_batch(
    device_months: list[tuple[dict, list[dict]]],
) -> dict:
    """Выгружает помесячные строки всех устройств в Google Sheets.

    Строки сортируются по (Год, Месяц_ИД, eso_row).
    Dedup: Дата(8) + Номер счетчика(11) + Точка учёта(10).
    Столбец U (Начисления) — формула =S{row}*T{row}.
    После upsert весь лист пересортировывается.
    """
    spreadsheet_id = current_app.config.get("GOOGLE_SHEETS_MOESK_ID", "")
    if not spreadsheet_id:
        return {"error": "GOOGLE_SHEETS_MOESK_ID не настроен"}

    sheet_name = current_app.config.get("GOOGLE_SHEETS_MOESK_SHEET", "moesk")

    raw_rows: list[list] = []
    for device, months in device_months:
        for m in months:
            row = _build_sheet_row(device, m)
            if row is not None:
                raw_rows.append(row)

    if not raw_rows:
        return {"appended": 0, "updated": 0}

    # Сортировка: по году (col 0), месяцу (col 2), eso_row (col 3)
    raw_rows.sort(key=lambda r: (r[0], r[2], r[3] if isinstance(r[3], int) else 0))

    svc = get_sheets_service()
    svc.ensure_header(spreadsheet_id, sheet_name, _MOESK_HEADER)

    existing = svc.read_sheet(spreadsheet_id, sheet_name)
    header_rows = 1 if existing else 0
    data_rows = existing[header_rows:]

    # Тариф (T) в БД нет — заполняем из листа, чтобы перевыгрузка не обнуляла
    # столбец и формула U (=S*T) считала начисления. Для обновляемой строки
    # сохраняем её собственный записанный тариф (мог быть внесён вручную, либо
    # это исторический период); для новой — переносим последний известный.
    tariff_by_serial, last_tariff = _last_recorded_tariffs(data_rows)

    key_to_idx: dict[tuple, int] = {
        svc._make_key(r, _MOESK_KEY_COLS): i
        for i, r in enumerate(data_rows)
    }

    to_update: list[tuple[int, list]] = []
    to_append: list[list] = []

    for row in raw_rows:
        key = svc._make_key(row, _MOESK_KEY_COLS)
        serial = str(row[11]).strip() if len(row) > 11 else ""
        if key in key_to_idx:
            idx = key_to_idx[key]
            existing_row = data_rows[idx]
            existing_t = str(existing_row[19]).strip() if len(existing_row) > 19 else ""
            if len(row) > 19 and str(row[19]).strip() == "":
                row[19] = existing_t or tariff_by_serial.get(serial, last_tariff)
            sheet_row = idx + header_rows + 1
            to_update.append((sheet_row, row + [f"=S{sheet_row}*T{sheet_row}"]))
        else:
            if len(row) > 19 and str(row[19]).strip() == "":
                row[19] = tariff_by_serial.get(serial, last_tariff)
            to_append.append(row)

    start_row = len(data_rows) + header_rows + 1
    rows_with_formula = [
        row + [f"=S{start_row + i}*T{start_row + i}"]
        for i, row in enumerate(to_append)
    ]

    for sheet_row, full_row in to_update:
        svc._update_row(spreadsheet_id, sheet_name, sheet_row, full_row)

    if rows_with_formula:
        svc._append(spreadsheet_id, sheet_name, rows_with_formula)

    _resort_moesk_sheet(svc, spreadsheet_id, sheet_name)

    log.info(
        "moesk sheets spreadsheet=%s sheet=%s appended=%d updated=%d",
        spreadsheet_id, sheet_name, len(rows_with_formula), len(to_update),
    )
    return {"appended": len(rows_with_formula), "updated": len(to_update)}


def _parse_monthly_range(args, today: date, limit: int) -> tuple[tuple[int, int], tuple[int, int], bool]:
    """Разбирает параметры запроса и возвращает (ym_from, ym_to, capped).

    Режимы (приоритет сверху вниз):
      year_from / month_from [year_to / month_to]  — диапазон год+месяц
      date_from / date_to                          — диапазон дат (обрезается до месяцев)
      months=N [exclude_current=y]                 — последние N месяцев
      year=YYYY [year_mode=ytd|ytd_excl|full]      — отчёт по году
      (ничего)                                     — текущий месяц
    """
    cur_ym = (today.year, today.month)

    year_from_s  = args.get("year_from",  "").strip()
    month_from_s = args.get("month_from", "").strip()
    year_to_s    = args.get("year_to",    "").strip()
    month_to_s   = args.get("month_to",   "").strip()
    date_from_s  = args.get("date_from",  "").strip()
    date_to_s    = args.get("date_to",    "").strip()
    months_s     = args.get("months",     "").strip()
    year_s       = args.get("year",       "").strip()

    exclude_current = args.get("exclude_current", "n").strip().lower() in ("y", "yes", "true", "1")
    year_mode       = args.get("year_mode", "ytd").strip().lower()  # ytd | ytd_excl | full

    if year_from_s or month_from_s:
        yf = int(year_from_s)  if year_from_s  else cur_ym[0]
        mf = int(month_from_s) if month_from_s else 1
        yt = int(year_to_s)    if year_to_s    else cur_ym[0]
        mt = int(month_to_s)   if month_to_s   else cur_ym[1]
        ym_from, ym_to = (yf, mf), (yt, mt)

    elif date_from_s or date_to_s:
        if date_from_s:
            d = date.fromisoformat(date_from_s)
            ym_from = (d.year, d.month)
        else:
            ym_from = cur_ym
        if date_to_s:
            d = date.fromisoformat(date_to_s)
            ym_to = (d.year, d.month)
        else:
            ym_to = cur_ym

    elif months_s:
        n = int(months_s)
        if n < 1:
            raise ValueError("months должен быть >= 1")
        end_ym = _add_months(cur_ym[0], cur_ym[1], -1) if exclude_current else cur_ym
        ym_to   = end_ym
        ym_from = _add_months(end_ym[0], end_ym[1], -(n - 1))

    elif year_s:
        y = int(year_s)
        ym_from = (y, 1)
        if year_mode == "full":
            ym_to = (y, 12)
        elif year_mode == "ytd_excl":
            if y < today.year:
                ym_to = (y, 12)
            elif y == today.year:
                ym_to = _add_months(cur_ym[0], cur_ym[1], -1) if cur_ym[1] > 1 else ym_from
            else:
                ym_to = ym_from
        else:  # ytd (default)
            if y < today.year:
                ym_to = (y, 12)
            elif y == today.year:
                ym_to = cur_ym
            else:
                ym_to = ym_from  # будущий год — только первый месяц пустым

    else:
        ym_from = ym_to = cur_ym

    if ym_from > ym_to:
        ym_from, ym_to = ym_to, ym_from

    count = _month_count(ym_from, ym_to)
    capped = count > limit
    if capped:
        ym_from = _add_months(ym_to[0], ym_to[1], -(limit - 1))

    return ym_from, ym_to, capped


@bp.route("/monthly")
def monthly():
    """
    GET /api/moesk/monthly?serial=<SN>[,<SN2>...][&sync=y][&<range_params>]

    Помесячный отчёт. Все месяцы диапазона присутствуют.
    Несколько серийников → массив, отсортированный по order_num.
    sync=y — выгружает данные в Google Sheets.

    Параметры диапазона (приоритет сверху вниз):
      year_from, month_from [year_to, month_to]  — диапазон год+месяц
      date_from [date_to]                        — диапазон дат
      months=N [exclude_current=y]               — последние N месяцев
      year=YYYY [year_mode=ytd|ytd_excl|full]    — отчёт по году
      (ничего)                                   — текущий месяц
    Максимум MONTHLY_REPORT_LIMIT месяцев (дефолт 24, в конфиге).
    """
    raw = request.args.get("serial", "").strip()
    if not raw:
        return jsonify({"error": "Параметр 'serial' обязателен"}), 400

    today = date.today()
    limit: int = current_app.config.get("MONTHLY_REPORT_LIMIT", 24)
    sync = request.args.get("sync", "n").strip().lower() in ("y", "yes", "true", "1")

    try:
        ym_from, ym_to, capped = _parse_monthly_range(request.args, today, limit)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": f"Ошибка в параметрах: {exc}"}), 400

    serials = _parse_serials(raw)
    multi = len(serials) > 1

    results = []
    device_months: list[tuple[dict, list[dict]]] = []

    for s in serials:
        device = _fetch_device(s)
        if device is None:
            if not multi:
                return jsonify({"error": f"Счётчик '{s}' не найден"}), 404
            continue
        r = _monthly_one(device, ym_from, ym_to)
        if capped:
            r["note"] = f"Период ограничен до {limit} месяцев"
        results.append(r)
        if sync:
            device_months.append((device, r["months"]))

    if sync and device_months:
        try:
            sync_stat = _push_moesk_monthly_batch(device_months)
        except Exception as exc:
            log.error("moesk monthly sheets sync: %s", exc)
            sync_stat = {"error": str(exc)}
        for r in results:
            r["sheets_sync"] = sync_stat

    return _respond(results, multi)
