# Документация Eltera Навигатор

Документы делятся на три группы:

- **Как есть** — описывают работающий код; каждое утверждение проверяется по репозиторию.
- **Как должно быть** — постановка; часть механизмов ещё не реализована.
- **Решения** — ограничения и вопросы, требующие ответа заказчика.

Если непонятно, описан ли работающий механизм или постановка, — смотрите
[AS-IS-VS-TO-BE.md](AS-IS-VS-TO-BE.md).

## Карта: тема → документ

| Тема | Документ |
|---|---|
| Назначение проекта, глоссарий | [OVERVIEW.md](OVERVIEW.md) |
| Пользовательские сценарии | [USER-FLOWS.md](USER-FLOWS.md) |
| Логика раздела подбора | [MATCHING.md](MATCHING.md) |
| Логика админ-панели | [ADMIN-PANEL.md](ADMIN-PANEL.md) |
| Работа с заявками, контрагентами, источниками | [REQUESTS-SOURCES-COUNTERPARTIES.md](REQUESTS-SOURCES-COUNTERPARTIES.md) |
| Автоматическая проверка недостающих данных | [VALIDATION.md](VALIDATION.md) |
| Telegram-робот и сообщения контрагентам | [OUTREACH-BOT.md](OUTREACH-BOT.md) |
| Внутренние и внешние названия заявок | [NAMING.md](NAMING.md) |
| Роли и доступы | [ROLES-AND-ACCESS.md](ROLES-AND-ACCESS.md) |
| Структура данных | [DATA-MODEL.md](DATA-MODEL.md) |
| Интеграции | [INTEGRATIONS.md](INTEGRATIONS.md) |
| Требования к мобильной адаптации | [MOBILE.md](MOBILE.md) |
| Переданный макет интерфейса | [FRONTEND-PACKAGE.md](FRONTEND-PACKAGE.md) |
| Известные ограничения и спорные места | [LIMITATIONS.md](LIMITATIONS.md) |
| Вопросы, требующие согласования | [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) |
| Что работает, а чего нет | [AS-IS-VS-TO-BE.md](AS-IS-VS-TO-BE.md) |

## Полный состав

### Как есть

| Файл | О чём |
|---|---|
| [DATA-MODEL.md](DATA-MODEL.md) | шесть миграций, 11 таблиц, поля позиции, идентификаторы |
| [REQUESTS-SOURCES-COUNTERPARTIES.md](REQUESTS-SOURCES-COUNTERPARTIES.md) | путь заявки от источника до позиции, все семь источников, контрагент |
| [MATCHING.md](MATCHING.md) | контракт `/api/navigator`, фильтры, сортировки, карточка, ставка рекрутера |
| [ADMIN-PANEL.md](ADMIN-PANEL.md) | что администратор может делать сегодня и где |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Google Sheets, Telegram, Seatable, LLM, Яндекс.Диск, Prometheus |
| [AS-IS-VS-TO-BE.md](AS-IS-VS-TO-BE.md) | построчная сверка «есть / нет» |
| [../REGISTRY.md](../REGISTRY.md) | рабочая записка: развёртывание на новую базу, порядок скриптов |
| [../README.md](../README.md) | запуск, структура репозитория, тесты, порядок разработки |

### Как должно быть

| Файл | О чём |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | зачем система, границы, принципы, глоссарий |
| [USER-FLOWS.md](USER-FLOWS.md) | сценарии рекрутера, администратора и автоматики |
| [VALIDATION.md](VALIDATION.md) | обязательность полей, состояния значения, типы проблем |
| [OUTREACH-BOT.md](OUTREACH-BOT.md) | правила общения робота с контрагентом |
| [NAMING.md](NAMING.md) | слои названий и правило неутечки |
| [ROLES-AND-ACCESS.md](ROLES-AND-ACCESS.md) | роли, вход, разделение данных |
| [MOBILE.md](MOBILE.md) | брейкпоинты, тач-таргеты, нижние листы, чек-лист приёмки |
| [FRONTEND-PACKAGE.md](FRONTEND-PACKAGE.md) | переданный макет `navigator/`: состав, запуск, что нельзя переносить |

### Решения

| Файл | О чём |
|---|---|
| [LIMITATIONS.md](LIMITATIONS.md) | временные решения, хрупкие места, спорное |
| [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) | вопросы к заказчику: без ответов часть задач начинать нельзя |

### Постановка задач

[../DEVELOPER_TASKS.md](../DEVELOPER_TASKS.md) — правила, порядок работ и сводный
указатель. Полные описания разложены по направлениям в [tasks/](tasks/):
`frontend`, `backend`, `database`, `telegram`, `integrations`, `security`, `mobile`,
`testing`.

## С чего начать

Новому разработчику: [OVERVIEW.md](OVERVIEW.md) → [../README.md](../README.md) →
[AS-IS-VS-TO-BE.md](AS-IS-VS-TO-BE.md) → [../DEVELOPER_TASKS.md](../DEVELOPER_TASKS.md).

Перед началом любой работы: [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).
