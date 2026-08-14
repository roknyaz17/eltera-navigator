"""Модели данных реестра и описание набора полей позиции.

Здесь же — единственное место, где перечислены поля позиции. И DDL, и
нормализация, и выгрузка в Sheets берут списки отсюда, чтобы добавление поля
не требовало правок в пяти файлах.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- поля позиции
#
# Тип поля важен не только для схемы: NULL в числовой колонке — это «в заявке
# не указано», и именно так это должно доехать до интерфейса. Пустая строка и
# ноль — не то же самое, что «неизвестно» (ровно на этом ломался прежний
# need_total = 0 + 0 + 0).

TEXT_FIELDS = [
    "counterparty",
    "vacancy_name",
    "vacancy_category",
    "city",
    "region",
    "object_name",
    "object_address",
    "work_format",
    "shift_type",
    "schedule",
    "work_pattern",
    "duties",
    "requirements",
    "gender",
    "citizenship_requirements",
    "housing_conditions",
    "medical_book_payer",
    "transport_terms",
    "advantages",
    "risks",
    "sb_policy",
]

INT_FIELDS = [
    "min_shifts",
    "shift_rate",
    "hourly_rate",
    "age_from",
    "age_to",
    "need_men",
    "need_women",
    "need_couples",
    "need_total",
    "housing_deduction",
    "meals_deduction",
    "meals_times_per_day",
    "uniform_deduction",
]

REAL_FIELDS = [
    "shift_hours",
]

BOOL_FIELDS = [
    "requires_tsd",
    "housing_available",
    "housing_free",
    "meals_available",
    "meals_free",
    "medical_book_required",
    "can_start_without_medical_book",
    "uniform_available",
    "uniform_free",
    "transport_paid",
    "transport_fully_paid",
]

# Исходные (ненормализованные) значения — хранятся рядом с каноническими,
# чтобы менеджер в панели «Как пришло / Как распозналось» видел, что именно
# было в заявке до унификации.
RAW_FIELDS = [
    "counterparty_raw",
    "vacancy_name_raw",
    "city_raw",
    "region_raw",
    "schedule_raw",
    "shift_rate_raw",
    "requirements_raw",
]

# Поля менеджера. Импорт их не трогает никогда — редактируются только руками
# в /registry (прежний PROTECTED_FIELDS из sheets_adapter.py).
MANAGER_TEXT_FIELDS = [
    "status",
    "priority",
    "responsible_manager",
    "recruiter_comment",
    "sales_script",
    "objections",
]
MANAGER_NUM_FIELDS = [
    "market_rate",
    "market_deviation",
]
MANAGER_FIELDS = MANAGER_TEXT_FIELDS + MANAGER_NUM_FIELDS

# Всё, что приходит из заявки и может быть перезаписано новой ревизией.
DATA_FIELDS = TEXT_FIELDS + INT_FIELDS + REAL_FIELDS + BOOL_FIELDS + RAW_FIELDS

# Поля, участвующие в fingerprint — ключе склейки одной и той же позиции
# между прогонами. Берутся уже нормализованными.
FINGERPRINT_FIELDS = [
    "counterparty",
    "city",
    "vacancy_name",
    "object_name",
    "work_format",
    "shift_type",
]


def sql_type(name: str) -> str:
    if name in INT_FIELDS or name in BOOL_FIELDS:
        return "INTEGER"
    if name in REAL_FIELDS:
        return "REAL"
    if name == "market_rate":
        return "INTEGER"
    if name == "market_deviation":
        return "REAL"
    return "TEXT"


# --------------------------------------------------------------------- модели

@dataclass
class RawRequest:
    """Входящая заявка «как пришла», до всякого разбора.

    Экстрактор источника собирает её и отдаёт в registry.ingest. Всё, что
    ниже, — это то, что источник знает про документ, ничего интерпретировать
    он не обязан.

    :param source: алиас источника (vahtapro, ametist, …, manual)
    :param source_ref: стабильный ключ документа внутри источника. Именно он,
        а не содержимое, определяет тождество заявки: у Telegram это id
        сообщения, у таблиц — естественный ключ строки/блока. Меняется
        содержимое — это новая ревизия той же заявки, а не новая заявка.
    :param raw_text: исходный текст для сверки, ровно как показывается менеджеру
    :param raw_payload: машинное представление (строка таблицы как dict и т.п.)
    """

    source: str
    source_ref: str
    raw_text: str
    source_name: str = ""
    source_url: str = ""
    counterparty_hint: str = ""
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    received_at: Optional[datetime] = None
    # Значения из структурированных колонок источника, которые ВСЕГДА главнее
    # разбора LLM. Так у Маркетстаффа «Должность» и «Объект» берутся из
    # таблицы: если доверить их модели, формулировка гуляет между прогонами
    # («Комплектовщик» → «Комплектовщик (сборка заказов)») и позиция двоится.
    field_overrides: Dict[str, Any] = field(default_factory=dict)
    # Значения, которые подставляются, только если LLM ничего не извлёк.
    field_defaults: Dict[str, Any] = field(default_factory=dict)
    # Куски для разбора, если документ слишком велик, чтобы разобрать его
    # одним вызовом. Заявка при этом остаётся ОДНА: пост в канале с двумя
    # десятками объектов — это один документ от контрагента, а нарезка по
    # «🚀» нужна только модели. raw_text хранит пост целиком.
    parse_chunks: Optional[List[str]] = None
    # Справка по проекту из базы знаний контрагента (папка на Яндекс.Диске).
    # Заявкой не является и позиций не создаёт: только дополняет поля, о
    # которых пост молчит, — адрес объекта, часы смены, условия проживания.
    # См. project_kb.py.
    extra_context: Optional[str] = None
    # Справка отдельно для каждого куска: в снимке дня один пост описывает
    # десяток разных проектов, и общего контекста у них нет.
    chunk_contexts: Optional[List[Optional[str]]] = None

    def context_for_chunks(self) -> List[Optional[str]]:
        """Справки по числу кусков разбора — ровно то, что нужно ingest."""
        chunks = self.parse_chunks or [self.raw_text]
        if self.chunk_contexts and len(self.chunk_contexts) == len(chunks):
            return list(self.chunk_contexts)
        return [self.extra_context] * len(chunks)

    @property
    def content_hash(self) -> str:
        """Хэш содержимого — детектор изменений, не идентификатор.

        В хэш идут и текст, и подстановки: если в таблице поменялась только
        колонка «Объект», текст мог не измениться, а позиция — да. Справка с
        диска — там же: контрагент дописал в описание проекта питание, текст
        поста не поменялся, а разобрать заявку заново нужно.
        """
        data = {
            "text": self.raw_text,
            "overrides": self.field_overrides,
            "defaults": self.field_defaults,
        }
        # Ключ добавляется, только когда справка есть: иначе появление самого
        # поля перебило бы хэши всех заявок всех источников разом и погнало бы
        # реестр на сплошной повторный разбор.
        contexts = [c for c in self.context_for_chunks() if c]
        if contexts:
            data["context"] = contexts
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class IngestStats:
    """Итог прогона одного источника через ingest."""

    requests_new: int = 0
    requests_changed: int = 0
    requests_unchanged: int = 0
    requests_failed: int = 0
    positions_added: int = 0
    positions_updated: int = 0
    positions_deactivated: int = 0
    llm_calls_saved: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "requests_new": self.requests_new,
            "requests_changed": self.requests_changed,
            "requests_unchanged": self.requests_unchanged,
            "requests_failed": self.requests_failed,
            "added": self.positions_added,
            "updated": self.positions_updated,
            "deactivated": self.positions_deactivated,
            "llm_calls_saved": self.llm_calls_saved,
        }

    def summary(self) -> str:
        return (
            f"заявок: новых={self.requests_new}, изменённых={self.requests_changed}, "
            f"без изменений={self.requests_unchanged}, ошибок={self.requests_failed}; "
            f"позиций: added={self.positions_added}, updated={self.positions_updated}, "
            f"deactivated={self.positions_deactivated}; "
            f"LLM-вызовов сэкономлено={self.llm_calls_saved}"
        )


def merge_stats(items: List[IngestStats]) -> IngestStats:
    total = IngestStats()
    for s in items:
        total.requests_new += s.requests_new
        total.requests_changed += s.requests_changed
        total.requests_unchanged += s.requests_unchanged
        total.requests_failed += s.requests_failed
        total.positions_added += s.positions_added
        total.positions_updated += s.positions_updated
        total.positions_deactivated += s.positions_deactivated
        total.llm_calls_saved += s.llm_calls_saved
    return total
