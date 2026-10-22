"""Подключение к SQLite и схема реестра.

Почему SQLite, а не Google Sheets: реестру нужны транзакции, версии заявок и
поиск, который не требует вычитывания всего листа целиком. Таблица остаётся
как выгрузка (см. registry/export_sheets.py).

Схема версионируется через PRAGMA user_version — миграции применяются
последовательно при первом обращении к базе.
"""

import os
import sqlite3
import threading
from datetime import datetime
from contextlib import contextmanager
from typing import Iterator, List, Optional

from loguru import logger

from registry.models import (
    DATA_FIELDS,
    MANAGER_FIELDS,
    sql_type,
)

DEFAULT_DB_PATH = os.getenv("REGISTRY_DB_PATH", "data/registry.db")

_init_lock = threading.Lock()
_initialized: set = set()


def _position_columns_ddl() -> str:
    """Колонки позиции собираются из списков в models.py."""
    lines: List[str] = []
    for name in DATA_FIELDS + MANAGER_FIELDS:
        lines.append(f"    {name} {sql_type(name)}")
    return ",\n".join(lines)


MIGRATIONS: List[str] = []

# --- v1: базовая схема -------------------------------------------------------
MIGRATIONS.append(f"""
CREATE TABLE requests (
    request_id      TEXT PRIMARY KEY,
    year            INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    source          TEXT NOT NULL,
    source_ref      TEXT NOT NULL,
    source_name     TEXT NOT NULL DEFAULT '',
    source_url      TEXT NOT NULL DEFAULT '',
    counterparty        TEXT NOT NULL DEFAULT '',
    counterparty_raw    TEXT NOT NULL DEFAULT '',
    raw_text        TEXT NOT NULL DEFAULT '',
    raw_payload     TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    received_at     TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    parsed_at       TEXT,
    parse_status    TEXT NOT NULL DEFAULT 'pending',
    parse_error     TEXT NOT NULL DEFAULT '',
    llm_model       TEXT NOT NULL DEFAULT '',
    llm_tokens_in   INTEGER NOT NULL DEFAULT 0,
    llm_tokens_out  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, source_ref),
    UNIQUE (year, seq)
);

CREATE INDEX idx_requests_source ON requests(source);
CREATE INDEX idx_requests_counterparty ON requests(counterparty);
CREATE INDEX idx_requests_last_seen ON requests(last_seen_at);
CREATE INDEX idx_requests_status ON requests(parse_status);

-- История содержимого заявки: при изменении текста прежняя версия уезжает
-- сюда, чтобы «сверка с исходником» работала и задним числом.
CREATE TABLE request_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL REFERENCES requests(request_id) ON DELETE CASCADE,
    revision     INTEGER NOT NULL,
    raw_text     TEXT NOT NULL DEFAULT '',
    raw_payload  TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    replaced_at  TEXT NOT NULL
);

CREATE INDEX idx_revisions_request ON request_revisions(request_id);

CREATE TABLE positions (
    position_id      TEXT PRIMARY KEY,
    seq              INTEGER NOT NULL,
    first_request_id TEXT NOT NULL REFERENCES requests(request_id),
    last_request_id  TEXT NOT NULL REFERENCES requests(request_id),
    source           TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    -- vacancy_id из прежней таблицы. Нужен, чтобы после миграции можно было
    -- сверить перенос строка в строку; в работе не участвует.
    legacy_id        TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
{_position_columns_ddl()},
    UNIQUE (source, fingerprint)
);

CREATE INDEX idx_positions_request ON positions(last_request_id);
CREATE INDEX idx_positions_active ON positions(is_active);
CREATE INDEX idx_positions_source ON positions(source);
CREATE INDEX idx_positions_city ON positions(city);
CREATE INDEX idx_positions_vacancy ON positions(vacancy_name);
CREATE INDEX idx_positions_counterparty ON positions(counterparty);
CREATE INDEX idx_positions_rate ON positions(shift_rate);
CREATE INDEX idx_positions_legacy ON positions(legacy_id);
-- Запасной ключ поиска позиции, когда fingerprint «поплыл» (см. ingest).
CREATE INDEX idx_positions_identity ON positions(source, counterparty, city, vacancy_name, object_name);

-- Какая заявка какие позиции принесла. Позиция живёт дольше одной заявки
-- (завтрашний снимок принесёт её снова), поэтому связь именно многие-ко-многим,
-- а не поле в positions. Без этого нельзя понять, какие позиции подтверждает
-- заявка, содержимое которой не изменилось и потому не перепарсивалось.
CREATE TABLE request_positions (
    request_id  TEXT NOT NULL REFERENCES requests(request_id) ON DELETE CASCADE,
    position_id TEXT NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE,
    PRIMARY KEY (request_id, position_id)
);

CREATE INDEX idx_request_positions_position ON request_positions(position_id);

-- Диффы по позициям: что именно поменяла новая ревизия заявки.
CREATE TABLE position_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE,
    request_id  TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  TEXT NOT NULL
);

CREATE INDEX idx_history_position ON position_history(position_id);
CREATE INDEX idx_history_changed ON position_history(changed_at);

-- Справочники унификации. confirmed=0 — это и есть очередь на подтверждение:
-- незнакомый вариант написания пишется как есть и ждёт решения человека,
-- вместо того чтобы молча схлопнуться в похожее значение.
CREATE TABLE dictionaries (
    kind       TEXT NOT NULL,
    alias      TEXT NOT NULL,
    canonical  TEXT NOT NULL,
    confirmed  INTEGER NOT NULL DEFAULT 0,
    hits       INTEGER NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, alias)
);

CREATE INDEX idx_dict_kind_confirmed ON dictionaries(kind, confirmed);
CREATE INDEX idx_dict_canonical ON dictionaries(kind, canonical);

CREATE TABLE id_counters (
    scope TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

-- Полнотекстовый поиск. Отдельная таблица, а не external content: индекс
-- склеивает исходный текст заявки и нормализованные поля позиции, то есть
-- строки из двух таблиц сразу. Наполняется из ingest.
CREATE VIRTUAL TABLE search_index USING fts5(
    position_id UNINDEXED,
    request_id  UNINDEXED,
    body,
    tokenize = "unicode61 remove_diacritics 2"
);
""")

# --- v2: ставки рекрутера ----------------------------------------------------
#
# Слой мотивации рекрутера. Из заявок он не приходит и приходить не может:
# это внутренняя договорённость с контрагентом. Поэтому отдельная таблица, а не
# колонка в positions, и заполняется она руками. Пусто — «ставка не задана»,
# додумывать процент от ставки кандидата нельзя.
MIGRATIONS.append("""
CREATE TABLE recruiter_rates (
    counterparty TEXT PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT 'fixed',   -- fixed | percent
    value        REAL,                            -- рубли для fixed, проценты для percent
    base_amount  INTEGER,                         -- оплата контрагента за кандидата
    base         TEXT NOT NULL DEFAULT '',        -- словами: за что платят
    rule         TEXT NOT NULL DEFAULT '',        -- условие начисления: «10 смен»
    stage        TEXT NOT NULL DEFAULT '',        -- этап выплаты
    guar         TEXT NOT NULL DEFAULT '',        -- гарантийный период
    updated_at   TEXT NOT NULL DEFAULT ''
);
""")


# --- v3: база знаний по проектам с Яндекс.Диска -------------------------------
#
# Индекс папок контрагента: одна строка — один проект. Живёт в реестре, а не в
# отдельном файле, потому что разбор заявки обязан читать его без сети — диск
# обновляется своим расписанием, прогон идёт своим.
MIGRATIONS.append("""
CREATE TABLE disk_projects (
    path        TEXT PRIMARY KEY,          -- путь папки внутри публичной ссылки
    source      TEXT NOT NULL DEFAULT '',  -- чей диск (алиас источника)
    category    TEXT NOT NULL DEFAULT '',  -- раздел верхнего уровня
    name        TEXT NOT NULL,             -- имя папки как есть, с галочками
    title       TEXT NOT NULL DEFAULT '',  -- то же без эмодзи — его видит человек
    tokens      TEXT NOT NULL DEFAULT '',  -- нормализованные токены названия
    url         TEXT NOT NULL DEFAULT '',  -- ссылка на папку для карточки заявки
    doc_text    TEXT NOT NULL DEFAULT '',  -- склеенный текст описаний проекта
    docs        TEXT NOT NULL DEFAULT '[]',   -- JSON: файлы папки
    albums      TEXT NOT NULL DEFAULT '[]',   -- JSON: фотоальбомы и число фото
    photos      INTEGER NOT NULL DEFAULT 0,
    modified    TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',  -- отпечаток содержимого папки
    indexed_at  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_disk_projects_source ON disk_projects(source);
""")


# --- v4: ссылка позиции на папку проекта + переименование ВахтаПро → Градус ---
#
# Кнопка «Фото объекта» в /navigator ведёт в папку проекта на Яндекс.Диске.
# Связь хранится отдельной таблицей, а не колонкой в positions: она не приходит
# из заявки, её не редактирует менеджер и она пересчитывается при каждом обходе
# диска — в истории изменений позиции ей делать нечего.
MIGRATIONS.append("""
CREATE TABLE position_kb (
    position_id TEXT PRIMARY KEY REFERENCES positions(position_id) ON DELETE CASCADE,
    source      TEXT NOT NULL DEFAULT '',
    project     TEXT NOT NULL DEFAULT '',   -- название папки проекта
    path        TEXT NOT NULL DEFAULT '',   -- путь внутри публичной ссылки
    folder_url  TEXT NOT NULL DEFAULT '',   -- папка проекта целиком
    photos_url  TEXT NOT NULL DEFAULT '',   -- куда ведёт кнопка «Фото объекта»
    photos      INTEGER NOT NULL DEFAULT 0,
    score       REAL NOT NULL DEFAULT 0,    -- уверенность сопоставления
    linked_at   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_position_kb_source ON position_kb(source);

-- Контрагент переименовался. Ключ источника (vahtapro) остаётся прежним, а имя,
-- которое видят люди, живёт в заявках — его и обновляем.
UPDATE requests SET source_name = 'Градус'
WHERE source = 'vahtapro' AND source_name = 'ВахтаПро';
""")


# --- v5: ставки рекрутёра правилами ------------------------------------------
#
# Прежняя таблица держала одну ставку на контрагента. Реальность сложнее и
# приходит картинкой раз в неделю: у Градуса лестница по сменам (15/20/30 смен
# → 15/20/23 тыс.), у ЯППИ поверх лестницы исключения по объектам и даже по
# разряду, у КНК просто «объект → ставка, действует до 31.07».
#
# Поэтому здесь не одна ставка, а правила с областью действия. Пустой объект
# означает «весь контрагент», пустая должность — «все должности объекта»,
# min_shifts = 0 — «без условия по сменам». Из этих трёх полей и складываются
# три способа выставления: на всего контрагента, лестницей по сменам и
# точечными исключениями.
#
# Контрагент здесь — источник (vahtapro, yappi, …), а не бренд заказчика: у
# ВахтаПро в counterparty лежит клиент («BMJ», «Молком»), и ставка,
# привязанная к нему, означала бы совсем другое.
MIGRATIONS.append("""
CREATE TABLE recruiter_rate_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,                   -- алиас контрагента-источника
    client      TEXT NOT NULL DEFAULT '',        -- объект/заказчик, '' — все
    vacancy     TEXT NOT NULL DEFAULT '',        -- должность, '' — все
    min_shifts  INTEGER NOT NULL DEFAULT 0,      -- ступень «от N смен», 0 — без условия
    amount      INTEGER NOT NULL,                -- рублей за кандидата
    note        TEXT NOT NULL DEFAULT '',        -- надбавки и оговорки словами
    payout      TEXT NOT NULL DEFAULT '',        -- когда платят: «адаптация 5 смен»
    valid_from  TEXT NOT NULL DEFAULT '',
    valid_to    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    UNIQUE (source, client, vacancy, min_shifts)
);

CREATE INDEX idx_rate_rules_source ON recruiter_rate_rules(source);

-- Ставки меняются каждую неделю, и вопрос «а сколько было в июле» возникает
-- регулярно. Пишем каждое изменение, а не только текущее состояние.
CREATE TABLE recruiter_rate_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    client      TEXT NOT NULL DEFAULT '',
    vacancy     TEXT NOT NULL DEFAULT '',
    min_shifts  INTEGER NOT NULL DEFAULT 0,
    amount      INTEGER,
    note        TEXT NOT NULL DEFAULT '',
    payout      TEXT NOT NULL DEFAULT '',
    valid_from  TEXT NOT NULL DEFAULT '',
    valid_to    TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT 'set',     -- set | delete
    changed_at  TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_rate_history_scope ON recruiter_rate_history(source, client, vacancy);

-- Переносим то, что успели завести в прежней таблице. Источник берём по
-- позициям того же контрагента: в старой схеме его просто не было.
INSERT INTO recruiter_rate_rules
    (source, client, vacancy, min_shifts, amount, note, payout, created_at, author)
SELECT
    COALESCE((SELECT p.source FROM positions p
              WHERE p.counterparty = r.counterparty LIMIT 1), ''),
    '', '', 0,
    CASE WHEN r.kind = 'percent' AND r.base_amount IS NOT NULL
         THEN CAST(ROUND(r.base_amount * COALESCE(r.value, 0) / 100.0) AS INTEGER)
         ELSE CAST(COALESCE(r.value, 0) AS INTEGER) END,
    TRIM(COALESCE(r.base, '') || ' ' || COALESCE(r.stage, '') || ' ' || COALESCE(r.guar, '')),
    COALESCE(r.rule, ''),
    COALESCE(r.updated_at, ''),
    'перенос из старой таблицы'
FROM recruiter_rates r;

DROP TABLE recruiter_rates;
""")


# --- v6: КПК → КНК -----------------------------------------------------------
#
# Контрагент всё это время был заведён с опечаткой в названии. Ключ источника
# (kpk) не трогаем по той же причине, что и у Градуса: он в колонке source у
# всех заявок, в KPK_MATRIX_ID и в метках метрик. Меняем то, что видят люди.
#
# У КНК название контрагента лежит ещё и в самих позициях (в отличие от
# Градуса, где в counterparty стоит заказчик), поэтому правим и его — иначе в
# карточке заказчиком остался бы «КПК». После этой миграции нужен
# `python scripts/renormalize.py`: fingerprint позиции считается в том числе
# по контрагенту, и его следует пересчитать.
MIGRATIONS.append("""
UPDATE requests  SET source_name = 'КНК'
WHERE source = 'kpk' AND source_name = 'КПК';

UPDATE requests  SET counterparty = 'КНК', counterparty_raw = 'КНК'
WHERE source = 'kpk' AND counterparty = 'КПК';

UPDATE positions SET counterparty = 'КНК'
WHERE source = 'kpk' AND counterparty = 'КПК';

UPDATE positions SET counterparty_raw = 'КНК'
WHERE source = 'kpk' AND counterparty_raw = 'КПК';

-- Прежнее написание остаётся алиасом: если оно ещё где-то всплывёт,
-- справочник приведёт его к новому названию, а не заведёт второго контрагента.
UPDATE dictionaries SET canonical = 'КНК'
WHERE kind = 'counterparty' AND canonical = 'КПК';

INSERT OR IGNORE INTO dictionaries
    (kind, alias, canonical, confirmed, hits, note, created_at, updated_at)
VALUES ('counterparty', 'кнк', 'КНК', 1, 0, 'переименование КПК → КНК', '', '');
""")


# --- v7: пользователи, роли, журнал входов -----------------------------------
#
# До сих пор учётная запись была одна и жила в переменных окружения: кто вошёл,
# тот и администратор, а история правок писалась без автора. Теперь люди
# заводятся в базе, каждому выдаётся роль, и каждая правка знает, чья она.
#
# Пароль администратор выдаёт временным: он живёт ограниченное время и работает
# ровно один раз — при первом входе человек задаёт свой. Так администратор
# перестаёт знать чужой пароль, а «кто это сделал» в журнале означает именно
# того, чьё имя стоит.
#
# Учётка не удаляется никогда: увольнение — это is_active = 0. Иначе история
# правок осталась бы с автором, которого больше нет ни в одной таблице.
MIGRATIONS.append("""
CREATE TABLE users (
    user_id       TEXT PRIMARY KEY,              -- USR-0001
    email         TEXT NOT NULL UNIQUE,          -- всегда в нижнем регистре
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    -- Временный пароль: сменить обязательно, и он протухает.
    must_change_password INTEGER NOT NULL DEFAULT 0,
    password_expires_at  TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL DEFAULT '',
    disabled_at   TEXT,
    disabled_by   TEXT NOT NULL DEFAULT '',
    last_login_at TEXT
);

CREATE INDEX idx_users_active ON users(is_active);

CREATE TABLE roles (
    role  TEXT PRIMARY KEY,
    title TEXT NOT NULL
);

INSERT INTO roles (role, title) VALUES
    ('recruiter', 'Рекрутер'),
    ('admin', 'Администратор');

CREATE TABLE user_roles (
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role       TEXT NOT NULL REFERENCES roles(role),
    granted_at TEXT NOT NULL,
    granted_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, role)
);

-- Журнал входов: и удачных, и нет. Пароль сюда не попадает никогда.
CREATE TABLE login_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    email      TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    ok         INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_login_audit_at ON login_audit(at);
CREATE INDEX idx_login_audit_email ON login_audit(email);

-- Автор правки. Раньше история изменений позиции писалась без него: видно,
-- что поле поменялось, и непонятно, приём это сделал или человек.
ALTER TABLE position_history ADD COLUMN changed_by TEXT NOT NULL DEFAULT '';
""")

# --- v8: кандидаты, звонки, очередь сообщений --------------------------------
#
# Другая сторона реестра. requests и positions — это то, что нужно заказчику:
# сколько людей и на какой объект. Здесь — сами люди, с которыми говорил
# голосовой агент, и то, что он о них узнал.
#
# Слово «заявка» намеренно не используется: в этой базе оно уже занято заявками
# заказчиков (таблица requests), и вторая сущность с тем же именем сделала бы
# любой разговор о коде двусмысленным.
#
# Ключ карточки — номер телефона, а не суррогатный id. Номер приходит от
# звонилки, которая набирала, и это единственное, что достоверно связывает
# разговор с человеком: ElevenLabs в режиме релея видит только добавочный 710.
# Побочный эффект — один человек с двух номеров даст две карточки. Для пилота
# приемлемо, слияние будет отдельной задачей.
MIGRATIONS.append("""
CREATE TABLE candidates (
    phone            TEXT PRIMARY KEY,           -- +7XXXXXXXXXX, нормализован звонилкой
    name             TEXT NOT NULL DEFAULT '',
    age              INTEGER,
    citizenship      TEXT NOT NULL DEFAULT '',
    region           TEXT NOT NULL DEFAULT '',
    city             TEXT NOT NULL DEFAULT '',
    profession       TEXT NOT NULL DEFAULT '',

    -- Три состояния, а не два: NULL значит «не обсуждали». Отличать это от
    -- твёрдого «нет» приходится постоянно — иначе рекрутёр решит, что человеку
    -- уже задавали вопрос, и не задаст его сам.
    has_medical_book INTEGER,
    ready_for_shift  INTEGER,
    ready_to_move    INTEGER,

    status           TEXT NOT NULL DEFAULT '',   -- см. call_status у агента
    interest         TEXT NOT NULL DEFAULT '',   -- high / medium / low / none
    preferred_channel TEXT NOT NULL DEFAULT '',  -- telegram / whatsapp
    last_position_id TEXT NOT NULL DEFAULT '',

    -- Свободный текст от агента: приводить его к справочникам сейчас рано,
    -- рекрутёр читает как есть.
    desired_salary   TEXT NOT NULL DEFAULT '',
    desired_schedule TEXT NOT NULL DEFAULT '',
    start_date       TEXT NOT NULL DEFAULT '',
    objections       TEXT NOT NULL DEFAULT '',
    questions        TEXT NOT NULL DEFAULT '',
    next_action      TEXT NOT NULL DEFAULT '',
    summary          TEXT NOT NULL DEFAULT '',

    calls_count      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX idx_candidates_status ON candidates(status);
CREATE INDEX idx_candidates_updated ON candidates(updated_at);

-- По строке на разговор. conversation_id уникален, и это не украшение:
-- повторный пуш того же звонка (ретрай сети, перезапуск обзвона) не должен
-- ни задваивать историю, ни слать карточку второй раз.
CREATE TABLE candidate_calls (
    call_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone            TEXT NOT NULL REFERENCES candidates(phone) ON DELETE CASCADE,
    conversation_id  TEXT NOT NULL UNIQUE,
    started_at       TEXT NOT NULL DEFAULT '',
    duration_secs    INTEGER,
    call_successful  TEXT NOT NULL DEFAULT '',
    termination_reason TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT '',
    transcript       TEXT NOT NULL DEFAULT '',
    -- Разговор целиком. Состав полей сбора данных у агента меняется, и без
    -- сырого ответа новое поле пришлось бы добывать повторным звонком.
    payload          TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE INDEX idx_candidate_calls_phone ON candidate_calls(phone);

-- Очередь исходящих сообщений. Разбирает её отдельный сервис переписки:
-- забирает pending через API, отчитывается sent или failed. Навигатор сам
-- никуда не отправляет и о мессенджерах ничего не знает.
CREATE TABLE candidate_messages (
    message_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL REFERENCES candidates(phone) ON DELETE CASCADE,
    channel     TEXT NOT NULL,                   -- telegram / whatsapp
    kind        TEXT NOT NULL DEFAULT 'vacancy_card',
    position_id TEXT NOT NULL DEFAULT '',
    -- Готовая карточка в JSON, собранная из реестра на момент постановки.
    -- Собираем сразу, а не при отправке: условия вакансии успеют измениться,
    -- а человеку обещали то, что назвали вслух.
    body        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending', -- pending / sending / sent / failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    sent_at     TEXT
);

CREATE INDEX idx_candidate_messages_status ON candidate_messages(status, created_at);
""")

# --- v9: то, что агент спрашивает, но складывать было некуда ----------------
#
# Пять полей, которые голосовой агент выясняет в разговоре, а карточка кандидата
# теряла. Судимость и статья — самое дорогое из потерянного: без них нечего
# отправлять в чат службы безопасности, а спрашивал их агент с первого дня.
#
# has_criminal_record объявлен как три состояния, а не два. «Не спрашивали» и
# «сказал, что нет» — разные вещи: во втором случае повторный вопрос человеку
# выглядит как недоверие, в первом его задать обязаны.
#
# criminal_article хранит номер статьи со слов кандидата. Это сведения особой
# категории: срок хранения и доступ к ним надо ограничить отдельно, колонка
# сама по себе такого ограничения не даёт.
MIGRATIONS.append("""
ALTER TABLE candidates ADD COLUMN has_criminal_record INTEGER;
ALTER TABLE candidates ADD COLUMN criminal_article TEXT NOT NULL DEFAULT '';

-- Перезвон и передача человеку: агент это собирает, а раньше складывать было
-- некуда, и оба признака молча пропадали вместе с ответом.
ALTER TABLE candidates ADD COLUMN callback_at TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN callback_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN needs_human INTEGER NOT NULL DEFAULT 0;

-- Кого звать первым: и просьба перезвонить, и запрос живого рекрутёра требуют
-- действия человека в конкретный срок.
CREATE INDEX idx_candidates_callback ON candidates(callback_requested, callback_at);
CREATE INDEX idx_candidates_needs_human ON candidates(needs_human);
""")

# --- v10: входящие от кандидата ---------------------------------------------
#
# До сих пор переписка была односторонней: очередь candidate_messages умеет
# только исходящие, поля направления в ней нет. Кандидат при этом отвечает —
# и присылает документы фотографиями в тот же чат.
#
# Входящие держим отдельной таблицей, а не флагом направления в исходящей.
# У них разный жизненный цикл: у исходящего есть попытки доставки и статус
# отправки, у входящего — разбор и решение. Общая таблица заставила бы половину
# колонок стоять пустыми у каждой строки.
#
# Содержимое вложений здесь НЕ хранится. Только факт: тип, размер, отпечаток и
# путь к файлу, если он вообще сохранён. Паспорт в SQLite не кладут: база едет
# в бэкапы целиком, а удалить оттуда одну картинку по требованию человека
# нельзя. Само хранилище появится отдельно и с ограниченным сроком жизни.
MIGRATIONS.append("""
CREATE TABLE candidate_inbound (
    inbound_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL,
    channel     TEXT NOT NULL,                   -- telegram / whatsapp / max
    -- Идентификатор собеседника внутри канала. Телефон известен не всегда:
    -- в Telegram ответ приходит от пользователя, а не от номера, и связать
    -- одно с другим можно только по записи, сделанной при отправке.
    peer        TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',        -- id сообщения в мессенджере
    kind        TEXT NOT NULL DEFAULT 'text',    -- text / photo / document / other
    text        TEXT NOT NULL DEFAULT '',
    -- Про вложение: что это, сколько весит, куда легло. Содержимое не здесь.
    media_type  TEXT NOT NULL DEFAULT '',
    media_size  INTEGER,
    media_sha256 TEXT NOT NULL DEFAULT '',
    media_path  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',     -- new / handled / ignored
    received_at TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Разбирать входящие надо в порядке поступления и по одному каналу за раз.
CREATE INDEX idx_inbound_status ON candidate_inbound(status, received_at);
CREATE INDEX idx_inbound_phone ON candidate_inbound(phone, received_at);
-- Мессенджеры переприсылают события при переподключении: одно и то же
-- сообщение не должно попасть в разбор дважды.
CREATE UNIQUE INDEX idx_inbound_external ON candidate_inbound(channel, external_id)
    WHERE external_id != '';

-- Кто есть кто в мессенджере. Telegram-драйвер удаляет контакт сразу после
-- отправки (мера против блокировки), и ответ прилетает от идентификатора, по
-- которому телефон уже не восстановить. Значит соответствие надо записать в
-- момент отправки, пока контакт ещё существует.
CREATE TABLE channel_peers (
    channel    TEXT NOT NULL,
    peer       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, peer)
);

CREATE INDEX idx_channel_peers_phone ON channel_peers(phone);
""")

# --- v11: заявки в службу безопасности --------------------------------------
#
# Куда отправлять пакет документов, зависит от вакансии. В реестре у позиции
# есть и контрагент (Марс, Балтика, Аметист — сто четыре штуки), и источник
# (vahtapro, yappi, ametist — семь). Чатов у компании около десяти, то есть
# они заведены не на каждого контрагента.
#
# Поэтому адресация не привязана жёстко ни к тому, ни к другому: правило
# указывает, по какому полю оно срабатывает. Ищем сначала точное совпадение по
# контрагенту, потом по источнику, потом общий чат. Так один и тот же механизм
# обслуживает и «для Марса отдельный чат», и «всё от ВахтаПро в один».
MIGRATIONS.append("""
CREATE TABLE sb_chats (
    chat_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- counterparty | source | default
    match_type  TEXT NOT NULL,
    match_value TEXT NOT NULL DEFAULT '',
    channel     TEXT NOT NULL DEFAULT 'telegram',
    chat_id     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_sb_chats_match ON sb_chats(match_type, match_value);

-- Заявка в СБ: пакет ушёл в чат и ждёт ответа живого человека.
--
-- Ответ приходит текстом от людей, которые не знают ни про какие идентификаторы,
-- поэтому связать его с заявкой можно только по чату и времени. Держим здесь
-- всё, что для этого нужно, плюс сырой текст ответа: разбор «одобрено или нет»
-- ошибается, и без исходника разбираться будет не с чем.
CREATE TABLE sb_requests (
    request_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL REFERENCES candidates(phone) ON DELETE CASCADE,
    position_id TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    channel     TEXT NOT NULL DEFAULT 'telegram',
    chat_id     TEXT NOT NULL DEFAULT '',
    -- Текст пакета сохраняем как отправленный. Условия в реестре меняются, а
    -- спорить с заказчиком придётся по тому, что он реально видел.
    body        TEXT NOT NULL DEFAULT '',
    -- draft: собрана, ждёт документов. queued: готова к отправке.
    -- sent: ушла, ждём ответа. approved / rejected / unclear: ответ получен.
    status      TEXT NOT NULL DEFAULT 'draft',
    external_id TEXT NOT NULL DEFAULT '',   -- id отправленного сообщения в чате
    answer_text TEXT NOT NULL DEFAULT '',
    answered_by TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    answered_at TEXT
);

CREATE INDEX idx_sb_requests_status ON sb_requests(status, created_at);
CREATE INDEX idx_sb_requests_phone ON sb_requests(phone);
CREATE INDEX idx_sb_requests_chat ON sb_requests(chat_id, status);

-- Очередь исходящих научилась адресовать не только кандидату. Раньше адресом
-- был телефон и только он; заявка в СБ уходит в групповой чат, где телефона
-- нет вовсе. Старые строки остаются валидными: пустой chat_id значит «слать
-- на phone», как и раньше.
ALTER TABLE candidate_messages ADD COLUMN chat_id TEXT NOT NULL DEFAULT '';
ALTER TABLE candidate_messages ADD COLUMN external_id TEXT NOT NULL DEFAULT '';
""")

# --- v12: дата выезда --------------------------------------------------------
#
# Спрашиваем её сразу после того, как собран комплект документов, а не после
# одобрения. Причина простая: проверка службой безопасности идёт своим ходом
# и занимает от часа до суток, а знать срок надо раньше — иначе одобренный
# кандидат сутки лежит без плана, и заказчик узнаёт дату последним.
#
# Храним и слова кандидата, и разобранную дату. Разбор ошибается, а человек
# писал что-то осмысленное: рекрутёру важнее увидеть «после майских», чем
# нашу догадку про 12 мая. Точность отделена от самой даты, потому что
# «через недельку» и «15 марта» одинаково полезны, но по-разному надёжны.
MIGRATIONS.append("""
ALTER TABLE candidates ADD COLUMN departure_raw TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN departure_date TEXT NOT NULL DEFAULT '';
-- exact / approx / unknown
ALTER TABLE candidates ADD COLUMN departure_precision TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN departure_asked_at TEXT NOT NULL DEFAULT '';

-- Сколько раз уже переспрашивали про срок. Один раз уточнить нормально, три
-- раза — это давление, после которого человек перестаёт отвечать вовсе.
ALTER TABLE candidates ADD COLUMN departure_asks INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_candidates_departure ON candidates(departure_date);
""")


# v13. Состояние переписки и разобранные документы.
#
# До этой миграции бот умел только слать и принимать: что человек прислал
# паспорт, было видно в candidate_inbound, а что паспорт признан годным —
# нигде. Вердикт жил в памяти воркера и умирал вместе с ним, поэтому после
# перезапуска бот просил документ заново у того, кто его уже прислал.
#
# Стадия нужна по той же причине. Без неё нельзя отличить «ждём документы» от
# «ждём дату» и «заявка уже у заказчика», а значит нельзя ни ответить по делу,
# ни напомнить вовремя, ни замолчать, когда диалог передан человеку.
MIGRATIONS.append("""
-- card_sent / docs / date / sb / approved / rejected / human / cold
ALTER TABLE candidates ADD COLUMN stage TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN stage_since TEXT NOT NULL DEFAULT '';

-- Напоминаний ровно два: через несколько часов и на следующее утро. Третье
-- это уже давление, после которого человек перестаёт отвечать совсем.
ALTER TABLE candidates ADD COLUMN reminders_sent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN last_reminder_at TEXT NOT NULL DEFAULT '';

CREATE INDEX idx_candidates_stage ON candidates(stage, stage_since);

-- Разбор присланного файла. Персональные данные ровно те, что уедут в заявку
-- службе безопасности: ФИО, дата рождения, пол, номер СНИЛС или ИНН. Адреса,
-- кем выдан и кода подразделения здесь нет намеренно — проверяющему они не
-- нужны, а каждое лишнее поле это ещё одни персональные данные у нас.
--
-- Сам файл лежит на диске, в базе только путь: паспорта тяжёлые, и хранить
-- их в SQLite рядом с реестром заявок значит получить базу в гигабайт и
-- неудаляемые сканы.
CREATE TABLE candidate_documents (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- passport_main / passport_registration / snils / inn
    outcome     TEXT NOT NULL,             -- accepted / retry / manual
    full_name   TEXT NOT NULL DEFAULT '',
    birth_date  TEXT NOT NULL DEFAULT '',
    sex         TEXT NOT NULL DEFAULT '',
    number      TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0,
    problems    TEXT NOT NULL DEFAULT '',
    media_path  TEXT NOT NULL DEFAULT '',
    inbound_id  INTEGER,
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_documents_phone ON candidate_documents(phone, kind);

-- Отдельно по дате создания: по нему ходит уборка, которая через неделю
-- стирает файлы с диска.
CREATE INDEX idx_documents_created ON candidate_documents(created_at);
""")


# v14. Раздел переписки для менеджеров.
#
# sent_text — то, что человек реально увидел. В очереди лежат данные, а не
# текст: формулировки живут в мессенджере и меняются без выкатки навигатора.
# Для отправки это верно, а для истории губительно — стоит поправить
# формулировку, и вся прошлая переписка задним числом станет другой. Поэтому
# отправленный текст записывается отдельно и больше не меняется никогда.
#
# author — кто написал: пусто у бота, почта менеджера у человека. Нужно и в
# интерфейсе, и для разбора потом: «кто это ему такое написал» должно иметь
# ответ.
MIGRATIONS.append("""
ALTER TABLE candidate_messages ADD COLUMN sent_text TEXT NOT NULL DEFAULT '';
ALTER TABLE candidate_messages ADD COLUMN author TEXT NOT NULL DEFAULT '';

-- Индекс по входящим завела ещё v10 — здесь он был бы вторым с тем же именем,
-- и executescript валится на этой строке, успев применить ALTER TABLE выше.
-- Версия схемы при этом не повышается, и база остаётся между двумя миграциями.
CREATE INDEX IF NOT EXISTS idx_messages_phone ON candidate_messages(phone, created_at);
""")


@contextmanager
def connect(db_path: str = None, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Открывает соединение, накатывает миграции при первом обращении.

    Коммит — при штатном выходе, откат — при исключении. Соединение на
    операцию: в SQLite это дёшево, а возни с общим соединением между потоками
    (Sheets-вызовы уходят в asyncio.to_thread) не хочется.
    """
    path = db_path or DEFAULT_DB_PATH
    _ensure_schema(path)
    conn = _open(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(path: str) -> None:
    if path in _initialized:
        return
    with _init_lock:
        if path in _initialized:
            return
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = _open(path)
        try:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current < len(MIGRATIONS):
                # Копия ДО первой миграции. Откатов у нас нет: SQLite не умеет
                # DROP COLUMN в старых сборках, обратных скриптов в проекте
                # тоже нет — единственный способ вернуться назад это файл.
                backup_before_migration(conn, path, current, len(MIGRATIONS))
                for version in range(current, len(MIGRATIONS)):
                    logger.info(f"[registry] применяю миграцию v{version + 1}")
                    conn.executescript(MIGRATIONS[version])
                    conn.execute(f"PRAGMA user_version={version + 1}")
                conn.commit()
        finally:
            conn.close()
        _initialized.add(path)


# Сколько копий перед миграциями держим. Каждая — полный файл базы (285 МБ
# на октябрь 2026), поэтому без ограничения каталог разрастается: к 14
# октября 2026 там лежал гигабайт в двадцати файлах, от v6 (август) до v17.
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "3"))


def backup_before_migration(conn: sqlite3.Connection, path: str, current: int, target: int) -> Optional[str]:
    """Снимает копию базы перед накатом миграций. Возвращает путь к копии.

    На пустой базе (user_version = 0 и ни одной таблицы) копировать нечего —
    это первый запуск, а не апгрейд.

    Копия делается штатным SQLite backup API, а не cp: он корректно работает
    с WAL и с параллельно открытыми соединениями.
    """
    if current == 0:
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if not tables:
            return None

    backup_dir = os.getenv("BACKUP_DIR", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(path)) or ".", "backups"
    )
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{os.path.basename(path)}.v{current}-{stamp}.bak"
    target_path = os.path.join(backup_dir, name)

    logger.info(f"[registry] копия перед миграцией v{current} → v{target}: {target_path}")
    dest = sqlite3.connect(target_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    _prune_backups(backup_dir, os.path.basename(path), BACKUP_KEEP)
    return target_path


def _prune_backups(backup_dir: str, db_name: str, keep: int) -> None:
    """Оставляет `keep` свежих копий, остальные убирает.

    Берём только то, что создаём сами: имя начинается с «registry.db.v» и
    кончается на «.bak». В том же каталоге лежат копии, снятые руками
    (registry.db.pre-tls.bak), и журналы незавершённых копий — их не трогаем.

    Сбой чистки не должен мешать миграции: база уже скопирована, и рисковать
    апгрейдом ради места незачем — поэтому только предупреждение в лог.
    """
    if keep < 1:
        return
    prefix = db_name + ".v"
    try:
        files = [
            os.path.join(backup_dir, name)
            for name in os.listdir(backup_dir)
            if name.startswith(prefix) and name.endswith(".bak")
        ]
        files.sort(key=os.path.getmtime, reverse=True)
        for stale in files[keep:]:
            os.remove(stale)
            # SQLite мог оставить рядом журнал незавершённой копии.
            journal = stale + "-journal"
            if os.path.exists(journal):
                os.remove(journal)
            logger.info(f"[registry] убрана старая копия: {os.path.basename(stale)}")
    except OSError as exc:
        logger.warning(f"[registry] старые копии в {backup_dir} не почищены: {exc}")


def reset_schema_cache() -> None:
    """Сбрасывает память о проинициализированных базах — нужно тестам."""
    _initialized.clear()


# v15. Дополнительные документы по контрагентам.
#
# Требования у всех разные, и узнаём мы о них отказом. Маркет Стафф без фото
# по пояс на светлом фоне не отправляет на проверку вовсе — «На проверку сб не
# могу отправить», Аметист просит то же «на пропуск». Спрашивать это надо
# сразу, вместе с паспортом, а не после того, как заявка вернулась.
#
# Список через запятую, а не отдельной таблицей: значений два-три на строку,
# меняются они раз в полгода, и связь один-к-многим тут дала бы вторую таблицу
# ради экономии, которой не будет.
MIGRATIONS.append("""
ALTER TABLE sb_chats ADD COLUMN extra_docs TEXT NOT NULL DEFAULT '';
""")


# v16. Очередь лидов в Битрикс.
#
# Отдельная таблица, а не общая очередь сообщений: там доставкой занимается
# сервис переписки, а Битрикс не мессенджер — у него нет ни канала, ни чата, и
# отправляет туда сам навигатор своим планировщиком.
#
# UNIQUE по паре «телефон и позиция»: тот же человек по той же вакансии — тот
# же лид, сколько бы раз он ни перезвонил. По другой вакансии лид новый, это
# уже другая работа и другой менеджер.
MIGRATIONS.append("""
CREATE TABLE crm_leads (
    crm_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL,
    position_id TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending / sent / failed
    bitrix_id   INTEGER,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (phone, position_id)
);

CREATE INDEX idx_crm_status ON crm_leads(status, created_at);
""")


# v17. Копия присланного файла у навигатора.
#
# media_path — это путь на диске сервиса переписки, и открыть его из браузера
# менеджер не может: файл лежит у мессенджера, навигатор о нём только знает.
# Менеджер видел вместо паспорта строку «смотрите в мессенджере», то есть
# документ, ради которого вся переписка и затевалась, до него не доезжал.
#
# media_file — имя того же файла уже в хранилище навигатора (каталог рядом с
# базой, не в самой базе: паспорт в SQLite не кладут — база целиком уезжает в
# бэкап, а удалить оттуда одну картинку по требованию человека нельзя).
# Отдельная колонка, а не замена media_path: по пути мессенджер продолжает
# собирать заявку в чат безопасности и стирать файлы через неделю.
MIGRATIONS.append("""
ALTER TABLE candidate_inbound ADD COLUMN media_file TEXT NOT NULL DEFAULT '';
""")

# --- v18: приоритетные объекты ------------------------------------------------
#
# Заказчик каждую неделю называет, какие объекты «горят»: на них нужно набрать
# людей в первую очередь. До этой таблицы приоритет жил только в переписке, а
# агент предлагал то, что выше по нашей выплате, — и звонил людей на объект,
# который уже укомплектован.
#
# Срок обязателен. Приоритет без даты окончания — это приоритет навсегда: он
# тихо продолжает действовать, когда неделя прошла и «горит» уже другое. Ровно
# так протухли двадцать три правила выплат, и заметили мы это случайно.
MIGRATIONS.append("""
CREATE TABLE priority_objects (
    pattern    TEXT NOT NULL,
    tier       INTEGER NOT NULL DEFAULT 1,
    valid_from TEXT NOT NULL,
    valid_to   TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (pattern, valid_from)
);

CREATE INDEX idx_priority_window ON priority_objects(valid_from, valid_to);
""")
