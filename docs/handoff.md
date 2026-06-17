# CounterDash2 — Handoff (полное описание проекта)

> Документ для быстрого ввода в курс дела (в т.ч. новой сессии Claude Code), чтобы
> не тратить токены на повторное исследование кода. Актуально на **2026-06-17**.
> Связанные авто-документы (подгружаются вместе с CLAUDE.md): `docs/resource_db.md`
> (схема MariaDB), `docs/rmon4dev.md` (схема MS SQL). Бизнес-правила — в авто-памяти
> `…/memory/MEMORY.md`.

---

## 1. Что это

Модульное Flask-приложение — единая точка получения показаний счётчиков ресурсов из
двух разнородных БД и выгрузки посуточных/помесячных отчётов в Google Sheets.
Разработка — Windows (`.venv`), прод — Debian 12 / HestiaCP (пользовательский systemd +
Gunicorn). Язык кода/комментариев/доков — русский. Python 3.11+, строго PEP 8.

**Источники данных:**
- **БД «Ресурс»** — MariaDB (PyMySQL). Счётчики электро/вода/тепло. → отчёты `/api/v1/resource` и `/api/v1/akron`.
- **БД «RMon4Dev» (МОЭСК)** — MS SQL Server (pyodbc/FreeTDS). Электросчётчики МОЭСК. → `/api/v1/moesk`.

**Приёмник:** Google Sheets (service account). Четыре независимые таблицы (Ресурс, МОЭСК, АКРОН, Инженерия).

---

## 2. Структура

```
app.py                 create_app() — фабрика, регистрирует 5 blueprint'ов
config.py              Config из .env (python-dotenv)
consolidate.py         CLI-обёртка над движком консолидации (без HTTP)
requirements.txt
.env / .env.example    секреты (НЕ в git) / шаблон
credentials/google_sa.json   ключ Google SA (НЕ в git)

api/
  resource.py     /api/v1/resource    — MariaDB: reading, period, daily, monthly, consolidate
  moesk.py        /api/v1/moesk       — MS SQL: devices, reading, period, daily, monthly
  akron.py        /api/v1/akron       — MariaDB: daily (АКРОН + ЭХО-Р-02, отдельный лист)
  backup.py       /api/v1/backup      — бэкап/восстановление листов Google (list/create/restore/cleanup)
  engineering.py  /api/v1/engineering — MariaDB: daily → таблица «Инженерия» (листы по месяцам)

services/
  db_mariadb.py   get_db() через flask.g, PyMySQL DictCursor, autocommit, utf8mb4
  db_mssql.py     get_db() через flask.g, pyodbc, autocommit
  sheets.py       SheetsService — весь обмен с Google Sheets API
  sheets_backup.py  JSON-бэкапы листов на диск (backups/sheets), ретенция по дням
  consolidation.py  движок свёртки суточных строк → помесячные (SheetSchema)
  logging_setup.py  TimedRotatingFileHandler (logs/app.log, ротация в полночь)

scripts/             bash-обёртки для cron (дёргают локальный HTTP-API)
deploy/              systemd-юнит counterdash2.service (gunicorn --reload) + README
docs/                resource_db.md, rmon4dev.md (схемы), handoff.md (этот файл)
logs/, backups/      создаются автоматически (в git игнорируются)
```

Все 5 blueprint'ов регистрируются в `app.py:create_app()`. БД-соединения живут в `flask.g`
и закрываются в `teardown_appcontext`.

---

## 3. Эндпоинты (кратко)

Все ответы — JSON UTF-8 (`app.json.ensure_ascii=False`), значения округлены до 1 знака,
нет данных → `"-"`. Серийники в запросе: ведущие нули игнорируются.

### `/api/v1/resource` (MariaDB)
| Метод/путь | Назначение | sync |
|---|---|---|
| GET `/reading?serial=&date=` | показание на дату (последнее ≤ date) | — |
| GET `/period?serial=&date_from=&date_to=` | потребление за период | да |
| GET `/daily?serial=&date_from=&date_to=` | посуточно, все дни периода | да |
| GET `/monthly?serial=&year=` | помесячно за год | да |
| POST `/consolidate` | свёртка суточных строк в помесячные | — |

- `serial` принимает список через запятую → массив результатов.
- Серийник в ответе **zfill(8)**. Лист один (`GOOGLE_SHEETS_RESOURCE_SHEET`), 17 столбцов.
- Upsert-ключи: месяц `[Год,Месяц_ИД,Счётчик]`, сутки/период `[Год,Дата,Счётчик]`.
- Тариф/Ктр/группа берутся из `b_count/b_group/b_tarif` (только ~20 счётчиков; иначе дефолт Ктр=1, тариф=7.07).

### `/api/v1/moesk` (MS SQL Server)
| Метод/путь | Назначение |
|---|---|
| GET `/devices?serial=` | метаданные счётчика(ов); без serial — все |
| GET `/reading?serial=&date=` | последнее A+ показание |
| GET `/period?serial=&date_from=&date_to=` | потребление (A+), `total_consumption_ktt = delta*КТТ` |
| GET `/daily?...` | посуточно (A+) |
| GET `/monthly?serial=&...&sync=y` | помесячно (MA+ архивные снимки на 1-е число); код пишет 21 столбец (A–U) |

- Теги: `A+0..3` — текущие показания; `MA+0..3` — архивные на 1-е число месяца.
- `/monthly` принимает гибкий диапазон (приоритет): `year_from`/`month_from`[+`year_to`/`month_to`] → `date_from`/`date_to` → `months=N`[+`exclude_current`] → `year`[+`year_mode=ytd|ytd_excl|full`] → текущий месяц. Ограничен `MONTHLY_REPORT_LIMIT` (дефолт 24).
- Начало месяца M = первый MA+ снимок месяца M; конец = первый MA+ снимок M+1 (для текущего незавершённого месяца — последнее A+). **Окно выборки MA+ расширено на месяц вперёд** (`_monthly_one`), иначе у последнего завершённого месяца диапазона конец/расход (Q/R/S) выходили пустыми — см. раздел 9.
- **Тариф (T) в БД RMon4Dev отсутствует**, вносится в лист вручную. При синке `_last_recorded_tariffs` переносит последний уже записанный тариф (по серийнику, макс. по (Год, Месяц_ИД); общий запасной): обновляемая строка сохраняет свой T, новая наследует последний известный. Код **не обнуляет** T.
- При `sync=y` весь лист пересортировывается по `(Год, Месяц_ИД, №)` и пересчитывается формула столбца U (`=S*T`). **Лист содержит сводные таблицы справа (V–AE и далее)**; `_resort_moesk_sheet` делает `clear_and_write` всего листа — сводные блоки на ранних (2020) строках сортируются стабильно, но `clear_and_write` читает `FORMATTED_VALUE`, превращая формулы сводных столбцов в значения (пред-существующее поведение).
- Dedup МОЭСК: `[Дата, Номер счётчика, Точка учёта]` (индексы 8,11,10).

### `/api/v1/akron` (MariaDB)
GET `/daily?akron=14331&ehor=9899&date_from=&date_to=&sync=y` — посуточный отчёт двух
счётчиков **бок о бок** в отдельный лист `АКРОН-01`. Подробности — раздел 4.

### `/api/v1/engineering` (MariaDB)
| Метод/путь | Назначение |
|---|---|
| GET `/daily?year=YYYY&month=M[&sync=y]` | посуточный расход счётчиков → лист «{Месяц} {Год}» |

- Таблица: `GOOGLE_SHEETS_ENGINEERING_ID` (1 таблица, листы по месяцам).
- Лист ищется строго как `«{Месяц} {Год}»` (рус.); dev-префикс «D- » убран.
- Если лист не найден — создаётся дублированием последнего листа в таблице:
  - очищаются ячейки данных (столбцы K-Q, V-AB, AG-AM, AR-AX, BC-BI, строки 6-80);
  - обновляется D1 (имя месяца);
  - обновляется строка 2 (даты по ISO-неделям, формат `пн|1 .06`).
- Показатели: из столбца B парсится серийный номер (`\d{6,}`); если найден — пишется
  `SUM(ConsumptionDelta) × КТР` (b_count) за сутки, `round(x, 2)`. Нет данных → 0.
  Дни позже вчера — пустая строка `""`.
- КТР: из `b_count` по серийнику; если счётчик не в `b_count` — КТР=1.
- Недели: 5 групп по 7 дней. Неделя 1 = ISO-неделя, содержащая 1-е число месяца (K-Q).
- Один batch-запрос к Sheets API на запись всех значений.

### `/api/v1/backup`
POST `/create`, `/restore`, `/cleanup`; GET `/list`. JSON-бэкапы листов в `backups/sheets/`.

---

## 4. Отчёт АКРОН / ЭХО-Р-02 ([api/akron.py](../api/akron.py))

Самостоятельный модуль (не зависит от `resource.py`). Два счётчика БД Ресурс:
- **АКРОН** — серийник `14331` (в БД `counter.Name = "(СВ.сч.41.05) 00014331-21 [АКРОН, Очистные]"`, `Obj_Id_Counter=1305`).
- **ЭХО-Р-02** — серийник `9899` (`counter.Name = "ЭХО-Р-02 9899 временно отключен"`, `Obj_Id_Counter=1181`).

Серийники — query-параметры `akron`/`ehor`, дефолты зашиты константами `_DEFAULT_AKRON/_DEFAULT_EHOR`.

**Отличия от `/api/v1/resource`:**
- Серийники **без ведущих нулей** (`_strip_serial`, не zfill).
- **Имена переопределяются** константами: `_AKRON_NAME="АКРОН-01"`, `_EHOR_NAME="ЭХО-Р-02"`
  (DB-имена не подходят пользователю). Передаются как `name_override` в `_counter_daily`.
- Отдельная таблица: `GOOGLE_SHEETS_AKRON_ID` / `GOOGLE_SHEETS_AKRON_SHEET` (лист `АКРОН-01`).
- 16 столбцов: 8 (АКРОН) + 8 (ЭХО-Р-02) бок о бок. Заголовок — `_AKRON_SHEET_HEADER`.
  Столбцы: Год, Месяц, Месяц_ИД, Дата показаний, Названия счетчика, Номер счетчика, Показания, За сутки — дважды.
- Сортировка **обратная** (новые сутки сверху). Все дни периода присутствуют (нет данных → пусто).
- Выгрузка: новые сутки **вставляются сверху** (со 2-й строки, сдвиг вниз — `SheetsService.insert_rows_top`),
  существующие — обновляются на месте. Дедуп **по дате** (канонический ключ `_date_key` → `YYYY-MM-DD`,
  формато-независимый: понимает и `DD.MM.YYYY`, и `YYYY-MM-DD`).
- «Дата показаний» — **настоящая дата-время** (не текст): значение пишется в ISO
  `YYYY-MM-DD HH:MM:SS`, столбцам D и L задаётся числовой формат `dd.mm.yyyy hh:mm:ss`
  (`SheetsService.set_columns_datetime_format`) → отображение `DD.MM.YYYY HH:MM:SS`, сортируется как дата.
  Время = `consumption.UpdateTime` последнего показания за день; для дня без данных — дата 00:00:00.
- **Заморозка истории** (правило пользователя): строки с датой `< _PROTECT_BEFORE`
  (`date(2024,11,1)`, т.е. ≤ 31.10.2024) **не трогаются** — не вставляются и не обновляются.
  Фильтр в `_push_sheet`; счётчик отброшенных строк → поле `protected` в ответе. Только для этого отчёта.

«Показания» = накопительное показание `consumption.Consumption` на конец суток; «За сутки» = дельта к предыдущему дню.
Ктр/тариф НЕ применяются (в макете их нет).

---

## 5. Слой Google Sheets ([services/sheets.py](../services/sheets.py))

`SheetsService` (singleton через `get_sheets_service()`, ключ из `GOOGLE_SA_KEY_PATH`):
- `upsert_rows(id, sheet, rows, key_cols)` — апсерт по составному ключу (append + update). Базовый для resource.
- `insert_rows_top(id, sheet, rows, after_header=True)` — вставка блока со 2-й строки (`batchUpdate insertDimension`), сдвиг существующих вниз. Используется в АКРОН.
- `set_columns_datetime_format(id, sheet, col_indices, pattern)` — формат DATE_TIME на целые столбцы (со 2-й строки). Используется в АКРОН.
- `ensure_header`, `read_sheet`, `clear_and_write`, `_update_row`, `_append`, `_make_key`, `_sheet_id`.
- `_make_key`: апостроф срезается, чисто-числовые строки нормализуются (Sheets съедает ведущие нули и апостроф при чтении).

**Важно про запись чисел/дат:** всё пишется с `valueInputOption=USER_ENTERED`. Локаль таблиц
русская → десятичный разделитель запятая, при обратном чтении (`FORMATTED_VALUE`) числа
приходят как `"157999,7"`. Поэтому дедуп-ключи строятся по датам/серийникам, не по числам.

---

## 6. Консолидация ([services/consolidation.py](../services/consolidation.py))

Свёртка суточных строк листа в помесячные (для старых периодов, чтобы лист не разрастался).
Универсальный движок на `SheetSchema` (описывает раскладку столбцов: год/месяц/дата/серийник,
какие столбцы суммируются `cols_sum`, какие берутся из последней строки группы `cols_from_last`).
`RESOURCE_SCHEMA` — преднастроена под лист Ресурс. Группировка по `(год, месяц, серийник)`,
консолидируются только периоды **строго до** cutoff; группа из 1 строки с датой = 1-е число
считается уже свёрнутой. Доступно как POST `/api/resource/consolidate` и CLI `consolidate.py`
(`--cutoff-date` | `--keep-months N`, `--dry-run`, `--remove-empty-rows`, `--yes`).

---

## 7. Cron-скрипты ([scripts/](../scripts/))

Bash-обёртки, дёргают локальный HTTP-API (сервис должен быть запущен). Общая обвязка —
`cron_common.sh` (`source`): загрузка `.env`, логи в `logs/cron/<имя>.log`, `api_get/api_post`,
коды возврата 0 (ок) / 1 (HTTP/транспорт) / 2 (в теле есть `"error"`).

| Скрипт | Окно | Эндпоинт |
|---|---|---|
| `cron_resource_daily.sh` | 4 дня до вчерашнего, 20 счётчиков Ресурс | `/api/v1/resource/daily` |
| `cron_akron_daily.sh` | последние 5 дней, АКРОН+ЭХО-Р | `/api/v1/akron/daily` |
| `cron_akron_prev_month.sh` | весь прошлый месяц, АКРОН+ЭХО-Р | `/api/v1/akron/daily` |
| `cron_moesk_monthly.sh` | текущий год, 6 счётчиков МОЭСК | `/api/v1/moesk/monthly` |
| `cron_resource_consolidate.sh` | `keep_months=6` | `/api/v1/resource/consolidate` |
| `cron_engineering_daily.sh` | месяц вчерашнего дня (или `YYYY M` аргументами) | `/api/v1/engineering/daily` |

Списки счётчиков и окна зашиты в скриптах. Даты прошлого месяца считаются через GNU `date -d`.
На Linux исполняемый бит уже выставлен (`100755`); файлы хранятся с LF.

---

## 8. Конфигурация (`.env`, см. `.env.example`)

Ключи: `FLASK_*`, `LOG_LEVEL`, `LOG_MAX_DAYS`, `DAILY_REPORT_LIMIT` (дефолт 62),
`MONTHLY_REPORT_LIMIT` (дефолт 24 — для `/api/moesk/monthly`),
`MARIADB_*`, `MSSQL_*` (на Linux `MSSQL_DRIVER=FreeTDS`), `GOOGLE_SA_KEY_PATH`,
`GOOGLE_SHEETS_{RESOURCE,MOESK,AKRON}_{ID,SHEET}`, `GOOGLE_SHEETS_ENGINEERING_ID`,
`SHEETS_BACKUP_DIR`, `SHEETS_BACKUP_MAX_DAYS`.
Реквизиты подключения к БД — в `docs/resource_db.md` и `docs/rmon4dev.md` (хосты/логины/пароли там).
`.env`, `credentials/`, `CLAUDE.md`, `docs/*_db.md`/`rmon4dev.md`, `logs/`, `backups/` — в `.gitignore`.

---

## 9. Ключевые подводные камни (накоплено за сессии)

- **MariaDB серийники:** `counter.SerialNumber` — varchar с ведущими нулями. Сравнение —
  `CAST(SerialNumber AS UNSIGNED)`. Вывод: resource → zfill(8), akron → lstrip('0').
- **MS SQL legacy-драйвер:** нет `TRY_CAST`/MARS. Курсор обязательно дренировать `fetchall()`
  перед новым `execute()` на том же соединении, иначе `HY000 Connection is busy`. `Value.IdDeviceTag → Tag.Id` (не `DeviceTag.Id`!).
- **Sheets дедуп:** ключи только по датам/строкам, не по числам (локаль-запятая, апостроф,
  ведущие нули теряются при обратном чтении). Для дат — канонизация в `YYYY-MM-DD`.
- **Дата-время в Sheets:** чтобы было настоящей датой (а не текстом) — писать ISO + задавать
  числовой формат столбца. Апостроф форсирует текст (так раньше делал resource).
- **`.env` кодировка:** должна быть UTF-8. Кириллическое имя листа в неправильной кодировке
  ломает запрос к Google (`Unable to parse range`). В консоли Windows кириллица — кракозябры
  (cp1251), на данные не влияет; `chcp 65001` для нормального вывода.
- **Заморозка истории АКРОН:** `date < 2024-11-01` не трогать (см. память `akron-history-freeze`).
- **MOESK monthly — окно MA+:** конец завершённого месяца M = MA+ снимок 1-го числа M+1. Для
  **последнего** месяца диапазона он лежит на верхней границе окна и отрезался условием
  `Datetime < dt_to` → столбцы Q/R/S пустели. Окно расширено на месяц вперёд (`_monthly_one`).
  Проявилось после перехода cron на «два завершённых месяца».
- **MOESK тариф (T):** в БД нет → переносится из листа (`_last_recorded_tariffs`), не обнуляется
  при синке. Заполняется пользователем вручную для первого периода.
- **MOESK лист — многотабличный:** справа от A–U сводные блоки (V–AE и далее). `_resort_moesk_sheet`
  делает `clear_and_write` всего листа; формулы сводных столбцов при этом становятся значениями.
- **Авто-перезагрузка прода:** systemd НЕ перезапускает приложение при правке `.env`/кода
  (`EnvironmentFile`/`load_dotenv()` читаются раз при старте). Решение — `gunicorn --reload`
  + `--reload-extra-file .env` в юните (`deploy/`). Cron-скрипты `.sh` рестарта не требуют.
  **На проде (2026-06-17) по факту запущен `python app.py` (dev-сервер)**, не gunicorn —
  systemd-юнит из `deploy/` ещё не перенесён в `~/.config/systemd/user/`. После изменений
  `.env` требуется `systemctl --user restart counterdash2.service`.
- **Engineering — формат ячеек:** формат `0.##` с русской локалью давал «7,» и «0,» для
  целых чисел. Кастомный формат убран; пишем plain `float` через `round(x, 2)`.
- **`cron_engineering_daily.sh` — `%-m`:** формат `date '%-m'` (месяц без нуля) не работает
  на этом дистрибутиве. Используется `$((10#$(date '+%m')))` — bash-арифметика.

---

## 10. Запуск и проверка

**Локально (Windows):** `.venv\Scripts\python.exe app.py` (или через тестовый клиент без
поднятия сервера — удобно для проверок, БД должны быть доступны по сети):

```python
from app import create_app
app = create_app(); c = app.test_client()
print(c.get('/api/akron/daily?date_from=2026-05-01&date_to=2026-05-31').get_json())
```

Запускать через `.venv\Scripts\python.exe -X utf8 ...` (utf8 — чтобы кириллица не падала в win-консоли).
Прод: Gunicorn (`app:create_app()`) под пользовательским systemd с `--reload` (авто-перезагрузка
при правке кода/`.env`); для MS SQL на Linux — FreeTDS (`unixodbc`, `freetds-*`,
`/etc/odbcinst.ini`). Готовый юнит и инструкция — в `deploy/` (`deploy/README.md`), общий деплой — в `README.md`.

---

## 11. Текущее состояние

- Актуально на **2026-06-17**, ветка `main`, последний коммит `6f7ade3`.
- **Все эндпоинты переименованы** `/api/` → `/api/v1/` (blueprints + cron-скрипты) — `910b866`.
- **Модуль «Инженерия»** (`api/engineering.py`, `6f7ade3`) задеплоен и работает на проде.
  Cron `0 7 * * *` добавлен в crontab. `GOOGLE_SHEETS_ENGINEERING_ID` добавлен в `.env` на сервере.
- **На проде запущен dev-сервер** (`python app.py`), не gunicorn. Systemd-юнит из `deploy/` ещё
  не перенесён. После изменений `.env` нужен `systemctl --user restart counterdash2.service`.
- **`/api/moesk/monthly`:** (1) пустое Q/R/S у последнего завершённого месяца — исправлено
  (`61fb19f`); (2) столбец T (Тариф) переносится из листа, не обнуляется (`2fb970f`).
- Отчёт АКРОН/ЭХО-Р готов и проверен на живой БД и реальном листе `АКРОН-01`.
- Тесты (pytest) в репозитории отсутствуют; проверка — ручная через тестовый клиент.
- Авторизация AD/LDAP, админка, APScheduler, Telegram/Email-уведомления — **ещё не реализованы**.
