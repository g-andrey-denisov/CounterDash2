#!/usr/bin/env bash
#
# cron_akron_prev_month.sh — посуточный отчёт АКРОН + ЭХО-Р-02 за ПРОШЛЫЙ месяц.
#
# Два счётчика выводятся бок о бок в отдельный лист Google Sheets
# (GOOGLE_SHEETS_AKRON_*). Окно — весь предыдущий календарный месяц
# (с 1-го по последнее число), с синхронизацией.
#
# Эндпоинт: GET /api/v1/akron/daily?date_from=...&date_to=...&sync=y
# Дедуп по дате — повторные запуски идемпотентны (существующие сутки
# обновляются, новые вставляются сверху). Строки с датой < 01.11.2024
# не трогаются (заморожённая история).
#
# Серийники по умолчанию (14331 / 9899) зашиты в эндпоинте; при необходимости
# переопределить — добавить параметры akron=<SN> / ehor=<SN> в api_get ниже.
#
# Пример crontab (1-го числа каждого месяца в 05:30):
#   30 5 1 * * /path/to/CounterDash2/scripts/cron_akron_prev_month.sh

source "$(dirname "$0")/cron_common.sh"

init_log "akron_prev_month"
load_env
init_base_url

# Прошлый календарный месяц: с 1-го по последнее число.
FIRST_OF_THIS="$(date '+%Y-%m-01')"
DATE_TO="$(date -d "${FIRST_OF_THIS} -1 day" '+%F')"   # последний день прошлого месяца
DATE_FROM="$(date -d "${DATE_TO}" '+%Y-%m-01')"        # первый день прошлого месяца

log "Старт: посуточный отчёт АКРОН+ЭХО-Р за прошлый месяц ${DATE_FROM}..${DATE_TO}"

rc=0
api_get "/api/v1/akron/daily" \
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
