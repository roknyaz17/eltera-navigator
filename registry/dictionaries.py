"""Справочники унификации и очередь подтверждения.

Ключевое правило: незнакомый вариант написания НЕ схлопывается автоматически
в похожий. Он сохраняется как есть, а в справочник попадает строкой с
confirmed=0 — это и есть очередь на подтверждение (/registry/dictionaries).
Пока человек не подтвердил соответствие, нормализация такой алиас не
применяет. Иначе «Комплектовщик СПК» тихо превратился бы в «Комплектовщик»,
и никто бы не заметил подмены.
"""

import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

# Виды справочников.
KIND_JOB_TITLE = "job_title"
KIND_CITY = "city"
KIND_CITY_REGION = "city_region"
KIND_REGION = "region"
KIND_COUNTERPARTY = "counterparty"
KIND_WORK_FORMAT = "work_format"
KIND_VACANCY_CATEGORY = "vacancy_category"
KIND_SCHEDULE_PATTERN = "schedule_pattern"
KIND_CITIZENSHIP = "citizenship"

KINDS = [
    KIND_JOB_TITLE,
    KIND_CITY,
    KIND_CITY_REGION,
    KIND_REGION,
    KIND_COUNTERPARTY,
    KIND_WORK_FORMAT,
    KIND_VACANCY_CATEGORY,
    KIND_SCHEDULE_PATTERN,
    KIND_CITIZENSHIP,
]

KIND_TITLES = {
    KIND_JOB_TITLE: "Должности",
    KIND_CITY: "Города",
    KIND_CITY_REGION: "Город → регион",
    KIND_REGION: "Регионы",
    KIND_COUNTERPARTY: "Контрагенты",
    KIND_WORK_FORMAT: "Формат работы",
    KIND_VACANCY_CATEGORY: "Категории",
    KIND_SCHEDULE_PATTERN: "Графики",
    KIND_CITIZENSHIP: "Гражданство",
}


def norm_key(value: Optional[str]) -> str:
    """Ключ поиска по справочнику.

    Регистр, ё/е, пунктуация и лишние пробелы не должны порождать разные
    записи: «Комплектовщик», «комплектовщик» и «Комплектовщик.» — один алиас.
    """
    if value is None:
        return ""
    text = str(value).strip().lower().replace("ё", "е")
    text = re.sub(r"[^\w\s/+-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load(conn: sqlite3.Connection, kind: str, include_pending: bool = False) -> Dict[str, str]:
    """Возвращает {alias: canonical}. По умолчанию — только подтверждённые."""
    sql = "SELECT alias, canonical FROM dictionaries WHERE kind = ?"
    if not include_pending:
        sql += " AND confirmed = 1"
    return {row["alias"]: row["canonical"] for row in conn.execute(sql, (kind,))}


def load_all(conn: sqlite3.Connection, include_pending: bool = False) -> Dict[str, Dict[str, str]]:
    return {kind: load(conn, kind, include_pending) for kind in KINDS}


def upsert(
        conn: sqlite3.Connection,
        kind: str,
        alias: str,
        canonical: str,
        confirmed: bool = False,
        note: str = "",
) -> None:
    """Добавляет или обновляет запись справочника.

    Подтверждённую запись случайное повторное «предложение» из ingest не
    сбрасывает обратно в очередь — confirmed идёт по MAX.
    """
    key = norm_key(alias)
    if not key or not (canonical or "").strip():
        return
    now = _now()
    conn.execute(
        """
        INSERT INTO dictionaries (kind, alias, canonical, confirmed, hits, note, created_at, updated_at)
        VALUES (?, ?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT(kind, alias) DO UPDATE SET
            canonical = excluded.canonical,
            confirmed = MAX(dictionaries.confirmed, excluded.confirmed),
            note = CASE WHEN excluded.note != '' THEN excluded.note ELSE dictionaries.note END,
            updated_at = excluded.updated_at
        """,
        (kind, key, canonical.strip(), 1 if confirmed else 0, note, now, now),
    )


def record_seen(conn: sqlite3.Connection, kind: str, alias: str, proposed: str) -> None:
    """Отмечает встреченный вариант написания.

    Новый — заводится с confirmed=0 (очередь). Известный — просто +1 к hits,
    чтобы в очереди сверху оказалось то, что встречается чаще всего.
    """
    key = norm_key(alias)
    if not key:
        return
    now = _now()
    conn.execute(
        """
        INSERT INTO dictionaries (kind, alias, canonical, confirmed, hits, note, created_at, updated_at)
        VALUES (?, ?, ?, 0, 1, '', ?, ?)
        ON CONFLICT(kind, alias) DO UPDATE SET
            hits = dictionaries.hits + 1,
            updated_at = excluded.updated_at
        """,
        (kind, key, (proposed or alias).strip(), now, now),
    )


def confirm(conn: sqlite3.Connection, kind: str, alias: str, canonical: str = None) -> None:
    key = norm_key(alias)
    params = [_now(), kind, key]
    sql = "UPDATE dictionaries SET confirmed = 1, updated_at = ?"
    if canonical is not None and canonical.strip():
        sql += ", canonical = ?"
        params.insert(1, canonical.strip())
    sql += " WHERE kind = ? AND alias = ?"
    conn.execute(sql, params)


def confirm_all(conn: sqlite3.Connection, kind: str = None) -> int:
    """Массовое подтверждение — для первичного наполнения после сида."""
    if kind:
        cur = conn.execute(
            "UPDATE dictionaries SET confirmed = 1, updated_at = ? WHERE kind = ? AND confirmed = 0",
            (_now(), kind),
        )
    else:
        cur = conn.execute(
            "UPDATE dictionaries SET confirmed = 1, updated_at = ? WHERE confirmed = 0",
            (_now(),),
        )
    return cur.rowcount


def remove(conn: sqlite3.Connection, kind: str, alias: str) -> None:
    conn.execute(
        "DELETE FROM dictionaries WHERE kind = ? AND alias = ?", (kind, norm_key(alias))
    )


def pending(conn: sqlite3.Connection, kind: str = None, limit: int = 500) -> List[sqlite3.Row]:
    """Очередь на подтверждение — чаще встречающиеся сверху."""
    sql = "SELECT * FROM dictionaries WHERE confirmed = 0"
    params: List = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY hits DESC, alias ASC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def pending_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS cnt FROM dictionaries WHERE confirmed = 0 GROUP BY kind"
    ).fetchall()
    return {row["kind"]: int(row["cnt"]) for row in rows}


def canonical_values(conn: sqlite3.Connection, kind: str) -> List[str]:
    """Список уже утверждённых канонических значений — для подсказок в UI."""
    rows = conn.execute(
        "SELECT DISTINCT canonical FROM dictionaries "
        "WHERE kind = ? AND confirmed = 1 ORDER BY canonical",
        (kind,),
    ).fetchall()
    return [row["canonical"] for row in rows]


def entries(conn: sqlite3.Connection, kind: str, limit: int = 1000) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM dictionaries WHERE kind = ? ORDER BY confirmed DESC, hits DESC, alias LIMIT ?",
            (kind, limit),
        )
    )
