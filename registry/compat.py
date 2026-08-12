"""Совместимость со старым форматом строк.

Прежние страницы (/vacancies, /navigator) и выгрузка в Google Sheets ждут
плоский словарь со строковыми значениями и колонками из TARGET_COLUMNS. Реестр
хранит данные типизированно (числа числами, «неизвестно» как NULL), поэтому
здесь один-единственный переходник — чтобы шаблоны и фронт не переписывать.
"""

from typing import Dict, List, Optional

from registry import db
from registry.models import BOOL_FIELDS
from registry.queries import all_positions
from sheets_adapter import TARGET_COLUMNS

# Прежние 61 колонка плюс то, чего в старой схеме не было. Порядок первых
# сохранён: на них завязаны /vacancies и внешние потребители выгрузки.
LEGACY_COLUMNS: List[str] = list(TARGET_COLUMNS) + [
    "request_id", "hourly_rate", "work_pattern",
    "vacancy_name_raw", "city_raw", "schedule_raw", "shift_rate_raw",
]

# Колонка старого формата → колонка реестра, если имена разошлись.
ALIASES = {
    "vacancy_id": "position_id",
    "created_at": "first_seen_at",
    "last_updated_at": "last_seen_at",
    "source": "source_name",
    "original_message": "raw_text",
}

_DATE_COLUMNS = {"created_at", "updated_at", "last_updated_at"}


def _raw(row, key: str, default=None):
    """sqlite3.Row не умеет .get() и кидает IndexError на отсутствующий ключ."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def legacy_cell(row, column: str) -> str:
    """Одно значение в старом формате: всё строкой, булевы как TRUE/FALSE."""
    if column == "needs_review":
        return "TRUE" if _raw(row, "parse_status", "ok") != "ok" else "FALSE"
    if column == "is_active":
        return "TRUE" if row["is_active"] else "FALSE"

    value = _raw(row, ALIASES.get(column, column))
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if column in BOOL_FIELDS and isinstance(value, int):
        return "TRUE" if value else "FALSE"
    if column in _DATE_COLUMNS and isinstance(value, str):
        return value[:10]
    return str(value)


def to_legacy_dict(row) -> Dict[str, str]:
    return {column: legacy_cell(row, column) for column in LEGACY_COLUMNS}


def legacy_rows(active_only: bool = False, db_path: Optional[str] = None) -> List[Dict[str, str]]:
    """Весь реестр в прежнем формате строк."""
    with db.connect(db_path) as conn:
        return [to_legacy_dict(row) for row in all_positions(conn, active_only=active_only)]
