#!/usr/bin/env bash
#
# cron_resource_daily.sh — Задача 1 (БД Ресурс).
#
# Посуточный отчёт за последние 5 дней (сегодня и 4 предыдущих, включительно)
# для фиксированного списка счётчиков, с синхронизацией в Google Sheets.
#
# Эндпоинт: GET /api/resource/daily?serial=...&date_from=...&date_to=...&sync=y
# Upsert по ключу (Год + Дата + Номер счётчика) — повторные запуски идемпотентны.
#
# Пример crontab (ежедневно в 06:10):
#   10 6 * * * /path/to/CounterDash2/scripts/cron_resource_daily.sh

source "$(dirname "$0")/cron_common.sh"

init_log "resource_daily"
load_env
init_base_url

# Счётчики БД Ресурс (19 шт.)
SERIALS="3763551,3796614,3797548,3828343,3837297,3837298,3837576,3837585,3837587,3837916,3837937,3837946,3856973,16363836,38625109,38625151,39080364,39083301,39099256,39107582"

# Окно: 4 календарных дня(до сегодняшнего).
DATE_TO="$(date -d '1 days ago' '+%F')"
DATE_FROM="$(date -d '4 days ago' '+%F')"

log "Старт: посуточный отчёт ${DATE_FROM}..${DATE_TO}, счётчиков=$(printf '%s' "${SERIALS}" | tr ',' '\n' | grep -c .)"

rc=0
api_get "/api/resource/daily" \
    "serial=${SERIALS}" \
    "date_from=${DATE_FROM}" \
    "date_to=${DATE_TO}" \
    "sync=y" || rc=$?

if [ "${rc}" -ne 0 ]; then
    log "ЗАВЕРШЕНО С ОШИБКОЙ (rc=${rc})"
    exit 1
fi

check_body_errors || rc=$?
log "Готово (rc=${rc})"
exit "${rc}"
