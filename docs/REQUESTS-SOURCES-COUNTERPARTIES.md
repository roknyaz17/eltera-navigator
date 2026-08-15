# Заявки, контрагенты и источники данных

Технический разбор по коду ветки `feat/navigator-frontend`. Каждое утверждение подкреплено ссылкой `файл:строка`. Разделы «как есть» описывают то, что реально исполняется; «как должно быть» — проект, в коде его нет.

Стек: FastAPI (`app.py:181`) + uvicorn (`Dockerfile:33`) + Jinja2 (`app.py:121`) + APScheduler (`app.py:141`) + SQLite в режиме WAL с FTS5 (`registry/db.py:386-393`, `registry/db.py:170-175`).

---

# ЧАСТЬ 1. ЗАЯВКИ

## 1.1. Заявка против позиции

Две разные сущности с разным сроком жизни.

| | Заявка (`requests`) | Позиция (`positions`) |
|---|---|---|
| Что это | документ, как он пришёл от источника | одна вакансия, извлечённая из документа |
| DDL | `registry/db.py:43-68` | `registry/db.py:89-105` |
| Тождество | `UNIQUE (source, source_ref)` — `registry/db.py:66` | `UNIQUE (source, fingerprint)` — `registry/db.py:104` |
| ID | `ELT-2026-000123` (`registry/ids.py:30-31`) | `ELT-2026-000123-01` (`registry/ids.py:34-35`) |
| Содержимое | `raw_text`, `raw_payload`, `content_hash`, `revision`, статус разбора, расход токенов | 53 колонки `DATA_FIELDS` + 8 `MANAGER_FIELDS` + 11 служебных, итого 72 (`registry/models.py:109`, `registry/db.py:31-36`) |

Связь — **многие ко многим**, таблица `request_positions` (`registry/db.py:122-126`). Причина: одна позиция переживает много заявок (её каждый день приносит новая сводка), одна заявка порождает несколько позиций. Номер позиции содержит номер **той заявки, что принесла её впервые** (`registry/ids.py:8-11`); свежая заявка уезжает в `positions.last_request_id`, а `first_request_id` не меняется (`registry/db.py:92-93`).

Витрина всегда джойнит позицию с её **последней** заявкой: `JOIN requests r ON r.request_id = p.last_request_id` (`navigator_api.py:718-730`, `registry/queries.py:131`). Одна заявка = один документ, даже если разбор шёл кусками: `parse_chunks` режут текст только для модели, `raw_text` хранит пост целиком (`registry/models.py:169-173`).

Практические следствия разделения:

- заявка неизменна как документ — её правят только ревизии (§1.5), позицию правят пофайлово через `position_history` (§1.8);
- деактивация касается **позиций**, а не заявок: `UPDATE positions SET is_active = 0` (`registry/ingest.py:597-618`); заявка живёт вечно;
- «менеджерские» поля (`status`, `priority`, `responsible_manager`, `recruiter_comment`, `sales_script`, `objections`, `market_rate`, `market_deviation` — `registry/models.py:94-106`) лежат на позиции и импортом не трогаются никогда;
- удаление заявки каскадно уносит ревизии, связи и историю позиций (`ON DELETE CASCADE` — `registry/db.py:80`, `:123-124`, `:133`), но такого кода в проекте нет: заявки не удаляются.

## 1.2. Полный путь: от экстрактора до search_index

**Шаг 1. Экстрактор собирает `RawRequest`** (`registry/models.py:137-211`). Обязательны `source`, `source_ref`, `raw_text` (`:154-156`), плюс `source_name`, `source_url`, `counterparty_hint`, `raw_payload`, `field_overrides`, `field_defaults`, `parse_chunks`, `extra_context`, `chunk_contexts`. Примеры сборки: `matrix_vacancy_extractor.py:41-50` (КНК), `yappi_vacancy_extractor.py:51-60`, `ametist_sheet_extractor.py:94-106`, `marketstaff_sheet_extractor.py:134-153`, `telegram_channel_processor.py:155-168` (Градус, AAA+).

**Шаг 2. Пайплайн собирает батчи.** `_collect_requests` (`pipeline.py:188-272`) возвращает `{alias: (список RawRequest, snapshot_flag)}`. Табличные источники идут через `asyncio.gather(..., return_exceptions=True)` (`pipeline.py:242`) — упавший источник логируется и выпадает из результата, остальные продолжают (`pipeline.py:246-248`).

**Шаг 3. Фаза подготовки, `_prepare`** (`registry/ingest.py:147-192`), одна транзакция: поиск заявки по `(source, source_ref)`, вставка новой или обновление старой, формирование списка на разбор и `keep_ids`. Детали — §1.3–1.5, §1.9.

**Шаг 4. Фаза разбора, `_parse`** (`registry/ingest.py:272-320`), async, вне транзакции — «не держать соединение с SQLite через `await`» (`registry/ingest.py:3-18`). Параллелизм — `asyncio.Semaphore(self.llm_concurrency)` (`:276`), по умолчанию 5 (`:54`). Вызов `parser.aparse_raw_ex(text, context)` (`:282`), реализация `vacancy_parser.py:398-432`: модель `gpt-4.1`, `temperature=0.0`, `max_tokens=16000` (`vacancy_parser.py:190`), эндпоинт из `TIMEWEB_BASE_URL`, ключ из `OPENAI_API_KEY` (`:205-208`). Главное правило промпта — ничего не додумывать: «пустое поле в реестре честнее, чем правдоподобная выдумка» (`vacancy_parser.py:93-98`).

**Шаг 5. Фаза записи, `_store`** (`registry/ingest.py:323-400`), снова одна транзакция. Порядок обработки одной распознанной вакансии:

1. `field_defaults` подставляются **только в пустые** поля (`registry/ingest.py:355-357`);
2. `field_overrides` перетирают всё, включая ответ модели (`:358`);
3. `Normalizer.normalize(merged)` (`:359`, `registry/normalize.py:394-476`) — детерминированная унификация по справочникам, LLM тут уже не участвует;
4. `_is_meaningful(fields)` (`:402-410`) — позиция заводится, только если есть хоть одно из `vacancy_name`, `object_name`, `city`;
5. `_upsert_position` (`:412-481`) — вставка или обновление по fingerprint (§1.6);
6. `position_id` немедленно попадает в `keep` (`:369`), чтобы следующая позиция той же заявки не опозналась как та же самая;
7. `_reindex` (`:562-582`) — `DELETE FROM search_index WHERE position_id = ?` плюс INSERT; тело индекса = `raw_text` заявки + значения 17 полей `SEARCHABLE_FIELDS` (`:34-52`) через `\n`;
8. связи: `DELETE FROM request_positions WHERE request_id = ?` и `INSERT OR IGNORE` пар (`:376-380`);
9. `requests.counterparty` = самое частое `counterparty` среди позиций заявки (`_pick_counterparty`, `:584-595`), запись через `COALESCE(NULLIF(?,''), counterparty)` (`:389`);
10. `parse_status='ok'`, `llm_model` = имя модели парсера (`:393`).

Про нарезку на куски: `parse_chunks` заполняет только Telegram-путь и только при `len(chunks) > 1` (`telegram_channel_processor.py:165`). Нарезка — `_iter_segments` (`:346-359`): если `segment_emoji` пуст или встречается меньше двух раз, кусок один; иначе `text.split(emoji)`, шапка (`parts[0]`) отбрасывается, каждый кусок проверяется `_has_vacancy_signals`, и если ни один не прошёл — в разбор уходит весь текст. Справки выравниваются под число кусков функцией `RawRequest.context_for_chunks()` (`registry/models.py:183-188`). Заявка при этом остаётся одна — куски разбираются параллельно и результаты складываются.

**Шаг 6. Снапшот-гашение** — `_deactivate_stale` при `snapshot=True` (`registry/ingest.py:398-400`), см. §1.7.

**Шаг 7. Чтение.** `search_index` — виртуальная таблица FTS5 без external content (`registry/db.py:170-175`), токенизатор `unicode61 remove_diacritics 2`. Запрос собирает `fts_query` (`registry/queries.py:32-45`): всё, кроме `[\w]+`, выбрасывается, токены соединяются через `AND`, последний получает префиксную звёздочку. Подстановка в WHERE — `p.position_id IN (SELECT position_id FROM search_index WHERE search_index MATCH ?)` (`registry/queries.py:112-120`).

Отдельно про шаг 3 списка — нормализацию. Принцип зафиксирован в `registry/normalize.py:1-13`: LLM только извлекает, унификация детерминированная, по справочникам; единственное разрешённое додумывание — регион по городу и только из **подтверждённого** справочника `city_region` (`registry/normalize.py:423-426`). `_lookup(kind, value)` (`:372-392`) при неизвестном значении возвращает его **как есть** и ставит в очередь подтверждения — молча приклеивать к похожему нельзя. Деньги проходят отсечку правдоподобности: `MIN_SHIFT_RATE = 100`, `MIN_HOURLY_RATE = 20` (`:77-78`), пересчёт час↔смена намеренно не делается (`:274-312`). `need_total` считается только из известных частей, при полном незнании остаётся `None`, а не `0` (`:478-494`).

**Куда всё это выходит наружу**: экран `/registry` и его API-двойник `/api/registry` (`app.py:601-686`), CSV-выгрузка `/registry/export.csv` с русскими подписями `FIELD_LABELS` (`app.py:689-727`), карточки `/registry/{request_id}` и `/registry/position/{position_id}` (`app.py:850-934`), витрина `/api/navigator` (`app.py:378-397`, сборка — `navigator_api.py:715-778`) и, при `SHEETS_EXPORT_ENABLED`, полная перезапись листа Google Таблицы (`registry/export_sheets.py:31-62`, вызов `pipeline.py:335-342`).

## 1.3. Дедуп по (source, source_ref)

Тождество заявки определяет **пара источник + внешняя ссылка**, а не содержимое: `SELECT * FROM requests WHERE source = ? AND source_ref = ?` (`registry/ingest.py:159-162`), подкреплено ограничением `UNIQUE (source, source_ref)` (`registry/db.py:66`).

`source_ref` у каждого источника свой (§2.1). Там, где естественный ключ строки может повториться в пределах листа, он оборачивается в `unique_ref(base, seen)` (`registry/sources.py:18-33`) — при повторе добавляется суффикс `#2`, `#3`. Ключ стабилен внутри прогона, но чувствителен к порядку строк: перестановка двух одинаковых строк переставит и их суффиксы.

## 1.4. content_hash — детектор изменений

`content_hash` — property `RawRequest` (`registry/models.py:190-211`). Это **не идентификатор**, а детектор: он отвечает на вопрос «нужно ли разбирать заново».

Входит в хэш (`registry/models.py:199-208`): `text` (весь `raw_text`), `overrides` (`field_overrides`), `defaults` (`field_defaults`) и `context` — список непустых справок из базы знаний, **только если справка есть**. Сериализация — `json.dumps(..., ensure_ascii=False, sort_keys=True, default=str)`, затем `sha256` (`:210-211`).

Почему подстановки входят в хэш (`registry/models.py:193-197`): если в таблице поменялась только колонка «Объект», текст заявки мог не измениться, а позиция — да. Почему ключ `context` условный (`:205-207`): безусловное появление поля перебило бы хэши всех заявок всех источников разом и погнало реестр на сплошной повторный разбор.

Не входят: `source_url`, `received_at`, `raw_payload`, `counterparty_hint`, `source_name`. Смена любого из них не вызывает переразбор, хотя `source_url` и `raw_payload` перезаписываются (`registry/ingest.py:237-261`).

## 1.5. Ревизии

Таблица `request_revisions` (`registry/db.py:77-85`): `request_id` с каскадным удалением, `revision`, `raw_text`, `raw_payload`, `content_hash`, `replaced_at`; индекс `idx_revisions_request` (`:87`).

При изменившемся содержимом сначала `_archive_revision` кладёт **прежнюю** версию в `request_revisions` (`registry/ingest.py:186`, реализация `:223-235`), затем `_update_request(bump=not unchanged)` перезаписывает `raw_text`, `raw_payload`, `content_hash`, `source_url`, `last_seen_at` и делает `revision = revision + 1` (`:187`, `:237-261`).

Деталь: заявка, попавшая на переразбор из-за прошлого сбоя (а не из-за изменения текста), ревизию **не** создаёт и счётчик не бампает — `bump=not unchanged`, а `stats.requests_changed` инкрементируется только при `not unchanged` (`registry/ingest.py:189-190`). Чтение — `revisions_of_request` (`registry/queries.py:203`), показ — `templates/registry_request.html`, роут `app.py:908-934`.

## 1.6. fingerprint и запасной ключ _rescue_match

`fingerprint(fields)` — `md5` от `"|".join(norm_key(поле))` по шести полям, первые 12 hex-символов (`registry/normalize.py:497-510`). Состав `FINGERPRINT_FIELDS` (`registry/models.py:113-120`): `counterparty, city, vacancy_name, object_name, work_format, shift_type`. Считается по **уже нормализованным** полям, поэтому подтверждение алиаса в справочнике меняет отпечаток.

Поиск: `SELECT * FROM positions WHERE source = ? AND fingerprint = ?` (`registry/ingest.py:421-425`). Не нашли — `_rescue_match` (`:427`). Не нашли и там — INSERT новой позиции (`:430-441`).

`_rescue_match` (`registry/ingest.py:483-560`) спасает позицию, у которой отпечаток «поплыл» — например, `shift_type` стал пустым после запрета додумывать. Правила:

- требуется непустое `vacancy_name` **или** `object_name` (`:517-518`);
- identity-поля `counterparty, city, vacancy_name, object_name`: для непустых значений условие `(col = ? OR col IS NULL)` — пустое в БД совместимо с любым (`:516-529`);
- различающие поля `shift_type`, `work_format`: `(col IS NULL OR col = ?)` — день с ночью не склеиваются (`:531-537`);
- позиции, уже занятые в этом прогоне, исключаются через `position_id NOT IN (...)` (`:539-542`);
- `LIMIT 2` и требование **ровно одного** совпадения: `if len(rows) != 1: return None` (`:544-549`) — неоднозначность лучше новой позиции;
- при успехе fingerprint переписывается на новый (`:552-555`).

Индекс под это — `idx_positions_identity(source, counterparty, city, vacancy_name, object_name)` (`registry/db.py:116`). Отдельно существует `make_vacancy_id` (`vacancy_parser.py:59-77`) — md5 по тем же шести полям; это ключ **прежнего** Sheets-контура, в реестре не используется, его наследие лежит в `positions.legacy_id` и «в работе не участвует» (`registry/db.py:96-98`).

## 1.7. Снапшот-лайфцикл и семантика reset

Каждый батч приходит с флагом: `ingest_batches(Dict[str, Tuple[Sequence[RawRequest], bool]])` (`registry/ingest.py:113-116`). `True` означает «эта пачка — полное состояние источника».

Откуда флаг (`pipeline.py:249-251`, `pipeline.py:270`): **табличные источники — всегда `True`** (таблица приходит целиком); **Telegram — `had_snapshot or reset`**, где `had_snapshot` ставится, когда в пачке встретилось сообщение со `snapshot_marker` (`telegram_channel_processor.py:126-127`).

Гашение: `_deactivate_stale` (`registry/ingest.py:597-618`) — `UPDATE positions SET is_active = 0, updated_at = ? WHERE source = ? AND is_active = 1 AND position_id NOT IN (keep)`. Строки не удаляются, только флаг.

**Семантика `reset` в реестровом пути ровно одна**: усилить snapshot-флаг Telegram-источников (`pipeline.py:290`, `:270`). Никакого предварительного массового гашения нет — слова `mark_sources_inactive` в `run_registry_pipeline` (`pipeline.py:275-344`) нет. На табличные источники `reset` не влияет вообще. В **прежнем** Sheets-пути (`REGISTRY_ENABLED=0`) семантика была другой: `reset` вызывал `sheets.mark_sources_inactive(...)` **до** прогона, гасил все строки источника, а апсёрт возвращал живые обратно (`pipeline.py:376-385`, `sheets_adapter.py:211-268`).

Расписание (`app.py:84-109`, `Europe/Moscow`): 09:30 `vahtapro+aaaplus` `reset=True`; 12:00 `kpk+yappi+marketstaff` `reset=True`; 13:00 `vahtapro` `reset=False`; 13:30 `ametist` `reset=False`.

## 1.8. position_history

Таблица — `registry/db.py:131-139`: `position_id` (FK с каскадом), `request_id` (**без** FK), `field`, `old_value`, `new_value`, `changed_at`; индексы `idx_history_position`, `idx_history_changed` (`:141-142`).

Пишется только в ветке «позиция нашлась и в ней что-то изменилось» (`registry/ingest.py:455-474`): одна строка на каждое изменившееся поле, вставка `executemany`. Сравнение — `_differs(old, new)` (`:621-638`): `None/None` → нет разницы; одно `None` → разница; float с допуском `1e-9`; иначе `str(...).strip()`.

Два правила определяют, что вообще попадает в историю: `new is None` пропускается (`:450-451`) — пустое новое значение **не затирает** известное старое, и записи не возникает; если изменений нет, обновляются только `last_request_id`, `last_seen_at`, `is_active`, а `updated_at` не трогается (`:476-480`).

История не покрывает: правки менеджера через `update_manager_fields` (`registry/queries.py:241-254`), деактивацию по снапшоту (`registry/ingest.py:597-618`) и связку с папкой Яндекс.Диска — последняя намеренно вынесена в `position_kb`, потому что пересчитывается при каждом обходе диска (`registry/db.py:228-231`). Чтение — `history_of_position(limit=200)` (`registry/queries.py:223`), показ — `templates/registry_position.html`, роут `app.py:850-880`.

## 1.9. Экономия LLM

Ключевое условие — `registry/ingest.py:175`: `if unchanged and row["parse_status"] == "ok":`. Совпал `content_hash` **и** прошлый разбор был успешен → модель не вызывается вовсе: обновляется `last_seen_at`, позиции заявки попадают в `keep_ids` через `_position_ids_of` (`:264-269`), инкрементируются `requests_unchanged` и `llm_calls_saved` (`:181-182`).

Заявка с прошлым провалом разбора перепарсивается даже при том же тексте — комментарий (`:172-174`) объясняет: иначе единичный сбой LLM навсегда оставил бы её без позиций.

Метрика — `eltrea_registry_llm_calls_saved_total{source}` (`metrics.py:137-165`, запись `pipeline.py:311-321`). Расход считает `TokenUsage` (`vacancy_parser.py:20-45`), цены из env `LLM_PRICE_INPUT_RUB_PER_MTOK` / `LLM_PRICE_OUTPUT_RUB_PER_MTOK` (`:15-17`). Поштучный расход возвращает `aparse_raw_ex` (`:398`) и он пишется в колонки `llm_tokens_in` / `llm_tokens_out` заявки (`registry/db.py:51-52`). Второй слой экономии — `MAX_RETRIES = 1` (`vacancy_parser.py:231`), то есть не более двух вызовов на разбор.

## 1.10. Ручной ввод

Форма — `templates/registry_manual.html`, роуты `app.py:789-793` (GET) и `app.py:796-847` (POST). Поля: `counterparty` (обязательное), `text` (обязательное), `channel` (необязательное).

Сборка (`app.py:817-826`): `source=SOURCE_MANUAL` (`registry/sources.py:15`, значение `manual`); `source_name` = `"Вручную (channel)"` или `"Вручную"`; `counterparty_hint` и `field_defaults={"counterparty": ...}` из формы; `raw_payload={"channel": channel, "entered_by": user}`, где `user` — логин Basic-аутентификации (`app.py:191-207`); **`source_ref = f"manual:{raw.content_hash[:16]}"`** (`app.py:826`) — ключ по содержимому: у ручной заявки нет внешнего id, а повторная вставка того же письма не должна плодить дубли (комментарий `app.py:815-816`). Побочный эффект: правка одной буквы даёт **новую** заявку, а не ревизию старой.

Приём: `RegistryIngestor(VacancyParserService()).ingest(SOURCE_MANUAL, [raw], snapshot=False)` (`app.py:829-832`). `snapshot=False` обязателен — одна заявка не является полным списком потребностей и не должна гасить остальные (комментарий `app.py:830-831`). Любое исключение превращается в текст на странице, ответ остаётся 200 (`app.py:843-847`).

Источник `manual` есть в `registry/sources.py:15` и `registry/labels.py:93-101`, но **отсутствует** в `pipeline.SOURCE_NAMES` (`pipeline.py:102-110`) — то есть его нельзя передать в `--sources` и в `POST /run`.

## 1.11. Поведение при ошибке разбора

**Уровень парсера.** `_extract_json_array` снимает markdown-fence и берёт кусок от первого `[` до последнего `]` (`vacancy_parser.py:278-297`); `_try_parse_raw` логирует причину и возвращает `None` (`:299-316`). Различаются `None` («модель не справилась») и `[]` («вакансий нет») — `:384-396`. Если со справкой модель вернула пустой список, разбор повторяется по голому тексту, и при непустом результате берётся он с записью warning (`:409-425`).

**Уровень фазы разбора.** Исключение по конкретной заявке кладётся в `item.error`, прогон не падает (`registry/ingest.py:293-296`). Не разобрался ни один чанк → `item.error = "LLM не вернул валидный JSON"` (`:308-309`). Разобралась часть — заявка сохраняется, пишется `logger.warning` (`:313-317`).

**Уровень записи.** При ошибке: `parse_status='failed'`, `parse_error = item.error[:500]`, токены всё равно записываются, а позиции **прошлой удачной версии добавляются в `keep`** (`registry/ingest.py:339-349`). Смысл прямой: сбой разбора — не повод считать, что потребность исчезла, поэтому снапшот их не погасит.

Видимость: фильтр `needs_review` → `r.parse_status != 'ok'` (`registry/queries.py:109-110`), сводка `overview` считает `failed` (`:272-296`), карточка источника в `/api/navigator` переходит в `status: "error"` при наличии заявок с ошибкой (`navigator_api.py:541-546`).

---

# ЧАСТЬ 2. ИСТОЧНИКИ

## 2.1. Таблица источников

Ключи неизменны: они лежат в колонке `source` таблиц реестра, в именах env-переменных и в метках Prometheus; отображаемые имена меняются отдельно (`pipeline.py:112-115`). Отсюда две миграции переименования: v4 ВахтаПро → Градус (`registry/db.py:249-250`) и v6 КПК → КНК (`registry/db.py:341-362`).

| Ключ / имя | Транспорт | Что читается | `source_ref` | `field_overrides` | `field_defaults` | `snapshot_marker` | Сегментация | Расписание |
|---|---|---|---|---|---|---|---|---|
| `kpk` / КНК | Google Sheets, `gspread` + сервисный аккаунт (`sheets_adapter.py:61-73`) | транспонированная матрица, лист «Таблица» (`pipeline.py:209`), колонка = город (`sheets_adapter.py:366-409`) | `unique_ref(city, seen)` (`matrix_vacancy_extractor.py:43`) | нет | `{counterparty, city}` (`matrix_vacancy_extractor.py:49`) | не применим: таблица всегда снимок (`pipeline.py:249-251`) | нет | 12:00, `reset=True` |
| `yappi` / ЯППИ | Seatable external link, JWT вытаскивается из HTML страницы (`seatable_adapter.py:41-52`) | первая таблица base (`seatable_adapter.py:99-104`), URL из env `SEATABLE_TABLE_YAPPI` (`pipeline.py:224`) | `row["_id"]` Seatable, иначе `ПРОЕКТ\|ВАКАНСИЯ\|ГОРОД` (`yappi_vacancy_extractor.py:50-52`) | нет | `{counterparty: "Yappi"}` (`yappi_vacancy_extractor.py:59`) | не применим | нет | 12:00, `reset=True` |
| `vahtapro` / Градус | Telegram userbot (Telethon), чат из `TELEGRAM_VAHTAPRO_CHAT_ID` (`pipeline.py:158`) | посты канала за окно с 07:00 МСК, максимум 20 (`pipeline.py:82-99`) | `f"msg:{msg_id}"` (`telegram_channel_processor.py:157`) | нет: override контрагента не задан (`telegram_channel_processor.py:140-142`) | нет | `"Описание проектов и актуальная потребность"` (`vahtapro_message_processor.py:26`) | по эмодзи «🚀» (`telegram_channel_processor.py:62`, `:346-359`) | 09:30 `reset=True`, 13:00 `reset=False` |
| `aaaplus` / AAA+ | Telegram userbot, `TELEGRAM_AAAPLUS_CHAT_ID` (`pipeline.py:162`) | посты канала, то же окно | `f"msg:{msg_id}"` | `{counterparty: "AAA+"}` (`aaaplus_message_processor.py:47`) | нет | `"🟡🟡👇🟡🟡🟡"` (`aaaplus_message_processor.py:22`) | выключена, `segment_emoji=None` (`aaaplus_message_processor.py:46`) | 09:30, `reset=True` |
| `ametist` / Аметист | Google Sheets + подтягивание постов TG по ссылкам (`telegram_post_fetcher.py`) | лист `"Потребность "` — с концевым пробелом (`pipeline.py:238`), строки-разделители регионов (`ametist_sheet_extractor.py:218-251`) | `unique_ref(f"{object}\|{position}", seen)` (`ametist_sheet_extractor.py:96`) | `{counterparty: "Аметист"}` (`ametist_sheet_extractor.py:102`) | `{object_name}` (`ametist_sheet_extractor.py:103-105`) | не применим (табличный путь) | нет | 13:30, `reset=False` |
| `marketstaff` / Маркетстафф | Google Sheets, лист «Объекты МО» (`pipeline.py:58`) | объединённые ячейки блока объекта, колонки «для всех объектов», нестабильные колонки 4–7 (`marketstaff_sheet_extractor.py:357-481`) | `unique_ref(f"{object}\|{city}\|{position}", seen)` (`marketstaff_sheet_extractor.py:136`) | `counterparty`, `vacancy_name`, `object_name`, `work_format="вахта"`, `shift_type` (`marketstaff_sheet_extractor.py:146-152`) | нет | не применим | нет | 12:00, `reset=True` |
| `manual` / Вручную | HTML-форма `/registry/manual` (`app.py:796-847`) | текст, вставленный человеком | `manual:{content_hash[:16]}` (`app.py:826`) | нет | `{counterparty}` (`app.py:824`) | нет: `snapshot=False` (`app.py:832`) | нет | нет, только вручную |

Устаревший путь: `ametist_message_processor.py` (Telegram-чат Аметиста, `SNAPSHOT_MARKER = "Обновляем потребность"`, `:32`) в `pipeline.py` не импортируется вообще — Аметист идёт только через таблицу. Алиасы запуска — только `gradus`/`градус` → `vahtapro` (`pipeline.py:116-119`); `кнк`, `яппи` и прочие русские написания дадут `ValueError` (`pipeline.py:122-134`).

## 2.2. Нетривиальные особенности чтения

- **КНК** — матрица транспонирована: строка 2 листа содержит города, данные идут с третьей строки, первый столбец — названия параметров; пустые значения дополняются из первого столбца данных (`sheets_adapter.py:381-405`). Строки с потребностью `"0"` или пустой пропускаются (`matrix_vacancy_extractor.py:38-40`).
- **ЯППИ** — фильтр по колонке `ПОТРЕБНОСТЬ`, отбрасываются значения из `EMPTY_NEED_TOKENS = {"стоп","заявка стоп","нет","0","-","—",""}` (`yappi_vacancy_extractor.py:13`, `:41-45`). Имя таблицы в base не задаётся, берётся первая (`seatable_adapter.py:99-104`). Файлы и картинки в текст не тянутся: `file/image` рендерятся как `"<N файл(ов)>"` (`seatable_adapter.py:147-179`).
- **Аметист** — строки-разделители регионов (заполнена только первая ячейка) задают `current_region` для всех строк ниже (`ametist_sheet_extractor.py:218-251`); пустой «Номер» означает вторую позицию того же объекта. Тексты постов Telegram подтягиваются по ссылке из колонки, найденной по подстроке `"ссылк"` (`:55`, `:253-261`), одним подключением на весь лист (`:263-274`); фетчер создаётся только при непустом `TELEGRAM_SESSION` (`pipeline.py:69-79`), иначе строки уезжают в модель без описаний.
- **Маркетстафф** — три особенности листа сразу (`marketstaff_sheet_extractor.py:357-456`): объединённые ячейки блока объекта протягиваются вниз по `BLOCK_FILL_KEYS` (`:87`), новый блок начинается при смене «Объект» или «Город» (`:410-419`); колонки «… для всех обьектов» подмешиваются в каждую строку (`:385`, `:451-453`); нестабильные колонки 4–7 классифицируются по форме значения, а не по заголовку (`_classify_unstable`, `:458-481`). Активность позиции считает `_demand_state` (`:521-533`), маркеры закрытия перебивают всё (`CLOSED_MARKERS`, `:92`).
- **Градус** — к тексту поста подмешивается справка о проекте из индекса Яндекс.Диска (`vahtapro_message_processor.py:35`, `:39-59`). Справка заявкой не является и позиций не создаёт, заявка главнее справки (`vacancy_parser.py:149-154`). Диск недоступен или проект не опознан — разбор идёт как раньше.
- **AAA+** — сегментация выключена намеренно: разделитель позиций в канале случайный эмодзи, поэтому сообщение целиком уходит в модель (`aaaplus_message_processor.py:46`). Расширены словари сигналов и подсказок по профессиям (`:26-35`).

## 2.3. Почему структурированные колонки главнее LLM

Порядок применения в `_store`: дефолты → ответ модели → оверрайды (`registry/ingest.py:355-358`). Соответственно:

- **`field_overrides` всегда главнее LLM** (`registry/models.py:162-166`). Их назначение — зафиксировать поля, входящие в `FINGERPRINT_FIELDS`. Комментарий в самом широком наборе (`marketstaff_sheet_extractor.py:143-146`) формулирует причину: должность, объект, формат и смена входят в ключ склейки позиции, а «плавающая» формулировка модели («Комплектовщик» → «Комплектовщик (сборка заказов)») заводила дубли. Колонка таблицы — стабильный ключ, ответ модели — нет.
- **`field_defaults` подставляются только в пустые поля** (`registry/models.py:167-168`, `registry/ingest.py:355-357`). Мотивировка у Аметиста: объект из колонки берётся, только если модель ничего не нашла, потому что в тексте он обычно сформулирован точнее (`ametist_sheet_extractor.py:103-104`).

Третий приём той же природы — явная инструкция в тексте для модели. У Маркетстаффа: «Это ОДНА позиция: верни ровно один объект в JSON-массиве» (`marketstaff_sheet_extractor.py:554-556`), иначе модель делила строку на день/ночь и задваивала потребность. У Аметиста: «описание ДОПОЛНЯЕТ строку, при расхождении верна строка таблицы» (`ametist_sheet_extractor.py:288-300`).

## 2.4. Где лежит конфигурация источника сейчас

Разложена по четырём местам, единой точки нет.

| Что | Где | Способ изменить |
|---|---|---|
| Список источников и отображаемые имена | `pipeline.py:102-110` | правка кода + деплой |
| Алиасы констант | `registry/sources.py:9-15` | правка кода |
| ID таблицы КНК и имя листа | `pipeline.py:50-51`, `pipeline.py:209` | правка кода |
| ID и лист Маркетстаффа | `pipeline.py:56-58` | правка кода |
| ID Аметиста, лист `"Потребность "` | `pipeline.py:62-63`, `pipeline.py:238` | правка кода |
| URL таблицы ЯППИ | env `SEATABLE_TABLE_YAPPI` (`pipeline.py:224`) | `.env` + рестарт |
| chat_id каналов | env `TELEGRAM_VAHTAPRO_CHAT_ID`, `TELEGRAM_AAAPLUS_CHAT_ID` | `.env` + рестарт |
| Fallback-URL каналов | `vahtapro_message_processor.py:25`, `aaaplus_message_processor.py:21` | правка кода |
| `snapshot_marker`, `segment_emoji`, `counterparty_override` | константы классов процессоров | правка кода |
| Ключ сервисного аккаунта Google | файл `credentials.json`, путь захардкожен в `GoogleSheetsService("credentials.json")` (`pipeline.py:278`, `app.py:113`) | замена файла |
| Расписание прогонов | `JOBS` (`app.py:84-109`) | правка кода |
| Копия расписания для UI | `navigator_api.py:51-59` и `navigator_api.py:703-710` | правка кода, **дважды** |

Проблемы, вытекающие отсюда:

1. **Добавить источник = править код в 5–8 местах**: ключ в `SOURCE_NAMES`, константа в `registry/sources.py`, экстрактор, ветка в `_collect_requests`, подпись в `labels.SOURCE_TITLES`, вид в `navigator_api.SOURCE_KIND`, расписание в `JOBS` и его копия в `SOURCE_SCHEDULE`.
2. **Расписание задублировано.** `JOBS` (`app.py:84-109`) — источник истины планировщика; `SOURCE_SCHEDULE` (`navigator_api.py:51-59`) и `next_run` со списком `[(9,30),(12,0),(13,0),(13,30)]` (`navigator_api.py:703-710`) — независимые копии в UI-слое. Изменение `JOBS` их не обновит.
3. **Идентификаторы Google-таблиц в коде, а не в конфиге.** Смена таблицы контрагентом требует коммита и деплоя.
4. **Экран настроек контрагента ничего не сохраняет.** `cpSave` (`templates/navigator.html:2097`) закрывает окно с текстом «Настройки не сохранены: хранилище настроек контрагентов не подключено»; роутов вида `POST /api/cps` в `app.py` нет.
5. **Блок «Конфигурация источников» в UI ссылается на файл, который конфигом не является.** `GITHUB` (`templates/navigator.html:158-163`) указывает на `registry/sources.py`; кнопка «Синхронизировать» — тост (`:2105`).

## 2.5. Запуск: расписание, ручные триггеры, CLI

Планировщик — `AsyncIOScheduler` внутри процесса приложения (`app.py:141`), задачи регистрируются в `lifespan` (`app.py:168-178`) с `misfire_grace_time=600` и `replace_existing=True` (`app.py:155-165`). Флага включения планировщика в коде нет — он стартует всегда.

| Задача | Cron (МСК) | Источники | `reset` |
|---|---|---|---|
| `morning_telegram` | 09:30 | `vahtapro`, `aaaplus` | `True` |
| `noon_tables` | 12:00 | `kpk`, `yappi`, `marketstaff` | `True` |
| `afternoon_vahtapro` | 13:00 | `vahtapro` | `False` |
| `afternoon_ametist` | 13:30 | `ametist` | `False` |

Определения — `app.py:84-109`. Ручные точки входа: `GET /jobs` (без авторизации, `app.py:314-325`), `POST /trigger/{name}` (`app.py:328-338`), `POST /run?sources=…&reset=…` (`app.py:341-353`, разбор списка через `parse_sources` — `pipeline.py:122-134`). Вне сервера — `python main.py --sources kpk,yappi --reset` (`main.py:31-52`). Обёртка джобы гасит любое исключение и логирует его (`app.py:144-152`), после прогона инвалидирует кеш (`app.py:149`).

Два эксплуатационных расхождения: bat-файлы в `cron/` для Windows-хоста не содержат прогона Аметиста, который есть в `JOBS`; планировщик живёт в том же процессе, что и веб, поэтому при нескольких воркерах uvicorn задачи продублируются — внешнего лока в коде нет.

## 2.6. Как должно быть: конфигурация источника

Таблица `sources` со строкой на источник: ключ, отображаемое имя, вид транспорта, адрес (spreadsheet id / seatable url / chat id), имя листа, `snapshot_marker`, `segment_emoji`, `counterparty_override`, cron-выражение, флаг `reset`, признак активности, `updated_at`/`updated_by`. Планировщик и UI читают одну и ту же строку; секреты остаются в env или внешнем хранилище, а в таблице лежит только ссылка на них. Такой таблицы в схеме нет: на v6 в БД ровно 11 обычных таблиц плюс FTS5, и `sources` среди них не значится (`registry/db.py:39-362`).

---

# ЧАСТЬ 3. КОНТРАГЕНТЫ

## 3.1. Как есть: текстовая колонка плюс справочник

**Собственной сущности «контрагент» в системе нет.** Таблицы `counterparties` не существует — миграции создают `requests`, `request_revisions`, `positions`, `request_positions`, `position_history`, `dictionaries`, `id_counters`, `disk_projects`, `position_kb`, `recruiter_rate_rules`, `recruiter_rate_history` и виртуальную `search_index` (`registry/db.py:39-362`). Контрагент существует в трёх видах.

**1. Текстовая колонка позиции.** `counterparty` входит в `TEXT_FIELDS` (`registry/models.py:21-43`), рядом `counterparty_raw` из `RAW_FIELDS` (`:82-90`). Индекс `idx_positions_counterparty` (`registry/db.py:112`). Та же пара колонок дублируется в заявке (`registry/db.py:48-49`) и заполняется `_pick_counterparty` — самым частым значением среди позиций заявки (`registry/ingest.py:584-595`).

**2. Строка справочника.** Вид `counterparty` — один из девяти в `dictionaries` (`registry/dictionaries.py:21`, `:32`, подпись «Контрагенты» — `:44`). Схема — `registry/db.py:147-157`: `(kind, alias)` как PK, `canonical`, `confirmed`, `hits`, `note`. Нормализация — `self._lookup(dicts.KIND_COUNTERPARTY, out["counterparty_raw"])` (`registry/normalize.py:408`); незнакомое значение возвращается **как есть** и ставится в очередь подтверждения, склеивать с похожим молча запрещено (`:372-392`). Очередь разбирается на `/registry/dictionaries` (`app.py:730-786`). То есть контрагент — это строка с алиасами: ни id, ни статуса, ни настроек, ни истории отношений, ни владельца.

**3. Карточка в витрине, собранная не из этой колонки.** `counterparties_block` (`navigator_api.py:555-617`) строит карточки **по ключу источника**: `key = row["srcKey"]`, `name = SOURCE_TITLES.get(key, key)` (`:568-570`). Докстрока (`:556-564`) объясняет решение: контрагент здесь — источник (ЯППИ, Градус, КНК, Аметист, AAA+), именно с ними есть договорённости, чаты и ставки; раньше карточки строились по `counterparty`, куда парсер Градуса и ЯППИ кладёт заказчика («BMJ», «Молком»), и в панели оказывалось три десятка несуществующих контрагентов. Заказчики теперь живут внутри карточки списком `clients` (`:590-600`). Ключ группировки — алиас источника, а не название, потому что название меняется (ВахтаПро → Градус), алиас нет (`:601-602`). Всё, что относится к переписке, в карточке — константы: `delivery = "отправка запросов не настроена"`, `deliveryOk = False`, `contact`, `bot`, `token`, `chat`, `thread`, `sendTime`, `alias` — пустые строки (`:579-591`).

Сводно, где сегодня живёт контрагент:

| Место | Что там лежит | Ссылка |
|---|---|---|
| `positions.counterparty` / `counterparty_raw` | нормализованное и исходное написание | `registry/models.py:21-43`, `:82-90` |
| `requests.counterparty` / `counterparty_raw` | самое частое значение среди позиций заявки | `registry/db.py:48-49`, `registry/ingest.py:584-595` |
| `dictionaries` (`kind='counterparty'`) | алиас → канон, флаг подтверждения, счётчик встреч | `registry/db.py:147-157`, `registry/dictionaries.py:21` |
| `recruiter_rate_rules.source` / `.client` | договор и объект, на которые выписана сетка | `registry/db.py:271-287`, `registry/rates.py:151-161` |
| `RawRequest.counterparty_hint`, `field_overrides`, `field_defaults` | подсказка от экстрактора | `registry/models.py:159-168` |
| `SOURCE_TITLES`, `SOURCE_KIND`, `SOURCE_SCHEDULE` | подписи и вид транспорта в UI | `registry/labels.py:93-101`, `navigator_api.py:51-69` |

Ни в одном из этих мест нет ни идентификатора контрагента, ни его статуса.

## 3.2. client_key() и почему object_name приоритетнее counterparty

```python
def client_key(counterparty: str, object_name: str) -> str:
    return (object_name or "").strip() or (counterparty or "").strip()
```

`registry/rates.py:132-139`. Докстрока объясняет: у Аметиста и КНК объект лежит в `object_name` («ДНС Пушкино»), у Градуса и ЯППИ парсер кладёт заказчика в `counterparty` («BMJ», «Молком»); для ставок это одно и то же понятие — то, что контрагент перечисляет в своей сетке.

Приоритет такой, потому что `object_name` конкретнее. Если заполнены оба поля, `counterparty` игнорируется: правило, выписанное на «BMJ», не применится к позиции, у которой объект указан как «BMJ Шарапово». Осознанный размен: ставка привязывается к точке, где реально работают люди.

Где используется: `position_row` кладёт результат в поле `client` каждой позиции (`navigator_api.py:384`) и им же разрешает ставку `rates.resolve(rules, source, client, vacancy, min_shifts)` (`:363-366`); `counterparties_block` собирает из него список заказчиков (`:598-600`); `_positions_affected` в предпросмотре сетки (`app.py:499-521`); разрешение правила — `rule.client in ("", client)` с приоритетом по `scope_rank = 2*client + vacancy` (`registry/rates.py:151-161`), то есть объект+должность > объект > контрагент. Поведение зафиксировано в `tests/test_rates.py`.

Важно: `source` в правиле ставки — это **алиас источника, а не бренд заказчика** (`registry/db.py:266-269`). Сетка выписывается на того, с кем есть договор.

## 3.3. Что из-за этого не работает

- **Публичного алиаса нет.** `cpAlias` в каждой позиции — пустая строка (`navigator_api.py:380`); кандидату вместо алиаса показывается категория объекта.
- **Настройки контрагента негде хранить** — см. §2.4, пункт 4.
- **Нет статуса** «исключён» / «в архиве»: `cpExclude`, `cpRestore`, `delConfirm` (`templates/navigator.html:2065-2097`) меняют объект в памяти вкладки до перезагрузки страницы.
- **Нет истории отношений**: `recruiter_rate_history` (`registry/db.py:291-307`) фиксирует только изменения сетки ставок, и то по ключу `(source, client, vacancy)`.
- **Переименование контрагента — миграция.** КПК → КНК потребовало UPDATE в `requests`, `positions`, `dictionaries` плюс `INSERT OR IGNORE` алиаса (`registry/db.py:341-362`), а поскольку контрагент входит в `FINGERPRINT_FIELDS`, ещё и ручного запуска `scripts/renormalize.py`: комментарий требует его прямо (`registry/db.py:339-340`), сама миграция не делает.

## 3.4. Как должно быть

Ниже — проект. В коде этого нет.

**`counterparties`** — тот, с кем есть договор (сегодня это ровно источники): `id`, `key` (стабильный алиас, совпадает с `positions.source`), `name`, `public_alias`, `status` (`active` | `paused` | `archived`), `contact_person`, `contact_phone`, `notes`, `created_at`, `updated_at`, `updated_by`. `positions.source` и `recruiter_rate_rules.source` получают FK на `counterparties.key`. Переименование становится UPDATE одной строки, а не миграцией по четырём таблицам.

**`counterparty_settings`** — то, что сегодня либо в константах, либо нигде: `counterparty_id`, `transport` (`telegram` | `sheets` | `seatable` | `manual`), `address`, `sheet_name`, `snapshot_marker`, `segment_emoji`, `schedule_cron`, `reset_flag`, `required_fields` (JSON; сейчас это глобальная константа `REQUIRED_FIELDS` — `navigator_api.py:33-40`), `outreach_chat_id`, `outreach_thread_id`, `send_time`, `message_template` (сейчас глобальный `DEFAULT_TEMPLATE` — `navigator_api.py:44-47`), `secret_ref` (ссылка на секрет во внешнем хранилище, не сам токен). Экран настроек пишет сюда, планировщик и UI читают отсюда — исчезает дубль расписания из §2.4.

**`objects`** — то, что сейчас размазано между `positions.object_name`, `positions.counterparty` и функцией `client_key`: `id`, `counterparty_id`, `name`, `aliases` (JSON), `city`, `address`, `public_alias`, `status`. `positions` получает `object_id` вместо свободного текста, `client_key` уходит из кода — ставка разрешается по `object_id`, а не по строковому совпадению. Алиасы объекта решают сегодняшнюю проблему «BMJ» против «BMJ Шарапово».

**Публичный алиас** нужен на двух уровнях — у контрагента и у объекта. Правило показа: в тексте кандидату подставляется алиас объекта, при его отсутствии — алиас контрагента, при отсутствии обоих — категория. Сейчас первые два уровня всегда пусты (`navigator_api.py:380`), а вырезание реальных названий делает клиентский `scrub` по стоп-словарю из `cp`, `obj`, `src` (`templates/navigator.html:448-461`) — защита работает вычитанием, а не подстановкой.

**Жизненный цикл**: `active` → `paused` (приём данных и рассылка остановлены, позиции гасятся снапшотом, история и ставки сохраняются) → `archived` (карточка скрыта из подбора, данные остаются, FK не рвутся) → возврат в `active`. Удаление физически не делается никогда: на контрагента ссылаются `positions`, `requests`, `recruiter_rate_rules`, `recruiter_rate_history`. Переходы логируются в отдельную `counterparty_history` по образцу `recruiter_rate_history` (`registry/db.py:291-307`).

## 3.5. Ограничения, которые задаёт текущий код миграции

Любой переход к §3.4 упирается в три места.

1. **Схема идёт только вперёд.** `_ensure_schema` накатывает миграции от `PRAGMA user_version` до `len(MIGRATIONS)` (`registry/db.py:396-416`); обратных скриптов нет. Новые таблицы добавляются как v7 и далее, откат — только восстановлением файла БД.
2. **Колонки `positions` не альтерятся.** DDL генерируется из `models.DATA_FIELDS + MANAGER_FIELDS` (`registry/db.py:31-36`) и применяется только в миграции v1. Добавление `object_id` в `models.py` существующую БД не изменит — ALTER надо писать руками отдельной миграцией.
3. **Контрагент входит в отпечаток.** Любая замена значения `counterparty` меняет `fingerprint` (`registry/normalize.py:497-510`), то есть требует пересчёта отпечатков всех позиций, иначе следующий прогон заведёт дубли. Инструмент есть — `scripts/renormalize.py` (покрыт `tests/test_renormalize.py`, есть `dry-run`), но вызывается вручную.

---

# Требует согласования

1. **`unique_ref` и порядок строк.** Суффиксы `#2`, `#3` (`registry/sources.py:18-33`) зависят от порядка строк в таблице. Данных о том, насколько часто источники переставляют строки, в коде нет; если переставляют — часть заявок будет опознаваться как новые. Решение: оставить как есть или перейти на ключ по стабильному полю строки.
2. **Что делать с `manual` в `SOURCE_NAMES`.** Источник есть в `registry/sources.py:15` и `registry/labels.py:93-101`, но отсутствует в `pipeline.SOURCE_NAMES` (`pipeline.py:102-110`). Осознанное решение или пропуск — по коду не определить.
3. **Границы понятия «контрагент».** Сегодня карточка контрагента = источник (`navigator_api.py:556-564`), заказчик — строка внутри. В §3.4 предложено развести их на `counterparties` и `objects`. Соответствует ли это договорной реальности (кто выставляет счета, на кого оформляется сетка ставок) — по коду не проверяется.
4. **Кто владелец публичного алиаса** и пересматривается ли он — неизвестно; поля под него в схеме нет вовсе.
5. **Судьба `reset` для табличных источников.** Сейчас флаг не влияет на них никак (`pipeline.py:249-251`). Нужно ли отдельное «мягкое» гашение для таблиц, прочитанных частично (например, лист оборвался из-за ошибки API) — такого режима в коде нет.
6. **Переиндекс после переименования.** `scripts/renormalize.py` требуется вручную после миграций, меняющих `counterparty` (`registry/db.py:339-340`). Встраивать ли его в миграцию или в деплой-процедуру — не решено.
