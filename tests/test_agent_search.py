"""Подбор вакансий для голосового агента.

Цена ошибки здесь несимметрична и в обе стороны. Отсечь позицию по признаку,
который парсер просто не извлёк, — потерять живую вакансию, а таких пробелов в
реестре много: города нет у четверти активных позиций, медкнижки — у трети.
Выдать же непроверенное за подходящее — пообещать человеку по телефону то, чего
никто не проверял, и получить отказ на проходной.

Поэтому почти все тесты ниже — про пустоту: что она не отсеивает и что она не
превращается в утверждение.
"""

import pytest

from agent_search import (
    age_ok,
    citizenship_ok,
    gender_ok,
    geo_ok,
    medical_ok,
    project,
    rank,
)


# --------------------------------------------------------------- гражданство

def test_требований_нет_значит_подходит_любому():
    """87% позиций гражданство указывают, остальные молчат — это не запрет."""
    assert citizenship_ok([], "УЗБ") is True


def test_кандидат_не_назвал_гражданство_не_отсекаем():
    assert citizenship_ok(["РФ"], "") is True


def test_прямое_совпадение():
    assert citizenship_ok(["РФ", "РБ"], "РФ") is True
    assert citizenship_ok(["РФ", "РБ"], "УЗБ") is False


def test_союзы_раскрываются():
    """В требованиях пишут то «ЕАЭС», то перечисление стран."""
    assert citizenship_ok(["ЕАЭС"], "КЗ") is True
    assert citizenship_ok(["ЕАЭС"], "УЗБ") is False
    assert citizenship_ok(["СНГ"], "УЗБ") is True


# -------------------------------------------------------------------- возраст

def test_возрастных_рамок_нет():
    assert age_ok(None, None, 55) is True


def test_односторонняя_рамка():
    """age_to заполнен у всех активных, age_from — заметно реже."""
    assert age_ok(None, 45, 30) is True
    assert age_ok(None, 45, 50) is False
    assert age_ok(18, None, 16) is False


def test_границы_включительно():
    assert age_ok(18, 45, 18) is True
    assert age_ok(18, 45, 45) is True


# ------------------------------------------------------------------------ пол

def test_пол_не_указан_в_позиции_подходит_всем():
    """Четверть активных позиций пол не указывает."""
    assert gender_ok("", "женщина") is True
    assert gender_ok("любые", "женщина") is True


def test_пол_ограничен():
    assert gender_ok("мужчины", "мужчина") is True
    assert gender_ok("мужчины", "женщина") is False
    assert gender_ok("женщины", "женщина") is True


def test_кандидат_пол_не_назвал():
    assert gender_ok("мужчины", "") is True


# ------------------------------------------------------------------ медкнижка

def test_книжка_есть_подходит_всё():
    for state in ("no", "need", "arranged", "na"):
        assert medical_ok(state, True) is True


def test_книжки_нет_отсекаем_только_однозначное_нужна():
    assert medical_ok("need", False) is False
    assert medical_ok("no", False) is True
    # Работодатель оформляет сам — это не препятствие.
    assert medical_ok("arranged", False) is True


def test_про_книжку_неизвестно_не_отсекаем():
    """medical_book_required заполнен у 64% активных. Пустота — не «нужна»."""
    assert medical_ok("na", False) is True


def test_кандидата_про_книжку_не_спросили():
    assert medical_ok("need", None) is True


# ------------------------------------------------------------------ география

def test_кандидат_город_не_назвал():
    assert geo_ok("домодедово", "москва и область", "", "") is True


def test_позиция_без_географии_не_отсекается():
    """У 25 из 86 активных позиций города нет вовсе."""
    assert geo_ok("", "", "москва", "") is True


def test_совпадение_по_городу_или_по_региону():
    """Объединение, а не пересечение: иначе Коледино выпадет из запроса «Москва»."""
    assert geo_ok("коледино", "москва и область", "москва", "") is True
    assert geo_ok("домодедово", "москва и область", "домодедово", "") is True


def test_другой_регион_отсекается():
    assert geo_ok("тверь", "тверская область", "москва", "") is False


# ------------------------------------------------------------------ проекция

def _row(**overrides):
    base = {
        "position_id": "ELT-2026-000001-01",
        "vacancy_name": "Грузчик",
        "city": "Домодедово",
        "region": "Московская область",
        "shift_rate": 3707,
        "shift_hours": 11.0,
        "schedule": "вахта от 15 смен",
        "min_shifts": 15,
        "age_from": None,
        "age_to": 45,
        "gender": "мужчины",
        "citizenship_requirements": "РФ, РБ",
        "medical_book_required": 1,
        "can_start_without_medical_book": 0,
        "housing_available": 1,
        "housing_free": 1,
        "housing_deduction": None,
        "housing_conditions": "г. Москва, ул. Люблинская, д. 13",
        "meals_available": 1,
        "meals_free": 0,
        "meals_deduction": None,
        "meals_times_per_day": 2,
    }
    base.update(overrides)
    return base


def test_адрес_общежития_наружу_не_уходит():
    """housing_conditions местами содержит полный адрес объекта.

    Кандидат узнаёт адрес от координатора после согласия, а не из первого
    холодного звонка. Заодно такое и вслух не зачитать.
    """
    card = project(_row(), city_regions={})
    assert "Люблинская" not in card["housing"]
    assert card["housing"] == "есть, бесплатно"


def test_внутренние_поля_отсутствуют():
    """Белый список, а не вычистка: контрагента и ставки рекрутёра тут нет."""
    card = project(_row(), city_regions={})
    forbidden = {"cp", "obj", "addr", "rec", "raw", "client", "counterparty"}
    assert not forbidden & set(card)


def test_ставка_продублирована_текстом():
    """Чтобы модели не приходилось считать — на пересчётах она и врёт."""
    card = project(_row(), city_regions={})
    assert card["rate_per_shift"] == 3707
    assert card["pay_text"] == "3707 рублей за смену на руки"


def test_ставки_нет_не_выдумываем_число():
    card = project(_row(shift_rate=None), city_regions={})
    assert card["rate_per_shift"] is None
    assert card["pay_text"] == "ставка не указана"


def test_жильё_неизвестно_остаётся_неизвестным():
    card = project(_row(housing_available=None), city_regions={})
    assert card["housing"] == "неизвестно"


def test_регион_приводится_к_единому_написанию():
    """«Московская область» и «Москва и МО» — одна область, а не две."""
    card = project(_row(), city_regions={})
    assert card["region"] == "Москва и область"


# --------------------------------------------------------------- ранжирование

def test_известная_ставка_выше_неизвестной():
    """Позиция без ставки всплывает, только если ничего определённого нет."""
    rows = [_row(shift_rate=None), _row(shift_rate=3000), _row(shift_rate=4000)]
    assert [r["shift_rate"] for r in rank(rows)] == [4000, 3000, None]
