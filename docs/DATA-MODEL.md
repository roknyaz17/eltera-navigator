# Структура данных

Фактическая схема SQLite по состоянию на последнюю миграцию (`PRAGMA user_version = 6`). Всё ниже — «как есть», по коду; ссылки вида `файл.py:строка` указывают на источник. Места, где описано «как должно быть», помечены явно.

## 1. Где живёт база и как открывается

- Путь: `REGISTRY_DB_PATH`, по умолчанию `data/registry.db` — `registry/db.py:25`.
- Соединение открывается **на операцию**, контекст-менеджером `db.connect()` — `registry/db.py:365-383`: commit при штатном выходе, rollback при исключении, `close()` в `finally`. Пула нет.
- PRAGMA при каждом открытии (`registry/db.py:386-393`): `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=30000`, `synchronous=NORMAL`; сам `sqlite3.connect(..., timeout=30.0, isolation_level="DEFERRED")`, `row_factory = sqlite3.Row`.
- Параметр `readonly` у `connect()` объявлен (`registry/db.py:366`), но в теле не используется — режима «только чтение» нет.

## 2. Механика версионирования

Миграции — список строк SQL `MIGRATIONS` (`registry/db.py:39`), дополняемый шестью `.append(...)`. Версия схемы хранится в `PRAGMA user_version`.

`_ensure_schema(path)` (`registry/db.py:396-416`): если путь уже инициализирован в этом процессе (`_initialized`, `registry/db.py:28`) — выход; иначе берётся `threading.Lock` (`registry/db.py:27`), создаётся каталог БД (`registry/db.py:404`), читается `PRAGMA user_version` (`registry/db.py:407`), и для каждого индекса от `current` до `len(MIGRATIONS)-1` выполняется `conn.executescript(MIGRATIONS[version])` + `PRAGMA user_version={version+1}` (`registry/db.py:411-412`), один общий `commit()` после цикла (`registry/db.py:413`). Лог каждого шага: `[registry] применяю миграцию v{N}` (`registry/db.py:410`). `reset_schema_cache()` (`registry/db.py:419-421`) чистит `_initialized` — нужен тестам.

Следствия: накат идёт **только вперёд**, обратных скриптов и даунгрейда нет; пустая база проходит все шесть миграций за один вызов; **номер версии = позиция в списке + 1**, поэтому вставка элемента в середину сдвинет версии всех последующих и на уже накатанной базе даст рассинхрон.

## 3. Шесть миграций по порядку

| v | Строки | Что делает |
|---|---|---|
| v1 | `registry/db.py:42-176` | Базовая схема: 8 таблиц + FTS5 |
| v2 | `registry/db.py:184-196` | `recruiter_rates` — одна ставка на контрагента |
| v3 | `registry/db.py:204-223` | `disk_projects` — индекс папок Яндекс.Диска |
| v4 | `registry/db.py:232-251` | `position_kb` + DML «ВахтаПро → Градус» |
| v5 | `registry/db.py:270-327` | `recruiter_rate_rules`, `recruiter_rate_history`, перенос данных, `DROP TABLE recruiter_rates` |
| v6 | `registry/db.py:341-362` | КПК → КНК; только DML, схему не меняет |

**v1** создаёт `requests`, `request_revisions`, `positions`, `request_positions`, `position_history`, `dictionaries`, `id_counters` и виртуальную FTS5-таблицу `search_index` со всеми индексами. Единственная миграция, в которой участвует генерация DDL из `models.py`: колонки позиции подставляются вызовом `_position_columns_ddl()` внутрь f-строки (`registry/db.py:103`). Все остальные миграции — обычные строки без подстановок.

**v2** заводит `recruiter_rates`: `counterparty TEXT PK`, `kind` (fixed|percent), `value REAL`, `base_amount INTEGER`, `base`, `rule`, `stage`, `guar`, `updated_at`. Индексов нет. Таблица живёт только между v2 и v5.

**v3** заводит `disk_projects` с PK по пути папки и индексом `idx_disk_projects_source`.

**v4** заводит `position_kb` (PK = `position_id`, FK на `positions` с `ON DELETE CASCADE`) и выполняет DML: `UPDATE requests SET source_name='Градус' WHERE source='vahtapro' AND source_name='ВахтаПро'` (`registry/db.py:249-250`). Ключ источника `vahtapro` намеренно не трогается — он в `source` у всех заявок и в метках метрик.

**v5** заменяет одну ставку на контрагента правилами с областью действия: создаёт `recruiter_rate_rules` и `recruiter_rate_history`, переносит содержимое старой таблицы (`registry/db.py:311-324`) и делает `DROP TABLE recruiter_rates` (`registry/db.py:326`). При переносе `source` вычисляется подзапросом по первой позиции того же контрагента, для `kind='percent'` сумма считается как `ROUND(base_amount * value / 100)`, `note` склеивается из `base + stage + guar`, `payout` берётся из `rule`, автор — строка `'перенос из старой таблицы'`.

**v6** — шесть DML-операторов (`registry/db.py:342-361`): `requests.source_name`; `requests.counterparty` и `counterparty_raw`; `positions.counterparty` и `counterparty_raw` для `source='kpk'`; `dictionaries.canonical` для `kind='counterparty'`; `INSERT OR IGNORE` алиаса `('counterparty','кнк','КНК',confirmed=1)`. Комментарий `registry/db.py:339-340` требует после этой миграции руками прогнать `python scripts/renormalize.py` — fingerprint считается в том числе по контрагенту. **Сама миграция пересчёт не делает**, это ручной шаг.

## 4. Схема на v6: 11 обычных таблиц + одна FTS5

Полный список объектов базы: `requests`, `request_revisions`, `positions`, `request_positions`, `position_history`, `dictionaries`, `id_counters`, `disk_projects`, `position_kb`, `recruiter_rate_rules`, `recruiter_rate_history` — одиннадцать обычных таблиц, плюс виртуальная FTS5-таблица `search_index`. `recruiter_rates` существовала только между v2 и v5 и удалена.

Связи (внешние ключи объявлены, `PRAGMA foreign_keys=ON` включён при каждом открытии):

```
requests 1—N request_revisions        (ON DELETE CASCADE)
requests N—M positions                (через request_positions, обе стороны CASCADE)
requests 1—N positions.first_request_id / last_request_id   (FK без CASCADE)
positions 1—N position_history        (ON DELETE CASCADE)
positions 1—1 position_kb             (ON DELETE CASCADE)
positions 1—1 search_index            (по position_id, FK нет — FTS5)
dictionaries, id_counters, disk_projects,
recruiter_rate_rules, recruiter_rate_history — без FK ни на что
```

Все временные метки — строки ISO-8601 с точностью до секунды (`datetime.now().isoformat(timespec="seconds")`, `registry/ingest.py:57-58`), в локальном времени процесса. Типа `DATETIME` в схеме нет, сравнение дат идёт лексикографически (`registry/queries.py` — фильтры `date_from` / `date_to`).

Жизненный цикл позиции выражен одной колонкой. `is_active = 1` ставится при любом подтверждении позиции прогоном (`registry/ingest.py:461`, `:477`); позиции источника, которых нет в текущем снимке, гасятся `is_active = 0` в `_deactivate_stale` (`registry/ingest.py:598-618`). Строки при этом **не удаляются** — из реестра ничего не пропадает, только перестаёт быть активным.

Денормализация в схеме есть и сделана сознательно. JSON лежит текстом в трёх местах: `requests.raw_payload` (машинное представление документа источника), `disk_projects.docs` и `disk_projects.albums`. Ограничений `json_valid()`, генерируемых колонок и индексов по JSON нет — разбор целиком на стороне Python (`project_kb.py:749-750`). `raw_text` заявки дублируется в `request_revisions` при каждой смене содержимого и ещё раз попадает в тело `search_index`; это цена того, что «сверка с исходником» и поиск работают задним числом.

### requests — `registry/db.py:43-73`

`request_id TEXT PK`, `year INTEGER NOT NULL`, `seq INTEGER NOT NULL`, `source TEXT NOT NULL`, `source_ref TEXT NOT NULL`, `source_name TEXT NOT NULL DEFAULT ''`, `source_url TEXT NOT NULL DEFAULT ''`, `counterparty TEXT NOT NULL DEFAULT ''`, `counterparty_raw TEXT NOT NULL DEFAULT ''`, `raw_text TEXT NOT NULL DEFAULT ''`, `raw_payload TEXT NOT NULL DEFAULT ''` (JSON строкой), `content_hash TEXT NOT NULL`, `revision INTEGER NOT NULL DEFAULT 1`, `received_at TEXT`, `first_seen_at TEXT NOT NULL`, `last_seen_at TEXT NOT NULL`, `parsed_at TEXT`, `parse_status TEXT NOT NULL DEFAULT 'pending'`, `parse_error TEXT NOT NULL DEFAULT ''`, `llm_model TEXT NOT NULL DEFAULT ''`, `llm_tokens_in INTEGER NOT NULL DEFAULT 0`, `llm_tokens_out INTEGER NOT NULL DEFAULT 0`.

UNIQUE: `(source, source_ref)`, `(year, seq)`. Индексы: `idx_requests_source(source)`, `idx_requests_counterparty(counterparty)`, `idx_requests_last_seen(last_seen_at)`, `idx_requests_status(parse_status)`.

Тождество заявки определяет `source_ref`, а не содержимое (`registry/ingest.py:159-162`); `content_hash` — детектор изменений, не идентификатор (`registry/models.py:190-211`).

### request_revisions — `registry/db.py:77-87`

`id INTEGER PK AUTOINCREMENT`, `request_id TEXT NOT NULL REFERENCES requests(request_id) ON DELETE CASCADE`, `revision INTEGER NOT NULL`, `raw_text TEXT NOT NULL DEFAULT ''`, `raw_payload TEXT NOT NULL DEFAULT ''`, `content_hash TEXT NOT NULL`, `replaced_at TEXT NOT NULL`. Индекс `idx_revisions_request(request_id)`. Пишет `_archive_revision` (`registry/ingest.py:223-235`), читает `queries.revisions_of_request` (`registry/queries.py:204-208`).

### positions — `registry/db.py:89-116`

Служебные колонки (11): `position_id TEXT PK`, `seq INTEGER NOT NULL`, `first_request_id TEXT NOT NULL REFERENCES requests(request_id)`, `last_request_id TEXT NOT NULL REFERENCES requests(request_id)`, `source TEXT NOT NULL`, `fingerprint TEXT NOT NULL`, `legacy_id TEXT`, `is_active INTEGER NOT NULL DEFAULT 1`, `first_seen_at TEXT NOT NULL`, `last_seen_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`.

Дальше подставляется блок `{_position_columns_ddl()}` (`registry/db.py:103`) — 53 колонки `DATA_FIELDS` и 8 колонок `MANAGER_FIELDS`, типы через `models.sql_type()`. **Итого 72 колонки.**

UNIQUE `(source, fingerprint)`. Индексы: `idx_positions_request(last_request_id)`, `idx_positions_active(is_active)`, `idx_positions_source(source)`, `idx_positions_city(city)`, `idx_positions_vacancy(vacancy_name)`, `idx_positions_counterparty(counterparty)`, `idx_positions_rate(shift_rate)`, `idx_positions_legacy(legacy_id)`, `idx_positions_identity(source, counterparty, city, vacancy_name, object_name)` — последний под запасной поиск `_rescue_match` (`registry/ingest.py:485+`).

`legacy_id` — прежний `vacancy_id` из Google Sheets, заполняется только `scripts/migrate_from_sheets.py:235-240`; в рабочем контуре не читается.

### request_positions — `registry/db.py:122-128`

`request_id TEXT NOT NULL REFERENCES requests(...) ON DELETE CASCADE`, `position_id TEXT NOT NULL REFERENCES positions(...) ON DELETE CASCADE`, `PRIMARY KEY (request_id, position_id)`; индекс `idx_request_positions_position(position_id)`. Связь многие-ко-многим: позиция живёт дольше одной заявки, завтрашний снимок принесёт её снова. Перезаписывается целиком на каждый разбор: `DELETE` + `INSERT OR IGNORE` (`registry/ingest.py:376-379`).

### position_history — `registry/db.py:131-142`

`id INTEGER PK AUTOINCREMENT`, `position_id TEXT NOT NULL REFERENCES positions(...) ON DELETE CASCADE`, `request_id TEXT NOT NULL` (без FK), `field TEXT NOT NULL`, `old_value TEXT`, `new_value TEXT`, `changed_at TEXT NOT NULL`. Индексы `idx_history_position(position_id)`, `idx_history_changed(changed_at)`. Пишется только на реальные диффы `DATA_FIELDS` (`registry/ingest.py:465-475`); правки менеджера сюда **не попадают**.

### dictionaries — `registry/db.py:147-160`

`kind TEXT NOT NULL`, `alias TEXT NOT NULL`, `canonical TEXT NOT NULL`, `confirmed INTEGER NOT NULL DEFAULT 0`, `hits INTEGER NOT NULL DEFAULT 0`, `note TEXT NOT NULL DEFAULT ''`, `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`, `PRIMARY KEY (kind, alias)`. Индексы `idx_dict_kind_confirmed(kind, confirmed)`, `idx_dict_canonical(kind, canonical)`.

Девять видов (`registry/dictionaries.py:17-36`): `job_title`, `city`, `city_region`, `region`, `counterparty`, `work_format`, `vacancy_category`, `schedule_pattern`, `citizenship`. `alias` хранится уже приведённым через `norm_key` (`registry/dictionaries.py:52-64`: нижний регистр, ё→е, пунктуация в пробелы), поэтому «Комплектовщик», «комплектовщик» и «Комплектовщик.» — один ключ. `confirmed = 0` — очередь на подтверждение в `/registry/dictionaries`; **неподтверждённый алиас нормализация не применяет**: незнакомое написание сохраняется как есть, а не схлопывается в похожее (`registry/dictionaries.py:1-9`). `hits` — счётчик встреч, по нему очередь сортируется.

### id_counters — `registry/db.py:162-165`

`scope TEXT PRIMARY KEY`, `value INTEGER NOT NULL DEFAULT 0`. Скоупы: `request:{год}` и `position:{request_id}` (`registry/ids.py:62`, `:68`).

### search_index (FTS5) — `registry/db.py:170-175`

```
CREATE VIRTUAL TABLE search_index USING fts5(
    position_id UNINDEXED, request_id UNINDEXED, body,
    tokenize = "unicode61 remove_diacritics 2");
```

Не external content: в `body` склеиваются исходный текст заявки и 17 нормализованных полей позиции (`SEARCHABLE_FIELDS`, `registry/ingest.py:34-52`). Пишет `_reindex` (`registry/ingest.py:565-582`, DELETE + INSERT по `position_id`) и `scripts/renormalize.py:128-131`. Читает `_apply_filters` через `MATCH` (`registry/queries.py:117-118`); пользовательский ввод очищается в `fts_query` (`registry/queries.py:33-45`) — спецсимволы FTS5 выбрасываются, последнее слово ищется по префиксу.

Оставшиеся четыре таблицы — `disk_projects`, `position_kb`, `recruiter_rate_rules`, `recruiter_rate_history` — разобраны в §7.

## 5. Поля позиции (`registry/models.py`)

`models.py` — единственное место, где перечислены поля позиции; DDL, нормализация, экспорт и CSV берут списки оттуда (`registry/models.py:1-6`).

**TEXT_FIELDS, 21** (`registry/models.py:21-43`): `counterparty` (контрагент после справочника), `vacancy_name` (должность), `vacancy_category` (укрупнённая категория), `city`, `region`, `object_name` (объект/склад), `object_address`, `work_format` (вахта / город / смена), `shift_type` (день/ночь), `schedule` (график как написано), `work_pattern` (шаблон графика), `duties`, `requirements`, `gender`, `citizenship_requirements`, `housing_conditions`, `medical_book_payer` (кто платит за медкнижку), `transport_terms`, `advantages`, `risks`, `sb_policy` (политика СБ по судимостям).

**INT_FIELDS, 13** (`registry/models.py:45-59`): `min_shifts` (вахта от N смен), `shift_rate` (ставка за смену), `hourly_rate`, `age_from`, `age_to`, `need_men`, `need_women`, `need_couples`, `need_total` (потребность), `housing_deduction`, `meals_deduction`, `meals_times_per_day`, `uniform_deduction` (удержания и кратность питания).

**REAL_FIELDS, 1** (`registry/models.py:61-63`): `shift_hours` — часов в смене.

**BOOL_FIELDS, 11** (`registry/models.py:65-77`), хранятся как `INTEGER`: `requires_tsd`, `housing_available`, `housing_free`, `meals_available`, `meals_free`, `medical_book_required`, `can_start_without_medical_book`, `uniform_available`, `uniform_free`, `transport_paid`, `transport_fully_paid`.

`DATA_FIELDS = TEXT + INT + REAL + BOOL + RAW` = **53 поля** (`registry/models.py:109`). `sql_type()` (`registry/models.py:123-132`): INT/BOOL → `INTEGER`, REAL → `REAL`, `market_rate` → `INTEGER`, `market_deviation` → `REAL`, остальное → `TEXT`.

### Правило NULL ≠ 0 ≠ «нет»

Зафиксировано комментарием `registry/models.py:14-19`. `NULL` в числовой колонке означает «в заявке не указано» и обязан доехать до интерфейса именно так. Ноль — это ноль, пустая строка — это пусто, и ни то, ни другое не равно «неизвестно». Именно на смешении этих трёх состояний ломался прежний `need_total = 0 + 0 + 0`.

Механика, которая правило поддерживает:

- нормализация стартует со словаря `{name: None for name in DATA_FIELDS}` (`registry/normalize.py:396`) — по умолчанию всё «неизвестно», а не пусто;
- при обновлении позиции пустое новое значение **не затирает** известное старое: `if new is None: continue` (`registry/ingest.py:447-449`) — в снимке источника поле могло просто не повториться;
- фильтр «есть пробелы» и метрика заполненности считают ровно `IS NULL` (`registry/queries.py:314`), по списку `KEY_FIELDS` (`registry/queries.py:26-28`: `vacancy_name`, `city`, `shift_rate`, `need_total`, `schedule`, `counterparty`).

### RAW_FIELDS

Семь полей (`registry/models.py:82-90`): `counterparty_raw`, `vacancy_name_raw`, `city_raw`, `region_raw`, `schedule_raw`, `shift_rate_raw`, `requirements_raw`. Хранят исходное, ненормализованное значение рядом с каноническим — для панели «Как пришло / Как распозналось» и для поиска: шесть из них входят в `SEARCHABLE_FIELDS` (`registry/ingest.py:34-52`), поэтому позиция находится по формулировке контрагента, даже если справочник её переписал. Формально это часть `DATA_FIELDS`, то есть импорт их перезаписывает наравне с остальными.

### MANAGER_FIELDS и почему импорт их не трогает

`MANAGER_TEXT_FIELDS` (`registry/models.py:94-101`): `status`, `priority`, `responsible_manager`, `recruiter_comment`, `sales_script`, `objections`. `MANAGER_NUM_FIELDS` (`registry/models.py:102-105`): `market_rate`, `market_deviation`. `MANAGER_FIELDS` — их сумма, 8 полей (`registry/models.py:106`); прежний `PROTECTED_FIELDS` из `sheets_adapter.py`.

Колонки в `positions` они получают наравне с остальными (`registry/db.py:34`), но приём данных их не видит **по построению, а не по проверке**: и INSERT новой позиции (`registry/ingest.py:432-435`), и расчёт диффов (`registry/ingest.py:445-446`) идут строго по `DATA_FIELDS`, в котором `MANAGER_FIELDS` нет. Имени менеджерского поля в SQL приёма попросту неоткуда взяться — забыть исключение невозможно.

Обратная сторона: запись менеджерских полей единственная — `queries.update_manager_fields` (`registry/queries.py:241-256`), и она отфильтровывает всё, чего нет в `MANAGER_FIELDS`. Вызывает её один POST-хендлер карточки (`app.py:898-903`), передавая только пять из восьми: `status`, `priority`, `responsible_manager`, `recruiter_comment`, `market_rate`.

### FINGERPRINT_FIELDS

Шесть полей (`registry/models.py:113-120`): `counterparty`, `city`, `vacancy_name`, `object_name`, `work_format`, `shift_type`. Берутся **уже нормализованными**, прогоняются через `norm_key` и склеиваются в md5, обрезанный до 12 символов (`registry/normalize.py:507-510`).

Это ключ склейки одной и той же позиции между прогонами, а **не** идентификатор (`registry/normalize.py:500-504`). Поиск позиции идёт по `(source, fingerprint)` (`registry/ingest.py:426-429`); если не нашлось — `_rescue_match` ищет по сути (контрагент + город + должность + объект) через `idx_positions_identity`, иначе любое изменение `work_format`/`shift_type` заводило бы позицию заново и обрывало историю.

## 6. Идентификаторы

- Заявка: `ELT-YYYY-NNNNNN` — `format_request_id` (`registry/ids.py:30-31`), `SEQ_WIDTH = 6` (`registry/ids.py:21`), префикс из `REGISTRY_ID_PREFIX`, по умолчанию `ELT` (`registry/ids.py:20`). Пример: `ELT-2026-000123`.
- Позиция: `ELT-YYYY-NNNNNN-NN` — `format_position_id` (`registry/ids.py:34-35`), `POSITION_WIDTH = 2` (`registry/ids.py:22`). Пример: `ELT-2026-000123-01`.
- Префикс позиции указывает на заявку, которая принесла её **впервые** (`registry/ids.py:8-11`); ссылка на свежую заявку живёт в `positions.last_request_id`.
- Выдача — через `id_counters` и `_bump()` (`registry/ids.py:38-56`): `INSERT ... ON CONFLICT DO NOTHING` → `UPDATE value = value + 1` → `SELECT value`. Два запроса вместо `RETURNING` — чтобы не зависеть от версии SQLite в базовом образе.
- Обратный разбор: `parse_request_id` (`registry/ids.py:72-77`), `request_id_of(position_id)` (`registry/ids.py:80-85`). После массовой заливки счётчики подтягивает `sync_counters` (`registry/ids.py:88-109`) по `MAX(seq)`.

ID заявки не меняется никогда — в отличие от прежнего `vacancy_id`, который был хэшем от содержимого и «переезжал» при правке города или должности, порождая дубль вместо обновления (`registry/ids.py:3-6`).

## 7. Четыре прикладные таблицы

### disk_projects — `registry/db.py:205-222`

**Назначение.** Локальный индекс папок контрагента на Яндекс.Диске: одна строка — один проект. Живёт в реестре, чтобы разбор заявки читал справку по проекту без сети: диск обходится своим расписанием, прогон идёт своим.

**Колонки.** `path TEXT PRIMARY KEY` (путь папки внутри публичной ссылки), `source TEXT NOT NULL DEFAULT ''` (чей диск, алиас источника), `category TEXT NOT NULL DEFAULT ''` (раздел верхнего уровня), `name TEXT NOT NULL` (имя папки как есть, с эмодзи), `title TEXT NOT NULL DEFAULT ''` (то же без эмодзи — его видит человек), `tokens TEXT NOT NULL DEFAULT ''` (нормализованные токены названия через пробел), `url TEXT NOT NULL DEFAULT ''`, `doc_text TEXT NOT NULL DEFAULT ''` (склеенный текст описаний проекта), `docs TEXT NOT NULL DEFAULT '[]'` (JSON, файлы папки), `albums TEXT NOT NULL DEFAULT '[]'` (JSON, фотоальбомы и число фото), `photos INTEGER NOT NULL DEFAULT 0`, `modified TEXT NOT NULL DEFAULT ''`, `fingerprint TEXT NOT NULL DEFAULT ''` (отпечаток содержимого папки), `indexed_at TEXT NOT NULL DEFAULT ''`. Индекс `idx_disk_projects_source(source)`.

**Кто пишет.** Только `project_kb.py`: `ProjectKB._save` — `INSERT ... ON CONFLICT(path) DO UPDATE` (`project_kb.py:705-724`); пропавшие на диске папки удаляются (`project_kb.py:598-602`). Точка входа — `refresh()` (`project_kb.py:565+`), запускается из пайплайна и скриптом `scripts/index_vahtapro_disk.py`.

**Кто читает.** `project_kb.py:232` и `:573` (загрузка индекса в `ProjectKB`), `project_kb.py:769` (`MAX(indexed_at)` для `refresh_if_stale`). Токены при чтении считаются заново из `title`, а не берутся из колонки (`project_kb.py:752-757`), чтобы правка `normalize_tokens` действовала сразу. Внутри пакета `registry` таблица не используется вообще.

### position_kb — `registry/db.py:233-245`

**Назначение.** Связь позиции с папкой проекта. Отдельная таблица, а не колонка в `positions`, потому что связь не приходит из заявки, её не редактирует менеджер и она пересчитывается при каждом обходе диска — в истории изменений позиции ей делать нечего.

**Колонки.** `position_id TEXT PRIMARY KEY REFERENCES positions(position_id) ON DELETE CASCADE`, `source TEXT NOT NULL DEFAULT ''`, `project TEXT NOT NULL DEFAULT ''` (название папки), `path TEXT NOT NULL DEFAULT ''`, `folder_url TEXT NOT NULL DEFAULT ''` (папка целиком), `photos_url TEXT NOT NULL DEFAULT ''` (куда ведёт кнопка «Фото объекта»), `photos INTEGER NOT NULL DEFAULT 0`, `score REAL NOT NULL DEFAULT 0` (уверенность сопоставления), `linked_at TEXT NOT NULL DEFAULT ''`. Индекс `idx_position_kb_source(source)`.

**Кто пишет.** Только `project_kb.link_positions` (`project_kb.py:483-503`): проходит все позиции источника, для каждой зовёт `kb.match_position`; не опознали — `DELETE FROM position_kb WHERE position_id = ?`, опознали — `INSERT ... ON CONFLICT(position_id) DO UPDATE`. Вызывается в конце `refresh()` (`project_kb.py:610`).

**Кто читает.** `navigator_api.build_payload` — `LEFT JOIN position_kb k` (`navigator_api.py:726`), и `navigator_api.media_block` (`navigator_api.py:322-345`), который собирает единственный тип материала `kind: "object_photo"`. Кнопка не показывается, если пусто `photos_url` или `photos = 0`.

### recruiter_rate_rules — `registry/db.py:271-287`

**Назначение.** Сколько платим рекрутёру. Из заявок не приходит и прийти не может — это внутренняя договорённость с контрагентом, которую руководитель переносит руками с присланной картинки (`registry/rates.py:1-22`). Не одна ставка, а правила с областью действия.

**Колонки.** `id INTEGER PK AUTOINCREMENT`, `source TEXT NOT NULL` (алиас контрагента-источника, **не** бренд заказчика — `registry/db.py:266-269`), `client TEXT NOT NULL DEFAULT ''` (объект/заказчик, `''` = все), `vacancy TEXT NOT NULL DEFAULT ''` (должность, `''` = все), `min_shifts INTEGER NOT NULL DEFAULT 0` (ступень «от N смен», 0 = без условия), `amount INTEGER NOT NULL` (рублей за кандидата), `note TEXT NOT NULL DEFAULT ''` (надбавки словами — посчитать их реестр не может), `payout TEXT NOT NULL DEFAULT ''`, `valid_from TEXT NOT NULL DEFAULT ''`, `valid_to TEXT NOT NULL DEFAULT ''`, `created_at TEXT NOT NULL DEFAULT ''`, `author TEXT NOT NULL DEFAULT ''`. **UNIQUE (source, client, vacancy, min_shifts)** — на неё опирается `ON CONFLICT` в `save_rules`. Индекс `idx_rate_rules_source(source)`.

Три способа выставления — одна и та же строка с разной областью: на всего контрагента (`client=''`, `vacancy=''`, `min_shifts=0`), лестница по сменам (`min_shifts = 15/20/30`), точечные исключения (`client='ДНС Пушкино'`, при необходимости с `vacancy`).

**Кто пишет.** `rates.save_rules` (`registry/rates.py:210-235`, UPSERT по UNIQUE), `rates.delete_rule` (`registry/rates.py:238-247`), `rates.clear_scope` (`registry/rates.py:250-268`, снос всей области при перевыставлении сетки). HTTP-точки: `POST /api/rates` и `DELETE /api/rates/{rule_id}`; плюс `scripts/recruiter_rates.py`.

**Кто читает.** `rates.load_rules` (`registry/rates.py:119-129`), дальше `rates.resolve` (`registry/rates.py:142-181`). Подбор: сначала правила, у которых `source` совпал, а `client` и `vacancy` либо пусты, либо равны запрошенным; среди них берётся максимальный `scope_rank` (`registry/rates.py:50-53`: `2*client + 1*vacancy`); затем `_pick_step` (`registry/rates.py:184-205`) выбирает ступень лестницы по `min_shifts` позиции. Ничего не нашлось — `None`, то есть «ставка не задана», а не ноль. Что считать объектом, решает `client_key` (`registry/rates.py:132-139`): `object_name` приоритетнее `counterparty`. Результат считается **на лету при рендере** (`navigator_api.py:364`) — колонки под ставку рекрутёра в `positions` нет. Просроченные правила (`valid_to` в прошлом) не прячутся, а помечаются `expired` (`registry/rates.py:63-75`). Предпросмотр «сколько позиций затронет» — `app.py:499-520`.

### recruiter_rate_history — `registry/db.py:291-307`

**Назначение.** Ставки перевыставляются еженедельно, вопрос «сколько было в июле» возникает регулярно. Пишется каждое изменение, а не только текущее состояние.

**Колонки.** `id INTEGER PK AUTOINCREMENT`, `source TEXT NOT NULL`, `client TEXT NOT NULL DEFAULT ''`, `vacancy TEXT NOT NULL DEFAULT ''`, `min_shifts INTEGER NOT NULL DEFAULT 0`, `amount INTEGER` (nullable — в отличие от `recruiter_rate_rules`), `note TEXT NOT NULL DEFAULT ''`, `payout TEXT NOT NULL DEFAULT ''`, `valid_from TEXT NOT NULL DEFAULT ''`, `valid_to TEXT NOT NULL DEFAULT ''`, `action TEXT NOT NULL DEFAULT 'set'` (значения `set` | `delete`), `changed_at TEXT NOT NULL DEFAULT ''`, `author TEXT NOT NULL DEFAULT ''`. Индекс `idx_rate_history_scope(source, client, vacancy)`.

**Кто пишет.** Только `rates._log` (`registry/rates.py:271-281`), вызываемая из `save_rules` (действие `set`) и `delete_rule` (действие `delete`). Удаления истории в коде нет.

**Кто читает.** `rates.history(conn, limit=50)` (`registry/rates.py:284-302`) — последние N записей по убыванию `id`, с человекочитаемой областью через `RateRule.scope_title`.

## 8. Чего в схеме нет

Как таблицы **отсутствуют**: `counterparties`, `objects`, `cities`, `sources`, `sync_runs`, `field_status`, `field_conflicts`, `outreach_*`, `position_media`, `position_public`, `users`, `roles`.

| Сущность целевой системы | Чем заменена сейчас |
|---|---|
| `counterparties` — карточка контрагента (чат, бот, шаблон, время отправки, обязательные поля) | Контрагент существует только как строка в `positions.counterparty` и записи `dictionaries` с `kind='counterparty'`. Настройки контрагента (моки `CPS`, `navigator/navigator.html:1384-1420`) хранить негде |
| `objects` — справочник объектов | Текстовая колонка `positions.object_name`; вида справочника под объекты в `dictionaries.KINDS` нет |
| `cities` — города с координатами | Константы в коде: `CITY_ALIASES` (`registry/geo.py:18`) и `CITY_COORDS` на 62 города (`registry/geo.py:37`). Расчёта расстояний и радиуса в коде нет вообще — только нормализация написания и отдача `lat`/`lon` во фронт (`navigator_api.py:667-684`) |
| `sources` — источники со статусом прогона | Константы `registry/sources.py` и `SOURCE_NAMES` в `pipeline.py`. Время последнего прогона, статус, число позиций и текст ошибки нигде не сохраняются |
| `sync_runs` — история прогонов | Ничего. `IngestStats` (`registry/models.py:214-246`) уходит в лог и в метрики Prometheus и после процесса не существует |
| `field_status`, `field_conflicts` — статус и конфликты по каждому полю | Ближайшее — `position_history` (диффы `DATA_FIELDS`) и `requests.parse_status` / `parse_error` на всю заявку. Пометки «конфликт», «не разобрано», «формат» по конкретному полю хранить негде |
| `outreach_*` — переписка с контрагентом (попытка, отправлено, дней молчания, спрошенные поля) | Ничего. Telegram работает **только на чтение** (Telethon-userbot, каналы Градус и AAA+, плюс `telegram_post_fetcher`); исходящих сообщений, Bot API, вебхуков и очереди дозапросов в коде нет |
| `position_media` — материалы позиции | Единственный тип `object_photo`, собираемый на лету из `position_kb` (`navigator_api.py:322-345`). У материала нет ни `title`, ни хранимой видимости; `alive` захардкожен `True`, проверки живости ссылок нет. Типы `housing_photo`, `video`, `route`, `telegram` — только метки во фронте |
| `position_public` — публичная карточка, публичный алиас контрагента | Ничего: публичного контура и алиасов в схеме нет |
| `users`, `roles` | Одна общая пара HTTP Basic из `WEB_USER` / `WEB_PASSWORD`, ролей нет. `/health`, `/metrics`, `/jobs` и mount `/static` открыты без авторизации |
| Дубли позиций | Не хранятся: `find_dupes` (`navigator_api.py:466`) считает их на лету при сборке payload |
| Ставка рекрутёра у позиции | Колонки нет, считается `rates.resolve` при рендере (`navigator_api.py:364`) |

Отдельно: полей `start_date` (дата начала) и `district` (район), которые есть в макете заказчика, в `DATA_FIELDS` нет.

## 9. Как правильно добавить поле позиции

Ключевой факт: `_position_columns_ddl()` (`registry/db.py:31-36`) генерирует DDL из `DATA_FIELDS + MANAGER_FIELDS`, но подставляется он **только внутрь миграции v1** (`registry/db.py:103`). На существующей базе с `user_version = 6` v1 никогда не выполнится повторно. Значит, добавление имени в `models.py` даёт колонку только на свежесозданной базе, а на рабочей — `OperationalError: no such column` на первом же INSERT позиции (`registry/ingest.py:432-435`).

Отсюда правило: **править существующую миграцию нельзя, нужно дописать новую.** Правка v1 разведёт схему dev и prod: у новой базы колонка появится, у накатанной — нет, и обе будут числиться версией 6.

Порядок действий:

1. Добавить имя поля в нужный список в `registry/models.py` — `TEXT_FIELDS`, `INT_FIELDS`, `REAL_FIELDS`, `BOOL_FIELDS`, `RAW_FIELDS` или, для менеджерского поля, `MANAGER_TEXT_FIELDS` / `MANAGER_NUM_FIELDS`.
2. Дописать **седьмым элементом** `MIGRATIONS.append("ALTER TABLE positions ADD COLUMN <имя> <тип>;")` в конец `registry/db.py`, вместе с нужными индексами. Тип обязан совпасть с тем, что вернёт `sql_type()` для этого имени. Порядок элементов списка менять нельзя — он и есть нумерация версий.
3. Проверить `sql_type()` (`registry/models.py:123-132`): для менеджерских числовых полей типы захардкожены по имени (`market_rate`, `market_deviation`), поэтому любое новое менеджерское числовое поле по умолчанию станет `TEXT`.
4. Заполнение поля прописать в `registry/normalize.py` — без этого оно останется `NULL` навсегда.
5. Если поле должно искаться — добавить в `SEARCHABLE_FIELDS` (`registry/ingest.py:34-52`) и прогнать `python scripts/renormalize.py`: FTS-индекс перестраивается только через `_reindex`.
6. Если поле влияет на тождество позиции — добавить в `FINGERPRINT_FIELDS` (`registry/models.py:113-120`). Это **ломает все существующие отпечатки**, поэтому `renormalize.py` после такой правки обязателен: иначе весь реестр переоткроется новыми позициями, а старые погаснут как исчезнувшие.
7. Если нужна сортировка по полю в `/registry` — добавить в белый список `SORTABLE` (`registry/queries.py:15-20`); имя колонки подставляется в SQL текстом, поэтому список закрытый.
8. Если поле менеджерское — добавить его в форму и в вызов `update_manager_fields` (`app.py:884-905`), иначе записать его будет нечем, как сейчас `sales_script`, `objections` и `market_deviation`.

Выгрузка CSV (`app.py:693`) и экспорт в Sheets подхватят поле сами — они идут по `DATA_FIELDS`. Автоматически при этом **не делается**: обратной миграции нет, пересчёта fingerprint нет, бэкапа перед `ALTER TABLE` нет.

## Требует согласования

1. **Единой точки «добавить поле» нет.** Комментарий `registry/models.py:1-6` обещает, что добавление поля не требует правок в пяти файлах, но фактически требует минимум двух (`models.py` + новая миграция), а на практике — до восьми шагов из §9. Решить: генерировать ли `ALTER TABLE`-миграцию из диффа списков или оставить ручной порядок и зафиксировать его как обязательный.
2. **Пересчёт fingerprint после миграций.** v6 требует ручного запуска `scripts/renormalize.py` (`registry/db.py:339-340`). Встраивать вызов в накат миграций или оставлять шагом рантбука — не решено.
3. **`legacy_id`.** Заполняется только миграцией из Sheets и нигде не читается. Оставлять ли колонку и до какого момента — не определено.
4. **Три менеджерских поля без записи.** `sales_script`, `objections`, `market_deviation` объявлены в `MANAGER_FIELDS`, колонки есть, интерфейса и роута записи нет. Довести форму или убрать поля.
5. **`db.connect(readonly=...)`.** Параметр объявлен (`registry/db.py:366`) и не реализован. Нужен ли режим только для чтения — не решено.
6. **Статус источников и история прогонов.** Админ-экран макета показывает по каждому источнику время последнего прогона, статус, число позиций и ошибку; таблицы под это нет. Решить: заводить `sync_runs` или брать данные из метрик Prometheus.
