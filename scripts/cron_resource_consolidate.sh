#!/usr/bin/env bash
#
# cron_resource_consolidate.sh — Задача 2 (БД Ресурс).
#
# Консолидация посуточных строк листа Ресурс в помесячные так, чтобы в суточной
# детализации остались только последние месяцы. keep_months=6 = текущий месяц
# + 5 предыдущих остаются нетронутыми («последние 5 месяцев, не считая текущего»);
# всё, что старше — сворачивается в одну помесячную строку на счётчик/месяц.
#
# Эндпоинт: POST /api/v1/resource/consolidate {"keep_months": 6, "remove_empty_rows": true}
#
# Запускать реже суточного отчёта (консолидация необратима для суточных строк,
# но Google Sheets бэкапятся — см. services/sheets_backup.py).
#
# Пример crontab (1-го числа каждого месяца в 03:30):
#   30 3 1 * * /path/to/CounterDash2/scripts/cron_resource_consolidate.sh

source "$(dirname "$0")/cron_common.sh"

init_log "resource_consolidate"
load_env
init_base_url

KEEP_MONTHS=6
BODY="{\"keep_months\": ${KEEP_MONTHS}, \"remove_empty_rows\": true}"

log "Старт: консолидация, keep_months=${KEEP_MONTHS}"

rc=0
api_post "/api/v1/resource/consolidate" "${BODY}" || rc=$?

if [ "${rc}" -ne 0 ]; then
    log "ЗАВЕРШЕНО С ОШИБКОЙ (rc=${rc})"
    exit 1
fi

check_body_errors || rc=$?
log "Готово (rc=${rc})"
exit "${rc}"
