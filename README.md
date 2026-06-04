# CounterDash2

Модульное веб-приложение для получения показаний счётчиков ресурсов.
Разрабатывается на Windows, деплоится на Debian 12 (HestiaCP).

---

## Стек

| Слой | Технология |
|---|---|
| Бэкенд | Python 3.11+, Flask 3 (Blueprint) |
| БД | MariaDB 10 (PyMySQL), MS SQL Server (pyodbc) |
| Интеграции | Google Sheets API (google-api-python-client) |
| Конфиг | python-dotenv (`.env`) |
| Логирование | `logging.handlers.TimedRotatingFileHandler` |
| Прод | Gunicorn + systemd |

---

## Структура проекта

```
CounterDash2/
├── app.py                    # Фабрика Flask (create_app)
├── config.py                 # Config-класс из .env
├── consolidate.py            # CLI-скрипт консолидации Google Sheets
├── requirements.txt
├── .env                      # Секреты и настройки (не в git)
│
├── credentials/              # Ключи сервисных аккаунтов (не в git)
│   └── google_sa.json        # Google Service Account JSON
│
├── api/
│   ├── resource.py           # Blueprint /api/resource — счётчики (MariaDB)
│   ├── moesk.py              # Blueprint /api/moesk   — МОЭСК (MS SQL Server)
│   └── akron.py              # Blueprint /api/akron   — АКРОН + ЭХО-Р-02 (MariaDB)
│
├── services/
│   ├── db_mariadb.py         # get_db() — соединение MariaDB через Flask g
│   ├── db_mssql.py           # get_db() — соединение MS SQL через Flask g (pyodbc)
│   ├── logging_setup.py      # Ротируемое логирование (init_logging)
│   ├── sheets.py             # Google Sheets — SheetsService, get_sheets_service()
│   └── consolidation.py      # Логика консолидации посуточных записей в помесячные
│
└── logs/                     # Лог-файлы (создаётся автоматически)
    └── app.log
```

---

## Быстрый старт (Windows / разработка)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# Скопируй .env.example → .env и заполни
# Положи google_sa.json в credentials/
python app.py
```

---

## Конфигурация (.env)

Шаблон со всеми ключами — в `.env.example`. Скопируй его в `.env` и заполни значениями:

```dotenv
FLASK_SECRET_KEY=change-me-in-production
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
FLASK_DEBUG=true

LOG_LEVEL=INFO          # DEBUG | INFO | WARNING | ERROR
LOG_MAX_DAYS=30         # Сколько дней хранить лог-файлы

DAILY_REPORT_LIMIT=62   # Макс. дней в посуточном отчёте
MONTHLY_REPORT_LIMIT=24 # Макс. месяцев в помесячном отчёте (МОЭСК)

# MariaDB (БД Ресурс)
MARIADB_HOST=192.168.1.101
MARIADB_PORT=3306
MARIADB_DB=
MARIADB_USER=
MARIADB_PASSWORD=

# MS SQL Server (RMon4Dev — МОЭСК)
MSSQL_DRIVER=SQL Server         # Windows: "SQL Server" / Linux: "FreeTDS"
MSSQL_HOST=192.168.11.102
MSSQL_PORT=1433
MSSQL_DB=RMon4Dev
MSSQL_USER=
MSSQL_PASSWORD=

# Google Sheets — путь к JSON-ключу сервисного аккаунта
GOOGLE_SA_KEY_PATH=credentials/google_sa.json

# Google Sheets — Spreadsheet IDs и имена листов
GOOGLE_SHEETS_RESOURCE_ID=          # ID таблицы Google для БД Ресурс
GOOGLE_SHEETS_RESOURCE_SHEET=       # Имя вкладки (все отчёты Ресурс пишутся сюда)
GOOGLE_SHEETS_MOESK_ID=             # ID таблицы Google для МОЭСК
GOOGLE_SHEETS_MOESK_SHEET=moesk     # Имя вкладки для МОЭСК
GOOGLE_SHEETS_AKRON_ID=             # ID отдельной таблицы для АКРОН + ЭХО-Р-02
GOOGLE_SHEETS_AKRON_SHEET=АКРОН     # Имя вкладки для АКРОН + ЭХО-Р-02
```

---

## REST API — `/api/resource`

Все ответы — JSON в UTF-8. Значения показаний округлены до 1 знака.
Если данных нет — поле возвращает строку `"-"`.
Серийный номер в запросе: ведущие нули игнорируются.
Серийный номер в ответе: всегда минимум 8 символов (дополняется нулями слева).

### Умолчания для `date_from` / `date_to` (`/period`, `/daily`)

| Переданы | Результат |
|---|---|
| оба отсутствуют | с 1-го числа текущего месяца по сегодня |
| только `date_from` | `date_to = date_from + DAILY_REPORT_LIMIT дней` |
| только `date_to` | `date_from = date_to − DAILY_REPORT_LIMIT дней` |
| оба указаны | как передано |

### Выгрузка в Google Sheets (`sync=y`)

Параметр `sync=y` доступен в `/period`, `/daily`, `/monthly`.
При указании данные записываются в таблицу Google (upsert по составному ключу).
Все отчёты БД Ресурс пишутся на один лист (`GOOGLE_SHEETS_RESOURCE_SHEET`).

**Структура строки в таблице:**

| Столбец | Описание |
|---|---|
| Год | Год |
| Месяц | Название месяца (русск., строчные) |
| Месяц_ИД | Номер месяца |
| Дата | 1-е число месяца (помесячный) или конкретная дата (посуточный) |
| Номер счётчика | Серийный номер (zfill 8) |
| Название счётчика | Наименование |
| Дельта | Потребление за период |
| Нач.показания | Начальное показание |
| Время нач.показаний | Временная метка начального |
| Кон.показания | Конечное показание |
| Время кон.показаний | Временная метка конечного |
| Ктр | Коэффициент трансформации |
| Тариф | Тариф (берётся на последний день месяца / дату / date_to) |
| Группа_ИД | ID группы тарификации |
| Группа | Название группы |
| Потребление * Ктр | Дельта × Ктр |
| Потребление * Ктр * Тариф | Дельта × Ктр × Тариф |

**Upsert-ключи (защита от дублей):**

| Отчёт | Ключ |
|---|---|
| Месячный | Год + Месяц_ИД + Номер счётчика |
| Суточный | Год + Дата + Номер счётчика |
| За период | Год + Дата(date_from) + Номер счётчика |

---

### GET `/api/resource/reading`

Показание счётчика на указанную дату (последнее до неё включительно).

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | да | — |
| `date` | нет | сегодня |

**Ответ:**
```json
{
  "serial_number": "39804761",
  "name": "(Эл.сч.14.01) 39804761-19 [Щитовая]",
  "state": "Работает",
  "req_date": "2024-06-15",
  "timestamp": "2024-06-15T09:16:28",
  "value": 2422.6,
  "b_data": {
    "ktr": 1.0,
    "tariff": 7.07,
    "group_id": 3,
    "group_name": "Энергопотребление общее"
  }
}
```

---

### GET `/api/resource/period`

Показания за период (начальное, конечное, разница). Тариф — по `date_to`.

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | да | — |
| `date_from` | нет | см. умолчания выше |
| `date_to` | нет | см. умолчания выше |
| `sync` | нет | `n` |

**Ответ:**
```json
{
  "serial_number": "39804761",
  "date_from": "2024-01-01",
  "date_to": "2024-01-31",
  "value_start": 2243.2,
  "timestamp_start": "2023-12-31T23:58:36",
  "value_end": 2282.7,
  "timestamp_end": "2024-01-31T23:58:41",
  "delta": 39.5,
  "b_data": { "ktr": 1.0, "tariff": 7.07, "group_id": 3, "group_name": "..." },
  "sheets_sync": { "appended": 1, "updated": 0 }
}
```

---

### GET `/api/resource/daily`

Посуточный отчёт за период. Ограничен `DAILY_REPORT_LIMIT` (дефолт 62 суток).

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | да | — |
| `date_from` | нет | см. умолчания выше |
| `date_to` | нет | см. умолчания выше |
| `sync` | нет | `n` |

**Ответ:**
```json
{
  "serial_number": "39804761",
  "date_from": "2024-01-01",
  "date_to": "2024-01-07",
  "value_start": 2243.2,
  "value_end": 2251.1,
  "delta": 7.9,
  "b_data": { "ktr": 1.0, "tariff": 7.07, "group_id": 3, "group_name": "..." },
  "days": [
    {
      "req_date": "2024-01-01",
      "value_start": 2243.2,
      "timestamp_start": "2023-12-31T23:58:36",
      "value_end": 2244.3,
      "timestamp_end": "2024-01-01T23:58:01",
      "delta": 1.1
    }
  ],
  "sheets_sync": { "appended": 6, "updated": 1 }
}
```

---

### GET `/api/resource/monthly`

Ежемесячный отчёт за год. Месяцы позже текущего возвращают `"-"`.

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | да | — |
| `year` | нет | текущий год |
| `sync` | нет | `n` |

**Ответ:**
```json
{
  "serial_number": "39804761",
  "req_year": 2024,
  "value_start": 2243.2,
  "value_end": 2663.1,
  "delta": 419.9,
  "b_data": { "ktr": 1.0, "tariff": 7.07, "group_id": 3, "group_name": "..." },
  "months": [
    {
      "req_month": 1,
      "value_start": 2243.2,
      "timestamp_start": "2023-12-31T23:58:36",
      "value_end": 2282.7,
      "timestamp_end": "2024-01-31T23:58:41",
      "delta": 39.5
    }
  ],
  "sheets_sync": { "appended": 0, "updated": 5 }
}
```

---

### POST `/api/resource/consolidate`

Консолидация посуточных строк Google Sheets в помесячные.
Группирует строки по `(год, месяц, счётчик)`, удаляет суточные, оставляет одну помесячную.
Группа с единственной строкой, у которой дата = 1-е число месяца, считается уже консолидированной и не трогается.
После обработки все строки сортируются по `Дата + Номер счётчика`.

**Поля консолидированной строки:**
- `Дата` = 1-е число месяца
- `Нач.показания` / `Время нач.показаний` — из первого дня группы
- `Кон.показания` / `Время кон.показаний` — из последнего дня группы
- `Дельта`, `Потребление * Ктр`, `Потребление * Ктр * Тариф` — суммируются
- `Ктр`, `Тариф`, `Группа` — из последнего дня группы

**Параметры запроса (один из первых двух обязателен):**

| Поле | Тип | Описание |
|---|---|---|
| `cutoff_date` | string | `"YYYY-MM-DD"` — консолидировать все месяцы строго до этой даты |
| `keep_months` | int | Оставить последние N месяцев нетронутыми |
| `dry_run` | bool | `true` — только подсчёт, без изменений |
| `remove_empty_rows` | bool | `true` — удалять строки, в которых все ячейки пусты |

**Примеры запросов:**
```bash
# Консолидировать всё до марта 2025
curl -X POST /api/resource/consolidate \
     -H "Content-Type: application/json" \
     -d '{"cutoff_date": "2025-03-01"}'

# Оставить 3 последних месяца, удалить пустые строки
curl -X POST /api/resource/consolidate \
     -H "Content-Type: application/json" \
     -d '{"keep_months": 3, "remove_empty_rows": true}'

# Предпросмотр без изменений
curl -X POST /api/resource/consolidate \
     -H "Content-Type: application/json" \
     -d '{"keep_months": 3, "dry_run": true}'
```

**Ответ:**
```json
{
  "consolidated": 5,
  "deleted": 45,
  "skipped": 2,
  "empty_removed": 3,
  "cutoff": "2025-03-01"
}
```

---

### Коды ответов `/api/resource`

| Код | Причина |
|---|---|
| `200` | OK |
| `400` | Не передан `serial` / невалидный параметр |
| `404` | Счётчик не найден |
| `503` | Google Sheets не настроен |

---

## CLI — `consolidate.py`

Консолидация посуточных записей листа ресурс в помесячные напрямую (без HTTP-запроса).
Требует настроенного `.env` с `GOOGLE_SHEETS_RESOURCE_ID`.
После обработки все строки сортируются по `Дата + Номер счётчика`.

```bash
# Консолидировать всё до марта 2025
python consolidate.py --cutoff-date 2025-03-01

# Оставить последние 3 месяца, остальное консолидировать
python consolidate.py --keep-months 3

# Предпросмотр без изменений
python consolidate.py --keep-months 3 --dry-run

# Без подтверждения + удалить пустые строки (для cron/автоматизации)
python consolidate.py --keep-months 3 --remove-empty-rows --yes
```

| Аргумент | Описание |
|---|---|
| `--cutoff-date YYYY-MM-DD` | Консолидировать всё до начала указанного месяца |
| `--keep-months N` | Оставить последние N месяцев без изменений |
| `--dry-run` | Показать план без изменений |
| `--remove-empty-rows` | Удалять строки, в которых все ячейки пусты |
| `--yes` / `-y` | Не запрашивать подтверждение |

---

## Консолидация произвольных листов (`SheetSchema`)

`services/consolidation.py` содержит универсальный движок консолидации.
Для листа с нестандартным набором столбцов создаётся `SheetSchema` и передаётся явно.

```python
from services.consolidation import SheetSchema, consolidate_resource
from services.sheets import get_sheets_service

schema = SheetSchema(
    col_year=0,        # индекс столбца «Год»
    col_month_id=1,    # индекс столбца «Месяц_ИД» (число 1-12)
    col_date=2,        # индекс столбца «Дата» (DD.MM.YYYY или YYYY-MM-DD)
    col_serial=3,      # индекс столбца-идентификатора (ключ группировки)
    cols_sum=[4, 7],   # индексы столбцов, которые суммируются по группе
    cols_from_last=[6],# индексы столбцов из последней строки группы (конечные значения)
                       # все остальные столбцы берутся из первой строки группы
    sum_precision={7: 2},  # {индекс: знаков после запятой}; умолчание — 2
)

with app.app_context():
    svc = get_sheets_service()
    result = consolidate_resource(
        svc, "spreadsheet_id", "sheet_name", cutoff_date, schema,
        remove_empty_rows=True,
    )
```

Для листа ресурс используется предопределённая константа `RESOURCE_SCHEMA`
(столбцы 0–16 согласно `_RESOURCE_SHEET_HEADER` в `api/resource.py`).

---

## REST API — `/api/akron`

Посуточный отчёт по двум счётчикам БД Ресурс (MariaDB) — **АКРОН** и **ЭХО-Р-02** —
выгружаемый в отдельный лист Google Sheets «бок о бок» (8 столбцов АКРОН + 8 столбцов ЭХО-Р-02).
Отличия от `/api/resource`:

- серийные номера **без ведущих нулей** в поиске и в выводе;
- отдельная таблица (`GOOGLE_SHEETS_AKRON_*`);
- выгрузка **вставкой сверху** (новые сутки — со 2-й строки, сдвигая данные вниз),
  сортировка обратная (новые сутки сверху);
- дедуп по дате: повторный запуск идемпотентен (существующие сутки обновляются на месте,
  новые вставляются блоком сверху).

### GET `/api/akron/daily`

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `akron` | нет | `14331` (серийник счётчика АКРОН) |
| `ehor` | нет | `9899` (серийник счётчика ЭХО-Р-02) |
| `date_from` | нет | см. умолчания `/api/resource` |
| `date_to` | нет | см. умолчания `/api/resource` |
| `sync` | нет | `n` |

Все дни периода присутствуют (без данных → `"-"`). Период ограничен `DAILY_REPORT_LIMIT`.

**Структура листа (16 столбцов):**

| Столбцы A–H (АКРОН) | Столбцы I–P (ЭХО-Р-02) |
|---|---|
| Год, Месяц, Месяц_ИД, Дата показаний, Названия счетчика, Номер счетчика, Показания, За сутки | те же 8 столбцов с суффиксом `(ЭХО-Р-02)` |

- `Показания` — накопительное показание на конец суток (сырое).
- `За сутки` — дельта к предыдущим суткам.
- Дедуп-ключ — `Дата показаний` (столбец D).

**Ответ:**
```json
{
  "date_from": "2026-05-28",
  "date_to": "2026-06-02",
  "akron": { "serial": "14331", "name": "...", "found": true },
  "ehor": { "serial": "9899", "name": "...", "found": true },
  "days": [
    {
      "req_date": "2026-06-02",
      "akron_value": 76547.0, "akron_delta": 8.0,
      "ehor_value": 158041.5, "ehor_delta": 2.5
    }
  ],
  "sheets_sync": { "appended": 5, "updated": 1 }
}
```

Если счётчик не найден — в ответе поле `error` (cron-скрипт расценит это как частичный сбой, rc=2).

---

## REST API — `/api/moesk`

Счётчики электроэнергии МОЭСК из БД `RMon4Dev` (MS SQL Server).
Все ответы — JSON в UTF-8. Значения округлены до 1 знака. Нет данных → `"-"`.

Два семейства тегов:

| Код тега | Применяется в |
|---|---|
| `A+0 / A+1 / A+2 / A+3` | `/reading`, `/period`, `/daily` — текущие показания |
| `MA+0 / MA+1 / MA+2 / MA+3` | `/monthly` — архивные помесячные снимки (1-е числа) |

**Общие поля метаданных** (присутствуют во всех ответах):

| Поле | Описание |
|---|---|
| `serial_number` | Серийный номер (zfill 8) |
| `name` | Наименование логического устройства |
| `meter_name` | Марка прибора учёта |
| `station` | Название станции / точки учёта |
| `station_address` | Адрес |
| `region` | Объект / организация |
| `filial` | Корпус / здание |
| `eso` | ЭСО |
| `agr_no` | Номер договора |
| `point_name` | Наименование точки учёта |
| `ktt` | КТТ (коэффициент трансформации тока) |

---

### GET `/api/moesk/devices`

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | нет | вернуть все |

---

### GET `/api/moesk/reading`

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | да | — |
| `date` | нет | не ограничено |

---

### GET `/api/moesk/period`

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | да | — |
| `date_from` | нет | сегодня |
| `date_to` | нет | сегодня |

Ответ содержит: `total_start/end/delta`, `total_consumption_ktt` (= delta × ktt).

---

### GET `/api/moesk/daily`

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | да | — |
| `date_from` | нет | сегодня |
| `date_to` | нет | сегодня |

Ограничен `DAILY_REPORT_LIMIT`. Массив `days[]` — по одной записи на каждый опрос внутри периода.

---

### GET `/api/moesk/monthly`

Помесячный отчёт (MA+ архивные снимки). Все месяцы диапазона присутствуют.
Ограничен `MONTHLY_REPORT_LIMIT` месяцами (дефолт 24).

| Параметр | Обязателен | По умолчанию |
|---|---|---|
| `serial` | да | — |
| `sync` | нет | `n` |

**Параметры диапазона (приоритет сверху вниз):**

| Режим | Параметры |
|---|---|
| Диапазон год+месяц | `year_from`, `month_from` [, `year_to`, `month_to`] |
| Диапазон дат | `date_from` [, `date_to`] — обрезается до границ месяцев |
| Последние N месяцев | `months=N` [, `exclude_current=y`] |
| По году | `year=YYYY` [, `year_mode=ytd\|ytd_excl\|full`] |
| По умолчанию | ничего → текущий месяц |

`year_mode`:
- `ytd` (по умолчанию) — с января по текущий включительно
- `ytd_excl` — с января, не включая текущий
- `full` — все 12 месяцев (будущие — пустые)

`start` месяца M = первое MA+ показание месяца M.
`end` месяца M = первое MA+ показание месяца M+1.
Для текущего незавершённого месяца `end` = последнее A+ показание.

При `sync=y` весь лист пересортировывается по `(Год, Месяц_ИД, №)` и пересчитывается формула столбца U.

---

## Деплой

### Windows (разработка)

```bash
python app.py
```

### Linux (Debian 12, пользовательский systemd + Gunicorn)

```bash
pip install gunicorn
```

**MS SQL Server — зависимости (только Linux):**

```bash
sudo apt install unixodbc unixodbc-dev freetds-dev freetds-bin tdsodbc
```

`/etc/odbcinst.ini`:
```ini
[FreeTDS]
Description = FreeTDS Driver
Driver      = /usr/lib/x86_64-linux-gnu/odbc/libtdsodbc.so
Setup       = /usr/lib/x86_64-linux-gnu/odbc/libtdsS.so
FileUsage   = 1
```

В `.env` на Linux: `MSSQL_DRIVER=FreeTDS`

**Systemd-сервис** — готовый юнит в [`deploy/`](deploy/) (см. [`deploy/README.md`](deploy/README.md)).
Кладётся в `~/.config/systemd/user/counterdash2.service`:

```ini
[Unit]
Description=CounterDash2 (Flask + Gunicorn)
After=network.target

[Service]
WorkingDirectory=/path/to/CounterDash2
EnvironmentFile=/path/to/CounterDash2/.env
ExecStart=/path/to/CounterDash2/.venv/bin/gunicorn \
    --workers 2 \
    --bind 127.0.0.1:5000 \
    --reload \
    --reload-extra-file /path/to/CounterDash2/.env \
    "app:create_app()"
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now counterdash2
```

**Авто-перезагрузка.** systemd сам по себе не перезапускает приложение при
изменении `.env`/исходников (`EnvironmentFile` и `load_dotenv()` читаются лишь при
старте). Флаг `--reload` заставляет Gunicorn следить за всеми загруженными
Python-модулями (`app.py`, `config.py`, `api/*`, `services/*`) и перезапускать
воркеры при правке, а `--reload-extra-file .env` добавляет в наблюдение `.env`
(перечитывается через `load_dotenv(override=True)`). Cron-скрипты `scripts/*.sh`
рестарта не требуют — cron читает их заново при каждом запуске. Подробности и
проверка — в [`deploy/README.md`](deploy/README.md).

---

## Cron-скрипты (`scripts/`)

Bash-обёртки для периодических задач. Каждая дёргает локальный HTTP-API
запущенного сервиса (`http://$FLASK_HOST:$FLASK_PORT` из `.env`) — поэтому
**сервис (systemd + Gunicorn) должен быть запущен** в момент срабатывания cron.

| Скрипт | Задача | Эндпоинт |
|---|---|---|
| `cron_resource_daily.sh` | Посуточный отчёт за последние 4 дня до вчерашнего (20 счётчиков Ресурс) + синхронизация Google | `GET /api/resource/daily?...&sync=y` |
| `cron_akron_daily.sh` | Посуточный отчёт АКРОН + ЭХО-Р-02 за последние 5 дней + синхронизация Google (отдельный лист) | `GET /api/akron/daily?...&sync=y` |
| `cron_akron_prev_month.sh` | Посуточный отчёт АКРОН + ЭХО-Р-02 за весь прошлый месяц + синхронизация Google | `GET /api/akron/daily?...&sync=y` |
| `cron_resource_consolidate.sh` | Консолидация листа Ресурс: суточные строки старше 6 месяцев → помесячные (`keep_months=6` = текущий + 5 предыдущих) | `POST /api/resource/consolidate` |
| `cron_moesk_monthly.sh` | Помесячный отчёт за текущий год (6 счётчиков МОЭСК) + синхронизация Google | `GET /api/moesk/monthly?...&sync=y` |
| `cron_common.sh` | Общие функции (загрузка `.env`, логирование, HTTP-хелперы). Подключается через `source`, отдельно не запускается. |

**Логи:** `logs/cron/<имя>.log`. **Коды возврата:** `0` — успех, `1` — сбой
транспорта/HTTP, `2` — HTTP 2xx, но в теле ответа есть `"error"` (частичный
сбой: счётчик не найден или сбой выгрузки в Google).

Списки счётчиков и окна (5 дней / `keep_months=6` / текущий год) зашиты в
скриптах — править там же.

**Установка в пользовательский crontab** (`crontab -e`):

```cron
# Уведомления о сбоях cron на почту
MAILTO=admin@example.com

# Посуточный отчёт Ресурс — ежедневно 06:10
10 6 * * *   /path/to/CounterDash2/scripts/cron_resource_daily.sh

# Посуточный отчёт АКРОН + ЭХО-Р-02 — ежедневно 06:20
20 6 * * *   /path/to/CounterDash2/scripts/cron_akron_daily.sh

# Посуточный отчёт АКРОН + ЭХО-Р-02 за прошлый месяц — 1-го числа 05:30
30 5 1 * *   /path/to/CounterDash2/scripts/cron_akron_prev_month.sh

# Помесячный отчёт МОЭСК — ежедневно 06:40
40 6 * * *   /path/to/CounterDash2/scripts/cron_moesk_monthly.sh

# Консолидация Ресурс — 1-го числа месяца 03:30
30 3 1 * *   /path/to/CounterDash2/scripts/cron_resource_consolidate.sh
```

Скрипты сами читают `.env` и не зависят от рабочего каталога cron — пути
вычисляются относительно их расположения. Перед первым запуском:
`chmod +x scripts/*.sh`.

---

## БД — таблицы

### MariaDB `resource`

**`counter`** — справочник счётчиков:

| Поле | Тип | Описание |
|---|---|---|
| `Obj_Id_Counter` | int PK | ID счётчика |
| `SerialNumber` | varchar(30) | Серийный номер |
| `Name` | varchar(100) | Наименование |
| `State` | varchar(120) | Состояние |

**`consumption`** — показания:

| Поле | Тип | Описание |
|---|---|---|
| `AI_Consumption` | int PK | Авто-ID |
| `Obj_Id_Counter` | int FK | Ссылка на counter |
| `Consumption` | double | Показание (накопительный итог) |
| `UpdateTime` | datetime | Время снятия показания |

**`b_count`** — счётчики с тарификацией:

| Поле | Описание |
|---|---|
| `SerialNumber` | Серийный номер |
| `Ktr` | Коэффициент трансформации |
| `Group_Id` | FK → `b_group.Id` |

**`b_group`** — группы тарификации:

| Поле | Описание |
|---|---|
| `Id` | PK |
| `Name` | Наименование группы |

**`b_tarif`** — история тарифов:

| Поле | Описание |
|---|---|
| `Group_Id` | FK → `b_group.Id` |
| `Cost` | Тариф (руб./кВт·ч) |
| `UpdDate` | Дата ввода тарифа |

---

### MS SQL Server `RMon4Dev`

Иерархия объектов: `Region → Filial → Eso → Station → LogicDevice`

| Таблица | Описание |
|---|---|
| `LogicDevice` | Логические устройства (счётчики) |
| `Station` | Точки учёта |
| `Eso` / `Filial` / `Region` | Организационная иерархия |
| `DeviceTag` | Типы тегов: `serial`, `A+0..3`, `MA+0..3` |
| `Tag` | Привязка тега к устройству |
| `Value` | Показания (`IdDeviceTag` → `Tag.Id`) |
| `LogicDeviceProperty` | Свойства: `kC`, `kV`, `AgrNo`, `MeasuringpointName` |
| `LogDevProp` | Марка прибора учёта (поле `Name`) |

> **Примечание:** ODBC-драйвер «SQL Server» (Windows legacy) не поддерживает `TRY_CAST`.
> Нормализацию серийных номеров выполняем в Python.
