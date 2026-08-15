# Переданный фронтенд-пакет `navigator/`

Каталог: `/Users/romanknyazev/Desktop/eltera-navigator/navigator/`. Ветка `feat/navigator-frontend`, каталог untracked (`git status` → `?? navigator/`).

Документ описывает пакет **как есть** на момент передачи. Всё, что в пакете заявлено словами, но не реализовано кодом, вынесено в отдельные разделы и помечено прямо.

---

## 1. Что это такое и чем не является

**Это макет-эталон интерфейса на моковых данных.** Две HTML-страницы (`navigator/index.html`, `navigator/navigator.html`), исполняемые собственным клиентским рантаймом `navigator/support.js`. Все данные — константы внутри `navigator/navigator.html:1091-1447`. Ни одного `fetch`, `XMLHttpRequest` или WebSocket в пакете нет; единственный вызов браузерного API наружу — `navigator.clipboard.writeText` (`navigator/navigator.html:2419`).

**Чем не является:**

- Это **не часть работающего приложения**. В `app.py` нет ни `mount` каталога `navigator/`, ни роута, ни ссылки: единственный статический mount — `app.py:184` `app.mount("/static", StaticFiles(directory="static"), name="static")`. Рабочий экран отдаётся роутом `app.py:366` `@app.get("/navigator")` и читает `templates/navigator.html`, а не файл из пакета.
- Это **не бэкенд-контракт**. Ни одного упоминания роутов `/api/navigator`, `/api/rates`, таблиц реестра или метрик в пакете нет.
- Это **не источник конфигурации**. `navigator/.env.example` описывает Flask-стек (`FLASK_ENV`, `DATABASE_URL`, `LLM_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`), которого в репозитории нет: приложение — FastAPI + uvicorn + Jinja2 + APScheduler + SQLite(WAL, FTS5), доступ — HTTP Basic по `WEB_USER`/`WEB_PASSWORD` (`app.py:191-208`). Ни одна переменная из `navigator/.env.example` питон-кодом не читается.
- Это **не прототип авторизации для прода**: пароли сравниваются в браузере (`navigator/navigator.html:1711`, `navigator/eltera/account.js:69`).

Ценность пакета — эталон верстки, палитры, состояний полей, текстов интерфейса и сценариев админки. Именно в этом качестве он и используется: часть уже перенесена в `templates/navigator.html` (см. §11).

---

## 2. Состав пакета

Размеры — фактические, из `wc -c`.

| Файл | Размер | Строк | Назначение |
|---|---|---|---|
| `navigator/README.md` | 2 112 Б | 41 | Инструкция запуска, демо-пароли, обещание серверной сессии |
| `navigator/package.json` | 479 Б | 18 | npm-скрипты, `devDependencies: serve ^14.2.1`, `engines.node >= 18`, `private: true` |
| `navigator/index.html` | 27 488 Б | 314 | Экран входа |
| `navigator/navigator.html` | 193 448 Б | 2 538 | Главный экран: подбор + админ-панель + все моковые данные |
| `navigator/support.js` | 64 222 Б | 1 768 | Сгенерированный рантайм dc-runtime |
| `navigator/eltera/account.js` | 5 245 Б | 112 | Единая учётная запись на localStorage |
| `navigator/scripts/check.js` | 2 029 Б | 43 | Проверка локальных ссылок и секретов |
| `navigator/scripts/build.js` | 1 230 Б | 29 | Копирование отдаваемых файлов в `dist/` |
| `navigator/.env.example` | 1 126 Б | 43 | Пример переменных окружения (устаревший, см. §1) |
| `navigator/.gitignore.example` | 363 Б | 25 | Пример `.gitignore` |
| `navigator/assets/background-loop.mp4` | 20 064 727 Б (≈20 МБ) | — | Фоновое видео обоих экранов |
| `navigator/assets/logos/eltera_logo_horizontal_on_dark.svg` | 7 996 Б | 18 | Логотип в шапке навигатора |
| `navigator/eltera/ui_kits/landing/assets/logo.svg` | 7 997 Б | 19 | Логотип на экране входа |

Итого 13 файлов, из них 20 МБ (98,5 % объёма) — одно видео.

---

## 3. Как запускать

Из `navigator/README.md:8-21`:

```
cd navigator
cp .env.example .env      # ничем не читается, шаг декоративный
npm start                 # → npx --yes serve . -l 5173
```

Открыть `http://127.0.0.1:5173` — это `index.html`, экран входа. Альтернатива без Node: `python3 -m http.server 5173` из каталога `navigator/` (`navigator/README.md:21`). Любой статический сервер годится: сборки нет, страницы открываются напрямую.

Скрипты (`navigator/package.json:6-11`):

| Скрипт | Команда | Что делает |
|---|---|---|
| `start` / `dev` | `npx --yes serve . -l 5173` | статический сервер на 5173 |
| `check` | `node scripts/check.js` | проверка ссылок и секретов |
| `build` | `node scripts/build.js` | `check` + копия в `dist/` |

**`npm run check` — `navigator/scripts/check.js`.** Зависимостей нет, только `fs` и `path`.

1. Локальные ссылки (`navigator/scripts/check.js:8-27`). Проверяются `index.html` и `navigator.html`. Регэксп `navigator/scripts/check.js:9`: `/(?:src|href)="(?!https?:|#|data:|mailto:)([^"{}]+)"/g` — внешние URL, якоря, `data:`, `mailto:` и любые значения с `{`/`}` (то есть интерполяции `{{ … }}`) пропускаются. Для каждой уникальной ссылки — `fs.existsSync`.
2. Секреты (`navigator/scripts/check.js:29-39`). Один регэксп: `bot[0-9]{6,}:[A-Za-z0-9_-]{30,}` (токен Bot API), `sk-[A-Za-z0-9]{20,}`, `-----BEGIN … PRIVATE KEY-----`. Рекурсивный обход от корня, пропуск `node_modules` и `.git`, только `.html|.js|.json|.md|.example`. Наличие `.env` в пакете — тоже ошибка (`:40`).

При `errors > 0` — `process.exit(1)`. **Чего проверка не ловит:** пароль `eltera2026` (`navigator/eltera/account.js:16`), пароль администратора `1207` (`navigator/navigator.html:1697`), маскированный токен `'7841…AAH'` в `CPS`, семь `chat_id` вида `-1001998877665`, ФИО и телефоны контактных лиц. То есть «секретов нет» в выводе скрипта означает только «нет строк указанных трёх форматов».

**`npm run build` — `navigator/scripts/build.js`.** Комментарий `navigator/scripts/build.js:2`: «Сборки как таковой нет — это статический пакет без бандлера».

Синхронно запускает `check.js` тем же интерпретатором (`navigator/scripts/build.js:12`) — при неуспехе билд падает; затем `fs.rmSync(dist, {recursive:true, force:true})` (`:14`) и рекурсивное копирование whitelist `['index.html','navigator.html','support.js','assets','eltera']` (`:10`). В `dist/` **не попадают** `package.json`, `README.md`, `scripts/`, `.env.example`, `.gitignore.example`. Минификации, бандлинга, хеширования имён и tree-shaking нет.

---

## 4. Формат x-dc и рантайм `support.js`

### Что это

`x-dc` — декларативный формат разметки, исполняемый рантаймом dc-runtime. Первая строка `navigator/support.js:1`: `// GENERATED from dc-runtime/src/*.ts — do not edit. Rebuild with 'cd dc-runtime && bun run build'.` — файл собран bun-бандлером из TypeScript-модулей (`src/react.ts`, `src/parse.ts`, `src/boot.ts`, `src/compile.ts`, `src/cdn.ts`, `src/registry.ts` и др.), завёрнут в IIFE со `"use strict"`.

Обе страницы устроены одинаково (`navigator/navigator.html:1-9`, `navigator/index.html:1-9`): в `<head>` — `<script src="./support.js">`, в `<body>` — `<x-dc>` с шаблоном, ниже — `<script type="text/x-dc" data-dc-script>` с классом логики (`navigator/navigator.html:1053-1054`, `navigator/index.html:187-188`). Тип `text/x-dc` браузером не исполняется — код извлекается рантаймом и вычисляется через `new Function` (`navigator/support.js:772-780`).

### Свои теги

| Тег | Обработчик | Атрибуты |
|---|---|---|
| `<x-dc>` | корневой контейнер шаблона | — |
| `<helmet>` → `<sc-helmet>` (`navigator/support.js:377`) | содержимое переносится в `<head>` | — |
| `<sc-for>` | `walkFor` (`navigator/support.js:547-579`) | `list="{{ … }}"`, `as="имя"` (default `item`), `hint-placeholder-count`; внутри доступны `{{ имя }}` и `{{ $index }}` |
| `<sc-if>` | `walkIf` (`navigator/support.js:582-596`) | `value="{{ … }}"`, `hint-placeholder-val` |
| `<x-import>` | `walkXImport` (`navigator/support.js:626+`) | `from="URL"` (только литерал), `component`, `dcProps`, `hint-size` |
| `<dc-import>` | `walkComponent` (`navigator/support.js:595-625`) | `name`/`component`, `dcProps` |
| `<sc-raw-select\|table\|tbody\|thead\|tfoot\|tr\|td\|th\|caption>` | `RAW_WRAP` (`navigator/support.js:303-312`) | обёртки против порчи HTML-парсером |

В пакете фактически используются только `x-dc`, `helmet`, `sc-for`, `sc-if`.

Атрибутный протокол: `{{ выражение }}` — интерполяция (`compileAttr`, `navigator/support.js:400-412`); `class` → `className`, `for` → `htmlFor` (`:436-437`); `on*` мапится по `EVENT_MAP` (`:317+`); `sc-camel-*` восстанавливает camelCase, потерянный парсером (`:297`); `ref="{{ … }}"` прокидывается как React ref; `style-<pseudo>="css"` порождает класс `scpN` с правилом `.scpN:<pseudo>{…}` в динамическом `<style>` (`createPseudoSheet`, `navigator/support.js:1428-1446`).

Класс логики наследуется от `DCLogic` (алиас `StreamableLogic`, `navigator/support.js:1755-1757`): поле-инициализатор `state = {…}`, методы-стрелки как обработчики, `setState`, `componentDidMount/DidUpdate/WillUnmount`, а значения для `{{ … }}` отдаёт метод `renderVals()`.

### Откуда грузит React

`navigator/support.js:1071-1082`:

```
REACT_URL     = "https://unpkg.com/react@18.3.1/umd/react.production.min.js"
REACT_DOM_URL = "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js"
BABEL_URL     = "https://unpkg.com/@babel/standalone@7.29.0/babel.min.js"
```

У всех трёх прописаны `integrity` (SRI) и `crossOrigin="anonymous"`. `cdnScriptFor` (`navigator/support.js:1078-1082`) сначала смотрит `window.__resources[url]` — если там строка, берётся она (механизм офлайн-подмены; в пакете `__resources` не определён). Babel грузится лениво, только под `<x-import kind="jsx">` (`ensureBabel`, `navigator/support.js:1104-1122`) — в этом пакете не срабатывает.

### Почему нужен интернет

`loadReactUmd()` (`navigator/support.js:1696-1706`) грузит React/ReactDOM и только после этого вызывает `init()`. Без доступа к `unpkg.com` (или без локальной подмены через `window.__resources`) **обе страницы остаются пустыми**, в консоли — `[dc] failed to load React or boot:`. Дополнительно с сети тянется шрифт Inter с `fonts.googleapis.com` (`navigator/navigator.html:15`, `navigator/index.html:13`) — уже без SRI.

### Почему в продакшн его тащить нельзя

1. **Логика страницы вычисляется через `new Function`** — `evalDcLogic` (`navigator/support.js:772-780`) и загрузка внешних модулей (`navigator/support.js:1148`). Это динамическое исполнение строк: строгий CSP (`script-src` без `'unsafe-eval'`) его ломает, а сам паттерн помечен в исходнике как `//! nosemgrep: eval-and-function-constructor`.
2. **Три внешние зависимости с CDN на критическом пути рендера.** Недоступность unpkg.com = белый экран, без деградации.
3. **Мост к хост-редактору.** Рантайм публикует в `window` набор `__dcUpdate`, `__dcSetProps`, `__dcTemplateSource`, `__dcRegistry`, `getDC`, `DCLogic` и шлёт `postMessage({type:'__dc_booted', …}, '*')` в `window.parent`, если страница во фрейме (`navigator/support.js:1712-1758`). Wildcard-origin и внешний API управления состоянием — это инструментарий редактора, не прод.
4. **Диагностика редактора в рантайме**: классы `.sc-placeholder`, `.sc-interp.sc-missing`, `.sc-unresolved`, `.sc-logic-error` (красная плашка `#b00020` поверх компонента, `z-index:2147483647`) — `navigator/support.js:86-131`. И **двойной разбор документа**: при отсутствии `window.__resources` рантайм дополнительно делает `fetch(location.href)` и повторно парсит сырой HTML (`boot()`, `navigator/support.js:150+`).

Рабочий `templates/navigator.html` от рантайма отказался целиком: строковый рендер на ванильном JS, делегирование событий по `data-act`/`data-on`, никаких внешних скриптов.

---

## 5. Экран входа `index.html` и единая учётная запись

### `index.html`

Корень `div.authRoot` (`navigator/index.html:46`), фон `#0A0F1E`, фоновое видео `assets/background-loop.mp4` + затемнение `rgba(0,0,0,.62)` (`:48-51`), карточка `max-width:428px` с анимацией `authFade .5s` (`:53`). Подключает store: `<script src="eltera/account.js">` (`navigator/index.html:40`).

Три стадии, `state.stage ∈ {'form','verify','recover'}` (`navigator/index.html:189`):

- **`form`** (`:67-143`) — почта и пароль. Валидация целиком: `emailOk = /.+@.+\..+/.test(s.email)`, кнопка активна при `emailOk && s.password.length >= 8` (`navigator/index.html:255-256`). Вход (`:207-214`): `acc.signIn(email, password, 'navigator')`; при `r.ok` — `window.location.href = 'navigator.html'` (`:204`). **Если `window.ElteraAccount` не найден — переход выполняется без всякой проверки** (`navigator/index.html:209`).
- **`verify`** (`:145-155`) — ввод 6 цифр, подпись «Прототип: введите любые 6 цифр» (`:153`). Стадия **недостижима**: `stage:'verify'` нигде не устанавливается.
- **`recover`** (`:157-176`) — форма восстановления; `setState({recoverSent:true})` и ничего больше (`:225`), реального сброса нет.

Скрытый блок регистрации (`navigator/index.html:92-126`) обёрнут в `<sc-if value="{{ never }}">`, а `never` в `renderVals()` не возвращается → `undefined` → **не рендерится никогда**. Тексты ошибок (`navigator/index.html:260-263`) прямо описывают модель единого аккаунта: «Учётная запись с такой почтой не найдена. Она общая для Навигатора, кабинета Eltera и Shellix.» / «Неверный пароль. Он тоже общий для всех трёх систем.» / «Учётная запись есть, но доступ к Навигатору не выдан — обратитесь к администратору.» Автовход (`navigator/index.html:229-244`): в `componentDidMount`, если `acc.session() && acc.get().access.navigator` — сразу редирект.

### `eltera/account.js`

IIFE, экспорт в `window.ElteraAccount` (`navigator/eltera/account.js:107-111`). **Ключи localStorage** (`:7-8`): `eltera.account.v1` — учётная запись, `eltera.session.v1` — сессия.

**Модель записи** (`DEFAULTS`, `navigator/eltera/account.js:12-22`):

```js
{ id:'EMP-0442', name:'Анна Кузнецова', email:'anna.kuznetsova@romashka.ru',
  password:'eltera2026', phone:'+7 900 123-45-67',
  access: { navigator:true, cabinet:true, crm:true },
  roles:  { navigator:'Менеджер по подбору', cabinet:'Сотрудник', crm:'Рекрутер' },
  updatedAt:'' }
```

`get()` (`:35-41`) сливает `DEFAULTS` с содержимым localStorage, отдельно домердживая `access` и `roles`. Следствие: запись **существует всегда**, даже при пустом хранилище — это и есть демо-учётка. Пароль лежит в открытом виде; комментарий `navigator/eltera/account.js:10-11` это признаёт: «в проде — только хеш на сервере».

**Гранты доступа к трём продуктам** (`PRODUCTS`, `navigator/eltera/account.js:24-28`):

| key | name | note | href |
|---|---|---|---|
| `navigator` | Навигатор | Подбор позиций | `Навигатор.dc.html` |
| `cabinet` | Кабинет Eltera | Результаты оценок | `Портал сотрудника.dc.html` |
| `crm` | Shellix | CRM подбора | `CRM.dc.html` |

Грант проверяется только в `signIn(email, password, product)` (`navigator/eltera/account.js:70`): `if (product && !a.access[product]) return { error: 'access' }`. `products()` (`:86-91`) отдаёт список с флагом `allowed` и ролью, но **ни `index.html`, ни `navigator.html` его не вызывают** — переключателя продуктов в интерфейсе нет.

> **Дефект пакета.** Все три `href` в `PRODUCTS` (`navigator/eltera/account.js:25-27`) указывают на файлы `Навигатор.dc.html`, `Портал сотрудника.dc.html`, `CRM.dc.html`, **которых в пакете нет** — в каталоге лежат `index.html` и `navigator.html`. `scripts/check.js` этого не ловит: он разбирает только атрибуты `src=`/`href=` в HTML двух страниц (`navigator/scripts/check.js:8-9`), а не строковые литералы в JS. Любая навигация, построенная на `products()`, приведёт в 404.

**Правило «смена почты или пароля разлогинивает всё»** — `update(patch)` (`navigator/eltera/account.js:51-61`), единственная точка изменения записи. Штампует `updatedAt` в формате `ДД.ММ.ГГГГ ЧЧ:ММ` (`stamp()`, `:43-47`), нормализует `patch.email` через `trim().toLowerCase()` (`:55`) и:

```js
if (patch && (patch.email || patch.password)) signOut();   // navigator/eltera/account.js:58
```

Сессия одна на все продукты (комментарий `navigator/eltera/account.js:79`), поэтому `signOut()` (`:84`) закрывает доступ сразу ко всем трём. Дополнительный контур защиты — `session()` (`:76-82`): если почта в сессии не совпала с текущей почтой учётки, сессия сносится. **Механизма нет:** `update()` в пакете **не вызывает ни один экран** — форм профиля и настроек аккаунта нет; правило реализовано, но недостижимо из интерфейса. Кнопки «Выйти» тоже нет нигде.

**Прочее:** межвкладочная синхронизация через `window.addEventListener('storage', …)` на ключи `ACC`/`SES` (`navigator/eltera/account.js:103-105`); подписка `subscribe(fn)` с отпиской (`:98-101`), ошибки подписчиков глотаются (`:95`). У сессии **нет срока жизни**: `at: Date.now()` пишется (`:71`), но нигде не проверяется.

**Главное про пакет в целом:** `navigator.html` этот store **не подключает вовсе** — ни `account.js`, ни `ElteraAccount`, ни `signOut` в файле не встречаются. Прямой переход на `navigator.html` открывает весь интерфейс без входа.

---

## 6. Экран `navigator.html`

Схема пропсов — атрибут `data-props` (`navigator/navigator.html:1053`): `employerDisplay` (enum `alias|type|geo|none`, default `alias`), `showInternal` (boolean, `true`), `gapHighlight` (boolean, `true`), `radiusDefault` (int 0…200 шаг 25, `0`). `gapHighlight` **не читается ни разу** — объявлен и мёртв.

### Режимы верхнего уровня

`state.screen ∈ {'search','norm'}` (`navigator/navigator.html:1055`). Шапка `position:sticky` (`:63-83`) содержит логотип, заголовок «Навигатор», зашитую строку `runLine = 'прогон 13:02 · следующий 12:00'` (`navigator/navigator.html:2280`) и две вкладки (`:2281-2284`): «Подбор» со счётчиком `ROWS.length` = 11 и «Админ» со счётчиком исключений; вторая проходит через `_openAdmin()` (`:1705-1708`) — пароль, если устройство не запомнено.

### Экран «Подбор» (`navigator/navigator.html:85-481`)

Две колонки: `aside.navAside` (`flex:1 1 320px`) и `section.navMain` (`flex:99 1 520px`), обе sticky, `height: calc(100vh - 124px)`. **Панель «Города»** (`:90-167`): поиск с нормализацией ё→е (`:2041`), чипы выбранных городов с крестиком и кнопкой «сбросить», список с чекбоксом / регионом / бейджем «N км» / числом позиций; тумблер «Соседние города» (`radius ? 0 : 50`, `:2304`), пилюли радиуса **25 / 50 / 100 км** (`:2305-2308`) и подпись `nearSummary` (`:2309-2311`). Расстояние — гаверсинус, `R = 6371` (`_dist`, `:1491-1496`); соседи — все города реестра, не выбранные вручную, у которых расстояние до любого якоря ≤ radius (`_nearNames`, `:1500-1508`).

**Панель «Условия и требования»** (`:169-218`). Числовые фильтры (`:2313-2316`): «Ставка кандидата от» (`rateMin`), «Возраст кандидата» (`ageCand`). Селекты (`:2317-2324`): Гражданство (`любое / РФ / РФ + РБ / ЕАЭС`), Проживание (`любое / есть / бесплатно / нет`), Питание (то же), Пол (`любой / мужчины / женщины / пары`), Медкнижка (`любая / не нужна / можно без неё`), Проверка СБ (`любая / нет / кроме полной`). Под кнопкой «Ещё фильтры» (`:2325-2329`): График (`вахта / 5/2 / 6/1 / 2/2`), Выплаты (`еженедельно / 2 раза в месяц / есть аванс`), Тип ставки рекрутера (`процент / фикс / не задана`). Счётчик активных доп-фильтров — `moreCount` (`:2332-2333`), «сбросить» — `resetFilters` (`:2335`, города и `q` не трогает). Фильтр `recPct` работает в `_pass` (`:1926`), но **контрола в интерфейсе нет**.

**Фильтрация** — `_pass(r)` (`:1916-1970`), порядок: города и радиус → `rate >= rateMin` → тип ставки рекрутера → `recPct` → проживание → питание → гражданство (`ЕАЭС` матчит `ЕАЭС`/`КЗ`, `РФ + РБ` требует `РБ`) → пол (`пары` требует `r.c > 0`) → возраст (`_passAge`, `:1908-1914`) → медкнижка → СБ → график → выплаты → scope → поиск. Поиск (`:1964-1968`): склейка `prof, req, cp, obj, city, district, cits, sched, src`, lowercase, ё→е, все токены обязаны входить (AND по подстроке).

**Scope-пилюли** (`:2337-2344`): «Все активные» (11), «Под заявку» (отбор `/комплектов|сборщик/i`), «С пробелами» (`_issues(r).length > 0`). При `scope==='req'` появляется плашка с зашитыми `reqTitle = 'Комплектовщик · Московская область · 25 человек'` и `reqMeta = 'ЗАК-2026-0184 · закрыть до 25 августа · ответственный Кузнецова'` (`:2346-2347`).

**Сортировки** (`_sorted`, `:1972-1987`): `rate` — ставка кандидата ↓ (default); `rec` — ранг типа (нет=0, фикс=1, процент=2), затем `value` ↓; `need` — `_needTotal` = `m + w + c*2` ↓ (`:1520`); `fresh` — `String(a.seen).localeCompare(…)`, то есть **лексикографическое сравнение строк**, а не дат; `dist` — от первого выбранного города. При смене любого фильтра список прокручивается в начало (`componentDidUpdate`, `:1474-1478`). Строка результата: `foundText` = «Найдено N из 11 активных позиций» (`:2357`), `gapText` = «N проблем требуют решения» (`:2358`).

### Карточка позиции (`navigator/navigator.html:267-468`)

Восемь блоков: 1) заголовок + место + плашка «Ставка рекрутера» (состояния `recKnown` с формулой и условием / `recGap` — точка `#D9A441` и «не задана»); 2) факты — Ставка кандидата, График, Смена, Потребность, Обновлено (`:2077-2083`); 3) «Условия» — Проживание, Питание, Гражданство с иконками (`:2085-2089`); 4) «Деньги — озвучить до выезда» — Выплаты, Удержания (со строкой «возврат после N смен»), Проезд (`:354-413`); 5) чипы: пол, возраст, МК, СБ, тест (`:2101-2107`); 6) материалы — живые ссылки `target="_blank" rel="noopener"`, битые — жёлтая кнопка «{label} — недоступна» с тостом (`:421-438`); 7) панель действий — «Текст кандидату», «Подробнее», «Требуют решения: N», справа id позиции; 8) плашка «только внутри» с контрагентом, объектом и `srcLine` (`:460-467`, скрывается при `showInternal === false`).

**Единая модель состояния поля** `_field(k,v,st)` (`:1989-1991`) — пять состояний: `yes` (значение), `no` (приглушённое), `def` (значение + янтарная точка + «значение по умолчанию — подтвердить»), `conflict` (красная точка + «конфликт источников» + текст конфликта), `na` (серая точка + «не указано»). `_mediaList` (`:1621-1631`) дедуплицирует по `kind|url` (url в lowercase, query отброшена) и прячет `housing_photo`, если `house.st !== 'yes'`.

### Экран «Админ» (`navigator/navigator.html:483-724`)

Вход: `_authed()` (`:1700-1703`) = `state.authOk || localStorage['eltera_navigator_admin_v1'] === 'ok'`; `_submitAuth()` (`:1710-1715`) сверяет с `ADMIN_PASS = '1207'` (`navigator/navigator.html:1697`). Левая колонка: блок «Автопроверка» с четырьмя KPI (`:2424-2429`) — принято автоматически / запросов заказчикам / ссылок перепроверяется / исключений; строка «Запросы уходят ежедневно в 09:00» (`SEND_TIME`, `:1352`) и кнопка «Отправить сейчас» (тост). Ниже «Шаблон сообщения» с `MAX_TRIES = 3` (`:1351`).

Три вкладки (`:2436-2440`):

- **«Исключения»** (`:542-586`) — очередь из трёх источников (`:2223-2265`): issues кодов `conflict`/`duplicate` (дубли дедуплицируются условием `i.dupWith < r.id`, `:2226`), «мёртвые» outreach со статусами «Нет ответа» / «Ошибка доставки», источники со `status === 'error'`. Действия: `mainAction` («Открыть обе заявки» при дубле, иначе «Уточнить в чате»), «Объединить» / «Разные позиции» при дубле, «Отложить на сутки».
- **«Запросы»** (`:588-629`) — сгруппированные по контрагенту тексты (`byCp`, `:2173-2190`; `_outreachText`, `:1741-1751`), статусы «Ждём ответа» / «Нет ответа N дн.» / «Не доставлено» (`:2212-2214`), блок «уже в запросе, повторно не спрашиваем: …» и **догоняющее сообщение** без приветствия. Кнопки «Открыть чат» и «Отправить повторно» — тосты.
- **«Контрагенты»** (`:631-718`) — 7 карточек со статусом, меню «⋮» (Редактировать / Исключить / Вернуть / Удалить), блок «Архив», блок «Конфигурация источников» (`GITHUB`, `:1429`) и блок «Материалы и ссылки» со всеми битыми ссылками (`:2268-2273`).

### Модалки (`navigator/navigator.html:727-1043`)

Общий каркас: оверлей `rgba(4,7,14,.66)` + `backdrop-filter:blur(3px)`, клик по оверлею закрывает; панель `.liquid-glass.appScroll`, `max-height: calc(100vh - 40px)`. Escape обрабатывается по приоритету `authOpen → cpDelete → cpMenu → cpEdit → modal → gaps → detail` (`_esc`, `:1452-1463`).

| Модалка | z-index | Ширина | Строки |
|---|---|---|---|
| Незаполненные поля | 80 | `min(460px,100%)` | `:727-750` |
| Подробнее о позиции | 80 | `min(640px,100%)` | `:752-866` |
| Текст для кандидата | 80 | `min(560px,100%)` | `:868-939` |
| Настройки контрагента | 85 | `min(560px,100%)` | `:984-1043` |
| Удаление контрагента | 88 | `min(420px,100%)` | `:962-982` |
| Пароль администратора | 90 | `min(380px,100%)` | `:941-960` |

«Подробнее» показывает пять групп полей (`dGroups`, `:2125-2160`): Обозначения, Условия, Кандидат, Быт, Деньги; плашку ставки рекрутера с пятью строками — **База расчёта, Формула, Условие начисления, Этап выплаты, Гарантийный период** (`:2373-2379`); секции «Материалы» и «Как пришло от заказчика» (`RAW`). «Текст для кандидата»: генератор `_candidateText` (`:1843-1906`) — 11 абзацев от должности до «Если подходит — напишите, забронирую место и пришлю порядок выхода»; чекбоксы материалов (доступны только `vis === 'public' && alive`); блок «Вырезано стоп-словарём» с чипами того, что вырезал `_scrub` (`:1824-1832` — заменяет `r.cp`, `r.obj`, `r.src` на `———`); копирование через `navigator.clipboard` (`:2419`).

«Удаление контрагента» сверяет пароль с тем же `ADMIN_PASS` (`_cpConfirmDelete`, `:1781-1794`) и по наличию связей выбирает между архивированием и полным удалением (`delNote`, `:2484-2488`).

### Состояния

- **Пусто**: список позиций (`:472-477`, «Под эти параметры позиций нет» + подсказка расширить радиус), исключения (`:580-585`), запросы (`:624-628`). Для вкладки «Контрагенты», списка городов и списка материалов пустого состояния **нет**.
- **Загрузка**: собственных скелетонов нет. `hint-placeholder-count` / `hint-placeholder-val` рисуются классом `.sc-placeholder` только при стриминге шаблона в редакторе; в статике не срабатывают.
- **Ошибка**: экрана ошибки нет. Ошибки выражены состояниями данных (`na`, `conflict`, «недоступна») и текстом «Неверный пароль» в двух модалках. Сбой логики компонента отрисуется плашкой `.sc-logic-error` рантайма (`navigator/support.js:112-114`).

**Тосты** (`_toast`, `:1485-1489`): позиция `fixed`, низ по центру, `.liquid-glass`, автоскрытие 2400 мс. Все действия админки — отправка, повтор, проверка подключения, тестовое сообщение, синхронизация с GitHub, запрос доступа — **только тосты**, сетевых вызовов нет.

**Живые баги макета:** `_gapList` вызывается в `_pass` (`navigator/navigator.html:1962`), но в файле **не определён** — клик по пилюле «С пробелами» валит рендер `TypeError`; `componentDidMount` ищет `video[src*="codenest_background"]` (`:1465`), тогда как реальный `src` — `assets/background-loop.mp4` (`:57`); сортировка «Свежие» сравнивает строки (`:1977`); `SOURCES` и `CPS` сопоставляются по имени (`:2454`), но пересекаются только по `КПК`.

---

## 7. Моковые данные

Все — константы класса в `navigator/navigator.html`.

| Константа | Строки | Объём | Поля |
|---|---|---|---|
| `CITIES` | `:1091-1102` | 10 | `name, region, lat, lon` |
| `RECRUITER_RATES` | `:1105-1114` | 8 контрагентов | `kind ('percent'\|'fixed'), value, baseAmount, base, rule, stage, guar`; ключ — имя контрагента |
| `ROWS` | `:1117-1286` | 11 позиций | см. ниже |
| `RAW` | `:1289-1295` | 5 | id → многострочный текст «как пришло от заказчика» |
| `RAWFIELD` | `:1297-1303` | 5 | id → `{ 'название поля': 'исходная строка' }` |
| `DICT_FIELDS` | `:1305` | 6 | `Должность, Город, Регион, График, Гражданство, Тип жилья` |
| `REQUIRED` | `:1308-1311` | 7 | `cits, house, meals, pay, rate, sched, need` → человеческие названия |
| `ISSUE_KINDS` | `:1313-1320` | 6 | `conflict, duplicate` (sev 1, `#FF7B6B`), `missing, format, unparsed` (sev 2, `#D9A441`), `source` (sev 3) |
| `CONFLICTS` | `:1322-1329` | 1 | id, поле, описание, значения по источникам, рекомендация |
| `FORMATS` | `:1331-1338` | 1 | id, поле, raw, рекомендация |
| `UNPARSED` | `:1340-1347` | 1 | id, поле, raw, рекомендация |
| `DUPES` | `:1349` | 1 пара | `ELT-2026-000473-01 ↔ ELT-2026-000412-01`, двусторонняя |
| `CP_TITLES` | `:1356-1368` | 11 | `{ t: заголовок у контрагента, ref: внешний номер }` |
| `OUTREACH` | `:1371-1376` | 4 | `attempt, sent, silent, greetedToday, asked[]` |
| `FIELD_KEYS` | `:1379-1382` | 7 | чипы «обязательные поля» в настройках контрагента |
| `CPS` | `:1384-1420` | 7 | `id, name, alias, kind, address, times, required[], contact, bot, token, chat, thread, sendTime, tpl, delivery, deliveryOk` (+ `excluded`, `archived` в рантайме) |
| `SOURCES` | `:1422-1427` | 4 | `id, cp, kind, address, times, last, status, pos, error` |
| `GITHUB` | `:1429` | 1 | `repo, branch, file, synced` |
| `LINK_STATE` | `:1431-1434` | 2 | ключ `{rowId}\|{mediaKind}` → `статус, dot, meta` |
| `MEDIA_META` | `:1436-1442` | 5 | `housing_photo, object_photo, video, telegram, route` → `label, emoji, text, icon` |
| `ALIASES` | `:1444-1447` | 2 | `kind, from, to, hits` — **нигде не используется** |

Плюс скаляры `MAX_TRIES = 3` и `SEND_TIME = '09:00'` (`navigator/navigator.html:1351-1352`). **Поля записи `ROWS`** (`navigator/navigator.html:1117-1286`): `id, ext, src, seen, cp, cpAlias, cpType, obj, prof, city, district, addr, rate, hourly, unit, net, sched, schedKind, hours, shiftType, minShifts, m, w, c, gender, ageFrom, ageTo, cits[], req, med, sb, test, house{st,cost,type,per,linen,transfer}, meals{st,times,note}, travel{st,full,amount,when,route}, pay{st,freq,advance,advAfter}, ded[], dedSt, media[]`. Элемент `media[]`: `{ kind, title, url, vis, alive, src, upd }`.

Города `CITIES`: Москва, Чехов, Домодедово, Подольск, Пушкино, Троицк, Владимир, Павлово, Санкт-Петербург, Шушары — с широтой и долготой.

Контрагенты в `RECRUITER_RATES`: ФМ Логистик, КПК, Озон, Все инструменты, Молком, БИГ, Лента, Миксит. Контрагенты `ФМ Чехов` и `Мерлион` из `ROWS` в таблице отсутствуют — это и есть демонстрация состояния «ставка не задана». Комментарий `:1104`: ставка «никогда не считается от ставки кандидата»; `_recAmount` (`:1525-1529`): `fixed → value`, `percent → round(baseAmount * value / 100)`.

**Мёртвые данные:** `RAWFIELD` и `DICT_FIELDS` используются только в `_normFields` (`:1993-2028`), который не вызывается; `ALIASES` не используется вовсе.

---

## 8. Дизайн-токены

CSS-переменных нет — все значения записаны инлайном в атрибутах `style`. Ниже фактическая сводка.

**Цвета**

| Роль | Значение | Где |
|---|---|---|
| Фон приложения | `#0A0F1E` | `navigator/navigator.html:17`, `navigator/index.html:15` |
| Базовый текст | `#E6F2FF` | `navigator/navigator.html:18` |
| Акцент-бирюза (ссылки, выбор) | `#A4F4FD` | `navigator/navigator.html:21` |
| Циан (фокус / «в процессе») | `#00d2ff` (index), `#16D7FF` (navigator) | `navigator/index.html:19`, `navigator/navigator.html:2213` |
| Успех | `#6EE7C1` | `navigator/navigator.html:2425` |
| Предупреждение, «по умолчанию», битая ссылка | `#D9A441` | `navigator/navigator.html:1316` |
| Ошибка, конфликт, дубль | `#FF7B6B` | `navigator/navigator.html:1314` |
| «Не указано» | `#64748B` | `navigator/navigator.html:302` |
| Акцентная кнопка | фон `#fff`, текст `#000` / `#0A0F1E` | `navigator/navigator.html:76` |

Ещё: чекбокс-акцент на входе `#1E5BFF` (`navigator/index.html:118`), оверлей модалок `rgba(4,7,14,.66)` + blur 3px (`navigator/navigator.html:729`), затемнение видеофона `rgba(0,0,0,.62)` (`:58`, `navigator/index.html:50`). Границы — `rgba(255,255,255,.08….15)`, поверхности — `rgba(255,255,255,.03….06)`, тёмные подложки под `<pre>` и поля — `rgba(10,15,30,.6)`, `rgba(20,30,52,.4)`. Заливки-состояния: янтарь `rgba(217,164,65,.10….18)` с рамкой `.25….35`; бирюза `rgba(164,244,253,.08….12)` с рамкой `.25….55`; красный `rgba(255,123,107,.12/.14)` с рамкой `rgba(248,113,113,.35)`; зелёный `rgba(110,231,193,.12)` с рамкой `.3`.

**Шрифты.** `'Inter', system-ui, -apple-system, sans-serif`, `-webkit-font-smoothing: antialiased` (`navigator/navigator.html:18`). Подключение с `fonts.googleapis.com`: навигатор — веса 400;600;700 (`:15`), вход — 400;500;600;700;800;900 (`navigator/index.html:13`). Шкала кеглей (px): 10 · 11 · 11.5 · 12 · 12.5 · 13 · 13.5 · 14 · 15 · 16 · 17 · 18 · 19 · 22; кнопки формы входа — `0.88rem`. Начертания 400 / 600 / 700. `letter-spacing: -.02em` у заголовков и крупных чисел, `.06em` у метки «только внутри». `font-variant-numeric: tabular-nums` у всех счётчиков, id и KPI. Все `<pre>` намеренно используют Inter, не моноширинный: `white-space: pre-wrap; word-break: break-word`.

**Радиусы:** 5 (чекбокс) · 10 (строка списка, пункт меню) · 12 (плитки, поля-плашки) · 14 (карточки админки, textarea) · 16 (модалки, иконки 56×56 на входе) · 18 (панели-секции) · 9999 / 100 px (пилюли, кнопки, поля ввода, тумблер, тост).

**liquid-glass** — два рецепта. Навигатор (`navigator/navigator.html:23-24`): `background: rgba(255,255,255,0.035)`, `background-blend-mode: luminosity`, `backdrop-filter: blur(18px) saturate(1.1)`, внутренние тени `inset 0 1px 1px rgba(255,255,255,0.14)` и `inset 0 -1px 0 rgba(255,255,255,.06)`, без внешней тени; рамка 1.4px — псевдоэлемент `::before` с вертикальным градиентом и `mask-composite: exclude`. Вход (`navigator/index.html:22-33`) тяжелее: `background: rgba(255,255,255,0.008)`, `backdrop-filter: blur(40px) saturate(1.3) brightness(1.06)`, плюс внешняя тень `0 30px 70px rgba(0,0,0,.4)`. Шапка навигатора (`:63`) повторяет рецепт инлайном, но без градиентной рамки.

**Анимации:** `@keyframes appFade { opacity 0→1, translateY(8px)→0 }` (`navigator/navigator.html:25`) — карточки `.35s`, модалки и тост `.25s`, easing `cubic-bezier(.22,1,.36,1)`; `@keyframes authFade` (`navigator/index.html:21`) — `.5s`. Переходы кнопок и полей `.2s`, тумблер `.3s cubic-bezier(.4,0,.2,1)`, кнопка входа `all .3s cubic-bezier(.22,1,.36,1)`. Нажатие главных белых кнопок — `style-active="transform:scale(.98)"`. Скроллбары `.appScroll` (`:26-29`): `thin`, thumb `rgba(255,255,255,.14)`, 8px, трек прозрачный. `@media (prefers-reduced-motion: reduce)` глушит всё в обоих файлах (`navigator/navigator.html:30`, `navigator/index.html:38`).

---

## 9. Адаптив

Всего четыре media-запроса на пакет (плюс печатный из рантайма).

**`navigator/navigator.html`**

1. `@media (prefers-reduced-motion: reduce)` (`:30`) — `* { animation: none !important; transition: none !important; }`.
2. `@media (max-width: 900px), (max-height: 560px)` (`:33-39`) — колонки перестают быть панелями во всю высоту, скроллит страница: `.navHeader { position: static !important; margin-top: 0 !important }`; `.navAside, .navMain { position: static !important; height: auto !important; min-height: 0 !important; overflow: visible !important }`; `.navAside .appScroll, .navMain .appScroll { max-height: none !important; overflow: visible !important }`; `.navAside .cityList { max-height: 260px !important; overflow-y: auto !important }`.
3. `@media (max-width: 700px)` (`:41-51`) — телефон: тач-таргеты `.navRoot button, select, a[target="_blank"] { min-height: 40px }`; тумблер выведен из-под этого правила — `.navSwitch { min-height: 0 !important; width: 58px !important; height: 32px !important }`, ручка `28×28`, сдвиг `translateX(26px)`; `.navRoot input, select, textarea { font-size: 16px !important; min-height: 42px }` (16px гасит автозум iOS); `[role="dialog"] { padding: 12px !important; align-items: end !important }` — модалки прижимаются к низу, `[role="dialog"] > .liquid-glass { max-height: calc(100dvh - 24px) !important; padding: 18px !important }`, последний блок диалога становится липкой панелью кнопок (`position: sticky; bottom: -18px; background: rgba(10,15,30,.92)`).

**`navigator/index.html`**

1. `@media (max-width: 700px)` (`:34-37`) — `.authRoot input, .authRoot select { font-size: 16px !important; }`, `.authRoot button { min-height: 42px; }`.
2. `@media (prefers-reduced-motion: reduce)` (`:38`).

**Текучие правила без media-запросов.** Контейнер: `width: min(1420px, 100%)`, `padding: clamp(16px,2.4vw,26px) clamp(14px,3vw,32px) 56px` (`navigator/navigator.html:61`). Экран входа: `padding: clamp(20px,5vw,48px)` (`navigator/index.html:46`). Колонки подбора: `flex: 1 1 320px` против `flex: 99 1 520px` (в админке main — `flex: 99 1 420px`). Все сетки — `repeat(auto-fit, minmax(N,1fr))` с N = 132px (факты, фильтры), 150px (строки ставки рекрутера), 170px (группы «Подробнее»), 190px (условия, деньги), 210px (поля контрагента). Модалки — `width: min(380|420|460|560|640 px, 100%)`. Высота панелей — `calc(100vh - 124px)`, `min-height: 480px`.

**Печать** приходит из рантайма (`navigator/support.js:117-130`): `@page { margin: 0.5cm }`, `figure, table { break-inside: avoid }`, `print-color-adjust: exact`, `backdrop-filter: none !important`, анимации и переходы схлопываются.

---

## 10. Что нельзя переносить в прод

1. **Демо-пароли в клиентском коде.** `eltera2026` с почтой `anna.kuznetsova@romashka.ru` (`navigator/eltera/account.js:15-16`, продублировано в `navigator/README.md:16`) и пароль администратора `1207` (`navigator/navigator.html:1697`, `navigator/README.md:17`). Оба сравниваются строкой в браузере: `_submitAuth` (`navigator/navigator.html:1711`), `_cpConfirmDelete` (`:1782`), `signIn` (`navigator/eltera/account.js:69`). Любой пользователь видит их в исходнике страницы.
2. **Авторизация на localStorage.** Флаг администратора `eltera_navigator_admin_v1 = 'ok'` (`navigator/navigator.html:1698,1712`) подделывается одной строкой в консоли и не имеет срока действия. Учётная запись с паролем в открытом виде лежит в `eltera.account.v1`, сессия — в `eltera.session.v1` без подписи и без TTL (`navigator/eltera/account.js:71,76-82`). Плюс `navigator.html` не защищён вовсе: store не подключён, прямой URL открывает весь интерфейс.
3. **`eval` — точнее, `new Function`.** Логика страницы компилируется из строки: `evalDcLogic` (`navigator/support.js:772-780`), загрузка внешних модулей — `new Function("React","module","exports","require", code)` (`navigator/support.js:1148`). Оба места помечены `//! nosemgrep: eval-and-function-constructor`. Несовместимо с CSP без `'unsafe-eval'`.
4. **React с CDN.** React 18.3.1, ReactDOM 18.3.1 и Babel 7.29.0 грузятся с `unpkg.com` (`navigator/support.js:1071-1077`) на критическом пути; SRI есть, деградации нет — без сети страница пустая. Шрифт Inter с `fonts.googleapis.com` (`navigator/navigator.html:15`) — уже без SRI.
5. **Ссылки-заглушки `disk.example.ru`.** 10 вхождений в `ROWS[].media[]` (`navigator/navigator.html:1130,1131,1147,1163,1179` и далее) — вида `https://disk.example.ru/klimovsk-dorm`, `…/pavlovo-dorm`, `…/butovo-fc`. Домен несуществующий; это данные для демонстрации состояний «живая» и «битая» ссылка. Рядом того же класса заглушки: `yandex.ru/maps/-/*`, `t.me/kpk_vahta/318`, `docs.google.com/spreadsheets/…`.
6. **Персональные данные в моках.** Семь ФИО с телефонами контактных лиц контрагентов (`navigator/navigator.html:1386-1416`), семь `chat_id` (`-1001998877665` и др.), `message_thread_id` 128/42/17, маскированный токен BotFather `'7841…AAH'`, бот `@eltera_navigator_bot`, каналы `@ametist_vahta` и `@vahtapro_zayavki` (`navigator/navigator.html:1385-1425`). `scripts/check.js` ничего из этого не считает секретом.
7. **Мутация констант класса вместо состояния.** `this.CPS[i] = …`, `this.CPS = this.CPS.filter(…)` в `_cpExclude/_saveCp/_cpConfirmDelete/_cpRestore` (`navigator/navigator.html:1769-1809`) — правки теряются при перезагрузке и идут мимо `setState`.
8. **Ассет 20 МБ** `navigator/assets/background-loop.mp4` с `preload="auto"` на обоих экранах, включая мобильный.
9. **Мост к хост-редактору**: `postMessage(…, '*')` в `window.parent` и глобальные `__dc*`-функции управления состоянием (`navigator/support.js:1712-1758`).
10. **`navigator/.env.example`** — описывает Flask/werkzeug-стек, которого нет; ни одна переменная оттуда кодом не читается. Переносить его как образец конфигурации нельзя.

---

## 11. Соотношение с рабочим `templates/navigator.html`

Рабочий экран: роут `app.py:366` `@app.get("/navigator")` под HTTP Basic (`verify_creds`), файл читается с диска и отдаётся как есть, **без Jinja-рендера** (`app.py:373-375`), чтобы `{` в JS/CSS не трактовались как шаблон. Данные — `fetch('/api/navigator')` (`templates/navigator.html:2189-2197`) поверх SQLite; запись — `POST /api/rates` и `DELETE /api/rates/{id}`.

### Уже перенесено

- Вся сетка экрана, шапка, липкие колонки, `.liquid-glass`, `appFade`, `.appScroll`, media-запросы 900/560/700 (макет `:32-51` → рабочий `templates/navigator.html:44-66`), палитра и типографика.
- Панель городов, чипы, радиус 25/50/100 км, гаверсинус `dist()` и `nearNames()` (макет `:1491-1508` → `templates/navigator.html:217-235`).
- Числовые и селект-фильтры, «Ещё фильтры» со счётчиком, «сбросить», пять сортировок, три скоупа; карточка позиции целиком (факты, «Условия», «Деньги — озвучить до выезда», чипы, материалы, кнопки, плашка «только внутри»).
- Модалки: пробелы, подробнее, текст кандидату (включая чекбоксы материалов и «Вырезано стоп-словарём»), вход в админку, удаление контрагента, настройки контрагента, тост с таймером 2400 мс.
- Константы `ISSUE_KINDS`, `MEDIA_META`, `FIELD_KEYS`, `MAX_TRIES = 3`, `SEND_TIME = '09:00'`, `ADMIN_PASS = '1207'`, ключ `eltera_navigator_admin_v1`, набор пропсов (макет `:1053` → `templates/navigator.html:90-95`); логика `_employer/_scrub/_candidateText/_pass/_mediaList/_issues/_resolve/_openAdmin/_submitAuth` — почти дословно.

### Различается

| Тема | Макет `navigator/navigator.html` | Рабочий `templates/navigator.html` |
|---|---|---|
| Движок | `x-dc` + `support.js` + React с CDN | строковый рендер на ванильном JS, делегирование по `data-act`/`data-on`/`data-fid` (`:2115-2185`) |
| Псевдоклассы | `style-hover`/`style-focus` | классы `.hLight/.hSoft/.hWhite/.hGold/.hCyan/.hRed/.fLine` (`:26-38`) |
| Данные | зашитые константы | `/api/navigator` |
| Ассеты | `assets/background-loop.mp4`, SVG-логотип на тёмном | `/static/media/navigator-bg.mp4`, `/static/assets/eltera-logo-transparent.png`; **SVG-логотип из макета в `static/assets/` не перенесён** |
| Ставка рекрутера | `percent`/`fixed`, `baseAmount`, «База расчёта / Формула / Условие начисления / Этап выплаты / Гарантийный период» (`:2373-2379`) | приходит подобранной из `registry/rates.py`: `amount, tier, tiers[], scope, payout, note, validTo, expired`; строки «За что / Лестница по сменам / Когда выплачивается / Надбавки и оговорки / Действует до» — **процентных ставок, базы расчёта и гарантийного периода нет** |
| Сортировка `rec` | percent выше fixed, затем по `value` (`:1978-1986`) | просто по сумме (`templates/navigator.html:611`) |
| Фильтр ставки рекрутера | `процент / фикс / не задана` (`:2329`) | `задана / не задана / просрочена` (`:1007`) |
| График | фиксированный список `вахта / 5/2 / 6/1 / 2/2` (`:2327`) | динамика из `DATA.schedules` (`:1006`) |
| Стартовый выбор города | `sel: ['Москва']` (`:1057`) | пустой (`:117`) |
| Строка прогона | зашита `'прогон 13:02 · следующий 12:00'` (`:2280`) | из `DATA.lastRun`/`nextRun` (`:899`) |
| Вкладки админки | три: Исключения / Запросы / Контрагенты (`:2436-2440`) | четыре: добавлена **«Ставки»** (`viewRates`, `:1378-1465`) — единственный раздел, который реально пишет в бэкенд |
| Тип источника контрагента | 2 варианта (`:995-996`) | 4: Telegram-чат, Google-таблица, SeaTable, Ручной ввод (`:1874`) |
| Обязательные поля | 7 ключей, включая `pay` (`:1379-1382`) | 6, без `pay` (`:164-167`) — реестр периодичность выплат не хранит |
| Сохранение настроек контрагента | `_saveCp()` пишет в `CPS` и тостит успех (`:1803-1809`) | `cpSave` честно пишет «хранилище настроек контрагентов не подключено» (`:2097`) |
| Безопасность вывода | подстановка на движке | экранирование `esc/attr` и белый список схем `safeUrl` (`https?\|mailto\|tg://\|/`) — `:171-183` |
| Сохранение фокуса и скролла | нет | есть, с `selectionStart/End`, между перерисовками (`:855-880`) |
| Дебаунс ввода | нет | 140 мс для `q/citySearch/rateMin/ageCand` (`:2122-2160`) |
| Состояния загрузки/ошибки | нет | «Загружаем реестр…» и «Не удалось загрузить данные» (`:883-884`) |
| Длина запроса в Telegram | не проверяется | проверка против `TG_LIMIT = 4096` (`:104, 777`) |
| `scrub()` | заменяет по всему тексту (`:1824-1832`) | выносит URL из-под замены, чтобы не ломать ссылки (`:451-459`) |
| «Не додумывать» | `houseText` подставляет «до 10 человек в комнате», «общежитие» при пустых полях (`:1552-1560`) | печатает только известное (`:267-278`) |

### Чего в рабочем нет

1. **Экран входа и единая учётная запись.** `navigator/index.html` и `navigator/eltera/account.js` не подключены: доступ к приложению — HTTP Basic по одной общей паре `WEB_USER`/`WEB_PASSWORD` (`app.py:191-208`), ролей нет.
2. **Состояние переписки.** `OUTREACH` (`attempt, sent, silent, greetedToday, asked[]`), статусы «Нет ответа N дн.», «попытка 2 из 3», «следующий повтор завтра в 09:00» и **догоняющее сообщение** (`:2206-2216`) — только в макете. В рабочем статус сводится к «Готово к отправке / Не отправлено» по наличию `TELEGRAM_BOT_TOKEN`.
3. **Проблемы типов `format` и `unparsed`.** В рабочем объявлены в `ISSUE_KINDS`, но ни один код-путь их не создаёт — кейсы «ставка за час и за смену в одном поле» и «дату начала не удалось распознать» потеряны.
4. **Скоуп «Под заявку»** — `activeRequest` в API всегда `null` (`navigator_api.py:777`), чип и плашка не показываются.
5. **`_normFields`** — таблица «нормализовано / источник / правило / как пришло» по 20 полям (макет `:1993-2028`, мёртвая и в самом макете). Рабочий показывает только `rawFields` списком.
6. **`ALIASES`**, `_rowStatus`, журнал решений `state.log`, черновики `state.drafts`, `_bulkDeadLinks()` (массовое снятие битых материалов) — не перенесены.
7. **Индивидуальный статус ссылки** (`LINK_STATE`: «недоступна с 2 августа · перепроверок 24 · запрос заказчику отправлен вчера») — в рабочем одна константа «перепроверяется · перепроверка каждые 6 часов» (`:814`), при том что задачи перепроверки в планировщике нет (`app.py:83-106` — четыре джоба: 09:30, 12:00, 13:00, 13:30).
8. **Секция «Материалы» в модалке «Подробнее»** (макет `:820-839`) — в рабочей модалке отсутствует; зато рабочий добавил поля, которых нет в макете: Категория, Формат работы, Минимум смен, Обязанности, Нужен ТСД, Кто платит за медкнижку, Человек в комнате, Постельное бельё, Транспорт до объекта, Плюсы, Риски, секция «Как пришло по полям».

---

## Требует согласования

1. **Куда девать пакет.** Каталог untracked и ни на что не влияет. Нужно решение: коммитить как справочный эталон (тогда 20 МБ видео попадут в историю git — лучше вырезать `assets/background-loop.mp4`), держать вне репозитория или удалить после переноса оставшихся расхождений из §11.
2. **Битые `href` в `PRODUCTS`** (`navigator/eltera/account.js:25-27`). Нет данных, где лежат `Портал сотрудника.dc.html` и `CRM.dc.html` и планируется ли переход между продуктами вообще. Пока функция `products()` не вызывается, дефект латентный.
3. **Модель ставки рекрутера.** Макет умеет процент от базы, гарантийный период и этап выплаты; `registry/rates.py` — только фиксированную сумму с лестницей по `min_shifts` и приоритетом `object_name` над контрагентом. Нужно решить, расширять ли схему до процентов и гарантийного периода или считать модель макета устаревшей.
4. **Фильтр «Выплаты» и поле `pay`.** В макете фильтр есть; бэкенд отдаёт `pay = {"st":"na"}` всегда (`navigator_api.py:428`). Без изменения парсера и схемы фильтр невозможен.
5. **Публичный алиас контрагента.** Макет показывает кандидату `cpAlias`; в реестре `cpAlias` всегда пустой (`navigator_api.py:381`). Нужен источник алиасов.
6. **Судьба экрана входа.** Сейчас доступ — одна общая пара HTTP Basic без ролей. Переход на учётные записи с грантами по продуктам (модель `access` из `account.js`) — отдельное решение уровня архитектуры; `roles` в макете заполнены, но ни на что не влияют.
7. **Хранилище настроек контрагентов.** Роутов вида `POST /api/cps` в `app.py` нет; макет демонстрирует полноценный CRUD, рабочий экран пишет «не подключено». Нужно решение по схеме и роутам.
8. **Перепроверка ссылок и здоровье источников.** Тексты «перепроверка каждые 6 часов» и «Перепроверка каждые 30 минут» — константы разметки в обоих файлах; соответствующих джобов в планировщике нет.
