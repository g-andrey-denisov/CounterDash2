#!/usr/bin/env bash
#
# cron_common.sh — общие функции для cron-скриптов CounterDash2.
#
# Подключается через `source` в начале каждого скрипта:
#     source "$(dirname "$0")/cron_common.sh"
#
# Предоставляет:
#   - PROJECT_ROOT      — корень проекта (родитель каталога scripts/)
#   - BASE_URL          — http://$FLASK_HOST:$FLASK_PORT (из .env)
#   - load_env          — загрузка переменных из .env
#   - log               — запись в лог с меткой времени (stdout + файл)
#   - api_get / api_post — HTTP-запросы с проверкой кода ответа
#   - check_body_errors — мягкая проверка тела ответа на "error"
#
# Коды возврата скриптов:
#   0 — успех
#   1 — ошибка транспорта или HTTP-код не 2xx
#   2 — HTTP 2xx, но в теле ответа есть "error" (частичный сбой, напр.
#       счётчик не найден или сбой синхронизации с Google Sheets)

set -euo pipefail

# --- Пути -------------------------------------------------------------------
# scripts/ лежит в корне проекта; PROJECT_ROOT — на уровень выше этого файла.
COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${COMMON_DIR}/.." && pwd)"

ENV_FILE="${PROJECT_ROOT}/.env"
LOG_DIR="${PROJECT_ROOT}/logs/cron"
CURL_TIMEOUT="${CURL_TIMEOUT:-120}"

# Имя скрипта-вызывателя (для имени лог-файла). Заполняется в init_log.
SCRIPT_NAME="${SCRIPT_NAME:-cron}"

# --- Загрузка .env ----------------------------------------------------------
# Простой парсер KEY=VALUE: пропускает пустые строки и комментарии (#...),
# срезает CR (на случай Windows-окончаний строк), сохраняет значения
# с пробелами (напр. MSSQL_DRIVER=SQL Server) без обрезки.
load_env() {
    [ -f "${ENV_FILE}" ] || { echo "WARN: .env не найден: ${ENV_FILE}" >&2; return 0; }
    local line key val
    while IFS= read -r line || [ -n "${line}" ]; do
        line="${line%$'\r'}"
        case "${line}" in
            ''|\#*) continue ;;
        esac
        [[ "${line}" == *=* ]] || continue
        key="${line%%=*}"
        val="${line#*=}"
        # срезать пробелы вокруг ключа (значение не трогаем)
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        export "${key}=${val}"
    done < "${ENV_FILE}"
}

# --- Логирование ------------------------------------------------------------
init_log() {
    SCRIPT_NAME="$1"
    mkdir -p "${LOG_DIR}"
    LOG_FILE="${LOG_DIR}/${SCRIPT_NAME}.log"
}

log() {
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    local msg="${ts} [${SCRIPT_NAME}] $*"
    echo "${msg}"
    if [ -n "${LOG_FILE:-}" ]; then
        echo "${msg}" >> "${LOG_FILE}"
    fi
}

# --- Базовый URL ------------------------------------------------------------
init_base_url() {
    local host="${FLASK_HOST:-127.0.0.1}"
    local port="${FLASK_PORT:-5000}"
    BASE_URL="http://${host}:${port}"
}

# --- HTTP-запросы -----------------------------------------------------------
# Печатает тело ответа в RESP_BODY, код — в RESP_CODE.
# Возвращает 1 при сбое транспорта или коде не 2xx.

# api_get <path> [key=value ...]
api_get() {
    local path="$1"; shift
    local url="${BASE_URL}${path}"
    local args=() p
    for p in "$@"; do
        args+=(--data-urlencode "${p}")
    done
    log "GET ${url} params=[$*]"
    local resp
    if ! resp="$(curl -sS -m "${CURL_TIMEOUT}" -G -w $'\n%{http_code}' "${args[@]}" "${url}")"; then
        log "ERROR: curl GET не выполнен (${url})"
        return 1
    fi
    RESP_CODE="${resp##*$'\n'}"
    RESP_BODY="${resp%$'\n'*}"
    log "HTTP ${RESP_CODE}"
    log "RESP ${RESP_BODY}"
    case "${RESP_CODE}" in
        2*) return 0 ;;
        *)  log "ERROR: код ответа ${RESP_CODE}"; return 1 ;;
    esac
}

# api_post <path> <json-body>
api_post() {
    local path="$1" body="$2"
    local url="${BASE_URL}${path}"
    log "POST ${url} body=${body}"
    local resp
    if ! resp="$(curl -sS -m "${CURL_TIMEOUT}" -X POST \
            -H 'Content-Type: application/json' \
            -d "${body}" -w $'\n%{http_code}' "${url}")"; then
        log "ERROR: curl POST не выполнен (${url})"
        return 1
    fi
    RESP_CODE="${resp##*$'\n'}"
    RESP_BODY="${resp%$'\n'*}"
    log "HTTP ${RESP_CODE}"
    log "RESP ${RESP_BODY}"
    case "${RESP_CODE}" in
        2*) return 0 ;;
        *)  log "ERROR: код ответа ${RESP_CODE}"; return 1 ;;
    esac
}

# Мягкая проверка тела на наличие "error" (частичный сбой при HTTP 2xx).
# Возвращает 2, если найдено.
check_body_errors() {
    if printf '%s' "${RESP_BODY:-}" | grep -q '"error"'; then
        log "WARN: в ответе присутствует \"error\" — частичный сбой"
        return 2
    fi
    return 0
}
