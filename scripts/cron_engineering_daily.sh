#!/usr/bin/env bash
#
# cron_engineering_daily.sh — посуточные показания в таблицу «Инженерия».
#
# Записывает ConsumptionDelta × КТР из БД Ресурс в Google Sheets
# (GOOGLE_SHEETS_ENGINEERING_ID). Заполняет только дни ≤ вчера.
#
# Эндпоинт: GET /api/v1/engineering/daily?year=YYYY&month=M&sync=y
#
# Использование:
#   cron_engineering_daily.sh [YYYY [M]]
#
#   Без аргументов — год и месяц вчерашнего дня.
#   $1 = год  (напр. 2026)
#   $2 = месяц (напр. 6; ведущий ноль не нужен)
#
# Примеры crontab (ежедневно в 07:00):
#   0 7 * * * /path/to/CounterDash2/scripts/cron_engineering_daily.sh
#   # За конкретный месяц вручную:
#   # /path/to/CounterDash2/scripts/cron_engineering_daily.sh 2026 5

source "$(dirname "$0")/cron_common.sh"

init_log "engineering_daily"
load_env
init_base_url

YEAR="${1:-$(date -d '1 day ago' '+%Y')}"
MONTH="${2:-$((10#$(date -d '1 day ago' '+%m')))}"

log "Старт: инженерия year=${YEAR} month=${MONTH}"

rc=0
api_get "/api/v1/engineering/daily" \
    "year=${YEAR}" \
    "month=${MONTH}" \
    "sync=y" || rc=$?

if [ "${rc}" -ne 0 ]; then
    log "ЗАВЕРШЕНО С ОШИБКОЙ (rc=${rc})"
    exit 1
fi

check_body_errors || rc=$?
log "Готово (rc=${rc})"
exit "${rc}"
