#!/usr/bin/env bash
#
# cron_akron_daily.sh — посуточный отчёт АКРОН + ЭХО-Р-02 (БД Ресурс).
#
# Два счётчика выводятся бок о бок в отдельный лист Google Sheets
# (GOOGLE_SHEETS_AKRON_*). Окно — последние 5 дней, с синхронизацией.
#
# Эндпоинт: GET /api/akron/daily?date_from=...&date_to=...&sync=y
# Дедуп по дате — повторные запуски идемпотентны (существующие сутки
# обновляются, новые вставляются сверху).
#
# Серийники по умолчанию (14331 / 9899) зашиты в эндпоинте; при необходимости
# переопределить — добавить параметры akron=<SN> / ehor=<SN> в api_get ниже.
#
# Пример crontab (ежедневно в 06:20):
#   20 6 * * * /path/to/CounterDash2/scripts/cron_akron_daily.sh

source "$(dirname "$0")/cron_common.sh"

init_log "akron_daily"
load_env
init_base_url

# Окно: 5 календарных дней (сегодня и 4 предыдущих).
DATE_TO="$(date '+%F')"
DATE_FROM="$(date -d '4 days ago' '+%F')"

log "Старт: посуточный отчёт АКРОН+ЭХО-Р ${DATE_FROM}..${DATE_TO}"

rc=0
api_get "/api/akron/daily" \
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
