import calendar
import logging
from datetime import date, datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from services.db_mariadb import get_db

bp = Blueprint("resource", __name__, url_prefix="/api/v1/resource")
logger = logging.getLogger(__name__)

_COUNTER_FIELDS = """
    c.SerialNumber  AS serial_number,
    c.Name          AS name,
    c.State         AS state
"""

_NOT_FOUND_ROW = {
    "value": "-",
    "timestamp": "-",
}


def _round(val) -> float | str:
    if val is None or val == "-":
        return "-"
    return round(float(val), 1)


def _is_num(v) -> bool:
    return isinstance(v, (int, float))


def _fmt_row(row: dict | None) -> dict:
    if row is None:
        return _NOT_FOUND_ROW.copy()
    ts = row["timestamp"]
    return {
        "value": _round(row["value"]),
        "timestamp": ts.isoformat() if isinstance(ts, datetime) else str(ts),
    }


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
    """Resolve date_from / date_to with smart defaults.

    Both absent  → 1st of current month .. today
    Only from    → to = from + default_days
    Only to      → from = to  - default_days
    Both present → parse as-is
    """
    has_from = bool(raw_from and raw_from.strip())
    has_to   = bool(raw_to   and raw_to.strip())

    if not has_from and not has_to:
        return date(today.year, today.month, 1), today
    if has_from and not has_to:
        d_from = _parse_date(raw_from, today)
        return d_from, d_from + timedelta(days=default_days)
    if has_to and not has_from:
        d_to = _parse_date(raw_to, today)
        return d_to - timedelta(days=default_days), d_to
    return _parse_date(raw_from, today), _parse_date(raw_to, today)


def _parse_serials(raw: str) -> list[str]:
    """Разбить строку серийных номеров через запятую."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _fetch_counter(serial: str) -> dict | None:
    db = get_db()
    with db.cursor() as cur:
        # CAST AS UNSIGNED игнорирует ведущие нули на обеих сторонах
        cur.execute(
            f"SELECT {_COUNTER_FIELDS}, c.Obj_Id_Counter AS counter_id"
            " FROM counter c"
            " WHERE CAST(c.SerialNumber AS UNSIGNED) = CAST(%s AS UNSIGNED)"
            " LIMIT 1",
            (serial,),
        )
        row = cur.fetchone()
    if row:
        row["serial_number"] = row["serial_number"].zfill(8)
    return row


def _last_before_or_on(counter_id: int, target_date: date) -> dict | None:
    """Последнее показание с датой <= target_date."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT Consumption AS value, UpdateTime AS timestamp"
            " FROM consumption"
            " WHERE Obj_Id_Counter = %s AND DATE(UpdateTime) <= %s"
            " ORDER BY UpdateTime DESC LIMIT 1",
            (counter_id, target_date),
        )
        return cur.fetchone()


def _last_before(counter_id: int, target_date: date) -> dict | None:
    """Последнее показание строго до target_date."""
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


def _first_in_period(counter_id: int, date_from: date, date_to: date) -> dict | None:
    """Первое показание внутри периода [date_from, date_to]."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT Consumption AS value, UpdateTime AS timestamp"
            " FROM consumption"
            " WHERE Obj_Id_Counter = %s AND DATE(UpdateTime) BETWEEN %s AND %s"
            " ORDER BY UpdateTime ASC LIMIT 1",
            (counter_id, date_from, date_to),
        )
        return cur.fetchone()


def _last_in_period(counter_id: int, date_from: date, date_to: date) -> dict | None:
    """Последнее показание внутри периода [date_from, date_to]."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT Consumption AS value, UpdateTime AS timestamp"
            " FROM consumption"
            " WHERE Obj_Id_Counter = %s AND DATE(UpdateTime) BETWEEN %s AND %s"
            " ORDER BY UpdateTime DESC LIMIT 1",
            (counter_id, date_from, date_to),
        )
        return cur.fetchone()


def _delta(start: dict, end: dict) -> float | str:
    sv, ev = start["value"], end["value"]
    if _is_num(sv) and _is_num(ev):
        return _round(ev - sv)
    return "-"


def _fetch_b_data(serial: str, ref_date: date) -> dict | None:
    """Данные из b_count/b_group/b_tarif. None если счётчик не в b_count."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT bc.Ktr      AS ktr,
                   bc.Group_Id AS group_id,
                   bg.Name     AS group_name,
                   bt.Cost     AS tariff
            FROM b_count bc
            JOIN b_group bg ON bg.Id = bc.Group_Id
            LEFT JOIN b_tarif bt
                   ON bt.Group_Id = bc.Group_Id
                  AND bt.UpdDate = (
                          SELECT MAX(t2.UpdDate) FROM b_tarif t2
                          WHERE t2.Group_Id = bc.Group_Id
                            AND t2.UpdDate <= %s
                      )
            WHERE bc.SerialNumber = CAST(%s AS UNSIGNED)
            """,
            (ref_date, serial),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "group_id": row["group_id"],
        "group_name": row["group_name"],
        "ktr": row["ktr"],
        "tariff": row["tariff"],
    }


# ---------------------------------------------------------------------------
# Sheets helpers
# ---------------------------------------------------------------------------

_RU_MONTHS = [
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]

_RESOURCE_SHEET_HEADER = [
    "Год", "Месяц", "Месяц_ИД", "Дата",
    "Номер счётчика", "Название счётчика",
    "Дельта", "Нач.показания", "Время нач.показаний",
    "Кон.показания", "Время кон.показаний",
    "Ктр", "Тариф", "Группа_ИД", "Группа",
    "Потребление * Ктр", "Потребление * Ктр * Тариф",
]


def _b_fields(b: dict | None) -> tuple:
    return (
        b["ktr"]        if b else 1,
        b["tariff"]     if b else 7.07,
        b["group_id"]   if b else "",
        b["group_name"] if b else "",
    )


def _resource_sheet_row(
    year: int, month: int, date_str: str,
    counter: dict, delta, v_start, ts_start, v_end, ts_end,
    ktr, tariff, group_id, group_name,
) -> list:
    delta_ktr = _round(delta * ktr) if _is_num(delta) and _is_num(ktr) else "-"
    delta_ktr_tariff = (
        round(delta * ktr * tariff, 2)
        if _is_num(delta) and _is_num(ktr) and _is_num(tariff)
        else "-"
    )
    return [
        year, _RU_MONTHS[month], month, f"'{date_str}",
        counter["serial_number"], counter["name"],
        delta, v_start, ts_start, v_end, ts_end,
        ktr, tariff, group_id, group_name,
        delta_ktr, delta_ktr_tariff,
    ]


def _sheets_push(spreadsheet_id: str, sheet_name: str, rows: list, key_cols: list) -> dict:
    from services.sheets import get_sheets_service
    svc = get_sheets_service()
    svc.ensure_header(spreadsheet_id, sheet_name, _RESOURCE_SHEET_HEADER)
    return svc.upsert_rows(spreadsheet_id, sheet_name, rows, key_cols=key_cols)


def _push_resource_period(
    counter: dict, date_from: date, date_to: date,
    delta, v_start, ts_start, v_end, ts_end,
    b_data: dict | None,
) -> dict:
    spreadsheet_id = current_app.config.get("GOOGLE_SHEETS_RESOURCE_ID", "")
    if not spreadsheet_id:
        return {"error": "GOOGLE_SHEETS_RESOURCE_ID не настроен"}

    if delta == "-":
        return {"appended": 0, "updated": 0}

    ktr, tariff, group_id, group_name = _b_fields(b_data)
    row = _resource_sheet_row(
        date_from.year, date_from.month, date_from.isoformat(),
        counter, delta, v_start, ts_start, v_end, ts_end,
        ktr, tariff, group_id, group_name,
    )
    sheet_name = current_app.config.get("GOOGLE_SHEETS_RESOURCE_SHEET", "resource")
    return _sheets_push(spreadsheet_id, sheet_name, [row], key_cols=[0, 3, 4])


def _push_resource_daily(counter: dict, days: list[dict]) -> dict:
    spreadsheet_id = current_app.config.get("GOOGLE_SHEETS_RESOURCE_ID", "")
    if not spreadsheet_id:
        return {"error": "GOOGLE_SHEETS_RESOURCE_ID не настроен"}

    rows = []
    for day in days:
        d = day["delta"]
        if d == "-":
            continue
        day_date = date.fromisoformat(day["req_date"])
        ktr, tariff, group_id, group_name = _b_fields(
            _fetch_b_data(counter["serial_number"], day_date)
        )
        rows.append(_resource_sheet_row(
            day_date.year, day_date.month, day_date.strftime("%d.%m.%Y"),
            counter, d,
            day["value_start"], day["timestamp_start"],
            day["value_end"], day["timestamp_end"],
            ktr, tariff, group_id, group_name,
        ))

    sheet_name = current_app.config.get("GOOGLE_SHEETS_RESOURCE_SHEET", "resource")
    return _sheets_push(spreadsheet_id, sheet_name, rows, key_cols=[0, 3, 4])


def _push_resource_monthly(counter: dict, year: int, months: list[dict]) -> dict:
    spreadsheet_id = current_app.config.get("GOOGLE_SHEETS_RESOURCE_ID", "")
    if not spreadsheet_id:
        return {"error": "GOOGLE_SHEETS_RESOURCE_ID не настроен"}

    today = date.today()
    rows = []
    for m in months:
        d = m["delta"]
        if d == "-":
            continue
        m_num = m["req_month"]
        ref = min(date(year, m_num, calendar.monthrange(year, m_num)[1]), today)
        ktr, tariff, group_id, group_name = _b_fields(
            _fetch_b_data(counter["serial_number"], ref)
        )
        rows.append(_resource_sheet_row(
            year, m_num, f"{year}-{m_num:02d}-01",
            counter, d,
            m["value_start"], m["timestamp_start"],
            m["value_end"], m["timestamp_end"],
            ktr, tariff, group_id, group_name,
        ))

    sheet_name = current_app.config.get("GOOGLE_SHEETS_RESOURCE_SHEET", "resource")
    return _sheets_push(spreadsheet_id, sheet_name, rows, key_cols=[0, 2, 4])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp.route("/reading")
def reading():
    """
    GET /api/resource/reading?serial=<SN>[,<SN2>,...][&date=YYYY-MM-DD]

    Показание на дату для одного или нескольких счётчиков.
    Возвращает массив результатов.
    """
    serials = _parse_serials(request.args.get("serial", ""))
    if not serials:
        return jsonify({"error": "Параметр 'serial' обязателен"}), 400

    today = date.today()
    target = _parse_date(request.args.get("date"), today)

    results = []
    for serial in serials:
        counter = _fetch_counter(serial)
        if counter is None:
            results.append({"serial_number": serial.zfill(8), "error": "не найден"})
            continue

        data = _fmt_row(_last_before_or_on(counter["counter_id"], target))
        results.append({
            "serial_number": counter["serial_number"],
            "name": counter["name"],
            "state": counter["state"],
            "req_date": target.isoformat(),
            "timestamp": data["timestamp"],
            "value": data["value"],
            "b_data": _fetch_b_data(serial, target),
        })

    return jsonify(results)


@bp.route("/period")
def period():
    """
    GET /api/resource/period?serial=<SN>[,<SN2>,...][&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD][&sync=y]

    Показания за период для одного или нескольких счётчиков.
    Возвращает массив результатов.
    """
    serials = _parse_serials(request.args.get("serial", ""))
    if not serials:
        return jsonify({"error": "Параметр 'serial' обязателен"}), 400

    sync = request.args.get("sync", "n").lower() == "y"
    today = date.today()
    date_from, date_to = _parse_period(
        request.args.get("date_from"), request.args.get("date_to"), today
    )
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    results = []
    for serial in serials:
        counter = _fetch_counter(serial)
        if counter is None:
            results.append({"serial_number": serial.zfill(8), "error": "не найден"})
            continue

        cid = counter["counter_id"]
        start_row = _last_before(cid, date_from)
        if start_row is None:
            start_row = _first_in_period(cid, date_from, date_to)
        start = _fmt_row(start_row)
        end_row = _last_in_period(cid, date_from, date_to)
        end = _fmt_row(end_row) if end_row is not None else start.copy()
        b_data = _fetch_b_data(serial, date_to)
        delta = _delta(start, end)

        item: dict = {
            "serial_number": counter["serial_number"],
            "name": counter["name"],
            "state": counter["state"],
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "value_start": start["value"],
            "timestamp_start": start["timestamp"],
            "value_end": end["value"],
            "timestamp_end": end["timestamp"],
            "delta": delta,
            "b_data": b_data,
        }

        if sync:
            try:
                item["sheets_sync"] = _push_resource_period(
                    counter, date_from, date_to,
                    delta, start["value"], start["timestamp"],
                    end["value"], end["timestamp"], b_data,
                )
            except Exception as exc:
                logger.error("resource period sheets sync: %s", exc)
                item["sheets_sync"] = {"error": str(exc)}

        results.append(item)

    return jsonify(results)


@bp.route("/daily")
def daily():
    """
    GET /api/resource/daily?serial=<SN>[,<SN2>,...][&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD][&sync=y]

    Посуточный отчёт для одного или нескольких счётчиков.
    Период ограничен DAILY_REPORT_LIMIT днями (дефолт 50).
    Возвращает массив результатов.
    """
    serials = _parse_serials(request.args.get("serial", ""))
    if not serials:
        return jsonify({"error": "Параметр 'serial' обязателен"}), 400

    sync = request.args.get("sync", "n").lower() == "y"
    today = date.today()
    date_from, date_to = _parse_period(
        request.args.get("date_from"), request.args.get("date_to"), today
    )
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    limit: int = current_app.config.get("DAILY_REPORT_LIMIT", 50)
    capped = False
    min_date_from = date_to - timedelta(days=limit - 1)
    if date_from < min_date_from:
        date_from = min_date_from
        capped = True

    results = []
    for serial in serials:
        counter = _fetch_counter(serial)
        if counter is None:
            results.append({"serial_number": serial.zfill(8), "error": "не найден"})
            continue

        cid = counter["counter_id"]

        prev_row = _last_before(cid, date_from)
        prev_value: float | None = None
        prev_ts: str = "-"
        if prev_row and _is_num(prev_row["value"]):
            prev_value = _round(prev_row["value"])
            _pt = prev_row["timestamp"]
            prev_ts = _pt.isoformat() if isinstance(_pt, datetime) else str(_pt)

        period_start_row = prev_row if prev_row else _first_in_period(cid, date_from, date_to)
        period_start = _fmt_row(period_start_row)
        period_end_row = _last_in_period(cid, date_from, date_to)
        period_end = _fmt_row(period_end_row) if period_end_row is not None else period_start.copy()

        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT cn.Consumption     AS value,
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
                (cid, date_from, date_to),
            )
            rows = cur.fetchall()

        day_map: dict[date, dict] = {r["day"]: r for r in rows}

        days = []
        cur_date = date_from
        while cur_date <= date_to:
            row = day_map.get(cur_date)
            if row:
                val = _round(row["value"])
                ts = row["timestamp"]
                ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)
                v_start = prev_value if prev_value is not None else "-"
                ts_start = prev_ts if prev_value is not None else "-"
                delta = _round(val - prev_value) if (_is_num(val) and prev_value is not None) else "-"
                if _is_num(val):
                    prev_value = val
                    prev_ts = ts_str
            else:
                val, ts_str = "-", "-"
                v_start, ts_start, delta = "-", "-", "-"

            days.append({
                "req_date": cur_date.isoformat(),
                "value_start": v_start,
                "timestamp_start": ts_start,
                "value_end": val,
                "timestamp_end": ts_str,
                "delta": delta,
            })
            cur_date += timedelta(days=1)

        item: dict = {
            "serial_number": counter["serial_number"],
            "name": counter["name"],
            "state": counter["state"],
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "value_start": period_start["value"],
            "timestamp_start": period_start["timestamp"],
            "value_end": period_end["value"],
            "timestamp_end": period_end["timestamp"],
            "delta": _delta(period_start, period_end),
            "b_data": _fetch_b_data(serial, date_to),
            "days": days,
        }
        if capped:
            item["note"] = f"Период ограничен до {limit} суток"

        if sync:
            try:
                item["sheets_sync"] = _push_resource_daily(counter, days)
            except Exception as exc:
                logger.error("resource daily sheets sync: %s", exc)
                item["sheets_sync"] = {"error": str(exc)}

        results.append(item)

    return jsonify(results)


@bp.route("/monthly")
def monthly():
    """
    GET /api/resource/monthly?serial=<SN>[,<SN2>,...][&year=YYYY][&sync=y]

    Ежемесячный отчёт за год для одного или нескольких счётчиков.
    Месяцы позже текущего возвращают "-".
    Возвращает массив результатов.
    """
    serials = _parse_serials(request.args.get("serial", ""))
    if not serials:
        return jsonify({"error": "Параметр 'serial' обязателен"}), 400

    try:
        year = int(request.args.get("year", date.today().year))
    except (ValueError, TypeError):
        return jsonify({"error": "Параметр 'year' должен быть числом"}), 400

    sync = request.args.get("sync", "n").lower() == "y"
    today = date.today()
    year_start = date(year, 1, 1)
    effective_year_end = min(date(year, 12, 31), today)
    _empty = {
        "value_start": "-", "timestamp_start": "-",
        "value_end": "-",   "timestamp_end": "-",
        "delta": "-",
    }

    results = []
    for serial in serials:
        counter = _fetch_counter(serial)
        if counter is None:
            results.append({"serial_number": serial.zfill(8), "error": "не найден"})
            continue

        cid = counter["counter_id"]

        if year_start > today:
            year_period_start = _fmt_row(None)
            year_period_end = _fmt_row(None)
        else:
            ys_row = _last_before(cid, year_start)
            if ys_row is None:
                ys_row = _first_in_period(cid, year_start, effective_year_end)
            year_period_start = _fmt_row(ys_row)
            ye_row = _last_in_period(cid, year_start, effective_year_end)
            year_period_end = _fmt_row(ye_row) if ye_row is not None else year_period_start.copy()

        months = []
        for m in range(1, 13):
            m_start = date(year, m, 1)
            if m_start > today:
                months.append({"req_month": m, **_empty})
                continue

            m_end = min(date(year, m, calendar.monthrange(year, m)[1]), today)
            start_row = _last_before(cid, m_start)
            if start_row is None:
                start_row = _first_in_period(cid, m_start, m_end)
            start = _fmt_row(start_row)
            end_row = _last_in_period(cid, m_start, m_end)
            end = _fmt_row(end_row) if end_row is not None else start.copy()
            months.append({
                "req_month": m,
                "value_start": start["value"],
                "timestamp_start": start["timestamp"],
                "value_end": end["value"],
                "timestamp_end": end["timestamp"],
                "delta": _delta(start, end),
            })

        item: dict = {
            "serial_number": counter["serial_number"],
            "name": counter["name"],
            "state": counter["state"],
            "req_year": year,
            "value_start": year_period_start["value"],
            "timestamp_start": year_period_start["timestamp"],
            "value_end": year_period_end["value"],
            "timestamp_end": year_period_end["timestamp"],
            "delta": _delta(year_period_start, year_period_end),
            "b_data": _fetch_b_data(serial, effective_year_end),
            "months": months,
        }

        if sync:
            try:
                item["sheets_sync"] = _push_resource_monthly(counter, year, months)
            except Exception as exc:
                logger.error("resource monthly sheets sync: %s", exc)
                item["sheets_sync"] = {"error": str(exc)}

        results.append(item)

    return jsonify(results)


@bp.route("/consolidate", methods=["POST"])
def consolidate():
    """
    POST /api/resource/consolidate

    JSON body (один из двух параметров):
      {"cutoff_date": "YYYY-MM-DD"}  — консолидировать все месяцы строго до указанного
      {"keep_months": N}             — оставить последние N месяцев нетронутыми

    Опционально:
      {"dry_run": true}              — только подсчёт, без изменений
      {"remove_empty_rows": true}    — удалять пустые строки (по умолчанию false)
    """
    from services.consolidation import analyze_resource, consolidate_resource

    body = request.get_json(silent=True) or {}
    today = date.today()
    dry_run = bool(body.get("dry_run", False))
    remove_empty_rows = bool(body.get("remove_empty_rows", False))

    if "cutoff_date" in body:
        try:
            raw = date.fromisoformat(body["cutoff_date"])
            cutoff = date(raw.year, raw.month, 1)
        except (ValueError, TypeError):
            return jsonify({"error": "Неверный формат cutoff_date (YYYY-MM-DD)"}), 400
    elif "keep_months" in body:
        try:
            keep = int(body["keep_months"])
            if keep < 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "keep_months должен быть неотрицательным целым числом"}), 400
        total = today.year * 12 + (today.month - 1)
        cutoff_total = max(12, total - keep + 1)
        cutoff = date(cutoff_total // 12, cutoff_total % 12 + 1, 1)
    else:
        return jsonify({"error": "Требуется 'cutoff_date' или 'keep_months'"}), 400

    spreadsheet_id = current_app.config.get("GOOGLE_SHEETS_RESOURCE_ID", "")
    if not spreadsheet_id:
        return jsonify({"error": "GOOGLE_SHEETS_RESOURCE_ID не настроен"}), 503

    sheet_name = current_app.config.get("GOOGLE_SHEETS_RESOURCE_SHEET", "resource")

    try:
        from services.sheets import get_sheets_service
        svc = get_sheets_service()
        from services.consolidation import RESOURCE_SCHEMA
        if dry_run:
            lines = analyze_resource(svc, spreadsheet_id, sheet_name, cutoff, RESOURCE_SCHEMA, remove_empty_rows=remove_empty_rows)
            return jsonify({"dry_run": True, "cutoff": cutoff.isoformat(), "preview": lines})
        result = consolidate_resource(svc, spreadsheet_id, sheet_name, cutoff, RESOURCE_SCHEMA, remove_empty_rows=remove_empty_rows)
        result["cutoff"] = cutoff.isoformat()
        return jsonify(result)
    except Exception as exc:
        logger.error("resource consolidate: %s", exc)
        return jsonify({"error": str(exc)}), 500
