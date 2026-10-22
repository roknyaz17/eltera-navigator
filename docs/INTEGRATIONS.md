# Интеграции

Все внешние системы, с которыми связан Eltera Navigator, **как есть в коде** ветки `feat/navigator-frontend`. Каждое утверждение подкреплено ссылкой
`файл:строка`. Того, чего в коде нет, здесь нет — несуществующие механизмы вынесены в раздел «Не реализовано».

Контекст: FastAPI (`app.py:181`) + uvicorn (`Dockerfile:33`) + Jinja2 (`app.py:121`) + APScheduler (`app.py:141`) + SQLite WAL/FTS5
(`registry/db.py:386-393`). Единственная авторизация приложения — общая пара HTTP Basic `WEB_USER`/`WEB_PASSWORD` (`app.py:191-207`), ролей нет.

| Интеграция | Направление | Состояние |
|---|---|---|
| Google Sheets | чтение трёх таблиц, запись витрины | работает |
| Seatable (ЯППИ) | чтение | работает |
| Telegram (Telethon userbot) | только чтение | работает |
| LLM (OpenAI-совместимый endpoint) | запрос-ответ | работает |
| Яндекс.Диск + база знаний проектов | чтение | работает |
| Prometheus + Grafana | отдача метрик | работает |
| Telegram Bot API, вебхуки, исходящие сообщения | — | не реализовано |

## 1. Google Sheets

**Назначение.** Три таблицы — источники заявок (`kpk`, `ametist`, `marketstaff`), четвёртая — витрина реестра для тех, кто привык смотреть данные в
Sheets (`registry/export_sheets.py:1-11`).

**Авторизация.** Сервисный аккаунт: `ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)` + `gspread.authorize`
(`sheets_adapter.py:64-73`); scope — `spreadsheets.google.com/feeds` и `www.googleapis.com/auth/drive` (`sheets_adapter.py:68-71`). Путь к ключу
**захардкожен строкой** `"credentials.json"` в трёх местах: `pipeline.py:278` (реестровый прогон), `pipeline.py:367` (прежний Sheets-путь),
`app.py:113` (создание сервиса на импорте модуля). Ни env-переменной, ни CLI-аргумента для пути нет. В контейнер файл монтируется отдельно
(`docker-compose.yml:29`, `./credentials.json:/app/credentials.json:ro`).

**Идентификаторы таблиц — константы в коде,** не в окружении:

| Константа | Значение | Строка |
|---|---|---|
| `TARGET_SPREADSHEET_ID` / `TARGET_SHEET_NAME` | `1Hiwitc…KZbk` / `Лист1` | `pipeline.py:47-48` |
| `KPK_MATRIX_ID`, лист `Таблица` | `18dIE1…8UOc` | `pipeline.py:50`, `pipeline.py:209` |
| `MARKETSTAFF_SPREADSHEET_ID`, лист `Объекты МО` | `1boGa7…CJvI` | `pipeline.py:56-58` |
| `AMETIST_SPREADSHEET_ID`, лист `Потребность ` (с концевым пробелом) | `1mnupy…2Akg` | `pipeline.py:62`, `pipeline.py:238` |

**Что читает.** КНК — транспонированную матрицу «колонка = город, строка = параметр» (`sheets_adapter.py:366-409`, разбор
`matrix_vacancy_extractor.py:38-49`). Аметист — построчно, с протяжкой региона из строк-разделителей (`ametist_sheet_extractor.py:218-251`).
Маркетстафф — построчно, с протяжкой объединённых ячеек блока объекта (`marketstaff_sheet_extractor.py:410-419`) и классификацией нестабильных
колонок 4–7 по форме значения (`marketstaff_sheet_extractor.py:458-481`).

**Что пишет.** Только витрину, **полной перезаписью листа**, не upsert: `worksheet.update("A1:{col}{n}", values, value_input_option="USER_ENTERED")` и затем `batch_clear` хвоста прошлой, более длинной выгрузки (`registry/export_sheets.py:54-59`). Вызов — в
конце прогона под флагом `SHEETS_EXPORT_ENABLED` (`pipeline.py:335-342`). Мотив в докстроке `registry/export_sheets.py:3-11`: таблица перестала быть
источником правды, слияние с ручными правками больше не нужно. Прежний путь (`REGISTRY_ENABLED=0`) пишет иначе — идемпотентный `upsert_vacancies` по
`vacancy_id` с защитой `PROTECTED_FIELDS` (`sheets_adapter.py:81-162`, `:35-39`).

**Переменные окружения.** `SHEETS_EXPORT_ENABLED` (дефолт `1`, `pipeline.py:44`), `REGISTRY_ENABLED` (дефолт `1`, `pipeline.py:41`). Идентификаторов
таблиц в env нет.

**Ограничения и хрупкие места.** `GoogleSheetsService("credentials.json")` создаётся на импорте `app.py:113` — без файла ключа процесс не стартует
вообще, даже для роутов, Sheets не использующих. Ретраев и обработки квот Google API нет: сбой выгрузки ловится одним `except` и логируется
(`pipeline.py:341-342`), сбой чтения выбрасывает источник из прогона (`pipeline.py:246-248`). Полная перезапись означает, что витрина буквально
повторяет реестр: при пустой БД `build_rows` вернёт одну строку заголовков (`registry/export_sheets.py:24-28`), остальное вычистится. Переименование
листа Аметиста (там значим концевой пробел) ломает источник молча. Смена ID таблицы требует правки кода и передеплоя. **Состояние: работает.**

## 2. Seatable — источник ЯППИ

**Назначение.** Единственный источник `yappi`; данные в base Seatable, доступной по публичной external-link (`seatable_adapter.py:1-14`).

**Авторизация — выдирание токена регулярками со страницы.** `authorize()` делает GET HTML страницы external-link и достаёт из встроенного JS-конфига
два значения: `re.search(r"dtableUuid:\s*'([^']+)'", r.text)` и `re.search(r"accessToken:\s*'([^']+)'", r.text)` (`seatable_adapter.py:41-51`). Не
нашли — `RuntimeError` «Не удалось извлечь dtableUuid/accessToken» (`seatable_adapter.py:47-50`). Токен — короткоживущий JWT с `permission='r'`,
уходит заголовком `Authorization: Token <jwt>` (`seatable_adapter.py:58`).

**Что читает.** `GET /api-gateway/api/v2/dtables/{uuid}/metadata/` (`seatable_adapter.py:68`) и `GET …/rows/?table_name=…&limit=…`
(`seatable_adapter.py:74-79`, `limit` 1000, таймауты 15/30 с). Имя таблицы из пайплайна не передаётся → берётся **первая таблица base**
(`seatable_adapter.py:99-104`). Select-поля резолвятся в текст по метаданным (`seatable_adapter.py:147-179`); файлы и картинки не скачиваются,
вместо них `"<N файл(ов)>"`. Ничего не пишет — доступ только на чтение.

**Переменные окружения.** `SEATABLE_TABLE_YAPPI` — URL external-link, читается как `os.environ[...]`, то есть обязательна (`pipeline.py:224`,
`:418`). Единственный идентификатор таблицы в проекте, вынесенный в окружение.

**Ограничения и хрупкие места.** Авторизация держится на разметке чужой страницы: изменение JS-конфига Seatable (кавычки, имя поля) ломает
интеграцию целиком. Ретраев нет — `requests.get` без повторов. Чтение первой таблицы base: добавление новой таблицы в base сместит источник данных.
**Состояние: работает.**

## 3. Telegram — только чтение

**Назначение.** Два канала-источника заявок плюс подтягивание текстов постов по ссылкам из таблицы Аметиста. Записи, ответов, рассылки нет (см. «Не
реализовано»).

**Авторизация.** Telethon-userbot от лица пользователя, не бот: `TelegramClient(StringSession(...), api_id, api_hash)`
(`telegram_userbot.py:37-41`). Строка сессии — в переменной окружения `TELEGRAM_SESSION`; пустая даёт `RuntimeError` с указанием запустить
`auth_userbot.py` (`telegram_userbot.py:27-32`), недействительная отлавливается через `is_user_authorized()` (`telegram_userbot.py:43-46`).
Подключение живёт на время `__aenter__`/`__aexit__` (`telegram_userbot.py:36-52`).

**Два канала.** `_read_telegram(sources)` открывает один сеанс userbot на прогон и читает только Градус и AAA+ (`pipeline.py:137-165`):
`TELEGRAM_VAHTAPRO_CHAT_ID` → Градус (`pipeline.py:158`, fallback-URL `t.me/c/2610083978` — `vahtapro_message_processor.py:25`),
`TELEGRAM_AAAPLUS_CHAT_ID` → AAA+ (`pipeline.py:162`, fallback-URL `t.me/c/3554828202` — `aaaplus_message_processor.py:21`). Окно — сообщения с
07:00 МСК сегодня (`TG_SINCE_HOUR_MSK = 7`, `pipeline.py:84`, `_since_today_msk` — `:91-99`), потолок `TG_LIMIT = 20` сообщений (`pipeline.py:82`).
Аметист с Telegram-чата переведён на таблицу, `AmetistMessageProcessor` в пайплайне не используется (`pipeline.py:87-88`).

**telegram_post_fetcher и кэш постов.** Не альтернатива userbot'у, а надстройка: достаёт текст конкретного поста по ссылке из таблицы Аметиста
(`telegram_post_fetcher.py:1-16`). `parse_link` понимает приватный формат `t.me/c/<id>/<thread>/<msg>`, берётся последний числовой сегмент
(`telegram_post_fetcher.py:26`, `:33-51`). Ссылки группируются по чату, одно подключение на всю пачку (`telegram_post_fetcher.py:101-136`). Кэш —
JSON `{ссылка: текст}` по пути `TELEGRAM_POSTS_CACHE` (дефолт `data/telegram_posts.json`, `pipeline.py:66`; чтение/запись —
`telegram_post_fetcher.py:73-92`). Это не оптимизация, а защита данных: без Telegram строка уехала бы в LLM без описания и позиция потеряла бы уже
извлечённые поля (`telegram_post_fetcher.py:65-68`). Фетчер не создаётся вовсе при пустом `TELEGRAM_SESSION` (`pipeline.py:69-79`).

**Что читает.** Только текст: `text = (msg.message or "").strip()`, пустые сообщения пропускаются (`telegram_userbot.py:85-87`); в результат идут
`id, date, text, channel_id, channel_title` (`telegram_userbot.py:88-94`).

**Что НЕ обрабатывается.**
1. **Медиа.** Фотографии, документы, голосовые не скачиваются и не учитываются — берётся исключительно `msg.message` (`telegram_userbot.py:85-87`).
   Сообщение с картинкой без подписи просто выпадает из выборки.
2. **Альбомы в канальном чтении.** `get_messages` не смотрит на `grouped_id` (`telegram_userbot.py:81-95`): альбом придёт как несколько сообщений,
   текст есть только у одного. Склейка альбомов реализована **только** в `telegram_post_fetcher._album_text` (`telegram_post_fetcher.py:159-179`,
   окно `ALBUM_SPAN = 9`), то есть работает для Аметиста и не работает для Градуса и AAA+.
3. **Ретраи.** В `_read_telegram` нет ни повторов, ни try/except (`pipeline.py:152-165`), и `run_registry_pipeline` его не оборачивает
   (`pipeline.py:287`) — падение Telegram роняет весь прогон, включая табличные источники. Для сравнения: у Яндекс.Диска ретраи есть
   (`yandex_disk.py:26`), у post-fetcher'а — хотя бы отдача кэша при сбое (`telegram_post_fetcher.py:128-133`).
4. Приватный атрибут: `TelegramPostFetcher` лезет в `bot._client` (`telegram_post_fetcher.py:123`, `:140`, `:169`) — формально не публичный API
   userbot'а.

**Переменные окружения.** `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` (обязательны, `telegram_userbot.py:25-26`), `TELEGRAM_SESSION`
(`telegram_userbot.py:27`, `pipeline.py:76`), `TELEGRAM_VAHTAPRO_CHAT_ID`, `TELEGRAM_AAAPLUS_CHAT_ID` (`pipeline.py:158`, `:162`),
`TELEGRAM_POSTS_CACHE` (`pipeline.py:66`). **Состояние: работает на чтение.**

## 4. LLM

**Назначение.** Единственная задача — извлечь структурированные поля вакансии из свободного текста заявки. Унификация значений делается
детерминированно, без модели (`registry/normalize.py:1-13`).

**Клиент и модель.** `ChatOpenAI` из `langchain_openai` (`vacancy_parser.py:10`, `:210-216`). Дефолты конструктора: `model_name="gpt-4.1"`,
`temperature=0.0`, `max_tokens=16000` (`vacancy_parser.py:188-195`). **Модель не читается из окружения** — ни `pipeline.py`, ни `app.py` её не
переопределяют, единственное место задания — дефолт аргумента. Цепочка `prompt_template | llm` собрана без `StrOutputParser`, чтобы достать
`usage_metadata` (`vacancy_parser.py:225-226`).

**Ключ и базовый URL.** `base_url = os.getenv("TIMEWEB_BASE_URL")`, `api_key = os.getenv("OPENAI_API_KEY")` (`vacancy_parser.py:205-208`); провайдер
— OpenAI-совместимый endpoint Timeweb Cloud (`vacancy_parser.py:200-204`). Проверки на пустой ключ нет — ошибка придёт от клиента при первом вызове.

**Цены.** `LLM_PRICE_INPUT_RUB_PER_MTOK` (дефолт `18.9`) и `LLM_PRICE_OUTPUT_RUB_PER_MTOK` (дефолт `37.8`), ₽ за миллион токенов
(`vacancy_parser.py:15-17`). Комментарий там же говорит, что это цены **DeepSeek V4 Flash**, то есть дефолтные цены и дефолтная модель `gpt-4.1` в
коде не совпадают; при смене модели цены надо переопределять через env. Расчёт — `TokenUsage.cost_rub` (`vacancy_parser.py:33-38`), итог прогона
уходит в лог (`pipeline.py:323-326`) и в метрики `LLM_REQUESTS`/`LLM_TOKENS`/`LLM_COST` (`pipeline.py:328-333`).

**Экономия на content_hash.** `RawRequest.content_hash` — sha256 от `{text, overrides, defaults}`, и только при наличии справки добавляется ключ
`context` (`registry/models.py:190-211`). Условность намеренная: иначе появление поля перебило бы хэши всех заявок всех источников и вызвало
сплошной повторный разбор (`registry/models.py:205-208`). В фазе подготовки приёма: заявка найдена, хэш совпал **и** прошлый `parse_status == 'ok'`
→ LLM не вызывается вовсе, обновляется только `last_seen_at`, счётчик `llm_calls_saved += 1` (`registry/ingest.py:175-182`). Заявка с прошлым
провалом разбора перепарсивается даже при том же тексте (`registry/ingest.py:172-175`). Метрика — `eltrea_registry_llm_calls_saved_total`
(`metrics.py:137-165`, инкремент `pipeline.py:319`).

**Ограничения и хрупкие места.** **Ограничителя вызовов нет.** Единственный сдерживающий механизм — параллелизм разбора:
`asyncio.Semaphore(self.llm_concurrency)` при `DEFAULT_LLM_CONCURRENCY = 5` (`registry/ingest.py:54`, `:276`). Ни задержек между запросами, ни
бюджета на прогон, ни лимита стоимости, ни backoff по 429. Повторов максимум один — `MAX_RETRIES = 1` (`vacancy_parser.py:231`), не более двух
вызовов на кусок текста. Есть скрытый множитель расхода: если со справкой модель вернула пустой список, разбор повторяется по голому тексту, токены
обеих попыток суммируются (`vacancy_parser.py:398-432`). Разбор ответа ручной — снятие markdown-fence и вырезка от первого `[` до последнего `]`
(`vacancy_parser.py:278-297`), JSON-mode не используется. **Состояние: работает.**

## 5. Яндекс.Диск и база знаний проектов

**Назначение.** У контрагента Градус есть публичная папка на Яндекс.Диске с описанием каждого проекта: пост в канале говорит, что нужно сегодня,
папка — что это за объект вообще (`project_kb.py:3-31`, `vahtapro_message_processor.py:6-10`).

**Авторизации нет.** Открытый REST публичных ресурсов, `API_ROOT = "https://cloud-api.yandex.net/v1/disk/public/resources"` (`yandex_disk.py:22`);
единственный «ключ» — сама публичная ссылка в параметре `public_key` (`yandex_disk.py:71`, `:98`). Ни OAuth, ни токена, ни заголовка `Authorization`
(`yandex_disk.py:3-5`); единственный заголовок — `User-Agent: eltera-navigator/1.0` (`yandex_disk.py:43`). Приватную папку подсистема прочитать не
сможет.

**Что индексируется.** Обход `DiskIndexer` (`project_kb.py:547-724`) идёт корень → категории → папки проектов → файлы и подпапки. Подпапка = альбом,
из неё берутся имя, число изображений и ссылка (`project_kb.py:651-660`, `_count_photos` — `:696-700`). Файлы, кроме `.docx/.txt/.md/.csv`,
отбрасываются (`yandex_disk.py:166-167`). Скачивается и превращается в текст только файл-описание: `DESCRIPTION_HINTS =
("опис","проект","инфо","памятка","услови")`, а имена с «направлени»/«заселени» исключаются всегда — там инструкции и телефоны коменданта
(`project_kb.py:533-544`); обрезка `MAX_DOC_CHARS = 4000` (`project_kb.py:56`). `.docx` разбирается регулярками по `word/document.xml`, без
python-docx (`yandex_disk.py:124-157`). Инкрементальность — `_fingerprint` по списку `(name, type, size, md5, modified)` (`project_kb.py:733-739`);
если хоть один документ не скачался, fingerprint пишется пустым, чтобы папка гарантированно перечиталась (`project_kb.py:690-692`). Результат —
строка в `disk_projects` (DDL `registry/db.py:204-223`, миграция v3).

**Как связывается с позициями.** `link_positions` читает `SELECT position_id, counterparty, object_name, city FROM positions WHERE source = ?`
(`project_kb.py:472-475`) и сопоставляет **по полям позиции**, а не по тексту заявки: пост-сводка описывает десяток проектов сразу
(`project_kb.py:419-423`). Совпало — upsert в `position_kb` (`project_kb.py:488-504`, DDL `registry/db.py:232-245`, миграция v4); не совпало —
строка удаляется и кнопка у позиции пропадает (`project_kb.py:482-484`). Пороги приёма `MIN_COVERAGE = 0.55`, `MIN_MARGIN = 0.12`,
`MIN_BRAND_COVERAGE = 0.30` (`project_kb.py:216-219`), политика явная — промах дешевле ошибки (`project_kb.py:212-214`). `photos_url` ведёт прямо в
альбом, только если альбом с фотографиями ровно один, иначе — в папку проекта (`project_kb.py:445-455`).

**Две точки потребления.** (1) Справка для LLM: `context_for(text)` (`project_kb.py:404-413`) подключается провайдером контекста в
`VahtaProMessageProcessor` (`vahtapro_message_processor.py:39-59`) и доезжает в `RawRequest.extra_context`/`chunk_contexts`
(`telegram_channel_processor.py:164-167`); папка без текстового описания справку не даёт (`project_kb.py:407-412`). (2) Материалы витрины:
`navigator_api.media_block` — единственный производитель материалов, отдаёт максимум один элемент `kind="object_photo"`, и пустой список, если нет
URL или `photos == 0` (`navigator_api.py:322-345`).

**Когда запускается.** `refresh_project_kb` стартует параллельно чтению Telegram, если в списке источников есть `vahtapro` (`pipeline.py:281-291`);
после приёма — `link_project_folders` в try/except, прогон из-за ссылок на фото не падает (`pipeline.py:299-306`). Ручной обход — `python -m
scripts.index_vahtapro_disk` с флагами `--force/--stats/--match/--url/--db` (`scripts/index_vahtapro_disk.py:76-93`).

**Переменные окружения.** `VAHTAPRO_DISK_URL` (дефолт `https://disk.yandex.ru/d/mThqHeyoM1rDGw`, `project_kb.py:48-50`), `VAHTAPRO_KB_ENABLED`
(дефолт `1`, `:51`), `VAHTAPRO_KB_REFRESH_HOURS` (дефолт `6`, `:52`), `REGISTRY_DB_PATH` (`registry/db.py:25`). Сам `yandex_disk.py` не читает ни
одной переменной окружения.

**Ограничения и хрупкие места.** Ретраи есть, но грубые: `RETRY_PAUSES = (1.0, 3.0, 7.0)` (`yandex_disk.py:26`), 404 не ретраится (`:58-60`);
скачивание идёт отдельным циклом с повторным запросом `href`, потому что ссылка ведёт на случайный узел хранилища и часть узлов отдаёт просроченный
сертификат — проверка сертификата при этом не отключается (`yandex_disk.py:87-92`). База знаний работает только для `vahtapro`: `source="vahtapro"`
по умолчанию во всех фасадах (`project_kb.py:229`, `:458`, `:560`). Живость ссылки на папку не проверяется, `alive` захардкожен `True`
(`navigator_api.py:341`) — расчёт на переобход диска. HTTP-роута «обойти диск сейчас» нет, в `JOBS` (`app.py:84-108`) такой задачи тоже нет. Поиск
по содержимому документов не индексируется: `doc_text` — обычная колонка, сопоставление идёт только по названию папки. **Состояние: работает.**

## 6. Prometheus и Grafana

**Что отдаёт приложение.** `GET /metrics` (`app.py:279-292`) — `generate_latest()` с `CONTENT_TYPE_LATEST`. Перед отдачей пересчитываются
снапшот-гейджи `_refresh_snapshot_metrics` (`app.py:220-276`) и, при `REGISTRY_ENABLED`, `_refresh_registry_metrics` (`app.py:295-311`); обе
обёрнуты в try/except — при сбое метрики всё равно отдаются.

**Группы метрик** (`metrics.py`, префикс `eltrea_`): Info `eltrea_app` (`:16`); UI — `page_views_total`, `filter_used_total{filter}`,
`filter_value_total{filter,value}`, `sort_used_total{column,order}` (`:19-36`, инкрементируются только на `/vacancies`, `app.py:1137-1174`);
Snapshot — `vacancies_in_table{source,is_active}`, `vacancies_total_need{source}`, `vacancies_avg_shift_rate{source}`, `vacancies_by_shift_type`,
`vacancies_by_min_shifts` (`:43-67`); Pipeline — `pipeline_runs_total{source,status}`, `pipeline_duration_seconds{source}`,
`…_added/updated/skipped/deactivated/needs_review_total{source}` (`:74-115`); LLM — `llm_requests_total`, `llm_tokens_total{source,type}`,
`llm_cost_rub_total{source}` (`:118-134`); Registry — `registry_requests_total{source,kind}`, `registry_llm_calls_saved_total`,
`registry_positions{source,is_active}`, `registry_empty_field_ratio{field}`, `registry_dictionary_pending{kind}` (`:137-165`); Cache —
`cache_hits_total`, `cache_refreshes_total{mode}`, `cache_fetch_failures_total{reason}` (`:168-183`). Плюс стандартные метрики процесса и GC из
`prometheus_client`.

**Скрейп и дашборд.** `prometheus/prometheus.yml`: `job_name: eltrea-bot`, `metrics_path: /metrics`, target `app:8000`, интервал 15 с; сервис
`prom/prometheus:latest`, порт `9090:9090`, хранение `--storage.tsdb.retention.time=30d` (`docker-compose.yml:36-52`). Grafana —
`grafana/grafana:latest`, порт `3000:3000`, `GF_SECURITY_ADMIN_USER: admin`, `GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-admin}`,
`GF_USERS_ALLOW_SIGN_UP: "false"` (`docker-compose.yml:54-70`); provisioning и дашборды монтируются read-only
(`grafana/provisioning/datasources/prometheus.yml`, `grafana/provisioning/dashboards/providers.yml`, `grafana/dashboards/eltrea-bot.json` — дашборд
«Eltrea Bot», 18 панелей: просмотры, активные вакансии, суммарная потребность, расход LLM за 24 ч, использование фильтров, распределения по
source/geo/возрасту/сменам/полу, запуски пайплайна, added/updated, токены LLM).

**Что открыто без авторизации.** `Depends(verify_creds)` отсутствует у четырёх точек: `GET /health` (`app.py:211-217`), `GET /metrics`
(`app.py:279-292`), `GET /jobs` (`app.py:314-325`) и mount `/static` (`app.py:184`). То есть весь `/metrics` и состав задач планировщика доступны
любому, кто дотянулся до порта 8000. Порты Prometheus (9090) и Grafana (3000) публикуются наружу; у Grafana пароль `admin` по умолчанию, если
`GRAFANA_PASSWORD` не задан. CORS, CSRF, rate-limit и security-заголовков в приложении нет — middleware нет вообще, кроме `StaticFiles`.
**Состояние: работает.**

## 7. Не реализовано

**Telegram Bot API.** Ни одного вызова Bot API в проекте нет. `TELEGRAM_BOT_TOKEN` читается ровно в одном месте — `navigator_api.py:771` — и только
для флага `outreachEnabled: bool(os.getenv("TELEGRAM_BOT_TOKEN","").strip())`. Флаг ничего не запускает, он лишь меняет подпись статуса в UI. Вся
работа с Telegram идёт через Telethon-userbot (`telegram_userbot.py:11-12`).

**Вебхук приёма ответов.** Роута `/webhook/telegram` (и любого другого приёмника входящих) в `app.py` нет. Полный список роутов: `/`, `/navigator`,
`/api/navigator`, `POST /api/rates`, `DELETE /api/rates/{rule_id}`, `/api/vacancies`, `/vacancies`, `/registry`, `/api/registry`,
`/registry/export.csv`, `/registry/dictionaries` (+`/confirm`, `/delete`, `/confirm-all`), `/registry/manual`, `/registry/position/{id}`,
`/registry/{request_id}`, `/jobs`, `POST /trigger/{name}`, `POST /run`, `/health`, `/metrics`. Роутов `/admin` и `/internal/api/v1/*` нет.
Соответственно нет ни разбора ответов контрагентов, ни сопоставления ответа с заявкой.

**Исходящие сообщения контрагентам.** Тексты запросов собираются целиком на клиенте: `computeOutreach()` (`templates/navigator.html:738-781`) и
`outreachText` (`:704-734`), с проверкой длины против `TG_LIMIT = 4096` (`:104`, `:777`). Кнопки «Отправить», «Отправить сейчас», «Отправить
тестовое», «Запросить у заказчика» — тосты-заглушки (`templates/navigator.html:1988`, `:2027-2029`, `:2058`, `:2099`, `:2104`). На бэкенде:
`delivery="отправка запросов не настроена"`, `deliveryOk=False`, поля `contact/bot/token/chat/thread/sendTime` — пустые строки
(`navigator_api.py:579-588`). Расписание рассылки `SEND_TIME='09:00'` и `MAX_TRIES=3` — константы UI (`templates/navigator.html:101-102`), задачи
рассылки в `JOBS` (`app.py:84-108`) нет.

**Смежное, чего тоже нет.** Таблиц `outreach_*`, очереди отправки и состояния переписки (`attempt`, `silent`, `greetedToday`, `asked`) в схеме нет:
`MIGRATIONS` (`registry/db.py`) содержит шесть шагов и даёт 11 обычных таблиц плюс FTS5 `search_index`. Хранилища настроек контрагентов нет —
`cpSave` только закрывает окно с текстом «Настройки не сохранены: хранилище настроек контрагентов не подключено» (`templates/navigator.html:2097`),
роутов `POST /api/cps` нет. Синхронизации конфигурации с GitHub нет (`ghSync` — тост, `:2105`). Проверки живости ссылок каждые 6 часов нет: строка в
интерфейсе — константа разметки (`:814`). Каталог `navigator/` (пакет заказчика) приложением не используется — ни mount, ни роута, ни ссылки;
`/navigator` отдаёт `templates/navigator.html` (`app.py:366-375`). Его `navigator/.env.example` описывает `FLASK_ENV`, `DATABASE_URL`,
`LLM_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` — **ни одна из этих переменных в питон-коде не читается**; реальные имена — `REGISTRY_DB_PATH`,
`OPENAI_API_KEY`, `WEB_USER`/`WEB_PASSWORD`, а ключ Google берётся файлом `credentials.json` по захардкоженному пути.

## 8. Сводная таблица источников данных

| Источник | Ключ | Имя | Транспорт | Авторизация | Идентификатор ресурса | Прогон (МСК) | Состояние |
|---|---|---|---|---|---|---|---|
| КНК | `kpk` | КНК | Google Sheets, матрица | сервисный аккаунт, `credentials.json` | `pipeline.py:50`, лист `Таблица` | 12:00, reset | работает |
| ЯППИ | `yappi` | ЯППИ | Seatable external-link | accessToken регулярками со страницы | env `SEATABLE_TABLE_YAPPI` | 12:00, reset | работает |
| Градус | `vahtapro` | Градус | Telegram-канал + база знаний Я.Диска | Telethon userbot, `StringSession` | env `TELEGRAM_VAHTAPRO_CHAT_ID` | 09:30 reset, 13:00 | работает |
| AAA+ | `aaaplus` | AAA+ | Telegram-канал | Telethon userbot, `StringSession` | env `TELEGRAM_AAAPLUS_CHAT_ID` | 09:30, reset | работает |
| Аметист | `ametist` | Аметист | Google Sheets + тексты постов из TG | сервисный аккаунт + userbot | `pipeline.py:62`, лист `Потребность ` | 13:30 | работает |
| Маркетстафф | `marketstaff` | Маркетстафф | Google Sheets | сервисный аккаунт | `pipeline.py:56-58`, лист `Объекты МО` | 12:00, reset | работает |
| Вручную | `manual` | Вручную | форма `/registry/manual` | HTTP Basic | — | по требованию | работает |
| Витрина | — | — | Google Sheets, запись | сервисный аккаунт | `pipeline.py:47-48` | после каждого прогона | работает |
| Я.Диск Градуса | — | — | публичный REST | нет (публичная ссылка) | env `VAHTAPRO_DISK_URL` | параллельно прогону `vahtapro` | работает |
| LLM | — | — | OpenAI-совместимый HTTP | `OPENAI_API_KEY` + `TIMEWEB_BASE_URL` | модель `gpt-4.1` в коде | по изменившейся заявке | работает |
| Prometheus | — | — | скрейп `GET /metrics` | нет | `app:8000` | каждые 15 с | работает |
| Telegram Bot API | — | — | — | — | — | — | **не реализовано** |

Расписание — `JOBS` (`app.py:84-108`), таймзона `Europe/Moscow`; флага `SCHEDULER_ENABLED` в коде нет. Имена источников — `SOURCE_NAMES`
(`pipeline.py:102-110`), алиасы запуска — только `gradus`/`градус` → `vahtapro` (`pipeline.py:116-119`).

## Требует согласования

1. **Ротация ключа сервисного аккаунта Google.** Путь `credentials.json` захардкожен в трёх местах (`pipeline.py:278`, `:367`, `app.py:113`);
   процедуры смены ключа в репозитории нет.
2. **Владелец аккаунта Telegram-userbot'а.** `StringSession` привязана к живому пользователю; кто им владеет, что происходит при смене номера, где
   резервная копия строки — не зафиксировано.
3. **Модель и цены LLM.** В коде дефолт `gpt-4.1` (`vacancy_parser.py:190`), а цены подписаны как DeepSeek V4 Flash (`:15-17`). Какая модель в проде
   и по каким ценам считать расход — надо зафиксировать и вынести имя модели в env.
4. **Бюджет и лимит на LLM.** Ограничителя вызовов нет; потолок расхода на прогон и реакция на 429 не определены.
5. **Публичность `/metrics` и `/jobs`** (`app.py:279`, `:314`) и **Grafana с паролем `admin` по умолчанию** (`docker-compose.yml:63`) — закрывать,
   ограничивать по сети или оставить.
6. **Отсутствие `.env.example` в корне.** Состав переменных восстанавливается только по коду, а `navigator/.env.example` описывает другой,
   несуществующий контур и вводит в заблуждение.
7. **Поведение при падении Telegram.** Сейчас сбой чтения канала роняет весь прогон, включая табличные источники (`pipeline.py:287`); изолировать ли
   Telegram так же, как табличные источники (`pipeline.py:246-248`) — вопрос к владельцу процесса.
