## База данных

### Что есть сейчас

Хранилище одно — SQLite, путь из `REGISTRY_DB_PATH` (`registry/db.py:25`, по умолчанию
`data/registry.db`; в контейнере `/app/data/registry.db`, именованный том `registry-data`
в `docker-compose.yml`). Соединение открывается на операцию контекст-менеджером
`registry.db.connect()` (`registry/db.py:365-383`); `_open()` (`registry/db.py:386-393`)
ставит `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=30000`, `synchronous=NORMAL`.
Параметр `readonly` у `connect()` объявлен, но в теле не используется — режима «только
чтение» нет.

Схема накатывается **автоматически при первом обращении к базе**: `_ensure_schema()`
(`registry/db.py:396-416`) под `threading.Lock` читает `PRAGMA user_version` и прогоняет
недостающие элементы списка `MIGRATIONS` через `conn.executescript(...)`, один общий
`conn.commit()` после цикла. Отдельной CLI-команды миграции нет; обратных скриптов нет;
резервного копирования нет ни в коде, ни в `docker-compose.yml`.

`MIGRATIONS` содержит **шесть** элементов (`registry/db.py:39` и далее `.append`):
v1 — базовая схема (`db.py:42-176`), v2 — `recruiter_rates` (`db.py:184-196`),
v3 — `disk_projects` (`db.py:204-223`), v4 — `position_kb` + переименование ВахтаПро → Градус
(`db.py:232-251`), v5 — `recruiter_rate_rules` / `recruiter_rate_history` + перенос данных +
`DROP TABLE recruiter_rates` (`db.py:270-327`), v6 — КПК → КНК, только DML (`db.py:341-362`).

Итог — 11 обычных таблиц плюс виртуальная FTS5: `requests`, `request_revisions`, `positions`,
`request_positions`, `position_history`, `dictionaries`, `id_counters`, `disk_projects`,
`position_kb`, `recruiter_rate_rules`, `recruiter_rate_history`, `search_index`.
**Нет**: `counterparties`, `counterparty_settings`, `objects`, `cities`, `sources`,
`sync_runs`, `field_status`, `field_conflicts`, `outreach_threads` / `outreach_messages` /
`outreach_questions`, `position_media`, `position_public`, `users`, `roles`.

Отдельная ловушка: DDL колонок таблицы `positions` собирается функцией
`_position_columns_ddl()` (`registry/db.py:31-36`) из списков `models.DATA_FIELDS` (53 поля)
и `models.MANAGER_FIELDS` (8 полей) и подставляется **только в текст миграции v1**
(`db.py:103`). На новой базе поле появляется, на боевой — нет: любое изменение набора полей
обязано идти новой миграцией с `ALTER TABLE`. Полей `ext_title`, `ext_ref`,
`counterparty_id`, `object_id`, `city_id` в `positions` нет.

Окружение позиции живёт текстом: `positions.counterparty` / `counterparty_raw`,
`object_name` / `object_address`, `city` / `city_raw`, `region` / `region_raw`; канонизация —
через `dictionaries` (9 видов, `registry/dictionaries.py:17-37`) и `registry/geo.py`, где
`CITY_ALIASES` (14 записей, `geo.py:18-34`) и `CITY_COORDS` (**62 города**, `geo.py:37-91`)
захардкожены. Функции расчёта расстояния на сервере нет вовсе — радиус считается в браузере
в `templates/navigator.html`, координаты уезжают во фронт через `navigator_api.cities_block`
(`navigator_api.py:667-686`).

Три несведённых понятия «контрагент»: текстовая колонка `positions.counterparty`; карточка
контрагента в `navigator_api.counterparties_block` (`navigator_api.py:555-617`), которая на
самом деле собирается **по источнику**; и `rates.client_key()` (`registry/rates.py:132-139`),
где приоритет отдан `object_name`, а `counterparty` — только запасной вариант.

Известные дефекты хранилища, которые закрываются задачами ниже: `search_index` не чистится
при деактивации (`ingest._deactivate_stale`, `ingest.py:597-618`) и при удалении позиции;
правки менеджера через `queries.update_manager_fields` (`registry/queries.py:241-254`) не
пишутся в `position_history` и автора правки нигде нет; `recruiter_rate_history` растёт без
ограничения — на каждое сохранение сетки `app.py:432-435` сначала зовёт `rates.clear_scope`
(строка `action='delete'` на каждое снятое правило), затем `rates.save_rules` (строка
`action='set'` на каждое сохранённое), а на экран уходит только `history(limit=30)`
(`navigator_api.py:657-663`).

Порядок работ жёсткий: DB-01 и DB-02 идут первыми, потому что все остальные задачи
накатывают новые таблицы и колонки на боевую базу, у которой сегодня нет ни копии, ни
управляемого момента миграции.

---

### DB-01. Явная команда миграции и запрет автоната в проде

**Что нужно сделать.** Разделить в `registry/db.py` «узнать состояние схемы» и «накатить
миграции». Вместо единственного `_ensure_schema()` (`registry/db.py:396-416`) сделать
публичные `schema_state(db_path) -> (current_version, target_version)` и
`apply_migrations(db_path, to=None) -> list[int]`, а автонакат оставить только под флагом.
Добавить CLI `scripts/migrate.py` с подкомандами `status`, `up`, `check` и переменную
окружения `REGISTRY_AUTO_MIGRATE` (по умолчанию `1`, в `docker-compose.yml` для сервиса
`app` выставляется `0`).

Отдельно исправить транзакционность: сейчас `executescript` в цикле, а `commit()` — один
после всех миграций (`registry/db.py:410-413`), при этом `sqlite3.executescript` сам
коммитит ожидающую транзакцию перед выполнением скрипта, поэтому частично применённый набор
уже возможен. Каждая миграция должна применяться и фиксировать свой `PRAGMA user_version`
отдельно, а первая же ошибка — останавливать процесс с указанием номера версии.

**Как должна работать логика.**
1. `schema_state(path)` открывает базу без создания файла (если файла нет — вернуть
   `(0, len(MIGRATIONS))` и признак «база не создана»), читает `PRAGMA user_version`.
2. `apply_migrations(path, to=None)`: `to` по умолчанию `len(MIGRATIONS)`; допускается только
   движение вперёд, `to < current` → ошибка «откат миграций не поддерживается, восстановите
   базу из копии (см. DB-02)». Для каждой версии по порядку: лог `[registry] миграция vN`,
   `executescript`, `PRAGMA user_version = N`, `commit`. Исключение — `rollback`, лог
   `[registry] миграция vN не применена: <текст>` и возврат ненулевого кода выхода.
3. `connect()` при `REGISTRY_AUTO_MIGRATE=1` ведёт себя как сегодня (нужно тестам и
   локальной разработке). При `REGISTRY_AUTO_MIGRATE=0` и `current < target` — не молчаливое
   создание, а `RuntimeError` с текстом «схема базы v{current}, код ожидает v{target};
   выполните `python -m scripts.migrate up`». При `current > target` (база новее кода —
   откатили релиз) — `RuntimeError` в обе стороны, независимо от флага: работать на базе
   из будущего нельзя.
4. `scripts/migrate.py status` печатает путь к базе, `current`, `target`, список
   неприменённых версий и размер файла базы, `-wal`, `-shm`. Код выхода `0`, если схема
   актуальна, `1` — если нет.
5. `scripts/migrate.py up [--to N] [--dry-run] [--no-backup] [--db ПУТЬ]`: `--dry-run`
   печатает план и выходит; без `--no-backup` перед накатом вызывается снятие копии из DB-02
   и путь к копии печатается в лог; после наката — `PRAGMA integrity_check` и повторный
   `status`.
6. `scripts/migrate.py check` — то же, что `status`, но только код выхода, для деплой-скрипта
   и `docker compose` healthcheck-обвязки.
7. Старт приложения: в `lifespan` (`app.py:168-178`) до `_register_jobs()` вызывать
   `schema_state`. Если схема отстаёт и автонакат выключен — планировщик **не стартует**,
   приложение поднимается в режиме «требуется миграция»: любой роут, кроме `/health`,
   отвечает `503` с телом `{"error":"migration_required","db":current,"code":target}`,
   а `/health` отдаёт `503` и `{"status":"migration_required","db_version":…,
   "code_version":…}`. Это нужно, чтобы контейнер не уходил в перезапускную петлю и чтобы
   дежурный увидел причину.
8. Скрипт делает `load_dotenv()` и правит `sys.path` так же, как `scripts/index_vahtapro_disk.py:28-29`.

**Экраны и компоненты.** `registry/db.py` (`MIGRATIONS`, `_ensure_schema`, `connect`,
`_open`, `reset_schema_cache`), новый `scripts/migrate.py`, `app.py` (`lifespan`, `/health`),
`docker-compose.yml` (переменная `REGISTRY_AUTO_MIGRATE=0` сервису `app`), `Dockerfile`
(команду миграции в образ тянуть не нужно, скрипт уже внутри), `README`/`docs/RUNBOOK`
раздел про деплой, `tests/test_migrations.py` (новый файл).

**Зависимости.** Ни от чего не зависит, делается первой. Формально решение фиксирует
вопрос **D1** из `docs/OPEN-QUESTIONS.md`; вариант по умолчанию — именно этот, поэтому
задачу можно начинать без ответа, но выключение автоната на проде согласуется с тем, кто
выполняет деплой. DB-02 нужен для флага `--no-backup`/автобэкапа: до готовности DB-02 команда
работает, просто печатает предупреждение «копия не снята».

**Критерии готовности.**
- [ ] `python -m scripts.migrate status` на базе с `user_version=6` печатает `6/6` и выходит с кодом 0.
- [ ] На пустом каталоге `python -m scripts.migrate up` создаёт файл базы и доводит `user_version` до `len(MIGRATIONS)`.
- [ ] `python -m scripts.migrate up --dry-run` не меняет `user_version` и не создаёт файл базы.
- [ ] `python -m scripts.migrate up --to N`, где `N` меньше текущей версии, завершается кодом 2 и сообщением про отсутствие отката.
- [ ] При `REGISTRY_AUTO_MIGRATE=0` и отставшей схеме `registry.db.connect()` бросает `RuntimeError`, а не накатывает миграцию.
- [ ] При `REGISTRY_AUTO_MIGRATE=0`, отставшей схеме и запуске приложения: `GET /health` отдаёт 503 и `status=migration_required`, `GET /registry` отдаёт 503, планировщик не стартовал (`GET /jobs` показывает пустой список либо тоже 503).
- [ ] Тест: миграция, бросающая исключение в середине списка, оставляет `user_version` равным последней успешно применённой версии, а не нулю и не конечной.
- [ ] Тест: база с `user_version` больше `len(MIGRATIONS)` вызывает ошибку при любом значении `REGISTRY_AUTO_MIGRATE`.
- [ ] `docker-compose.yml` содержит `REGISTRY_AUTO_MIGRATE=0` для сервиса `app`, а инструкция деплоя содержит шаг «бэкап → migrate up → старт».

**Приоритет.** P0.

**Риски.** Если выключить автонакат и забыть шаг миграции в деплое — приложение поднимется
в режиме 503 и внешне это выглядит как полный отказ; ошибку обязана объяснять строка в
`/health` и лог. Тесты, которые сейчас неявно полагаются на автонакат при первом обращении
(`registry/db.py:396-416`, `reset_schema_cache` — `db.py:419-421`), должны получать
`REGISTRY_AUTO_MIGRATE=1` через фикстуру, иначе упадут пачкой. Обратного пути у миграций нет
и не появляется — единственный откат остаётся восстановлением из копии.

---

### DB-02. Резервная копия базы и восстановление

**Что нужно сделать.** Завести `scripts/backup.py` с подкомандами `create`, `list`,
`verify`, `restore`, `prune` и суточное снятие копии. Копия снимается **онлайн-механизмом
SQLite** (`sqlite3.Connection.backup(dst)` либо `VACUUM INTO`), а не `cp` файла: база
работает в WAL (`registry/db.py:389`), и простое копирование `registry.db` без `-wal`/`-shm`
даёт битую или устаревшую копию. Рядом с каждой копией кладётся манифест JSON.

**Как должна работать логика.**
1. `create [--db ПУТЬ] [--dir КАТАЛОГ] [--tag ТЕКСТ]`: открыть источник, выполнить
   `Connection.backup()` в файл `registry-YYYYMMDD-HHMMSS[-tag].db` в
   `REGISTRY_BACKUP_DIR` (по умолчанию `data/backups`), затем на копии выполнить
   `PRAGMA integrity_check` и `PRAGMA user_version`, затем сжать в `.db.gz`.
2. Манифест `registry-….json` рядом: `created_at`, `db_path`, `user_version`, `size_bytes`,
   `gz_size_bytes`, `sha256` архива, `integrity_ok`, счётчики `requests`, `positions`,
   `positions_active`, `search_index`, `app_version`. Счётчики нужны, чтобы отличить копию
   пустой базы от копии рабочей до распаковки.
3. `list` — таблица копий: имя, дата, `user_version`, размер, счётчик позиций.
   `verify ФАЙЛ` — распаковать во временный файл, проверить sha256, `integrity_check`,
   `user_version`; код выхода 1 при любом расхождении.
4. `restore ФАЙЛ [--to ПУТЬ] [--force]`: отказывается работать, если целевой файл существует
   и нет `--force`; при `--force` перед перезаписью сам снимает копию текущей базы
   (`--tag pre-restore`). После распаковки — `verify`, затем сравнение `user_version` копии
   с `len(MIGRATIONS)`: меньше — печатать «после восстановления выполните
   `python -m scripts.migrate up`»; больше — отказ с текстом «копия новее кода».
   Файлы `-wal` и `-shm` рядом с целевой базой удаляются, иначе SQLite подтянет журнал от
   прежней базы.
5. `prune [--keep-daily 14] [--keep-weekly 8] [--keep-monthly 6]`: удаляет копии по
   правилу «последние N суточных, по одной за неделю, по одной за месяц»; `--dry-run`
   печатает список к удалению. Значения по умолчанию — из `REGISTRY_BACKUP_KEEP_*`.
6. Расписание: задача APScheduler `nightly_backup` в `JOBS` (`app.py:84-108`),
   `CronTrigger(hour=4, minute=0, timezone="Europe/Moscow")`, `misfire_grace_time=3600` —
   раньше утреннего прогона `morning_telegram` (09:30) и после всех дневных. Задача
   выполняет `create` + `prune`, результат пишет в лог и в метрику
   (`registry_backup_age_seconds`, `registry_backup_size_bytes`), чтобы Grafana могла
   заметить пропавшие копии.
7. Каталог копий монтируется **отдельным томом** в `docker-compose.yml`
   (`registry-backups:/app/data/backups`), не внутрь `registry-data`: том с базой и том с
   копиями не должны погибнуть вместе.
8. Никакой миграции данных задача не делает: новых таблиц не создаёт, схему не трогает,
   обратима полностью (удалить скрипт, задачу и том).

**Экраны и компоненты.** Новый `scripts/backup.py`, `app.py` (`JOBS`, регистрация задачи,
метрики), `docker-compose.yml` (том `registry-backups`, переменные `REGISTRY_BACKUP_DIR`,
`REGISTRY_BACKUP_KEEP_*`), `.env.example`, `docs` (раздел эксплуатации),
`tests/test_backup.py`.

**Зависимости.** Ни от чего не зависит, можно вести параллельно с DB-01. Все задачи
DB-03…DB-14 обязаны начинаться после того, как `create` и `restore` проверены на копии
боевой базы. Пересечение с направлением эксплуатации: если там заводится своя задача
на тома и мониторинг, метрики и том берутся оттуда — здесь остаётся сам скрипт.

**Критерии готовности.**
- [ ] `python -m scripts.backup create` на работающем приложении (идёт запись в базу) создаёт архив, `integrity_ok=true` в манифесте.
- [ ] `python -m scripts.backup verify <файл>` на намеренно испорченном байте архива возвращает код 1.
- [ ] `python -m scripts.backup restore <файл> --to /tmp/x.db` даёт базу, на которой `python -m scripts.migrate status` отвечает без ошибок, а `SELECT COUNT(*) FROM positions` совпадает со счётчиком из манифеста.
- [ ] `restore` в существующий файл без `--force` завершается кодом 2 и ничего не перезаписывает.
- [ ] `restore --force` предварительно создаёт копию с тегом `pre-restore`.
- [ ] `prune --dry-run` на наборе из 40 синтетических копий печатает список к удалению по правилу N/неделя/месяц и не удаляет ничего.
- [ ] После суток работы контейнера в каталоге копий лежит хотя бы одна ночная копия, `GET /metrics` отдаёт `registry_backup_age_seconds` меньше 90000.
- [ ] Восстановление проверено вручную на копии боевой базы и результат записан в документ эксплуатации (дата, размер, время восстановления).

**Приоритет.** P0.

**Риски.** Копия занимает примерно столько же, сколько база (WAL при этом чекпойнтится) —
на маленьком диске ночная задача может забить том; отсюда обязательный `prune` и метрика
размера. `Connection.backup()` держит читающую транзакцию: на очень больших базах он
удлиняет чекпойнт WAL, поэтому окно выбрано ночным. Самый опасный сценарий — копии,
которые никто ни разу не восстанавливал: критерий с ручной проверкой восстановления
обязателен, без него задача считается невыполненной.

---

### DB-03. Очистка `search_index` при деактивации и удалении позиции

**Что нужно сделать.** Закрыть рост и рассинхронизацию FTS5-индекса. Сейчас
`search_index` (виртуальная таблица, `registry/db.py:170-175`) наполняется только
`ingest._reindex` (`registry/ingest.py:562-582`, `DELETE` + `INSERT` по одной позиции), а
`_deactivate_stale` (`ingest.py:597-618`) гасит позиции и индекс не трогает; строк,
оставшихся от удалённых позиций, тоже никто не убирает — на FTS5 не действует
`ON DELETE CASCADE`, потому что внешнего ключа на виртуальную таблицу нет. Нужна миграция
с двумя триггерами, правка `_deactivate_stale` и команда полной переиндексации.

**Как должна работать логика.**
1. Правило: `search_index` содержит строки **только для существующих и активных** позиций.
2. Новая миграция создаёт триггеры:
   `CREATE TRIGGER trg_positions_ad AFTER DELETE ON positions BEGIN DELETE FROM search_index WHERE position_id = old.position_id; END;`
   и
   `CREATE TRIGGER trg_positions_deactivate AFTER UPDATE OF is_active ON positions WHEN new.is_active = 0 AND old.is_active = 1 BEGIN DELETE FROM search_index WHERE position_id = new.position_id; END;`
   Триггеры выбраны вместо правки кода, потому что позиции гасятся не только из `ingest`,
   и любой будущий путь удаления обязан чистить индекс без напоминаний.
3. Та же миграция разово убирает уже накопленный мусор:
   `DELETE FROM search_index WHERE position_id NOT IN (SELECT position_id FROM positions WHERE is_active = 1);`
4. Возврат позиции в строй ничего дополнительно не требует: `_store` зовёт `_reindex`
   (`ingest.py:370`) для каждой позиции пачки, а `_upsert_position` (`ingest.py:412-481`)
   в обеих ветках ставит `is_active = 1` — то есть реактивированная позиция получает свою
   строку обратно в том же прогоне. В тесте это надо зафиксировать явно.
5. Следствие для реестра, которое обязано быть закрыто в этой же задаче: фильтр `q`
   в `queries._apply_filters` (`registry/queries.py:112-120`) сводится к
   `p.position_id IN (SELECT position_id FROM search_index WHERE search_index MATCH ?)`, и
   при `is_active=false` либо `is_active=all` (`app.py`, `/registry`) полнотекстовый поиск
   по погашенным позициям перестанет находить что-либо. Правило: если `q` задан и
   `is_active != "true"`, к условию добавляется `OR` по `LIKE '%…%'` на
   `p.counterparty`, `p.vacancy_name`, `p.city`, `p.object_name` от исходной строки запроса
   (без FTS-синтаксиса). Это заведомо у́же полнотекстового поиска — на экране рядом с полем
   поиска показывается подсказка «по погашенным позициям поиск идёт по названию, городу,
   объекту и контрагенту»; текст подсказки — задача направления интерфейса.
6. `scripts/reindex_search.py` — полная перестройка индекса: `DELETE FROM search_index`,
   затем по всем активным позициям `SELECT p.*, r.raw_text FROM positions p JOIN requests r
   ON r.request_id = p.last_request_id WHERE p.is_active = 1` и вставка тела по тем же
   правилам, что `_reindex` (сырой текст заявки + значения `ingest.SEARCHABLE_FIELDS`,
   17 полей, `ingest.py:34-52`), пачками по 500 в одной транзакции. Флаги `--db`,
   `--dry-run` (печатает, сколько строк будет удалено и вставлено).
7. Из существующих текстовых колонок ничего не мигрирует: задача только удаляет лишнее и
   пересобирает тело индекса из уже имеющихся полей.
8. Обратимость: триггеры снимаются `DROP TRIGGER`, содержимое индекса восстанавливается
   `scripts/reindex_search.py`. Удалённые строки индекса невосстановимы напрямую, но и не
   являются данными — они производные.

**Экраны и компоненты.** `registry/db.py` (новая миграция), `registry/ingest.py`
(`_deactivate_stale`, `_reindex` — привести к общему хелперу), `registry/queries.py`
(`_apply_filters`, ветка `q`), новый `scripts/reindex_search.py`, `templates/registry.html`
(подсказка у поля поиска — совместно с направлением интерфейса),
`tests/test_queries.py` и новый `tests/test_search_index.py`.

**Зависимости.** DB-01 (миграция накатывается явной командой), DB-02 (копия перед накатом:
миграция удаляет строки). Вопросом из `docs/OPEN-QUESTIONS.md` не заблокирована.

**Критерии готовности.**
- [ ] После `_deactivate_stale` в `search_index` нет ни одной строки с `position_id` погашенной позиции (тест).
- [ ] `DELETE FROM positions WHERE position_id = ?` не оставляет строк в `search_index` (тест на триггере, без участия Python-кода).
- [ ] Позиция, погашенная снапшотом и вернувшаяся в следующем прогоне, снова находится по `q` (тест полного цикла ingest).
- [ ] На копии боевой базы миграция удаляет строки-сироты, и `SELECT COUNT(*) FROM search_index` совпадает с числом активных позиций.
- [ ] `/registry?q=…&is_active=false` возвращает непустой результат для позиции, у которой строка запроса встречается в названии вакансии, городе, объекте или контрагенте.
- [ ] `python -m scripts.reindex_search --dry-run` печатает планируемые числа и не меняет базу; полный прогон восстанавливает индекс так, что результаты `q` совпадают с результатами до перестройки (сравнение на выборке из 50 запросов).
- [ ] В отчёте задачи записан размер файла базы до и после (по `page_count * page_size`), чтобы был измеренный эффект.

**Приоритет.** P0.

**Риски.** Триггер на `UPDATE OF is_active` срабатывает на каждый снапшот-прогон — при
массовом гашении источника это удаление тысяч строк FTS5 в одной транзакции; проверить
время на копии боевой базы, при необходимости резать `_deactivate_stale` на пачки.
Пункт 5 — видимое изменение поведения: поиск по погашенным позициям станет уже; если
заказчику это неприемлемо, альтернатива — не удалять, а переносить строки в
`search_index_archive` с той же схемой, и это отдельное решение, а не дефолт.
Полная переиндексация читает `raw_text` всех заявок — на большой базе это заметная нагрузка,
запускать вне рабочего окна.

---

### DB-04. Таблицы `counterparties` и `counterparty_settings`

**Что нужно сделать.** Завести сущность контрагента как юридического заказчика и отдельную
таблицу его настроек. Миграция создаёт `counterparties` и `counterparty_settings`, наполняет
их из уже накопленных данных (`positions.counterparty` и справочник
`dictionaries` вида `counterparty`), и добавляет модуль доступа
`registry/counterparties.py` (чтение, upsert, поиск по ключу, список с числом позиций).
Колонку `positions.counterparty_id` эта задача **не** добавляет — это DB-07.

**Как должна работать логика.**
1. `counterparties`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `key TEXT NOT NULL UNIQUE` (нормализованный ключ, `dictionaries.norm_key(name)` —
   `registry/dictionaries.py:52-63`), `name TEXT NOT NULL` (каноническое название),
   `alias TEXT NOT NULL DEFAULT ''` (публичный алиас для кандидата),
   `kind TEXT NOT NULL DEFAULT ''` (тип: склад, производство, розница…),
   `inn TEXT NOT NULL DEFAULT ''`, `status TEXT NOT NULL DEFAULT 'active'`
   (`active` | `archived` | `excluded` — три состояния, которые уже использует экран
   подбора), `needs_review INTEGER NOT NULL DEFAULT 0`, `note TEXT NOT NULL DEFAULT ''`,
   `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`, `updated_by TEXT NOT NULL DEFAULT ''`.
   Индексы: `UNIQUE(key)` (создаётся объявлением колонки), `idx_counterparties_status(status)`.
2. `counterparty_settings`: `counterparty_id INTEGER PRIMARY KEY REFERENCES counterparties(id)
   ON DELETE CASCADE`, `required_fields TEXT NOT NULL DEFAULT '[]'` (JSON-массив имён полей;
   пустой массив = «берём общесистемный список»), `contact TEXT NOT NULL DEFAULT ''`,
   `chat_id TEXT NOT NULL DEFAULT ''`, `thread_id TEXT NOT NULL DEFAULT ''`,
   `bot_env TEXT NOT NULL DEFAULT ''` — **имя переменной окружения** с токеном, не сам токен,
   `send_time TEXT NOT NULL DEFAULT '09:00'`, `max_tries INTEGER NOT NULL DEFAULT 3`,
   `outreach_enabled INTEGER NOT NULL DEFAULT 0`, `template TEXT NOT NULL DEFAULT ''`,
   `base_payout INTEGER` (nullable — база расчёта ставки рекрутера; NULL означает «не задана»,
   а не ноль), `updated_at TEXT NOT NULL`, `updated_by TEXT NOT NULL DEFAULT ''`.
3. Наполнение из накопленных данных, одной миграцией, в порядке:
   а) кандидаты в контрагенты = объединение `SELECT DISTINCT canonical FROM dictionaries
   WHERE kind='counterparty' AND confirmed=1` и `SELECT DISTINCT counterparty FROM positions
   WHERE counterparty IS NOT NULL AND counterparty <> ''`;
   б) `key = norm_key(name)`, `name` — само значение; `created_at = updated_at = <момент
   миграции>`, `updated_by = 'migration'`;
   в) пустое и NULL-значение контрагента строку **не** порождает.
4. Неоднозначные случаи:
   - два разных написания дают один `key` (например «КНК» и «кнк») — побеждает то, у
     которого больше активных позиций (`SELECT COUNT(*) FROM positions WHERE is_active=1
     AND counterparty = ?`); при равенстве — первое по алфавиту. Проигравшее написание
     **не** создаёт вторую строку, а добавляется в `dictionaries` как алиас
     (`kind='counterparty'`, `alias=norm_key(loser)`, `canonical=<победитель>`,
     `confirmed=0`) — подтверждает человек;
   - название похоже на объект, а не на юрлицо (содержит запятую либо город из
     `geo.CITY_COORDS`/`geo.CITY_ALIASES`, например «BMJ, Шарапово») — строка создаётся, но
     с `needs_review = 1`; автоматически разделять на контрагента и объект **запрещено**,
     решение принимает человек в админке;
   - `alias`, `kind`, `inn`, `base_payout` миграцией не заполняются вовсе: выдумывать
     публичный алиас нельзя, он вводится администратором.
5. `counterparty_settings` создаётся по строке на каждого контрагента со значениями по
   умолчанию (все пустые, `outreach_enabled = 0`), чтобы у экрана настроек всегда была
   строка и не приходилось различать «нет настроек» и «настройки пустые».
6. Модуль `registry/counterparties.py`: `all(conn, status=None)`, `get(conn, cp_id)`,
   `by_key(conn, key)`, `resolve(conn, name)` (norm_key → строка или None; ничего не создаёт),
   `upsert(conn, name, **fields, author="")`, `settings(conn, cp_id)`,
   `save_settings(conn, cp_id, values, author="")`, `with_counts(conn)` (число активных
   позиций и объектов на контрагента).
7. Обратимость: полная — `DROP TABLE counterparty_settings; DROP TABLE counterparties;`,
   существующие таблицы не изменяются. Обратный скрипт положить в комментарий к миграции.
8. Размер и индексы: ожидаемо десятки-сотни строк, вклад в размер файла пренебрежимый;
   индексов два, оба маленькие.

**Экраны и компоненты.** `registry/db.py` (новая миграция), новый `registry/counterparties.py`,
`registry/dictionaries.py` (используется `norm_key`, вид `counterparty`), таблицы
`counterparties`, `counterparty_settings`, `dictionaries`, `positions` (только чтение),
`tests/test_counterparties.py`. Потребители появятся позже: `navigator_api.counterparties_block`
(`navigator_api.py:555-617`) сегодня строит карточку по источнику — его переписывание
относится к направлению API и в эту задачу не входит.

**Зависимости.** DB-01, DB-02. Блокируется вопросом **B1** («что такое контрагент» —
от ответа зависит, юрлицо это, источник или и то и другое); вариант по умолчанию — первый,
на нём и построена схема. Дополнительно от **C5** зависит смысл `alias` (кто заводит
публичный алиас), от **B5** — смысл `base_payout`, от **B3** — смысл `required_fields`,
от **C4** — договорённость, что в `bot_env` лежит имя переменной, а не секрет. Без ответа
B1 задачу начинать нельзя; ответы C5, B5, B3, C4 нужны только к моменту, когда эти колонки
начнут заполняться, схему они не меняют.

**Критерии готовности.**
- [ ] После миграции `SELECT COUNT(*) FROM counterparties` равно числу различных `norm_key` непустых `positions.counterparty` плюс подтверждённых канонов справочника, посчитанному контрольным запросом.
- [ ] Ни одной строки с пустым `name` или пустым `key`; на `key` есть UNIQUE.
- [ ] У каждой строки `counterparties` есть ровно одна строка `counterparty_settings`.
- [ ] Коллизия ключа (заведены «КНК» и «кнк») даёт одну строку контрагента и один новый алиас в `dictionaries` с `confirmed=0`.
- [ ] Названия с запятой или городом получают `needs_review = 1`; список таких строк печатается в лог миграции.
- [ ] `counterparties.alias` и `base_payout` после миграции пусты/NULL у всех строк.
- [ ] Повторный запуск миграции невозможен (`user_version`), а повторный вызов `upsert` с тем же `name` не создаёт дубль.
- [ ] `DROP TABLE` обеих таблиц возвращает базу к прежнему поведению: тесты реестра и подбора проходят.
- [ ] В `bot_env` нет ни одной строки, похожей на токен (тест-проверка формата: только `[A-Z0-9_]+`).

**Приоритет.** P0.

**Риски.** Если B1 будет решён иначе (контрагент = источник), таблица заполнена не тем и
переделка задевает DB-05, DB-07, DB-12, DB-13 — это и есть цена старта без ответа.
Наполнение из `positions.counterparty` тянет за собой мусор нормализации: часть значений —
это объекты и бренды, поэтому `needs_review` обязателен, иначе в админке появится список,
который никто не сможет разгрести. Хранение секрета в `bot_env` по недосмотру превратится
в хранение самого токена — нужна проверка формата.

---

### DB-05. Таблица `objects`

**Что нужно сделать.** Завести сущность объекта (склад, площадка, магазин) и наполнить её
из текстовых `positions.object_name`, `positions.object_address`, `positions.city`.
Миграция создаёт `objects` и модуль `registry/objects.py`. Колонку `positions.object_id`
добавляет DB-07; правила ставок (`registry/rates.py`, `client_key` по тексту) эта задача
**не трогает**.

**Как должна работать логика.**
1. `objects`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `counterparty_id INTEGER REFERENCES counterparties(id) ON DELETE SET NULL` (nullable —
   объект без опознанного контрагента возможен), `key TEXT NOT NULL` (`norm_key(name)`),
   `name TEXT NOT NULL`, `address TEXT NOT NULL DEFAULT ''`,
   `city TEXT NOT NULL DEFAULT ''` (текстом, до появления `city_id`),
   `city_id INTEGER` (nullable, заполняется после DB-06),
   `kind TEXT NOT NULL DEFAULT ''`, `lat REAL`, `lon REAL`,
   `status TEXT NOT NULL DEFAULT 'active'`, `needs_review INTEGER NOT NULL DEFAULT 0`,
   `note TEXT NOT NULL DEFAULT ''`, `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`,
   `updated_by TEXT NOT NULL DEFAULT ''`.
   Уникальность: `UNIQUE(counterparty_id, key, city)` — один и тот же «Склад» у двух
   контрагентов и в двух городах это разные объекты. Индексы:
   `idx_objects_counterparty(counterparty_id)`, `idx_objects_city(city)`, `idx_objects_key(key)`.
2. Наполнение: группировка активных и неактивных позиций по тройке
   `(norm_key(counterparty), norm_key(object_name), norm_key(city))` при непустом
   `object_name`. Для каждой группы создаётся строка: `name` — самое частое написание
   `object_name` в группе, `city` — самое частое `city`, `counterparty_id` — из
   `counterparties.by_key(norm_key(counterparty))`, если найден, иначе NULL.
3. Адрес: `address` — самое частое непустое `object_address` в группе. Если в группе
   встречается больше одного различного непустого адреса, в `note` пишется
   «прочие адреса из заявок: a; b» и ставится `needs_review = 1`. Склеивать адреса
   запрещено.
4. Неоднозначные случаи:
   - пустой `object_name` — объект не создаётся; такие позиции просто останутся без
     `object_id` (DB-07). Придумывать объект из названия вакансии запрещено;
   - `object_name` совпадает с названием контрагента (у КНК и Аметиста заказчик лежит
     именно в объекте — см. `registry/rates.py:132-139`) — строка создаётся, но с
     `needs_review = 1` и `note = 'совпадает с названием контрагента'`;
   - один и тот же объект в двух написаниях с разными `norm_key` («ДНС Пушкино» и
     «DNS Пушкино») миграцией **не** склеивается: слияние — ручная операция, для неё в
     `registry/objects.py` предусмотреть `merge(conn, src_id, dst_id, author)`, который
     переносит ссылки и удаляет источник; вызывать её будет админка (другое направление).
5. `lat`/`lon` миграцией не заполняются: координат объекта в данных нет, а координата города
   объекту не принадлежит.
6. `registry/objects.py`: `all(conn, counterparty_id=None)`, `get`, `by_key(conn,
   counterparty_id, key, city)`, `resolve(conn, counterparty, object_name, city)` (только
   чтение, ничего не создаёт), `upsert(conn, …, author="")`, `merge(conn, src, dst, author)`,
   `with_counts(conn)`.
7. Обратимость: `DROP TABLE objects` — полная, чужих таблиц миграция не меняет.
8. Размер: сотни-тысячи строк, три индекса; вклад в размер базы — единицы мегабайт в худшем
   случае.

**Экраны и компоненты.** `registry/db.py` (миграция), новый `registry/objects.py`, таблицы
`objects`, `counterparties`, `positions` (чтение), `tests/test_objects.py`.
Потребители — карточка позиции и экран подбора — появятся после DB-07 и относятся к
направлениям реестра и API.

**Зависимости.** DB-01, DB-02, DB-04 (нужен `counterparty_id`). DB-06 не блокирует:
`city_id` остаётся NULL и заполняется в рамках DB-07. Блокируется вопросом **B1** (объект
как отдельная сущность против «объект = контрагент») — вариант по умолчанию первый.

**Критерии готовности.**
- [ ] После миграции число строк `objects` равно числу различных троек `(norm_key(counterparty), norm_key(object_name), norm_key(city))` с непустым `object_name` — проверено контрольным запросом.
- [ ] Нет строк с пустым `name`; UNIQUE-ограничение по `(counterparty_id, key, city)` не нарушается.
- [ ] Позиции с пустым `object_name` не породили ни одной строки.
- [ ] Группа с двумя разными адресами даёт одну строку с самым частым адресом, `needs_review = 1` и перечислением остальных в `note` (тест).
- [ ] Объект, чьё имя совпадает с именем контрагента, помечен `needs_review = 1` (тест на данных КНК/Аметиста).
- [ ] `objects.merge` переносит ссылки и не оставляет висячих `object_id` (тест выполняется после DB-07; до этого проверяется на самой таблице).
- [ ] `lat`, `lon`, `city_id` после миграции пусты у всех строк.
- [ ] Лог миграции печатает: создано объектов, из них `needs_review`, позиций без объекта.

**Приоритет.** P1.

**Риски.** Позиции по одному объекту приходят из разных источников с разными написаниями —
объектов получится больше, чем их есть на самом деле; это ожидаемо и лечится ручным
слиянием, но список в админке на первых порах будет шумным. Нормализации объекта, в отличие
от города и контрагента, в системе нет вовсе — качество наполнения полностью зависит от
того, как парсер положил `object_name`. Если позже придёт решение считать ставку по
`object_id`, а не по `client_key` текстом, сегодняшние дубли объектов сразу превратятся в
расхождение сумм — поэтому переключение ставок на `object_id` вынесено из этой задачи.

---

### DB-06. Таблица `cities` и наполнение координатами

**Что нужно сделать.** Перенести справочник городов из кода в базу. Миграция создаёт
`cities`, засевает её 62 городами из `registry/geo.CITY_COORDS` (`geo.py:37-91`), добирает
все города, встречающиеся в `positions.city`, и оставляет им координаты пустыми. Добавляется
`registry/cities.py` (доступ к таблице и функция отбора по радиусу) и
`scripts/cities.py` (импорт датасета, статистика, ручная правка). Константы в `geo.py`
**не удаляются**: они остаются посевом миграции и источником `GEO_TOKENS` для сопоставления
проектов (`project_kb.py:121-134`).

**Как должна работать логика.**
1. `cities`: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `key TEXT NOT NULL UNIQUE`
   (`norm_key(name)`), `name TEXT NOT NULL`, `region TEXT NOT NULL DEFAULT ''`,
   `lat REAL`, `lon REAL` (обе nullable — «координата неизвестна» не равно нулю),
   `timezone TEXT NOT NULL DEFAULT ''`, `population INTEGER`,
   `origin TEXT NOT NULL DEFAULT ''` (`geo` | `positions` | `dataset` | `manual`),
   `confirmed INTEGER NOT NULL DEFAULT 0`, `needs_review INTEGER NOT NULL DEFAULT 0`,
   `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`, `updated_by TEXT NOT NULL DEFAULT ''`.
   Индексы: `UNIQUE(key)`, `idx_cities_coords(lat, lon)` (для отсечки по прямоугольнику),
   `idx_cities_region(region)`.
2. Наполнение миграцией, по шагам:
   а) 62 строки из `geo.CITY_COORDS`: `name` — ключ словаря как есть, `lat`/`lon` — из
   кортежа, `origin='geo'`, `confirmed=1`;
   б) `SELECT DISTINCT city FROM positions WHERE city IS NOT NULL AND city <> ''`, каждое
   значение прогоняется через `geo.normalize_city` (`geo.py:109-129`); если `norm_key`
   результата ещё не в таблице — строка с `origin='positions'`, `confirmed=0`, координаты
   NULL;
   в) `region` берётся как самое частое непустое `positions.region` для этого города, иначе
   из подтверждённого справочника `dictionaries` вида `city_region`, иначе пустая строка;
   г) 14 записей `geo.CITY_ALIASES` (`geo.py:18-34`) в `cities` **не** попадают — они
   добавляются в `dictionaries` (`kind='city'`, `confirmed=1`) через `INSERT OR IGNORE`,
   потому что это варианты написания, а не города.
3. Неоднозначные случаи:
   - одноимённые города в разных регионах (Красноармейск в МО и в Саратовской обл.):
     `key` занимает тот, чей регион совпадает с регионом хотя бы одной существующей позиции;
     остальные получают `key = norm_key(name) + '|' + norm_key(region)`, `confirmed=0`,
     `needs_review=1`;
   - город из позиций, который на самом деле посёлок или район («раменский район, с.
     михайловская слобода») — строка создаётся как есть после `normalize_city`, с
     `needs_review = 1`, если в исходном значении была запятая;
   - города без координат остаются с NULL: подставлять координату региона или центра страны
     запрещено — иначе фильтр по радиусу начнёт врать.
4. `scripts/cities.py`:
   - `stats` — сколько городов всего, сколько без координат, сколько активных позиций
     приходится на города без координат (это и есть метрика полноты справочника);
   - `import ФАЙЛ.csv [--origin dataset] [--force]` — колонки `name,region,lat,lon[,population,timezone]`,
     кодировка `utf-8-sig`; сопоставление по `norm_key(name)` и, если он неоднозначен, по
     паре с регионом. Строки с `confirmed=1` **не** перезаписываются без `--force`. Импорт
     печатает: обновлено, добавлено, пропущено, конфликтов;
   - `set НАЗВАНИЕ --lat … --lon … [--region …]` — ручной ввод, ставит `origin='manual'`,
     `confirmed=1`;
   - `unresolved` — список городов без координат, отсортированный по числу активных позиций.
5. `registry/cities.py`: `resolve(conn, raw_city)` (нормализация через `geo.normalize_city` +
   поиск по `norm_key`, ничего не создаёт), `get`, `all(conn, with_coords=None)`,
   `upsert(conn, …, author="")`, `within_radius(conn, lat, lon, km)` — возвращает
   `[(city_id, distance_km)]`: сначала отсечка по прямоугольнику
   (`lat BETWEEN ? AND ?`, `lon BETWEEN ? AND ?` по индексу `idx_cities_coords`), затем
   точная гаверсинусная формула в Python по отобранным строкам. Функции расстояния на
   сервере сегодня нет ни одной (`geo.py` отдаёт только `normalize_city`,
   `normalize_region`, `coords`) — она появляется здесь.
6. Изменение контракта `/api/navigator` (перенос радиуса и мультивыбора городов с браузера
   на сервер) в эту задачу **не входит** — это направление API; здесь только слой данных и
   `within_radius`.
7. Обратимость: `DROP TABLE cities` — полная. Добавленные в `dictionaries` алиасы городов
   остаются, они безвредны.
8. Размер: при загрузке открытого датасета городов России — порядка полутора тысяч строк
   плюс два индекса, единицы мегабайт.

**Экраны и компоненты.** `registry/db.py` (миграция), `registry/geo.py` (остаётся как есть,
используется как посев и как нормализатор), новые `registry/cities.py` и `scripts/cities.py`,
таблицы `cities`, `dictionaries`, `positions` (чтение), `navigator_api.cities_block`
(`navigator_api.py:667-686`) — потребитель, переводится на таблицу в задаче направления API,
`tests/test_cities.py`.

**Зависимости.** DB-01, DB-02. Блокируется вопросами **D3** (откуда берутся координаты:
разовая загрузка датасета плюс ручная правка — вариант по умолчанию) и **B6** (радиус на
сервере или в браузере: от ответа зависит, нужен ли `within_radius` вообще и меняется ли
контракт API). Схема таблицы от обоих ответов не зависит, поэтому создание и наполнение
можно начинать, а `within_radius` — только после B6.

**Критерии готовности.**
- [ ] После миграции в `cities` есть 62 строки с `origin='geo'`, у всех заполнены `lat` и `lon`, координаты побайтово совпадают с `geo.CITY_COORDS`.
- [ ] Каждый непустой `positions.city` после `geo.normalize_city` находится в `cities` ровно одной строкой (контрольный запрос без совпадений «не найдено»).
- [ ] Ни одна строка не получила координаты «по умолчанию»: `SELECT COUNT(*) FROM cities WHERE lat = 0 OR lon = 0` равно нулю.
- [ ] `python -m scripts.cities stats` печатает число городов без координат и долю активных позиций в них.
- [ ] `python -m scripts.cities import` не перезаписывает строки с `confirmed=1` без `--force` (тест).
- [ ] Одноимённые города из датасета не ломают UNIQUE: второй получает составной ключ и `needs_review=1` (тест).
- [ ] `within_radius(conn, 55.75, 37.62, 50)` возвращает Москву с расстоянием 0 и не возвращает Санкт-Петербург; города без координат не попадают в выдачу никогда.
- [ ] `geo.CITY_COORDS` и `geo.CITY_ALIASES` не удалены, `project_kb` (`GEO_TOKENS`) работает по-прежнему — тесты `test_project_kb` проходят.

**Приоритет.** P1.

**Риски.** Датасет городов приносит омонимы и посёлки — без `needs_review` и ручной правки
справочник станет шумным, а фильтр по радиусу начнёт подтягивать не те населённые пункты.
Города, которых нет в справочнике, радиусом не подхватываются — это существующее поведение
(`geo.py:8-10`), и после переноса на сервер оно станет заметнее: позиция просто не попадёт
в выдачу по радиусу. Если координаты берутся из внешнего датасета, нужно проверить его
лицензию до загрузки — это вопрос к заказчику, а не к разработке.

---

### DB-07. Новые колонки позиции: `ext_title`, `ext_ref`, `counterparty_id`, `object_id`, `city_id`

**Что нужно сделать.** Добавить в `positions` пять колонок отдельной миграцией с
`ALTER TABLE ... ADD COLUMN`, заполнить связи для уже накопленных позиций и закрыть корневую
причину проблемы: DDL таблицы генерируется из `models.py` только для миграции v1
(`registry/db.py:31-36`, `db.py:103`), поэтому забытая миграция сегодня обнаруживается
только на проде. Нужен тест, который сверяет фактические колонки `positions` с
`models.DATA_FIELDS + MANAGER_FIELDS` после прогона всех миграций.

**Как должна работать логика.**
1. Миграция выполняет пять отдельных `ALTER TABLE positions ADD COLUMN`:
   `ext_title TEXT NOT NULL DEFAULT ''` (как позицию называет контрагент у себя),
   `ext_ref TEXT NOT NULL DEFAULT ''` (внешний номер строки или поста),
   `counterparty_id INTEGER`, `object_id INTEGER`, `city_id INTEGER` (все три nullable,
   NULL = «связь не установлена»).
   Внешних ключей в `ALTER TABLE` SQLite не добавляет — целостность обеспечивается кодом и
   проверочным запросом из критериев, это отмечается комментарием в миграции.
2. `ext_title` и `ext_ref` **не** добавляются в `models.DATA_FIELDS`: иначе они попадут в
   общий проход `_upsert_position` (`ingest.py:444-463`) и в `_position_columns_ddl`. Это
   служебные колонки уровня «как позиция называется у источника», рядом с `legacy_id`
   (`db.py:96-98`). Изменение схемы `models.py` в этой задаче не делается.
3. Заполнение накопленных данных, одним проходом внутри миграции:
   - `counterparty_id` — по `norm_key(positions.counterparty)` из `counterparties.key`;
   - `object_id` — по тройке `(counterparty_id, norm_key(object_name), norm_key(city))`
     из `objects`; при NULL-контрагенте ищется объект с `counterparty_id IS NULL`;
   - `city_id` — по `norm_key(geo.normalize_city(positions.city))` из `cities.key`;
   - `ext_ref` — из `requests.source_ref` заявки `last_request_id` с отброшенным префиксом
     до первого двоеточия (`source_ref` и есть естественный ключ строки/поста у источника,
     см. `ingest._prepare`, `ingest.py:159-162`); если `source_ref` пуст — остаётся `''`;
   - `ext_title` — из `vacancy_name_raw`, если непусто, иначе `''`. Это заведомо
     приблизительно: настоящее «название у контрагента» должно приходить из источника, и это
     работа направления приёма заявок.
4. Неоднозначные случаи: связь не нашлась — колонка остаётся NULL. **Ничего не выдумывать**:
   не подбирать «похожего» контрагента, не привязывать позицию к объекту другого
   контрагента, не подставлять ближайший город. По итогам прохода миграция печатает в лог
   четыре числа: позиций всего, без `counterparty_id`, без `object_id`, без `city_id`.
5. `scripts/link_report.py` — отчёт по несвязанным: топ значений `positions.counterparty`,
   `object_name`, `city`, для которых связь не нашлась, с числом позиций. Это рабочий
   список для админки.
6. `fingerprint` не меняется: `models.FINGERPRINT_FIELDS` (`models.py:113-120`) остаются
   текстовыми (counterparty, city, vacancy_name, object_name, work_format, shift_type).
   Добавлять идентификаторы в отпечаток **запрещено** — это перебьёт `UNIQUE(source,
   fingerprint)` у всех позиций и потребует полной перенормализации.
7. Текстовые колонки `counterparty`, `object_name`, `city` остаются и продолжают
   заполняться приёмом: период двойной записи не ограничивается этой задачей, удаление
   текстовых колонок сюда не входит.
8. Индексы: `idx_positions_counterparty_id(counterparty_id)`,
   `idx_positions_object_id(object_id)`, `idx_positions_city_id(city_id)`. Существующие
   индексы по текстовым колонкам (`idx_positions_counterparty`, `idx_positions_city` —
   `db.py:107-116`) не удаляются.
9. Поддержание связей на новых данных: `ingest._store` (`ingest.py:323-400`) после
   `normalizer.normalize` вызывает `resolve` из `registry/counterparties.py`,
   `registry/objects.py`, `registry/cities.py` и проставляет идентификаторы; ненайденное
   остаётся NULL и попадает в отчёт. Автосоздание новых контрагентов, объектов и городов из
   приёма **выключено** по умолчанию (флаг `REGISTRY_AUTOLINK_CREATE=0`) — иначе один сбой
   парсера заводит мусорную сущность.
10. Обратимость: `ALTER TABLE ... DROP COLUMN` доступен только в SQLite 3.35+ и в проекте
    не используется; считать миграцию **необратимой** и откатывать восстановлением из копии
    (DB-02). Обратный путь без потери данных — оставить колонки и просто перестать их читать.

**Экраны и компоненты.** `registry/db.py` (миграция, комментарий про генерацию DDL),
`registry/models.py` (только чтение списков в тесте), `registry/ingest.py` (`_store`,
`_upsert_position` — простановка связей на новых данных), новые `scripts/link_report.py`,
`tests/test_migrations.py` (сверка колонок), `tests/test_ingest.py` (связи на приёме).
Таблицы: `positions`, `counterparties`, `objects`, `cities`, `requests`.

**Зависимости.** DB-01, DB-02, DB-04, DB-05, DB-06. Косвенно блокируется **B1** и **D3**
через них. Заполнение `ext_title` «правильным» значением из источника — задача направления
приёма заявок (источники и парсинг), здесь только колонка и приблизительный посев.

**Критерии готовности.**
- [ ] `PRAGMA table_info(positions)` на новой и на мигрированной базе даёт одинаковый список колонок — тест `test_positions_columns_match_models` сверяет его с `10 служебных + DATA_FIELDS + MANAGER_FIELDS + 5 новых`.
- [ ] Тест падает, если поле добавили в `models.py` и не добавили миграцию — проверено намеренной поломкой.
- [ ] После миграции доля активных позиций с непустым `counterparty_id` не меньше доли активных позиций с непустым текстовым `counterparty` минус 2 процентных пункта (расхождение объясняется в отчёте).
- [ ] Нет ни одной позиции, у которой `counterparty_id` указывает на несуществующую строку `counterparties` (контрольный `LEFT JOIN` даёт ноль), то же для `object_id` и `city_id`.
- [ ] Позиция с пустым `object_name` имеет `object_id IS NULL`.
- [ ] `SELECT COUNT(*) FROM positions WHERE ext_ref = ''` объяснимо: совпадает с числом позиций, у чьей заявки пустой `source_ref`.
- [ ] Значения `fingerprint` до и после миграции совпадают у всех позиций (контрольная выгрузка).
- [ ] Новый прогон приёма проставляет связи у новых позиций и не создаёт новых строк в `counterparties`/`objects`/`cities` при `REGISTRY_AUTOLINK_CREATE=0`.
- [ ] `python -m scripts.link_report` печатает топ-50 несвязанных значений с числом позиций.

**Приоритет.** P1.

**Риски.** Миграция необратима на используемой сборке SQLite — это главный аргумент за
готовый и проверенный DB-02. Проход по всем позициям с тремя джойнами на большой базе
займёт заметное время в момент деплоя: миграция должна выполняться командой из DB-01, а не
на первом HTTP-запросе. Если DB-04/DB-05 наполнены с ошибками, ошибка немедленно
тиражируется в миллионы связей — отсюда требование «ненайденное остаётся NULL». `ext_title`
после посева из `vacancy_name_raw` выглядит правдоподобно, но это не то, что видит
контрагент: пока источники не начнут отдавать настоящее название, показывать `ext_title`
на экранах нельзя.

---

### DB-08. Таблицы `field_status` и `field_conflicts`

**Что нужно сделать.** Дать значению поля позиции состояние, отличное от «NULL».
Сегодня `NULL` в колонке `positions` означает одновременно «в заявке не сказано», «у этого
контрагента такого не бывает» и «мы спросили и ждём ответа» (`registry/models.py:14-19`),
а пробелы считаются на лету двумя несогласованными списками — `navigator_api.REQUIRED_FIELDS`
(6 полей) и `registry/queries.KEY_FIELDS` (6 полей, пересечение — 3). Миграция создаёт
`field_status` и `field_conflicts`, наполняет `field_status` для согласованного набора
полей и добавляет модуль `registry/field_status.py`. Сам слой проверки (кто и когда
выставляет состояния, как это показывается) — работа направления бэкенда и интерфейса.

**Как должна работать логика.**
1. `field_status`: `position_id TEXT NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE`,
   `field TEXT NOT NULL`, `state TEXT NOT NULL` (`known` | `unknown` | `not_applicable` |
   `asked` | `confirmed`), `origin TEXT NOT NULL DEFAULT ''` (`llm` | `manager` |
   `counterparty` | `default` | `migration`), `confidence REAL`,
   `updated_at TEXT NOT NULL`, `updated_by TEXT NOT NULL DEFAULT ''`,
   `PRIMARY KEY (position_id, field)`. Индекс `idx_field_status_state(state, field)`.
2. `field_conflicts`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `position_id TEXT NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE`,
   `field TEXT NOT NULL DEFAULT ''`, `kind TEXT NOT NULL` (`conflict` | `duplicate` |
   `missing` — только те три вида, которые реально порождаются кодом; `format`, `unparsed`,
   `source` из `ISSUE_KINDS` в `templates/navigator.html` не заводятся, пока нет ответа
   по B4), `value_a TEXT`, `origin_a TEXT NOT NULL DEFAULT ''`, `value_b TEXT`,
   `origin_b TEXT NOT NULL DEFAULT ''`, `peer_position_id TEXT` (для `duplicate`),
   `status TEXT NOT NULL DEFAULT 'open'` (`open` | `resolved` | `ignored`),
   `resolved_value TEXT`, `resolved_by TEXT NOT NULL DEFAULT ''`, `resolved_at TEXT`,
   `detected_at TEXT NOT NULL`. Индексы: `idx_field_conflicts_position(position_id)`,
   `idx_field_conflicts_open(status, kind)`,
   `UNIQUE(position_id, field, kind, peer_position_id)` — повторное обнаружение той же
   проблемы не плодит строк, а обновляет `detected_at`.
3. Наполнение `field_status` миграцией — **ограниченное**, иначе таблица взорвётся:
   строки создаются только для активных позиций и только для объединения
   `REQUIRED_FIELDS` ∪ `KEY_FIELDS` (девять полей: `citizenship_requirements`,
   `housing_available`, `meals_available`, `shift_rate`, `schedule`, `need_total`,
   `vacancy_name`, `city`, `counterparty` — точный список зафиксировать в миграции
   константой, а не импортом, чтобы миграция не менялась вместе с кодом).
   Правило: значение `IS NOT NULL` и не пустая строка → `state='known'`, иначе
   `state='unknown'`. `origin='migration'`, `confidence` NULL.
   Состояние `not_applicable` миграцией **не** выставляется никогда: данных, из которых его
   можно вывести, нет, а угадывание «у этого контрагента жилья не бывает» — прямая порча
   реестра.
4. `field_conflicts` создаётся **пустой**: истории проблем в базе нет, восстанавливать её не
   из чего. Первые строки появятся, когда слой проверки начнёт работать.
5. Строки для остальных полей заводятся лениво — при первой записи состояния. Функция
   `set_state(conn, position_id, field, state, origin, author='', confidence=None)` делает
   `INSERT ... ON CONFLICT(position_id, field) DO UPDATE`.
6. Остальной API модуля `registry/field_status.py`: `for_position(conn, position_id)`,
   `counts(conn, filters)` (сколько позиций с `unknown` по каждому полю),
   `bulk_set(conn, rows)`, `open_conflicts(conn, position_id=None)`,
   `resolve(conn, conflict_id, value, author)`, `ignore(conn, conflict_id, author)`.
7. Взаимодействие с приёмом: при обновлении поля в `_upsert_position` (`ingest.py:444-474`)
   состояние поля обязано переходить в `known`; если поле стало пустым, а раньше было
   заполнено — приём его не затирает (`new is None` пропускается, `ingest.py:450-451`),
   поэтому состояние не меняется. Правило записать в тест.
8. Обратимость: `DROP TABLE` обеих таблиц, `positions` не меняется — полная.
9. Размер и индексы: 9 полей × число активных позиций. При 20 000 активных позиций — 180 000
   строк, порядка 10–15 МБ с индексами. Это верхняя оценка и главный аргумент против
   наполнения по всем 61 полю (получилось бы больше миллиона строк).

**Экраны и компоненты.** `registry/db.py` (миграция), новый `registry/field_status.py`,
`registry/ingest.py` (`_store`, `_upsert_position` — простановка `known`),
`registry/queries.py` (`KEY_FIELDS` — остаётся как есть, слой проверки его заменит позже),
`navigator_api.py` (`REQUIRED_FIELDS` — то же), таблицы `field_status`, `field_conflicts`,
`positions`, `tests/test_field_status.py`.

**Зависимости.** DB-01, DB-02. Блокируется вопросами **B3** (один список обязательных полей,
два разных или настройка на контрагента — от ответа зависит, откуда слой проверки берёт
набор полей; на схему влияет только тем, что при варианте «на контрагента» набор читается
из `counterparty_settings.required_fields` из DB-04) и **B4** (какие виды проблем существуют
— от ответа зависит перечень допустимых значений `field_conflicts.kind`). Начинать без
ответов нельзя: наполнение придётся переделывать. Слой вычисления проблем — направление
бэкенда.

**Критерии готовности.**
- [ ] После миграции `SELECT COUNT(*) FROM field_status` равно 9 × число активных позиций.
- [ ] Для каждой активной позиции и каждого из девяти полей есть ровно одна строка; дублей нет (PRIMARY KEY).
- [ ] Ни одной строки со `state='not_applicable'` после миграции.
- [ ] `field_conflicts` после миграции пуста.
- [ ] Удаление позиции удаляет её строки в обеих таблицах (тест на `ON DELETE CASCADE` при `foreign_keys=ON`).
- [ ] Повторный вызов `set_state` с тем же полем обновляет строку и не создаёт вторую.
- [ ] Повторная фиксация того же конфликта обновляет `detected_at` и не создаёт вторую строку (UNIQUE).
- [ ] Прогон приёма, заполнивший ранее пустое поле, переводит его состояние в `known` (тест).
- [ ] Измерен и записан прирост размера файла базы на копии боевой базы.

**Приоритет.** P1.

**Риски.** Самый вероятный сбой — попытка завести строки по всем полям всех позиций «на
всякий случай»: таблица станет крупнее самой `positions`, и обход карточки начнёт тормозить.
Второй риск — рассинхронизация: если состояние выставляет только слой проверки, а приём
пишет значения мимо, `field_status` быстро начнёт врать; поэтому простановка `known` в
`_upsert_position` входит в эту задачу, а не в следующую. Пока B3 не решён, набор из девяти
полей — компромисс, и при варианте «обязательность на контрагента» наполнение придётся
пересчитывать (это дешёвая операция, но её надо предусмотреть командой).

---

### DB-09. Пользователи, роли и автор правки в `position_history`

**Что нужно сделать.** Дать системе понятие «кто это сделал». Сейчас на всё приложение одна
пара HTTP Basic (`WEB_USER`/`WEB_PASSWORD`, `app.py:191-207`), ролей нет, а правки менеджера
через `queries.update_manager_fields` (`registry/queries.py:241-254`) вообще не попадают в
`position_history` — история содержит только изменения от приёма (`ingest.py:464-474`).
Миграция создаёт `users`, `roles`, `user_roles`, добавляет в `position_history` колонки
автора и происхождения правки, и правит `update_manager_fields`, чтобы ручные изменения
писались в историю. Механика входа, сессий и проверки прав — направление безопасности.

**Как должна работать логика.**
1. `users`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `login TEXT NOT NULL UNIQUE COLLATE NOCASE`, `name TEXT NOT NULL DEFAULT ''`,
   `email TEXT NOT NULL DEFAULT ''`, `password_hash TEXT NOT NULL DEFAULT ''`
   (пустая строка = «пароль не задан, войти нельзя»), `is_active INTEGER NOT NULL DEFAULT 1`,
   `external_id TEXT NOT NULL DEFAULT ''` (учётная запись во внешнем кабинете),
   `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`, `last_login_at TEXT`.
   Индекс `idx_users_active(is_active)`.
2. `roles`: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `code TEXT NOT NULL UNIQUE`,
   `title TEXT NOT NULL`, `permissions TEXT NOT NULL DEFAULT '[]'` (JSON-массив кодов прав),
   `created_at TEXT NOT NULL`. Посев: `admin`, `manager`, `recruiter`, `viewer` с пустыми
   массивами прав — содержательный перечень прав определяет направление безопасности.
3. `user_roles`: `user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE`,
   `role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE`,
   `granted_at TEXT NOT NULL`, `granted_by TEXT NOT NULL DEFAULT ''`,
   `PRIMARY KEY (user_id, role_id)`. Третья таблица обязательна: связь «многие ко многим»,
   иначе роль придётся дублировать колонкой в `users` и потом переносить.
4. Наполнение: одна строка `users` с `login` из `WEB_USER` (если переменная пуста — строка
   не создаётся), `password_hash = ''`, роль `admin`. Пароль **не переносится**: он лежит в
   окружении открытым текстом, и хеш ставится отдельной командой
   `python -m scripts.users set-password ЛОГИН` из направления безопасности. До этого вход
   продолжает работать через HTTP Basic — существующее поведение не ломается.
5. `position_history` получает две колонки:
   `author TEXT NOT NULL DEFAULT ''` и `origin TEXT NOT NULL DEFAULT 'ingest'`
   (`ingest` | `manager` | `cli` | `migration`). Все существующие строки после миграции
   имеют `author = ''` и `origin = 'ingest'` — это соответствует действительности: ручные
   правки туда никогда не писались. Индекс `idx_history_author(author, changed_at)`.
6. `queries.update_manager_fields(conn, position_id, values)` получает параметр
   `author: str = ''`: перед записью читает текущие значения полей из `positions`,
   применяет фильтр по `MANAGER_FIELDS` (как сейчас, `queries.py:246-249`) и на каждое
   реально изменившееся поле пишет строку в `position_history` с `request_id = ''`
   (менеджерская правка не связана с заявкой; колонка `NOT NULL`, но внешнего ключа на ней
   нет — `db.py:131-139`), `old_value`, `new_value`, `changed_at = <сейчас>`,
   `author`, `origin='manager'`. Поле, чьё значение не изменилось, строку не порождает.
7. Вызывающая сторона (обработчик сохранения менеджерских полей в `app.py`) передаёт
   `author` — это значение `verify_creds`, то есть `creds.username` (`app.py:207`), тем же
   способом, каким оно уже уходит в `author` правил ставок (`app.py:434-435, 447`) и в
   `raw_payload.entered_by` ручной заявки (`app.py:823`). Замена HTTP Basic на настоящие
   учётные записи в эту задачу не входит.
8. Неоднозначные случаи: правка выполнена скриптом — `author = 'cli:<имя скрипта>'`,
   `origin='cli'`; правка от миграции — `author='migration'`. Пустой `author` при
   `origin='manager'` считается ошибкой и должен ловиться тестом.
9. Обратимость: `users`, `roles`, `user_roles` — `DROP TABLE`, полная. Две колонки
   `position_history` не удаляются (см. DB-07, п. 10) — откат через восстановление из копии.
10. Размер: единицы-десятки строк в трёх новых таблицах; две текстовые колонки в
    `position_history` — таблица растёт на каждое изменение поля при приёме, прирост
    оценить на копии боевой базы и записать.

**Экраны и компоненты.** `registry/db.py` (миграция), `registry/queries.py`
(`update_manager_fields`), `app.py` (передача `author` в обработчике сохранения
менеджерских полей карточки), новый `registry/users.py` (`all`, `get`, `by_login`,
`upsert`, `set_roles`, `roles`), таблицы `users`, `roles`, `user_roles`,
`position_history`, `positions`; `templates/registry_position.html` (показ автора в истории —
совместно с направлением интерфейса), `tests/test_users.py`, `tests/test_queries.py`.

**Зависимости.** DB-01, DB-02. Блокируется вопросом **C1** (как заводятся пользователи:
личный вход у каждого, общая учётка на отдел или внешний провайдер) — от ответа зависят
наличие `password_hash` и `external_id` и вообще смысл таблицы. Вариант по умолчанию —
первый. Хеширование пароля, сессии, форма входа и проверка прав на роутах — направление
безопасности (`SEC-*`); эта задача обязана оставить вход через HTTP Basic работоспособным.

**Критерии готовности.**
- [ ] После миграции в `users` ровно одна строка с логином из `WEB_USER` и ролью `admin`; при пустом `WEB_USER` таблица пуста и миграция не падает.
- [ ] `password_hash` у всех строк пуст; пароля из окружения в базе нет (проверяется поиском значения `WEB_PASSWORD` по всем текстовым колонкам).
- [ ] `roles` содержит четыре кода: `admin`, `manager`, `recruiter`, `viewer`.
- [ ] Все существующие строки `position_history` после миграции имеют `origin='ingest'` и пустой `author`.
- [ ] Изменение менеджерского поля через `update_manager_fields` порождает ровно одну строку истории на каждое изменившееся поле, с `origin='manager'`, непустым `author` и корректными `old_value`/`new_value` (тест).
- [ ] Сохранение менеджерского поля тем же значением не порождает строк истории (тест).
- [ ] Правка из карточки позиции в работающем приложении видна в истории с логином HTTP Basic.
- [ ] Существующий вход по HTTP Basic продолжает работать; тесты HTTP-слоя, если они есть, не меняются.
- [ ] Удаление пользователя каскадно удаляет его строки в `user_roles` и **не** трогает `position_history` (автор остаётся строкой).

**Приоритет.** P1.

**Риски.** Автор в истории хранится строкой, а не ссылкой на `users.id` — намеренно: логин
может исчезнуть, а запись истории обязана пережить удаление учётной записи; обратная сторона
— переименование сотрудника историю не переписывает. Если C1 будет решён в пользу внешнего
провайдера, `password_hash` окажется лишним, но пустая колонка вреда не приносит.
Соблазн «заодно закрыть открытые роуты» здесь нужно погасить: это направление безопасности,
иначе задача разрастётся и заблокируется на нём.

---

### DB-10. Таблицы `sources` и `sync_runs`

**Что нужно сделать.** Перенести конфигурацию источников из кода в базу и завести журнал
прогонов. Сегодня идентификаторы Google-таблиц — константы в `pipeline.py`, названия
источников — `SOURCE_NAMES` (`pipeline.py:103-110`) и `labels.SOURCE_TITLES`
(`registry/labels.py:93-101`), алиасы — `registry/sources.py:9-15`, а расписание живёт в
словаре `JOBS` (`app.py:84-108`) и **продублировано в двух местах** `navigator_api.py`
(`navigator_api.py:51-59`, `703-710`) — три источника правды для одного расписания.
Журнала прогонов нет вовсе: что сделал ночной прогон, восстанавливается только по логам.
Миграция создаёт `sources` и `sync_runs`, наполняет `sources` из перечисленных констант и
добавляет `registry/source_config.py`. Переключение планировщика и экранов на таблицу —
работа направлений приёма заявок и API.

**Как должна работать логика.**
1. `sources`: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `key TEXT NOT NULL UNIQUE`
   (`kpk`, `yappi`, `vahtapro`, `aaaplus`, `ametist`, `marketstaff`, `manual`),
   `title TEXT NOT NULL` (КНК, ЯППИ, Градус, AAA+, Аметист, Маркетстафф, Вручную),
   `kind TEXT NOT NULL` (`telegram` | `sheets` | `seatable` | `manual`),
   `enabled INTEGER NOT NULL DEFAULT 1`,
   `snapshot INTEGER NOT NULL DEFAULT 1` (что передаётся в `ingest(..., snapshot=)` —
   для источников-дельт `0`, см. `ingest.py:105-108`),
   `counterparty_id INTEGER REFERENCES counterparties(id) ON DELETE SET NULL`,
   `config TEXT NOT NULL DEFAULT '{}'` (JSON: `spreadsheet_id`, `sheet_name`, `disk_url`,
   `chat_env` — **имя** переменной окружения с chat_id, не значение),
   `schedule TEXT NOT NULL DEFAULT '[]'` (JSON-массив
   `{"job":"morning_telegram","cron":"30 9 * * *","tz":"Europe/Moscow","reset":true}`),
   `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`, `updated_by TEXT NOT NULL DEFAULT ''`.
   Индекс `idx_sources_enabled(enabled)`.
2. `sync_runs`: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `source TEXT NOT NULL`
   (ключ источника, без внешнего ключа — прогон должен переживать удаление настройки),
   `job TEXT NOT NULL DEFAULT ''` (имя задачи из `JOBS` либо `manual`, `trigger`, `cli`),
   `started_at TEXT NOT NULL`, `finished_at TEXT`,
   `status TEXT NOT NULL DEFAULT 'running'` (`running` | `ok` | `failed` | `partial`),
   `reset INTEGER NOT NULL DEFAULT 0`,
   `stats TEXT NOT NULL DEFAULT '{}'` (JSON из `IngestStats.as_dict()`,
   `registry/models.py:227-237`), `llm_calls INTEGER NOT NULL DEFAULT 0`,
   `tokens_in INTEGER NOT NULL DEFAULT 0`, `tokens_out INTEGER NOT NULL DEFAULT 0`,
   `error TEXT NOT NULL DEFAULT ''`, `actor TEXT NOT NULL DEFAULT ''`.
   Индексы: `idx_sync_runs_source(source, started_at DESC)`, `idx_sync_runs_status(status)`.
3. Наполнение `sources` миграцией: семь строк, `title` из `labels.SOURCE_TITLES`,
   `kind` — по фактическому транспорту (`vahtapro`, `aaaplus` — `telegram`;
   `kpk`, `yappi`, `marketstaff`, `ametist` — `sheets`; `manual` — `manual`);
   идентификаторы таблиц и `sheet_name` переносятся из констант `pipeline.py` в `config`
   **значениями**, потому что это не секреты; chat_id — **именем** переменной
   (`TELEGRAM_VAHTAPRO_CHAT_ID`, `TELEGRAM_AAAPLUS_CHAT_ID`), потому что оно приходит из
   окружения (`pipeline.py:158-162, 260`).
   `schedule` наполняется разворачиванием `JOBS`: задача, охватывающая три источника,
   даёт по строке расписания каждому из них с одинаковыми `cron` и `reset`.
4. `sync_runs` создаётся **пустой**: истории прогонов нет и восстанавливать её не из чего.
   Задним числом парсить логи запрещено — журнал начинается с момента внедрения.
5. `registry/source_config.py`: `all(conn, enabled_only=False)`, `get(conn, key)`,
   `upsert(conn, key, …, author='')`, `schedule(conn)` (объединённое расписание всех
   включённых источников, отсортированное по времени — это будущий единственный источник
   правды для планировщика и для экрана), `start_run(conn, source, job, reset, actor)`
   (вставка со `status='running'`, возврат id), `finish_run(conn, run_id, status, stats,
   error='')`, `last_runs(conn, limit=50)`, `last_run_by_source(conn)`.
6. Неоднозначные случаи: прогон оборвался вместе с процессом и строка осталась в
   `running` — при старте приложения все строки со `status='running'` и `started_at`
   старше шести часов переводятся в `failed` с `error='прогон прерван'`. Это единственная
   автоматическая правка журнала.
7. Из существующих текстовых колонок не мигрирует ничего: `positions.source` остаётся
   строковым ключом и продолжает быть таковым, `sources.key` с ним совпадает по значению.
8. Ретеншен: строки `sync_runs` старше 180 суток удаляются командой из DB-14.
9. Обратимость: `DROP TABLE sync_runs; DROP TABLE sources;` — полная, пока планировщик
   читает `JOBS`. После переключения планировщика на таблицу откат означает возврат к
   константам — это уже другая задача.
10. Размер: `sources` — семь строк; `sync_runs` — при четырёх задачах в сутки порядка 1500
    строк в год, единицы мегабайт с учётом JSON статистики.

**Экраны и компоненты.** `registry/db.py` (миграция), новый `registry/source_config.py`,
таблицы `sources`, `sync_runs`, `counterparties`; будущие потребители — `app.py` (`JOBS`,
`/jobs`, `/trigger/{name}`, `/run`), `pipeline.py` (константы таблиц), `navigator_api.py`
(`SOURCE_SCHEDULE`, `next_run` — `navigator_api.py:51-59, 703-710`), `templates/navigator.html`
(блок расписания). Переключение потребителей — направления приёма заявок и API.
`tests/test_source_config.py`.

**Зависимости.** DB-01, DB-02, DB-04 (для `counterparty_id`; при отсутствии — NULL).
Блокируется вопросом **D2** (конфигурация источников в базе, в git или гибрид) — вариант
по умолчанию первый, при этом расписание в любом случае обязано иметь один источник правды.
Запись в `sync_runs` из пайплайна и чтение расписания планировщиком — другие направления,
здесь только схема, наполнение и функции доступа.

**Критерии готовности.**
- [ ] После миграции в `sources` семь строк, ключи совпадают с `pipeline.SOURCE_NAMES` плюс `manual`, `title` совпадают с `labels.SOURCE_TITLES`.
- [ ] `config` каждого источника типа `sheets` содержит непустой `spreadsheet_id`, совпадающий с константой в `pipeline.py` (сверка тестом).
- [ ] Ни в одном `config` нет значения chat_id, токена или ключа — только имена переменных окружения (тест по формату).
- [ ] `source_config.schedule(conn)` возвращает те же четыре задачи с тем же временем, что `JOBS` в `app.py` (сверка тестом — до тех пор, пока `JOBS` существует).
- [ ] `sync_runs` после миграции пуста.
- [ ] `start_run` + `finish_run` дают строку со `status='ok'`, заполненными `finished_at` и `stats`, из которых восстанавливается `IngestStats.as_dict()`.
- [ ] Строка, оставшаяся в `running` дольше шести часов, при следующем старте приложения переводится в `failed` (тест).
- [ ] Удаление строки `counterparties` не удаляет источник (`ON DELETE SET NULL`).

**Приоритет.** P1.

**Риски.** Пока планировщик читает `JOBS`, а таблица заполнена, источников правды становится
четыре вместо трёх — расхождение должно ловиться тестом-сверкой, и он обязателен именно
поэтому. Перенос идентификаторов таблиц в базу означает, что теперь их можно поменять без
ревью — это осознанный размен по D2, и в админке правка `config` должна быть доступна
только администратору. Журнал прогонов, начатый «с нуля», первое время будет выглядеть
пустым — это нормально и должно быть проговорено, чтобы никто не пытался наполнить его
задним числом.

---

### DB-11. Таблица `position_media`

**Что нужно сделать.** Отвязать материалы позиции от единственного источника — папки на
Яндекс.Диске. Сегодня материалы производит только `navigator_api.media_block`
(`navigator_api.py:322-345`) из `position_kb` (`registry/db.py:232-245`): ровно один элемент
вида `object_photo`, `alive` захардкожен `True`, `title` не отдаётся, завести материал
вручную нельзя. Во фронте объявлено пять видов (`MEDIA_META`,
`templates/navigator.html:147-153`), четыре из которых бэкенд не производит. Миграция
создаёт `position_media`, переносит в неё связи из `position_kb` и добавляет
`registry/media.py`. Фоновая проверка живости ссылок и переписывание `media_block` —
другие направления.

**Как должна работать логика.**
1. `position_media`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `position_id TEXT NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE`,
   `kind TEXT NOT NULL` (`object_photo` | `housing_photo` | `video` | `telegram` | `route`),
   `title TEXT NOT NULL DEFAULT ''`, `url TEXT NOT NULL`,
   `visibility TEXT NOT NULL DEFAULT 'internal'` (`public` | `internal`),
   `origin TEXT NOT NULL` (`kb` | `manual` | `source`),
   `object_id INTEGER REFERENCES objects(id) ON DELETE SET NULL`,
   `photos INTEGER NOT NULL DEFAULT 0`,
   `alive INTEGER` (nullable: NULL = «не проверяли», 1 = доступна, 0 = недоступна),
   `checked_at TEXT`, `http_status INTEGER`, `fail_count INTEGER NOT NULL DEFAULT 0`,
   `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`,
   `created_by TEXT NOT NULL DEFAULT ''`.
   `UNIQUE(position_id, kind, url)`. Индексы: `idx_position_media_position(position_id)`,
   `idx_position_media_check(alive, checked_at)`.
2. Перенос из `position_kb`: по строке на каждую запись, где `photos_url` непуст и
   `photos > 0` (те же условия, что в `media_block`, `navigator_api.py:329-332`):
   `kind='object_photo'`, `url = photos_url`, `title = project`, `origin='kb'`,
   `visibility='public'` (папка контрагента открыта по ссылке — как сейчас),
   `photos = position_kb.photos`, `alive = NULL`, `checked_at = NULL`,
   `created_at = updated_at = position_kb.linked_at`.
   **`alive` намеренно не ставится в 1**: сегодняшняя единица — это захардкоженное значение,
   а не результат проверки, и переносить его как факт нельзя.
3. `position_kb` остаётся и продолжает наполняться `project_kb.link_positions`
   (`project_kb.py:458-510`): это индекс сопоставления, а не витрина материалов.
   Правило владения: строки с `origin='kb'` полностью управляются пересчётом связей —
   при каждом `link_positions` они пересоздаются для затронутых позиций (удалить строки
   `origin='kb'` этой позиции и вставить актуальные). Строки `origin='manual'` пересчёт
   **не трогает никогда** — это и есть причина заводить отдельную таблицу.
4. Неоднозначные случаи:
   - ручная строка и строка из базы знаний с одинаковым `url` и `kind` — конфликт
     `UNIQUE`; побеждает `manual`: вставка `kb` в этом случае пропускается
     (`INSERT ... ON CONFLICT DO NOTHING` плюс проверка `origin`);
   - у позиции есть папка, но `photos = 0` — материал не заводится (как сегодня), однако
     ссылка на папку остаётся доступной через `position_kb`;
   - `object_id` при переносе не заполняется: связь материала с объектом появится, когда
     материалы начнут заводиться на объект, а не на позицию.
5. `registry/media.py`: `for_position(conn, position_id, visibility=None)`,
   `add(conn, position_id, kind, url, …, author)`, `remove(conn, media_id, author)`,
   `replace_kb(conn, position_id, rows)` (атомарная замена строк `origin='kb'`),
   `due_for_check(conn, older_than_hours, limit)` (для будущей фоновой проверки),
   `mark_checked(conn, media_id, alive, http_status)`.
6. Обратимость: `DROP TABLE position_media` — полная, `position_kb` не изменяется.
7. Размер: не больше числа позиций с папкой на первом шаге; при ручном добавлении — единицы
   строк на позицию. Два индекса, вклад незначительный.

**Экраны и компоненты.** `registry/db.py` (миграция), новый `registry/media.py`,
`project_kb.py` (`link_positions` — вызов `replace_kb`), таблицы `position_media`,
`position_kb`, `positions`, `objects`; потребители — `navigator_api.media_block`
(`navigator_api.py:322-345`) и модалка «Текст для кандидата» с чекбоксами материалов
(`templates/navigator.html`) — переписываются в направлении API и интерфейса.
`tests/test_media.py`, `tests/test_project_kb.py`.

**Зависимости.** DB-01, DB-02. DB-05 нужен только для `object_id` (колонка nullable, можно
делать раньше). Вопросами из `docs/OPEN-QUESTIONS.md` не заблокирована. Фоновая проверка
живости ссылок (заполнение `alive`, `checked_at`, `fail_count`) — отдельная задача
направления эксплуатации; ручное заведение материала в интерфейсе — направление интерфейса.

**Критерии готовности.**
- [ ] Число строк `position_media` с `origin='kb'` после миграции равно числу строк `position_kb` с непустым `photos_url` и `photos > 0`.
- [ ] У всех перенесённых строк `alive IS NULL` и `checked_at IS NULL`.
- [ ] `link_positions` после правки заменяет строки `origin='kb'` затронутых позиций и не удаляет строки `origin='manual'` (тест).
- [ ] Вставка `kb`-строки с `url`, уже занятым ручной строкой, не создаёт дубля и не затирает ручную (тест).
- [ ] Удаление позиции удаляет её материалы (`ON DELETE CASCADE`).
- [ ] `due_for_check` возвращает строки с `checked_at IS NULL` в первую очередь.
- [ ] `media_block` (после переписывания в другом направлении) отдаёт тот же набор материалов, что и до миграции, для 20 контрольных позиций — сверка выгрузок.

**Приоритет.** P2.

**Риски.** Пока `media_block` читает `position_kb`, а не новую таблицу, данные существуют в
двух местах — риск расхождения; поэтому перенос потребителя должен идти сразу следующей
задачей другого направления, а не «когда-нибудь». Отказ от захардкоженного `alive=true`
означает, что в интерфейсе появится состояние «не проверено» — его нужно осмысленно
показать, иначе рекрутер решит, что ссылка сломана. `visibility='public'` у всех
перенесённых строк — наследие текущего поведения; если хотя бы одна папка контрагента на
самом деле закрытая, это утечка, и перед включением публичной проекции (DB-12) видимость
надо перепроверить.

---

### DB-12. Таблица `position_public` и стоп-словарь

**Что нужно сделать.** Перенести публичную проекцию позиции с клиента на сервер. Сегодня
единственный барьер — функция `scrub()` в `templates/navigator.html` (вырезает контрагента,
объект и источник из текста кандидату, заменяя на `———`, и намеренно не трогает URL, чтобы
не ломать ссылки), то есть последний рубеж, а не первый: `/api/navigator` отдаёт всё,
включая ставку рекрутера и внутренние обозначения. Ни `PUBLIC_FIELDS`, ни серверной проекции
нет. Миграция создаёт `position_public` и `public_stopwords`, наполняет словарь из
накопленных названий и добавляет `registry/public_projection.py` с белым списком полей и
чистой функцией сборки. Публичный роут и подключение проекции к API — направление API.

**Как должна работать логика.**
1. `position_public`: `position_id TEXT PRIMARY KEY REFERENCES positions(position_id)
   ON DELETE CASCADE`, `title TEXT NOT NULL DEFAULT ''`,
   `body TEXT NOT NULL DEFAULT ''` (готовый текст для кандидата),
   `fields TEXT NOT NULL DEFAULT '{}'` (JSON только из `PUBLIC_FIELDS`),
   `employer_alias TEXT NOT NULL DEFAULT ''` (публичный алиас контрагента, иначе тип
   объекта, иначе пусто), `removed TEXT NOT NULL DEFAULT '[]'` (JSON-массив вырезанных
   значений — для блока «Вырезано стоп-словарём»), `hash TEXT NOT NULL DEFAULT ''`
   (отпечаток исходных данных, по которому понятно, не устарела ли проекция),
   `built_at TEXT NOT NULL`, `built_from TEXT NOT NULL DEFAULT ''`
   (значение `positions.updated_at` на момент сборки). Индекс
   `idx_position_public_built(built_at)`.
2. `public_stopwords`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `kind TEXT NOT NULL` (`counterparty` | `object` | `source` | `manual`),
   `value TEXT NOT NULL`, `replacement TEXT NOT NULL DEFAULT '———'`,
   `counterparty_id INTEGER REFERENCES counterparties(id) ON DELETE CASCADE`
   (NULL = правило действует глобально), `enabled INTEGER NOT NULL DEFAULT 1`,
   `created_at TEXT NOT NULL`, `author TEXT NOT NULL DEFAULT ''`.
   `UNIQUE(kind, value, counterparty_id)`. Индекс `idx_public_stopwords_enabled(enabled)`.
3. Наполнение стоп-словаря миграцией:
   - все `counterparties.name` → `kind='counterparty'`;
   - все `objects.name` → `kind='object'`;
   - значения `labels.SOURCE_TITLES` (`registry/labels.py:93-101`) и `pipeline.SOURCE_NAMES`
     → `kind='source'`;
   - `replacement = '———'`, `counterparty_id = NULL`, `author='migration'`.
4. Неоднозначные случаи: значение короче четырёх символов либо совпадающее с
   общеупотребительным словом (проверка по нижнему регистру против списка из названий
   городов `cities.name` и слов «градус», «аметист», «склад», «объект» — список зафиксировать
   в миграции константой) заводится с `enabled = 0`. Иначе «Градус» вырежет слово «градус»
   из описания условий, а «Аметист» — название объекта. Включает такие правила человек.
5. Замена выполняется по границе слова, без учёта регистра, и **никогда не трогает
   подстроки внутри токенов, начинающихся с `http://` или `https://`** — это существующее и
   обязательное к сохранению поведение клиентского `scrub()`. Все сработавшие значения
   складываются в `removed`.
6. `registry/public_projection.py`:
   - константа `PUBLIC_FIELDS` — явный белый список полей позиции, которые вообще могут
     уехать наружу. В него **не входят**: `counterparty`, `counterparty_raw`, `object_name`,
     `object_address`, `source`, любые `*_raw`, все `MANAGER_FIELDS`
     (`models.py:94-106`), ставка рекрутера (она не в `positions`, а считается
     `rates.resolve`, `navigator_api.py:363-366` — в проекцию не попадает никогда),
     `ext_title`, `ext_ref`, `legacy_id`, идентификаторы заявок;
   - `build(conn, position_id) -> dict` — чистая функция: читает позицию, отбирает
     `PUBLIC_FIELDS`, подставляет `employer_alias`, применяет стоп-словарь, считает `hash`;
   - `save(conn, position_id, projection)` — upsert в `position_public`;
   - `stale(conn, limit)` — позиции, у которых `positions.updated_at` новее `built_from`
     либо проекции нет вовсе.
7. `position_public` **не** наполняется миграцией для всех позиций: сборка идёт по мере
   надобности и фоновой задачей, иначе миграция превратится в многочасовой проход. Миграция
   создаёт таблицу пустой; первичное наполнение — командой `scripts/build_public.py
   [--limit N] [--all]`.
8. Обратимость: `DROP TABLE position_public; DROP TABLE public_stopwords;` — полная,
   клиентский `scrub()` продолжает работать и остаётся вторым рубежом.
9. Размер: `position_public` — по строке на позицию с текстом карточки, порядка 1–3 КБ на
   строку; при 20 000 активных позиций это десятки мегабайт. Отсюда правило собирать
   проекцию только для активных позиций и удалять её при деактивации (тем же приёмом, что
   в DB-03: триггер `AFTER UPDATE OF is_active ... WHEN new.is_active = 0`).

**Экраны и компоненты.** `registry/db.py` (миграция), новый
`registry/public_projection.py`, новый `scripts/build_public.py`, таблицы `position_public`,
`public_stopwords`, `positions`, `counterparties`, `objects`;
`templates/navigator.html` (`scrub()` и модалка «Текст для кандидата» — переводятся на
серверные данные в направлении интерфейса), `navigator_api.py` (подключение проекции —
направление API). `tests/test_public_projection.py`.

**Зависимости.** DB-01, DB-02, DB-04, DB-05. Блокируется вопросом **C5** (кто заводит и
подтверждает публичный алиас контрагента) — от ответа зависит, что попадает в
`employer_alias` и можно ли отдавать позицию наружу без алиаса. Косвенно — **B1**.
Публичный роут `/api/v1/*` и открытие экрана наружу — направление API, начинать его
до готовности этой задачи запрещено (см. критический путь в `docs/AS-IS-VS-TO-BE.md`, п. 4).

**Критерии готовности.**
- [ ] `PUBLIC_FIELDS` объявлен явным списком; тест проверяет, что ни одно поле из `MANAGER_FIELDS`, ни одно `*_raw`, ни `counterparty`, ни `object_name`, ни `object_address`, ни `source` в него не входят.
- [ ] Тест: `build()` для позиции с известными контрагентом, объектом и источником не содержит ни одной из этих строк ни в `body`, ни в `fields`.
- [ ] Тест: ссылка `https://disk.yandex.ru/d/...` в тексте остаётся целой после применения стоп-словаря.
- [ ] Тест: стоп-слово «Градус» не вырезает слово «градус» из фразы «до 30 градусов» (правило границы слова и правило `enabled=0` для конфликтных значений).
- [ ] После миграции все правила короче четырёх символов имеют `enabled = 0`.
- [ ] `removed` содержит ровно те значения, которые были заменены, и пуст, если не заменено ничего.
- [ ] `stale()` возвращает позицию после изменения любого её поля приёмом.
- [ ] Деактивация позиции удаляет её строку из `position_public` (триггер).
- [ ] Измерен и записан размер `position_public` при наполнении на 1000 позиций — оценка полного наполнения приведена в отчёте.

**Приоритет.** P2.

**Риски.** Стоп-словарь — не гарантия: он вырезает известные названия, но не выдумки парсера
и не название контрагента, написанное иначе, чем в справочнике. Настоящая защита — белый
список `PUBLIC_FIELDS`, стоп-словарь только подчищает свободный текст; если поменять эти
роли местами, наружу утечёт всё, что не попало в словарь. Двойная сборка (сервер и
клиентский `scrub()`) на переходный период даст расхождение текстов — пока оба живы, тексты
надо сверять на выборке. Слишком агрессивный словарь испортит описания, слишком мягкий —
раскроет заказчика; поэтому `enabled=0` по умолчанию для сомнительных значений.

---

### DB-13. Таблицы переписки: `outreach_threads`, `outreach_messages`, `outreach_questions`

**Что нужно сделать.** Подготовить хранилище для робота-запросчика. Сегодня исходящих
сообщений нет вообще: Telegram работает только на чтение (Telethon userbot, каналы Градус
и AAA+), `TELEGRAM_BOT_TOKEN` читается один раз, чтобы показать кнопку
(`navigator_api.py:771`), тексты запросов собираются на фронте
(`templates/navigator.html`), а все кнопки «Отправить» — тосты-заглушки. Миграция создаёт
три таблицы и модуль `registry/outreach.py`; отправка, вебхук, разбор ответов и очередь —
направление Telegram-робота.

**Как должна работать логика.**
1. `outreach_threads`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `counterparty_id INTEGER NOT NULL REFERENCES counterparties(id) ON DELETE CASCADE`,
   `chat_id TEXT NOT NULL DEFAULT ''`, `thread_id TEXT NOT NULL DEFAULT ''`,
   `status TEXT NOT NULL DEFAULT 'idle'` (`idle` | `waiting` | `answered` | `dead`),
   `attempt INTEGER NOT NULL DEFAULT 0`, `max_tries INTEGER NOT NULL DEFAULT 3`,
   `last_sent_at TEXT`, `last_reply_at TEXT`, `next_retry_at TEXT`,
   `greeted_on TEXT NOT NULL DEFAULT ''` (дата последнего приветствия, чтобы не здороваться
   дважды за день), `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`.
   `UNIQUE(counterparty_id, chat_id, thread_id)`. Индекс
   `idx_outreach_threads_due(status, next_retry_at)`.
2. `outreach_messages`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `thread_id INTEGER NOT NULL REFERENCES outreach_threads(id) ON DELETE CASCADE`,
   `direction TEXT NOT NULL` (`out` | `in`),
   `kind TEXT NOT NULL DEFAULT 'request'` (`request` | `follow_up` | `reply` | `service`),
   `body TEXT NOT NULL DEFAULT ''`, `tg_message_id TEXT NOT NULL DEFAULT ''`,
   `draft INTEGER NOT NULL DEFAULT 1` (черновик, ещё не отправлен — режим «формирует
   система, отправляет человек кнопкой»), `approved_by TEXT NOT NULL DEFAULT ''`,
   `approved_at TEXT`, `sent_at TEXT`, `delivered INTEGER`,
   `error TEXT NOT NULL DEFAULT ''`, `created_at TEXT NOT NULL`.
   Индексы: `idx_outreach_messages_thread(thread_id, created_at)`,
   `idx_outreach_messages_draft(draft, created_at)`.
   Ограничение длины тела не в схеме, а в коде: лимит Telegram 4096 символов уже проверяется
   на фронте (`TG_LIMIT`, `templates/navigator.html`).
3. `outreach_questions`: `id INTEGER PRIMARY KEY AUTOINCREMENT`,
   `message_id INTEGER NOT NULL REFERENCES outreach_messages(id) ON DELETE CASCADE`,
   `position_id TEXT NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE`,
   `field TEXT NOT NULL`, `conflict_id INTEGER REFERENCES field_conflicts(id) ON DELETE SET NULL`,
   `status TEXT NOT NULL DEFAULT 'asked'` (`asked` | `answered` | `skipped`),
   `asked_at TEXT NOT NULL`, `answered_at TEXT`, `answer TEXT NOT NULL DEFAULT ''`,
   `applied INTEGER NOT NULL DEFAULT 0` (ответ применён к позиции).
   `UNIQUE(message_id, position_id, field)`. Индекс
   `idx_outreach_questions_open(position_id, status)`.
4. Из существующих текстовых колонок **не мигрирует ничего**: переписки нет, истории нет,
   таблицы создаются пустыми. Заполнять их задним числом из логов запрещено.
5. Правило «уже в запросе, повторно не спрашиваем» реализуется запросом
   `SELECT field FROM outreach_questions WHERE position_id = ? AND status = 'asked'` —
   функция `open_questions(conn, position_id)`. Это единственное правило, которое обязано
   быть в слое данных: без него робот задаст один вопрос трижды.
6. Неоднозначные случаи: контрагент без `chat_id` — строка `outreach_threads` не создаётся,
   а `registry/outreach.py:ensure_thread` возвращает None и причину `'чат не настроен'`;
   один контрагент с несколькими чатами — несколько строк, выбор чата делает вызывающая
   сторона; ответ пришёл в чат, где нет ни одного открытого вопроса — сообщение
   сохраняется с `direction='in'`, `kind='reply'`, но ни к какому вопросу не привязывается.
7. `registry/outreach.py`: `ensure_thread(conn, counterparty_id)`,
   `create_draft(conn, thread_id, body, questions, author)` (одной транзакцией — сообщение
   и его вопросы), `approve(conn, message_id, author)`, `mark_sent(conn, message_id,
   tg_message_id)`, `mark_failed(conn, message_id, error)`, `record_reply(conn, thread_id,
   body, tg_message_id)`, `answer_question(conn, question_id, answer, author)`,
   `due_threads(conn, now)`, `open_questions(conn, position_id)`.
8. Ретеншен: сообщения и вопросы старше 12 месяцев удаляются командой из DB-14;
   `outreach_threads` не удаляются никогда (их мало, и в них состояние).
9. Обратимость: `DROP TABLE` трёх таблиц в порядке questions → messages → threads — полная.
10. Размер: при сотне запросов в сутки — порядка 40 000 сообщений в год, единицы-десятки
    мегабайт; основной вклад даёт `body`.

**Экраны и компоненты.** `registry/db.py` (миграция), новый `registry/outreach.py`,
таблицы `outreach_threads`, `outreach_messages`, `outreach_questions`, `counterparties`,
`counterparty_settings` (`chat_id`, `thread_id`, `send_time`, `max_tries`,
`outreach_enabled`, `bot_env` — из DB-04), `field_conflicts` (из DB-08), `positions`.
Потребители — направление Telegram-робота (`app.py`, вебхук, очередь) и направление
интерфейса (блок «Запросы заказчикам» в `templates/navigator.html`).
`tests/test_outreach.py`.

**Зависимости.** DB-01, DB-02, DB-04, DB-08 (внешний ключ на `field_conflicts`).
Блокируется вопросами **C3** (согласие контрагентов на автоматические сообщения; вариант по
умолчанию — «сначала черновик, отправляет человек», именно поэтому в схеме есть `draft`
и `approved_by`) и **B1** (кому адресуется сообщение — контрагенту или источнику).
Сам транспорт (Bot API, вебхук, очередь, ретраи) в эту задачу не входит.

**Критерии готовности.**
- [ ] Все три таблицы созданы, после миграции пусты.
- [ ] `create_draft` одной транзакцией создаёт сообщение с `draft=1` и все его вопросы; при ошибке на вопросах сообщение тоже не создаётся (тест на откате).
- [ ] Повторное создание того же вопроса в том же сообщении нарушает `UNIQUE` и не создаёт дубля.
- [ ] `open_questions` не возвращает поле, по которому вопрос уже `answered` или `skipped`.
- [ ] Удаление позиции удаляет её вопросы и не удаляет сообщение и тред.
- [ ] Удаление контрагента каскадно удаляет тред, его сообщения и вопросы.
- [ ] `ensure_thread` для контрагента без `chat_id` возвращает None и не создаёт строк.
- [ ] `approve` без предварительного `create_draft` невозможен (тест на несуществующий id).
- [ ] Ни одна функция модуля не отправляет ничего в сеть — модуль не импортирует Telegram-клиент (проверка тестом).

**Приоритет.** P2.

**Риски.** Схема пишется под робота, которого ещё нет: часть полей может оказаться лишней
или недостающей, и первая же реальная интеграция потребует правки — поэтому таблицы
создаются пустыми и их изменение до запуска робота дёшево. Хранение тела сообщений — это
хранение переписки с контрагентами: доступ к ней должен закрываться ролями (DB-09 и
направление безопасности), иначе получится общедоступный архив деловой переписки.
Если C3 будет решён в пользу немедленной автоотправки, поле `draft` останется мёртвым —
это дешевле, чем добавлять его потом.

---

### DB-14. Ретеншен `recruiter_rate_history` и политика хранения журналов

**Что нужно сделать.** Ограничить рост журнальных таблиц и зафиксировать, какая из них
чистится, а какая нет. `recruiter_rate_history` (`registry/db.py:291-307`) растёт быстрее
всего: на каждое сохранение сетки `app.py:432-435` сначала зовёт `rates.clear_scope`
(строка `action='delete'` на каждое снятое правило, `registry/rates.py:250-268`), затем
`rates.save_rules` (строка `action='set'` на каждое сохранённое, `rates.py:210-235`), то
есть одно нажатие «Сохранить» на лестнице из четырёх ступеней пишет до восьми строк, а на
экран уходит только `history(limit=30)` (`navigator_api.py:657-663`). Нужна команда
`scripts/retention.py` и записанная политика.

**Как должна работать логика.**
1. Политика по таблицам, зафиксировать в документации и в коде команды:
   - `recruiter_rate_history` — хранить 365 суток **и** всегда оставлять последние 200
     строк на каждую область `(source, client, vacancy)`, что бы ни говорил срок;
   - `sync_runs` (DB-10) — 180 суток;
   - `outreach_messages` и `outreach_questions` (DB-13) — 365 суток;
   - `request_revisions` — не чистится (архив исходных текстов заявок, по нему
     восстанавливается разбор);
   - `position_history` — **не чистится никогда**: это журнал изменений, в том числе ручных
     (DB-09);
   - `search_index` — чистится триггерами (DB-03), в ретеншен не входит;
   - `position_public` (DB-12) — удаляется при деактивации позиции, в ретеншен не входит.
2. `scripts/retention.py rates [--keep-days 365] [--keep-per-scope 200] [--dry-run]`:
   считает, сколько строк подпадает под удаление, печатает разбивку по областям, при
   отсутствии `--dry-run` удаляет их одной транзакцией и печатает итог.
3. Отдельный необязательный режим `--collapse-rewrites`: схлопывает пары
   «`delete` и следом `set` в пределах двух секунд по той же области, где все поля правила
   (`amount`, `note`, `payout`, `valid_from`, `valid_to`) совпадают» в одну строку с
   `action='set'`. Это ровно тот шум, который порождает связка `clear_scope` + `save_rules`
   при сохранении неизменившегося правила. Режим выключен по умолчанию, потому что
   переписывает журнал; в `--dry-run` он обязателен к предварительному прогону.
4. Команда `scripts/retention.py all [--dry-run]` — прогон по всем таблицам политики.
5. Перед любым удалением команда требует свежую копию: проверяет через модуль из DB-02, что
   последняя копия не старше 24 часов, иначе отказывается работать без флага
   `--i-know-what-i-am-doing`.
6. После удаления — `PRAGMA incremental_vacuum` либо (по флагу `--vacuum`) полный `VACUUM`.
   Ограничение записать прямо в справку команды: `VACUUM` требует свободного места
   примерно в размер базы и переписывает файл целиком, на боевой базе запускать только в
   окно обслуживания и после копии.
7. Периодичность: ежемесячная задача APScheduler `monthly_retention` (первое число, 05:00
   МСК, после ночной копии из DB-02) в режиме без `--collapse-rewrites` и без `--vacuum`.
8. Из текстовых колонок ничего не мигрирует; схему задача не меняет вовсе — только удаляет
   строки.
9. Обратимость: удалённые строки журналов невосстановимы иначе как из копии (DB-02) —
   отсюда пункт 5.

**Экраны и компоненты.** Новый `scripts/retention.py`, `app.py` (`JOBS` — задача
`monthly_retention`), таблицы `recruiter_rate_history`, `sync_runs`, `outreach_messages`,
`outreach_questions`; `registry/rates.py` (`history`, `_log` — только чтение правил),
`navigator_api.py:657-663` (потребитель истории ставок), документ эксплуатации,
`tests/test_retention.py`.

**Зависимости.** DB-02 (проверка свежей копии — обязательна). DB-10 и DB-13 нужны только
для соответствующих режимов команды: пока таблиц нет, режимы пропускаются с сообщением
«таблица отсутствует», а не падают. Вопросами из `docs/OPEN-QUESTIONS.md` не заблокирована;
сроки хранения (365/180 суток) — предложение по умолчанию, при наличии требований по
хранению коммерческих условий их подтверждает заказчик.

**Критерии готовности.**
- [ ] `python -m scripts.retention rates --dry-run` печатает число строк к удалению и разбивку по областям, базу не меняет.
- [ ] После прогона в каждой области `(source, client, vacancy)` остаётся не меньше 200 строк, даже если все они старше 365 суток (тест на синтетических данных).
- [ ] Строки моложе 365 суток не удаляются ни при каких условиях (тест).
- [ ] `--collapse-rewrites --dry-run` на данных, где сохранение не меняло правило, показывает схлопывание; на данных, где сумма менялась, не схлопывает ничего (тест).
- [ ] Команда отказывается удалять, если последняя копия старше 24 часов, и печатает, как снять копию.
- [ ] Экран ставок после прогона показывает те же 30 последних записей (`history(limit=30)`), что и до него, если ничего из этих 30 не подпадало под удаление.
- [ ] Задача `monthly_retention` зарегистрирована в планировщике, видна в `GET /jobs`, время не пересекается с ночной копией и утренним прогоном.
- [ ] Размер файла базы до и после прогона с `--vacuum` измерен и записан.

**Приоритет.** P3.

**Риски.** Ретеншен — это удаление данных, и ошибка в условии отбора уносит историю
коммерческих условий, которую восстановить неоткуда, кроме копии; отсюда обязательные
`--dry-run`, проверка свежести копии и правило «минимум 200 строк на область».
`--collapse-rewrites` переписывает журнал — при споре с контрагентом о том, когда именно
поменялась ставка, схлопнутая запись даст неполную картину; поэтому режим выключен по
умолчанию. `VACUUM` на большой базе блокирует запись на всё время работы — запуск вне
окна обслуживания оборвёт прогон источников.
