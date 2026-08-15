# Eltera Навигатор

Система превращает разрозненные заявки контрагентов (Telegram-каналы, Google-таблицы,
Seatable) в единый нормализованный реестр позиций и отдаёт рекрутеру готовую карточку
позиции — со ставкой кандидата, ставкой рекрутера и справкой по проекту с Яндекс.Диска.

> **Прочитайте перед первым коммитом.** Репозиторий содержит две разные вещи:
> **рабочий код** (приём заявок, нормализация, реестр, витрина подбора, ставки рекрутера,
> база знаний проектов) и **макет интерфейса** в каталоге `navigator/`, который
> приложение пока не использует ни одним роутом.
> Карта «что есть / чего нет» — [docs/AS-IS-VS-TO-BE.md](docs/AS-IS-VS-TO-BE.md).
> Постановка работ — [DEVELOPER_TASKS.md](DEVELOPER_TASKS.md).
> Спорное и несогласованное — [docs/OPEN-QUESTIONS.md](docs/OPEN-QUESTIONS.md).

## Стек

| Слой | Чем реализовано |
|---|---|
| Веб-приложение | **FastAPI** (`app.py`, `navigator_api.py`), ASGI-сервер **uvicorn** |
| Шаблоны | Jinja2 (`templates/`), рендеринг на сервере, без сборки фронтенда |
| Планировщик | APScheduler (`AsyncIOScheduler`) внутри процесса приложения |
| Хранилище | SQLite (WAL) + FTS5, шесть миграций в `MIGRATIONS` (`registry/db.py`) |
| Извлечение полей | LLM через `langchain-openai` (`vacancy_parser.py`) |
| Интеграции | Google Sheets, Telegram (Telethon-userbot), Seatable, Яндекс.Диск |
| Наблюдаемость | `prometheus_client` (`/metrics`), Prometheus + Grafana, `loguru` |
| Тесты | pytest (`tests/`), без сети и без ключей |

Python 3.11 (образ `python:3.11-slim`).

## Структура репозитория

```
app.py                     FastAPI: роуты, HTTP Basic, APScheduler, /metrics, /health
navigator_api.py           сборка ответа GET /api/navigator для экрана подбора
main.py                    CLI-обёртка над пайплайном (без веба и планировщика)
pipeline.py                run_pipeline: порядок источников, reset, экспорт витрины
vacancy_parser.py          LLM-извлечение полей из свободного текста

registry/                  ядро реестра
  db.py                    подключение к SQLite, схема, шесть миграций
  models.py                ЕДИНСТВЕННОЕ объявление полей позиции + RawRequest
  ids.py                   ELT-2026-000123 и ELT-2026-000123-01
  ingest.py                приём, дедуп, ревизии, fingerprint, снапшот-лайфцикл
  normalize.py             унификация по справочникам, разбор ставок и графиков
  dictionaries.py          справочники, очередь подтверждения (confirmed=0)
  queries.py               SQL интерфейса: фильтры, сортировки, FTS, сводка
  rates.py                 правила ставки рекрутера и их разрешение
  geo.py                   нормализация названий городов + координаты 62 городов
  labels.py                человеческие названия полей и группы карточки
  compat.py                строки в прежнем формате для /vacancies
  export_sheets.py         выгрузка витрины в Google Таблицу (полная перезапись)

project_kb.py              база знаний проектов: индексация и связывание с позициями
yandex_disk.py             клиент Яндекс.Диска
telegram_post_fetcher.py   выкачка и кеш постов Telegram
*_extractor.py             адаптеры табличных форматов
*_message_processor.py     адаптеры Telegram-сообщений
sheets_adapter.py          клиент Google Sheets
seatable_adapter.py        клиент Seatable

templates/                 Jinja2-шаблоны экранов, главный — navigator.html
static/                    логотипы и фоновое видео экрана подбора
scripts/                   разовые операции: миграция, сид, пересчёт, индексация диска
tests/                     pytest
smoke_navigator.js         дымовой прогон фронта без браузера
cron/                      .bat/.ps1 для планировщика Windows
prometheus/, grafana/      конфиги наблюдаемости
Dockerfile, docker-compose.yml

navigator/                 ПЕРЕДАННЫЙ МАКЕТ ИНТЕРФЕЙСА — приложением не используется
docs/                      документация системы (см. docs/README.md)
DEVELOPER_TASKS.md         постановка задач для разработчика
REGISTRY.md                рабочая записка по реестру (историческая, не удалять)
```

## Локальный запуск

```bash
git clone git@github.com:roknyaz17/eltera-navigator.git
```

```bash
cd eltera-navigator && python3.11 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Заполните `.env` (см. таблицу ниже) и положите рядом `credentials.json` — JSON сервисного
аккаунта Google. Оба файла в `.gitignore`.

```bash
uvicorn app:app --reload --port 8000
```

Схема базы создаётся сама при первом обращении: `registry/db.py` сверяет
`PRAGMA user_version` и последовательно накатывает недостающие элементы `MIGRATIONS`.
Отдельной команды миграции нет — она выполнится в момент первого запроса после деплоя,
до всякой резервной копии. Это заведено задачей `DB-01`.

Доступ закрыт HTTP Basic (`WEB_USER` / `WEB_PASSWORD`). **Без авторизации открыты
`/health`, `/metrics`, `/jobs` и `/static`** — через `/metrics` наружу уходит статистика
по источникам и потребности. См. [docs/ROLES-AND-ACCESS.md](docs/ROLES-AND-ACCESS.md).

### Прогон без веб-сервера

```bash
python main.py --sources vahtapro,ametist
```

```bash
python main.py --reset
```

`--reset` означает «считать пачку полным состоянием источника». Табличные источники и так
приходят целиком; флаг влияет на Telegram: без него и без «снимка дня» в посте пачка
считается набором отдельных апдейтов и ничего не гасит. Когда пачка признана снимком,
позиции источника, которых в ней нет, получают `is_active = 0`.

### Первичное наполнение базы

```bash
python scripts/migrate_from_sheets.py --dry-run && python scripts/migrate_from_sheets.py
```

```bash
python scripts/seed_dictionaries.py
```

Затем откройте `/registry/dictionaries` и подтвердите предложенные соответствия.
Неподтверждённый вариант к данным не применяется — значение показывается как пришло.

```bash
python scripts/renormalize.py --dry-run && python scripts/renormalize.py
```

```bash
python scripts/clear_legacy_guesses.py --dry-run && python scripts/clear_legacy_guesses.py
```

Индексация базы знаний проектов с Яндекс.Диска и настройка ставок рекрутера:

```bash
python scripts/index_vahtapro_disk.py
```

```bash
python scripts/recruiter_rates.py
```

## Экраны и API

| Путь | Метод | Что это |
|---|---|---|
| `/` | GET | редирект на `/registry` (при `REGISTRY_ENABLED=0` — на `/vacancies`) |
| `/navigator` | GET | подбор для рекрутера (`templates/navigator.html`) |
| `/api/navigator` | GET | данные экрана подбора: позиции, справка, ставки, справочники |
| `/api/rates` | POST | сохранить правила ставки рекрутера (стратегии `all`, `shifts`, `clients`, режим `dryRun`) |
| `/api/rates/{rule_id}` | DELETE | удалить правило, запись в `recruiter_rate_history` |
| `/vacancies`, `/api/vacancies` | GET | прежняя выдача через кеш витрины |
| `/registry` | GET | реестр позиций: 19 фильтров, сортировка, пагинация |
| `/registry/position/{position_id}` | GET, POST | карточка позиции; POST сохраняет пять полей менеджера |
| `/registry/{request_id}` | GET | исходная заявка, ревизии, её позиции |
| `/registry/manual` | GET, POST | ручной ввод заявки |
| `/registry/dictionaries` (+`confirm`, `delete`, `confirm-all`) | GET, POST | очередь подтверждения справочников |
| `/registry/export.csv` | GET | выгрузка текущей выборки |
| `/jobs`, `/trigger/{name}`, `/run` | GET, POST | фоновые задачи |
| `/health`, `/metrics` | GET | health-чек и метрики Prometheus |

Роутов `/admin`, `/internal/api/v1/*` и `/webhook/telegram/*` в коде нет.

## Фоновые прогоны

Расписание задано в `JOBS` (`app.py`), таймзона `Europe/Moscow`:

| Задача | Время | Источники | reset |
|---|---|---|---|
| `morning_telegram` | 09:30 | Градус, AAA+ | да |
| `noon_tables` | 12:00 | КНК, ЯППИ, Маркетстафф | да |
| `afternoon_vahtapro` | 13:00 | Градус | нет |
| `afternoon_ametist` | 13:30 | Аметист | нет |

Планировщик поднимается вместе с приложением. **Запускать более одного инстанса нельзя** —
прогоны и перезапись витрины задвоятся; флага для отключения планировщика в коде нет.

## Переменные окружения

Полный список — [.env.example](.env.example). Минимум для локального запуска:
`WEB_USER`, `WEB_PASSWORD`, `OPENAI_API_KEY`, плюс `credentials.json` для источников
на Google Sheets.

Идентификаторы Google-таблиц зашиты константами в `pipeline.py`, путь к
`credentials.json` — строкой в коде. Это не секреты, но и не конфигурация: добавление
контрагента требует правки кода и релиза.

## Тесты

```bash
python -m pytest
```

Сеть и ключи не нужны: LLM подменяется заглушкой (`tests/conftest.py`), база поднимается
во временном каталоге. Текущий набор: `test_ids`, `test_ingest`, `test_normalize`,
`test_queries`, `test_renormalize`, `test_clear_legacy_guesses`, `test_project_kb`,
`test_rates`, `test_ametist_telegram`.

Дымовой прогон экрана подбора без браузера:

```bash
node smoke_navigator.js nav_api.json
```

## Макет интерфейса `navigator/`

Каталог `navigator/` — переданный заказчиком **эталон интерфейса** на моковых данных.
Приложение о нём не знает: ни `StaticFiles`, ни роута, ни ссылки.

```bash
cd navigator && npm start
```

Открыть `http://127.0.0.1:5173` — экран входа. Без Node.js подойдёт любой статический
сервер: `python3 -m http.server 5173`.

Это документ формата `x-dc`, который исполняет рантайм `navigator/support.js`, подгружая
React 18 и Babel с `unpkg.com`, — **нужен интернет**. Авторизация демонстрационная,
пароли лежат в клиентском коде, данные моковые. Задача — воспроизвести макет
в `templates/navigator.html`, а не подключать его как есть.

Подробно — [docs/FRONTEND-PACKAGE.md](docs/FRONTEND-PACKAGE.md).

## Docker

```bash
docker compose up -d --build
```

Три сервиса: приложение (`:8000`, uvicorn, healthcheck на `/health`), Prometheus
(`:9090`), Grafana (`:3000`). База — в volume `registry-data`, `credentials.json`
монтируется на чтение. Перед миграцией схемы делайте копию файла базы.

## Порядок дальнейшей разработки

Задачи в [DEVELOPER_TASKS.md](DEVELOPER_TASKS.md) сгруппированы по направлениям, но
браться за них следует этапами:

1. **Согласовать спорное** — [docs/OPEN-QUESTIONS.md](docs/OPEN-QUESTIONS.md).
   Без ответов часть задач начинать нельзя.
2. **Безопасность и эксплуатация** — закрыть открытые роуты, явная команда миграции,
   резервная копия, защита от двойного запуска планировщика.
3. **Фундамент данных** — сущности контрагента, объекта и города; вынос конфигурации
   источников из кода.
4. **Автопроверка** — обязательность полей, состояния значения, список пробелов.
5. **Интерфейс** — перенос макета `navigator/` в `templates/`, включая мобильную адаптацию.
6. **Роли и доступы** — замена общей Basic-пары на пользователей и роли.
7. **Робот** — Bot API, очередь вопросов, вебхук ответов. Только после автопроверки.

Тесты пишутся вместе с задачей, а не после.

## Правила работы с секретами

В репозиторий не попадают токены Telegram, ключи Google и LLM, пароли и `.env`.
Рабочие настройки живут в базе, секреты — в переменных окружения или секрет-хранилище.
`TELEGRAM_SESSION` — полноценный доступ к живому Telegram-аккаунту, а не токен бота.

Если секрет попал в коммит — считать скомпрометированным и перевыпустить, удаления
из истории недостаточно.

## Документация

Точка входа — [docs/README.md](docs/README.md): карта «тема → документ».
