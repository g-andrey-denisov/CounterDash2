# Деплой — systemd (пользовательский)

Юнит для прод-окружения (Debian 12 / HestiaCP, пользовательский systemd).
Кладётся в `~/.config/systemd/user/`.

| Файл | Назначение |
|---|---|
| `counterdash2.service` | Основной сервис (Gunicorn + Flask) с авто-перезагрузкой при изменении кода и `.env` |

## Авто-перезагрузка при изменении кода и конфигурации

По умолчанию systemd **не перезагружает** приложение при правке `.env`/исходников:
`EnvironmentFile=` читается лишь при старте, а `config.py` зовёт `load_dotenv()`
один раз при импорте — значения замораживаются в процессе Gunicorn.

Решение — флаги Gunicorn в `ExecStart`:

- **`--reload`** — рекурсивно следит за всеми загруженными Python-модулями
  (`app.py`, `config.py`, `api/*.py`, `services/*.py`) и перезапускает воркеры
  при правке любого из них. Подхватываются и новые исходники после `git pull`.
- **`--reload-extra-file /path/to/CounterDash2/.env`** — добавляет `.env` в
  наблюдение. Так как `config.py` вызывает `load_dotenv(override=True)` при
  импорте, при перезагрузке воркера значения `.env` перечитываются автоматически.

Один механизм покрывает и `.env`, и весь код приложения. Для надёжного слежения
поставьте пакет `inotify` (иначе Gunicorn опрашивает mtime поллингом):

```bash
.venv/bin/pip install inotify
```

> **Cron-скрипты (`scripts/*.sh`)** перезапуска сервиса **не требуют**: они не
> импортируются приложением, cron запускает их отдельными процессами и каждый раз
> читает актуальный файл. Менять `.sh` можно без рестарта — изменения вступают в
> силу со следующего срабатывания cron.

## Установка

1. Замените в `counterdash2.service` все `/path/to/CounterDash2` на реальный путь.

2. Скопируйте юнит:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/counterdash2.service ~/.config/systemd/user/
   ```

3. Включите автозапуск user-сервисов без активной сессии (если ещё не включено):

   ```bash
   loginctl enable-linger "$USER"
   ```

4. Перечитайте конфигурацию и запустите:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now counterdash2.service
   ```

## Проверка авто-перезагрузки

```bash
# Правка .env → воркеры должны перезагрузиться
touch /path/to/CounterDash2/.env
journalctl --user -u counterdash2.service -n 20 --no-pager
# В журнале появится строка вида "Worker reloading: ... modified"
```

То же произойдёт при правке любого `.py` приложения.

## Замечание о проде

`--reload` — встроенный механизм Gunicorn; на низконагруженном внутреннем сервисе
отчётов он работает надёжно. Если нужен полный перезапуск мастер-процесса
(например, после смены `--bind`/`--workers` в самом юните) — это делается вручную:
`systemctl --user restart counterdash2.service`.
