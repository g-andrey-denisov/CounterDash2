#!/usr/bin/env bash
#
# cron_moesk_monthly.sh — Задача 3 (БД МОЭСК).
#
# Помесячный отчёт за текущий год для фиксированного списка счётчиков МОЭСК,
# с синхронизацией в Google Sheets.
#
# Эндпоинт: GET /api/moesk/monthly?serial=...&year=<текущий>&sync=y
# Upsert по ключу (Дата + Номер счётчика + Точка учёта) — повторные запуски
# идемпотентны; данные текущего/прошедших месяцев обновляются по мере поступления.
#
# Пример crontab (ежедневно в 06:40):
#   40 6 * * * /path/to/CounterDash2/scripts/cron_moesk_monthly.sh

source "$(dirname "$0")/cron_common.sh"

init_log "moesk_monthly"
load_env
init_base_url

# Счётчики МОЭСК (6 шт.)
SERIALS="14741821,14741825,14744249,13201464,14744403,14744408"

YEAR="$(date '+%Y')"

log "Старт: помесячный отчёт за ${YEAR}, счётчиков=$(printf '%s' "${SERIALS}" | tr ',' '\n' | grep -c .)"

rc=0
api_get "/api/moesk/monthly" \
    "serial=${SERIALS}" \
    "year=${YEAR}" \
    "sync=y" || rc=$?

if [ "${rc}" -ne 0 ]; then
    log "ЗАВЕРШЕНО С ОШИБКОЙ (rc=${rc})"
    exit 1
fi

check_body_errors || rc=$?
log "Готово (rc=${rc})"
exit "${rc}"
