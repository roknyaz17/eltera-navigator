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
from contextlib import contextmanager
from typing import Iterator, List

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
                for version in range(current, len(MIGRATIONS)):
                    logger.info(f"[registry] применяю миграцию v{version + 1}")
                    conn.executescript(MIGRATIONS[version])
                    conn.execute(f"PRAGMA user_version={version + 1}")
                conn.commit()
        finally:
            conn.close()
        _initialized.add(path)


def reset_schema_cache() -> None:
    """Сбрасывает память о проинициализированных базах — нужно тестам."""
    _initialized.clear()
