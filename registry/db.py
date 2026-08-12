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
