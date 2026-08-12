"""Внутренние идентификаторы реестра.

Заявка получает ID вида ELT-2026-000123 один раз при первом появлении и не
меняет его никогда — в отличие от прежнего vacancy_id, который был хэшем от
содержимого и «переезжал» при любой правке города или должности, порождая
дубль вместо обновления.

Позиция внутри заявки — ELT-2026-000123-01. Префикс указывает на заявку,
которая принесла позицию ВПЕРВЫЕ; если завтра та же позиция придёт в новом
снимке, id останется прежним, а ссылка на свежую заявку уедет в
positions.last_request_id.
"""

import os
import re
import sqlite3
from datetime import datetime
from typing import Optional, Tuple

ID_PREFIX = os.getenv("REGISTRY_ID_PREFIX", "ELT")
SEQ_WIDTH = 6
POSITION_WIDTH = 2

REQUEST_ID_RE = re.compile(rf"^{re.escape(ID_PREFIX)}-(\d{{4}})-(\d{{{SEQ_WIDTH},}})$")
POSITION_ID_RE = re.compile(
    rf"^{re.escape(ID_PREFIX)}-(\d{{4}})-(\d{{{SEQ_WIDTH},}})-(\d{{{POSITION_WIDTH},}})$"
)


def format_request_id(year: int, seq: int) -> str:
    return f"{ID_PREFIX}-{year:04d}-{seq:0{SEQ_WIDTH}d}"


def format_position_id(request_id: str, seq: int) -> str:
    return f"{request_id}-{seq:0{POSITION_WIDTH}d}"


def _bump(conn: sqlite3.Connection, scope: str) -> int:
    """Инкремент счётчика в пределах текущей транзакции.

    Два UPDATE/SELECT вместо RETURNING — чтобы не зависеть от версии SQLite
    в базовом образе. Запись в SQLite всё равно сериализуется, гонки нет.
    """
    conn.execute(
        "INSERT INTO id_counters (scope, value) VALUES (?, 0) "
        "ON CONFLICT(scope) DO NOTHING",
        (scope,),
    )
    conn.execute(
        "UPDATE id_counters SET value = value + 1 WHERE scope = ?",
        (scope,),
    )
    row = conn.execute(
        "SELECT value FROM id_counters WHERE scope = ?", (scope,)
    ).fetchone()
    return int(row["value"])


def next_request_id(conn: sqlite3.Connection, year: Optional[int] = None) -> Tuple[str, int, int]:
    """Выдаёт следующий ID заявки. Возвращает (request_id, year, seq)."""
    year = year or datetime.now().year
    seq = _bump(conn, f"request:{year}")
    return format_request_id(year, seq), year, seq


def next_position_id(conn: sqlite3.Connection, request_id: str) -> Tuple[str, int]:
    """Выдаёт следующий ID позиции внутри заявки. Возвращает (position_id, seq)."""
    seq = _bump(conn, f"position:{request_id}")
    return format_position_id(request_id, seq), seq


def parse_request_id(value: str) -> Optional[Tuple[int, int]]:
    """'ELT-2026-000123' -> (2026, 123). None, если формат не наш."""
    match = REQUEST_ID_RE.match((value or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def request_id_of(position_id: str) -> Optional[str]:
    """'ELT-2026-000123-01' -> 'ELT-2026-000123'."""
    match = POSITION_ID_RE.match((position_id or "").strip())
    if not match:
        return None
    return format_request_id(int(match.group(1)), int(match.group(2)))


def sync_counters(conn: sqlite3.Connection) -> None:
    """Подтягивает счётчики под фактическое содержимое таблиц.

    Нужно после массовой заливки (миграция из Sheets), когда строки
    вставлялись с уже готовыми id в обход next_*_id.
    """
    rows = conn.execute("SELECT year, MAX(seq) AS max_seq FROM requests GROUP BY year").fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO id_counters (scope, value) VALUES (?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET value = MAX(value, excluded.value)",
            (f"request:{int(row['year'])}", int(row["max_seq"] or 0)),
        )
    rows = conn.execute(
        "SELECT first_request_id, MAX(seq) AS max_seq FROM positions GROUP BY first_request_id"
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO id_counters (scope, value) VALUES (?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET value = MAX(value, excluded.value)",
            (f"position:{row['first_request_id']}", int(row["max_seq"] or 0)),
        )
