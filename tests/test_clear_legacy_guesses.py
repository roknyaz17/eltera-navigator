"""Чистка следов удалённых правил-догадок.

Главное, что здесь проверяется, — что чистка не сносит настоящие данные.
На реальном прогоне грубый вариант («стереть смену у всех») схлопнул дневную
и ночную позиции одного объекта в одну: смена была единственным, что их
различало, и она была написана в заявке, а не подставлена.
"""

import pytest

from scripts.clear_legacy_guesses import RULES, normalize_text, region_from_text


def clears(field: str, value, text: str) -> bool:
    return RULES[field].should_clear(value, normalize_text(text))


def test_default_shift_is_cleared_when_text_is_silent():
    assert clears("shift_type", "day", "Комплектовщик, вахта от 30 смен, 3498 р/смена")


def test_stated_shift_is_kept():
    """«ночь: 6 ж / день: 8 ж» — это реальные две позиции, а не подстановка."""
    text = "Миксит склад - сборщик - ночь: 6 ж 2 м / день: 8 ж 3 м"
    assert not clears("shift_type", "day", text)
    assert not clears("shift_type", "night", text)


def test_night_and_mixed_are_never_cleared():
    """По умолчанию подставлялся только «day» — остальное всегда из текста."""
    silent = "Комплектовщик, вахта от 30 смен"
    assert not clears("shift_type", "night", silent)
    assert not clears("shift_type", "mixed", silent)


def test_meals_cleared_only_when_not_mentioned():
    assert clears("meals_available", 1, "Грузчик, вахта от 15 смен, 3200 р/смена")
    assert not clears("meals_available", 1, "вахта от 15 смен, питание бесплатное")
    assert not clears("meals_free", 1, "комплексный обед - 200р (вычет из расчета)")


def test_medbook_cleared_only_when_not_mentioned():
    assert clears("medical_book_required", 1, "Фасовщик на пищевое производство")
    assert not clears("medical_book_required", 1, "Фасовщик, нужна медкнижка")
    assert not clears("medical_book_required", 1, "Упаковщик, ЛМК за счёт компании")


def test_none_is_not_touched():
    assert not clears("shift_type", None, "что угодно")


@pytest.mark.parametrize("region,text,expected", [
    # регион прямо назван — оставляем
    ("Московская область", "Миксит склад (Московская обл.)", True),
    ("Тульская область", "объект в Тульской области", True),
    # в тексте только город — регион вывела модель
    ("Московская область", "Молком, Пушкино, комплектовщик", False),
    ("Ленинградская область", "Санкт-Петербург, грузчик", False),
    (None, "что угодно", True),
])
def test_region_traceability(region, text, expected):
    assert region_from_text(region, normalize_text(text)) is expected


def test_region_word_endings_do_not_matter():
    """«Тульская» в справочнике и «Тульской» в тексте — одно и то же."""
    assert region_from_text("Тульская область", normalize_text("работа в Тульской обл"))
