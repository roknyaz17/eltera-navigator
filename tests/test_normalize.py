"""Унификация значений: одинаковое по смыслу должно стать одинаковым по форме,
а отсутствующее — остаться пустым."""

import pytest

from registry import dictionaries as dicts
from registry.normalize import (
    Normalizer,
    canon_gender,
    canon_sb_policy,
    canon_shift_type,
    canon_work_format,
    canon_city,
    fingerprint,
    parse_rate,
    parse_schedule,
    title_ru,
    to_bool,
    to_int,
)


@pytest.mark.parametrize("raw,expected", [
    ("3 498 р/смена", (3498, None)),
    ("320 р/час - 3 520 р/смена (первые 3 смены, далее %)", (3520, 320)),
    ("300₽/час (3300₽/смена 11 часов)", (3300, 300)),
    ("Ставка: 3498", (3498, None)),
    (3498, (3498, None)),
    # «первые 3 смены» синтаксически похоже на ставку, но 3 ₽ за смену не бывает
    ("первые 3 смены", (None, None)),
    ("45000 руб в месяц", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_parse_rate(raw, expected):
    assert parse_rate(raw) == expected


def test_parse_rate_does_not_convert_hour_to_shift():
    """Пересчёт часа в смену — это домысел: неоплачиваемый перерыв не виден."""
    shift, hourly = parse_rate("320 р/час")
    assert hourly == 320
    assert shift is None


@pytest.mark.parametrize("raw,expected", [
    ("вахта от 30 смен", ("вахта", 30, None)),
    ("6/1 по 12 ч", ("6/1", None, 12.0)),
    ("2/2", ("2/2", None, None)),
    ("20/30/45 смен", (None, 20, None)),
    ("Вахта от 20 смен, смена 11 часов", ("вахта", 20, 11.0)),
    ("", (None, None, None)),
])
def test_parse_schedule(raw, expected):
    assert parse_schedule(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Без тяж.статей", "без тяж.статей"),
    ("без тяжких", "без тяж.статей"),
    ("легкие статьи допускаются", "лёгкие статьи допускаются"),
    ("Лёгкие статьи допускаются", "лёгкие статьи допускаются"),
    ("ПРОВЕРКА СБ", "проверка СБ"),
    ("выборочная проверка СБ", "выборочная"),
    ("БЕЗ СБ", "нет"),
    ("", None),
])
def test_canon_sb_policy(raw, expected):
    assert canon_sb_policy(raw) == expected


def test_enum_canonicalization():
    assert canon_shift_type("ночь") == "night"
    assert canon_shift_type("DAY") == "day"
    assert canon_shift_type("что-то своё") is None
    assert canon_gender("М") == "мужчины"
    assert canon_gender("м/ж") == "любые"
    assert canon_work_format("Вахта от 30 смен") == "вахта"
    assert canon_work_format("непонятно") is None


def test_city_and_title_cleanup():
    assert canon_city("г. Пушкино") == "Пушкино"
    assert canon_city("САНКТ-ПЕТЕРБУРГ") == "Санкт-Петербург"
    assert title_ru("КОМПЛЕКТОВЩИК") == "Комплектовщик"
    assert title_ru("СПК") == "СПК"
    assert title_ru("сборщик/комплектовщик") == "Сборщик/комплектовщик"


def test_to_bool_distinguishes_unknown_from_false():
    assert to_bool(None) is None
    assert to_bool("") is None
    assert to_bool(False) == 0
    assert to_bool("нет") == 0
    assert to_bool(True) == 1


def test_to_int_handles_spaced_numbers():
    assert to_int("3 498") == 3498
    assert to_int("3 498 ₽") == 3498
    assert to_int("нет") is None


def test_nothing_is_invented(conn):
    """Главное требование: чего нет в заявке — того нет и в реестре."""
    fields = Normalizer(conn).normalize({"vacancy_name": "Грузчик"})
    filled = {name: value for name, value in fields.items() if value is not None}
    assert filled == {"vacancy_name": "Грузчик", "vacancy_name_raw": "Грузчик"}
    # ни смены по умолчанию, ни питания «раз вахта», ни нулевой потребности
    assert fields["shift_type"] is None
    assert fields["meals_available"] is None
    assert fields["need_total"] is None
    assert fields["region"] is None


def test_need_total_sums_only_known_parts(conn):
    normalizer = Normalizer(conn)
    assert normalizer.normalize({"need_men": 5, "need_women": 10})["need_total"] == 15
    assert normalizer.normalize({"need_total": 7, "need_men": 5})["need_total"] == 7
    assert normalizer.normalize({"vacancy_name": "Грузчик"})["need_total"] is None


def test_region_comes_only_from_confirmed_dictionary(conn):
    normalizer = Normalizer(conn)
    # Регион не указан и справочник пуст — поле остаётся пустым.
    assert normalizer.normalize({"vacancy_name": "Грузчик", "city": "Пушкино"})["region"] is None

    dicts.upsert(conn, dicts.KIND_CITY_REGION, "пушкино", "Московская область", confirmed=True)
    normalizer.reload()
    assert normalizer.normalize({"vacancy_name": "Грузчик", "city": "Пушкино"})["region"] == "Московская область"


def test_unconfirmed_dictionary_entry_is_not_applied(conn):
    """Незнакомый вариант не склеивается с похожим, пока его не подтвердили."""
    dicts.upsert(conn, dicts.KIND_JOB_TITLE, "комплектовщик спк", "Комплектовщик", confirmed=False)
    normalizer = Normalizer(conn)
    fields = normalizer.normalize({"vacancy_name": "Комплектовщик СПК"})
    assert fields["vacancy_name"] == "Комплектовщик СПК"

    dicts.confirm(conn, dicts.KIND_JOB_TITLE, "комплектовщик спк", "Комплектовщик")
    normalizer.reload()
    assert normalizer.normalize({"vacancy_name": "Комплектовщик СПК"})["vacancy_name"] == "Комплектовщик"


def test_unknown_values_go_to_confirmation_queue(conn):
    Normalizer(conn).normalize({"vacancy_name": "Жиловщик мяса", "city": "Клин"})
    pending = {(row["kind"], row["alias"]) for row in dicts.pending(conn)}
    assert (dicts.KIND_JOB_TITLE, "жиловщик мяса") in pending
    assert (dicts.KIND_CITY, "клин") in pending


def test_fingerprint_ignores_letter_case_and_spacing(conn):
    normalizer = Normalizer(conn)
    left = normalizer.normalize({"vacancy_name": "КОМПЛЕКТОВЩИК", "city": "г. Пушкино"})
    right = normalizer.normalize({"vacancy_name": "комплектовщик", "city": "Пушкино"})
    assert fingerprint(left) == fingerprint(right)


@pytest.mark.parametrize("raw,expected", [
    # запятая как разделитель тысяч: «3,960» — это 3960 ₽, а не 3 ₽
    ("3,960 руб фикс.", 3960),
    ("1,234,567", 1234567),
    # запятая как дробная часть
    ("3,5", 3),
    ("3 498", 3498),
    ("3498 р", 3498),
])
def test_to_int_handles_thousands_separator(raw, expected):
    assert to_int(raw) == expected


def test_money_fields_ignore_implausible_numbers(conn):
    """Ставка 10 ₽ за смену — это не ставка.

    Отсечку в parse_rate обходил общий разбор числовых полей, и из старых
    строк, где в колонке ставки лежало количество людей, в реестр приезжали
    ставки 10 и 20 ₽.
    """
    normalizer = Normalizer(conn)
    for junk in ("10", "20", "первые 3 смены"):
        fields = normalizer.normalize({"vacancy_name": "Грузчик", "shift_rate": junk})
        assert fields["shift_rate"] is None, f"{junk!r} не должно стать ставкой"
        assert fields["shift_rate_raw"] == junk, "исходное значение сохраняется для сверки"


def test_money_field_survives_valid_value(conn):
    fields = Normalizer(conn).normalize({"vacancy_name": "Грузчик", "shift_rate": "3,960 руб фикс."})
    assert fields["shift_rate"] == 3960
