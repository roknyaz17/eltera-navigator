"""Чтение реестра: поиск, фильтры, сортировка, карточка заявки.

Весь SQL для интерфейса собран здесь, чтобы app.py остался про HTTP.
"""

import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from registry.models import DATA_FIELDS, MANAGER_FIELDS

# Колонки, по которым разрешена сортировка. Белый список, потому что имя
# колонки подставляется в SQL текстом.
SORTABLE = {
    "position_id", "counterparty", "vacancy_name", "city", "region",
    "object_name", "shift_rate", "hourly_rate", "min_shifts", "shift_type",
    "need_total", "need_men", "need_women", "sb_policy", "source",
    "last_seen_at", "first_seen_at", "updated_at", "is_active", "status",
}
DEFAULT_SORT = "last_seen_at"
DEFAULT_ORDER = "desc"

# Поля, пустота которых мешает работать с заявкой. Фильтр «есть пробелы»
# и метрика заполненности считаются по ним.
KEY_FIELDS = [
    "vacancy_name", "city", "shift_rate", "need_total", "schedule", "counterparty",
]

_FTS_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def fts_query(text: str) -> Optional[str]:
    """Пользовательский ввод → безопасный запрос FTS5.

    Спецсимволы FTS5 (кавычки, скобки, NEAR) в поиске по имени объекта
    встречаются сплошь и рядом, поэтому не экранируем их, а просто выкидываем
    всё, кроме слов и цифр. Последнее слово ищем по префиксу — чтобы «компл»
    находило «комплектовщик» ещё до того, как менеджер дописал слово.
    """
    tokens = _FTS_TOKEN_RE.findall(text or "")
    if not tokens:
        return None
    quoted = [f'"{tok}"' for tok in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    return " AND ".join(quoted)


def _apply_filters(filters: Dict[str, Any]) -> Tuple[List[str], List[Any]]:
    where: List[str] = []
    params: List[Any] = []

    active = filters.get("is_active", "true")
    if active == "true":
        where.append("p.is_active = 1")
    elif active == "false":
        where.append("p.is_active = 0")

    for name, column in (
            ("source", "p.source"),
            ("counterparty", "p.counterparty"),
            ("shift_type", "p.shift_type"),
            ("work_format", "p.work_format"),
            ("gender", "p.gender"),
            ("sb_policy", "p.sb_policy"),
            ("status", "p.status"),
    ):
        value = filters.get(name)
        if value:
            where.append(f"{column} = ?")
            params.append(value)

    for name, column in (("city", "p.city"), ("region", "p.region"),
                         ("vacancy_name", "p.vacancy_name")):
        value = filters.get(name)
        if value:
            where.append(f"{column} LIKE ?")
            params.append(f"%{value}%")

    if filters.get("rate_min"):
        where.append("p.shift_rate >= ?")
        params.append(filters["rate_min"])
    if filters.get("rate_max"):
        where.append("p.shift_rate <= ?")
        params.append(filters["rate_max"])

    if filters.get("max_shifts"):
        # Позиции без указанного минимума не отсекаем: неизвестно — не значит
        # «не подходит».
        where.append("(p.min_shifts IS NULL OR p.min_shifts <= ?)")
        params.append(filters["max_shifts"])

    if filters.get("age"):
        where.append("(p.age_from IS NULL OR p.age_from <= ?)")
        params.append(filters["age"])
        where.append("(p.age_to IS NULL OR p.age_to >= ?)")
        params.append(filters["age"])

    if filters.get("date_from"):
        where.append("r.first_seen_at >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        where.append("r.first_seen_at <= ?")
        params.append(filters["date_to"] + "T23:59:59")

    if filters.get("has_gaps"):
        gaps = " OR ".join(f"p.{name} IS NULL" for name in KEY_FIELDS)
        where.append(f"({gaps})")

    if filters.get("needs_review"):
        where.append("r.parse_status != 'ok'")

    query = filters.get("q")
    if query:
        prepared = fts_query(query)
        if prepared:
            where.append(
                "p.position_id IN (SELECT position_id FROM search_index "
                "WHERE search_index MATCH ?)"
            )
            params.append(prepared)

    return where, params


def search(
        conn: sqlite3.Connection,
        filters: Dict[str, Any],
        sort: str = DEFAULT_SORT,
        order: str = DEFAULT_ORDER,
        page: int = 1,
        per_page: int = 50,
) -> Tuple[List[sqlite3.Row], int]:
    """Возвращает (строки страницы, всего найдено)."""
    where, params = _apply_filters(filters)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM positions p "
        f"JOIN requests r ON r.request_id = p.last_request_id{where_sql}",
        params,
    ).fetchone()["cnt"]

    if sort not in SORTABLE:
        sort = DEFAULT_SORT
    direction = "DESC" if order == "desc" else "ASC"
    per_page = max(1, min(per_page, 500))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))

    rows = conn.execute(
        f"""
        SELECT p.*, r.request_id AS request_id, r.source_name, r.source_url,
               r.raw_text, r.first_seen_at AS request_first_seen_at,
               r.parse_status, r.revision
        FROM positions p
        JOIN requests r ON r.request_id = p.last_request_id
        {where_sql}
        ORDER BY p.{sort} IS NULL, p.{sort} {direction}, p.position_id
        LIMIT ? OFFSET ?
        """,
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    return list(rows), total


def facets(conn: sqlite3.Connection, active_only: bool = True) -> Dict[str, List[str]]:
    """Наполнение выпадающих списков — по фактически видимым строкам."""
    scope = " WHERE is_active = 1" if active_only else ""
    out: Dict[str, List[str]] = {}
    for name, column in (
            ("sources", "source"),
            ("counterparties", "counterparty"),
            ("cities", "city"),
            ("regions", "region"),
            ("sb_policies", "sb_policy"),
            ("statuses", "status"),
            ("work_formats", "work_format"),
    ):
        rows = conn.execute(
            f"SELECT DISTINCT {column} AS v FROM positions{scope} "
            f"{'AND' if scope else 'WHERE'} {column} IS NOT NULL AND {column} != '' "
            f"ORDER BY {column}"
        ).fetchall()
        out[name] = [row["v"] for row in rows]
    return out


def get_request(conn: sqlite3.Connection, request_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()


def positions_of_request(conn: sqlite3.Connection, request_id: str) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT p.* FROM positions p "
        "JOIN request_positions rp ON rp.position_id = p.position_id "
        "WHERE rp.request_id = ? ORDER BY p.position_id",
        (request_id,),
    ))


def revisions_of_request(conn: sqlite3.Connection, request_id: str) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM request_revisions WHERE request_id = ? ORDER BY revision DESC",
        (request_id,),
    ))


def get_position(conn: sqlite3.Connection, position_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.*, r.request_id AS request_id, r.raw_text, r.source_name,
               r.source_url, r.parse_status, r.revision, r.received_at
        FROM positions p
        JOIN requests r ON r.request_id = p.last_request_id
        WHERE p.position_id = ?
        """,
        (position_id,),
    ).fetchone()


def history_of_position(conn: sqlite3.Connection, position_id: str, limit: int = 200) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM position_history WHERE position_id = ? "
        "ORDER BY changed_at DESC, id DESC LIMIT ?",
        (position_id, limit),
    ))


def requests_of_position(conn: sqlite3.Connection, position_id: str) -> List[sqlite3.Row]:
    """Все заявки, которые приносили эту позицию."""
    return list(conn.execute(
        "SELECT r.request_id, r.source, r.source_name, r.first_seen_at, r.last_seen_at "
        "FROM requests r JOIN request_positions rp ON rp.request_id = r.request_id "
        "WHERE rp.position_id = ? ORDER BY r.first_seen_at DESC",
        (position_id,),
    ))


def update_manager_fields(
        conn: sqlite3.Connection,
        position_id: str,
        values: Dict[str, Any],
        author: str = "",
) -> int:
    """Правки менеджера. Приём данных эти поля не трогает никогда.

    Каждое изменение уходит в position_history с автором. Раньше ручные правки
    не записывались вообще: в истории были только диффы от новых ревизий заявки,
    и «кто поставил этот статус» восстановить было нельзя.
    """
    allowed = {k: v for k, v in values.items() if k in MANAGER_FIELDS}
    if not allowed:
        return 0

    before = conn.execute(
        f"SELECT {', '.join(allowed)} FROM positions WHERE position_id = ?", (position_id,)
    ).fetchone()

    assignments = ", ".join(f"{name} = ?" for name in allowed)
    params = list(allowed.values()) + [position_id]
    rowcount = conn.execute(
        f"UPDATE positions SET {assignments} WHERE position_id = ?", params
    ).rowcount

    if rowcount and before is not None:
        now = datetime.now().isoformat(timespec="seconds")
        for name, new_value in allowed.items():
            old_value = before[name]
            # Пишем только то, что действительно изменилось: иначе каждое
            # сохранение карточки плодит десяток пустых записей.
            if (old_value or "") == (new_value or ""):
                continue
            conn.execute(
                "INSERT INTO position_history "
                "(position_id, request_id, field, old_value, new_value, changed_at, changed_by) "
                "VALUES (?, '', ?, ?, ?, ?, ?)",
                (position_id, name,
                 None if old_value is None else str(old_value),
                 None if new_value is None else str(new_value),
                 now, author),
            )
    return rowcount


def all_positions(conn: sqlite3.Connection, active_only: bool = False) -> List[sqlite3.Row]:
    """Полный срез — для выгрузки в Sheets и совместимости со старым фронтом."""
    scope = " WHERE p.is_active = 1" if active_only else ""
    return list(conn.execute(
        f"""
        SELECT p.*, r.request_id AS request_id, r.source_name, r.source_url,
               r.raw_text, r.parse_status
        FROM positions p
        JOIN requests r ON r.request_id = p.last_request_id
        {scope}
        ORDER BY p.last_seen_at DESC, p.position_id
        """
    ))


def overview(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Сводка для шапки раздела и для метрик."""
    row = conn.execute(
        "SELECT COUNT(*) AS positions, "
        "SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active "
        "FROM positions"
    ).fetchone()
    requests = conn.execute("SELECT COUNT(*) AS cnt FROM requests").fetchone()["cnt"]
    failed = conn.execute(
        "SELECT COUNT(*) AS cnt FROM requests WHERE parse_status != 'ok'"
    ).fetchone()["cnt"]
    pending_dict = conn.execute(
        "SELECT COUNT(*) AS cnt FROM dictionaries WHERE confirmed = 0"
    ).fetchone()["cnt"]
    updated = conn.execute(
        "SELECT MAX(last_seen_at) AS ts FROM requests"
    ).fetchone()["ts"]
    return {
        "positions": row["positions"] or 0,
        "active": row["active"] or 0,
        "requests": requests,
        "failed": failed,
        "dict_pending": pending_dict,
        "updated_at": updated or "",
    }


def fill_ratio(conn: sqlite3.Connection) -> Dict[str, float]:
    """Доля активных позиций, где ключевое поле пустое.

    После отказа от додумывания это главный индикатор качества источника:
    видно, кто из контрагентов systematically не пишет ставку или график.
    """
    total = conn.execute(
        "SELECT COUNT(*) AS cnt FROM positions WHERE is_active = 1"
    ).fetchone()["cnt"]
    if not total:
        return {name: 0.0 for name in KEY_FIELDS}
    out = {}
    for name in KEY_FIELDS:
        empty = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM positions "
            f"WHERE is_active = 1 AND ({name} IS NULL OR {name} = '')"
        ).fetchone()["cnt"]
        out[name] = round(empty / total, 4)
    return out


def positions_by_source(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT source, is_active, COUNT(*) AS cnt FROM positions GROUP BY source, is_active"
    ))
