"""Человеческие названия полей — для интерфейса и выгрузок.

Держим отдельно от models.py: там технический контракт схемы, здесь — то, что
читает менеджер. Порядок в FIELD_GROUPS задаёт и порядок вывода в карточке.
"""

from typing import Dict, List, Tuple

FIELD_LABELS: Dict[str, str] = {
    "counterparty": "Контрагент",
    "vacancy_name": "Должность",
    "vacancy_category": "Категория",
    "city": "Город",
    "region": "Регион",
    "object_name": "Объект",
    "object_address": "Адрес объекта",
    "work_format": "Формат работы",
    "shift_type": "Смена",
    "schedule": "График",
    "work_pattern": "Шаблон графика",
    "min_shifts": "Минимум смен",
    "shift_hours": "Часов в смене",
    "shift_rate": "Ставка за смену, ₽",
    "hourly_rate": "Ставка за час, ₽",
    "duties": "Обязанности",
    "requirements": "Требования",
    "requires_tsd": "Нужен ТСД",
    "gender": "Пол",
    "age_from": "Возраст от",
    "age_to": "Возраст до",
    "citizenship_requirements": "Гражданство",
    "need_men": "Нужно мужчин",
    "need_women": "Нужно женщин",
    "need_couples": "Нужно семейных",
    "need_total": "Потребность всего",
    "housing_available": "Проживание есть",
    "housing_free": "Проживание бесплатное",
    "housing_deduction": "Вычет за проживание, ₽",
    "housing_conditions": "Условия проживания",
    "meals_available": "Питание есть",
    "meals_free": "Питание бесплатное",
    "meals_deduction": "Вычет за питание, ₽",
    "meals_times_per_day": "Питаний в день",
    "medical_book_required": "Нужна медкнижка",
    "medical_book_payer": "Кто платит за медкнижку",
    "can_start_without_medical_book": "Можно выйти без медкнижки",
    "uniform_available": "Спецодежда есть",
    "uniform_free": "Спецодежда бесплатная",
    "uniform_deduction": "Вычет за спецодежду, ₽",
    "transport_paid": "Проезд оплачивается",
    "transport_fully_paid": "Проезд оплачивается полностью",
    "transport_terms": "Условия проезда",
    "advantages": "Плюсы",
    "risks": "Риски",
    "sb_policy": "Проверка СБ",
    # исходники
    "counterparty_raw": "Контрагент (как в заявке)",
    "vacancy_name_raw": "Должность (как в заявке)",
    "city_raw": "Город (как в заявке)",
    "region_raw": "Регион (как в заявке)",
    "schedule_raw": "График (как в заявке)",
    "shift_rate_raw": "Ставка (как в заявке)",
    "requirements_raw": "Требования (как в заявке)",
    # менеджерские
    "status": "Статус",
    "priority": "Приоритет",
    "responsible_manager": "Ответственный",
    "recruiter_comment": "Комментарий рекрутера",
    "sales_script": "Скрипт продаж",
    "objections": "Возражения",
    "market_rate": "Рыночная ставка, ₽",
    "market_deviation": "Отклонение от рынка",
}

# Группы для карточки позиции: (заголовок, поля).
FIELD_GROUPS: List[Tuple[str, List[str]]] = [
    ("Кто и что", ["counterparty", "vacancy_name", "vacancy_category", "object_name", "object_address"]),
    ("Где", ["city", "region"]),
    ("Условия работы", ["work_format", "shift_type", "schedule", "work_pattern",
                        "min_shifts", "shift_hours", "shift_rate", "hourly_rate"]),
    ("Потребность", ["need_total", "need_men", "need_women", "need_couples"]),
    ("Требования", ["requirements", "duties", "requires_tsd", "gender", "age_from",
                    "age_to", "citizenship_requirements", "sb_policy"]),
    ("Проживание и питание", ["housing_available", "housing_free", "housing_deduction",
                              "housing_conditions", "meals_available", "meals_free",
                              "meals_deduction", "meals_times_per_day"]),
    ("Прочее", ["medical_book_required", "medical_book_payer", "can_start_without_medical_book",
                "uniform_available", "uniform_free", "uniform_deduction",
                "transport_paid", "transport_fully_paid", "transport_terms",
                "advantages", "risks"]),
]

SOURCE_TITLES = {
    "kpk": "КПК",
    "yappi": "Yappi",
    "vahtapro": "ВахтаПро",
    "aaaplus": "AAA+",
    "ametist": "Аметист",
    "marketstaff": "Маркетстафф",
    "manual": "Вручную",
}


def label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def display(value, field: str = "") -> str:
    """Значение поля для показа. Пустое остаётся пустым, а не превращается в 0."""
    from registry.models import BOOL_FIELDS

    if value is None or value == "":
        return ""
    if field in BOOL_FIELDS:
        return "да" if value else "нет"
    if field == "shift_type":
        return {"day": "день", "night": "ночь", "mixed": "смешанная"}.get(str(value), str(value))
    return str(value)
