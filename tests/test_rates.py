"""Ставки рекрутёра: подбор под позицию и три стратегии выставления.

Сеть и LLM не нужны — это чистая логика областей действия. Числа взяты из
настоящих сеток контрагентов: лестница Градуса (15/20/30 смен) и лист ЯППИ,
где поверх лестницы идут исключения по объектам и разрядам.
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry import db, rates


def rule(**kwargs) -> rates.RateRule:
    kwargs.setdefault("source", "vahtapro")
    return rates.RateRule(**kwargs)


LADDER = [
    rule(min_shifts=15, amount=15000),
    rule(min_shifts=20, amount=20000),
    rule(min_shifts=30, amount=23000),
]


# ------------------------------------------------------------------- подбор

def test_no_rules_means_no_rate():
    """Пусто — это «ставка не задана», а не ноль."""
    assert rates.resolve([], "vahtapro", "BMJ", "Грузчик", 20) is None


def test_flat_rule_covers_whole_counterparty():
    hit = rates.resolve([rule(amount=20000)], "vahtapro", "BMJ", "Грузчик", 20)
    assert hit["amount"] == 20000
    assert hit["scope"] == "все объекты контрагента"


def test_rule_of_another_counterparty_does_not_apply():
    assert rates.resolve([rule(source="yappi", amount=40000)],
                         "vahtapro", "BMJ", "Грузчик", 20) is None


@pytest.mark.parametrize("min_shifts, expected", [
    (15, 15000),
    (20, 20000),
    (25, 20000),   # ступень «от 20», следующая начинается с 30
    (30, 23000),
    (45, 23000),
])
def test_ladder_step_follows_vahta_length(min_shifts, expected):
    hit = rates.resolve(LADDER, "vahtapro", "BMJ", "Грузчик", min_shifts)
    assert hit["amount"] == expected
    # Вся лестница едет рядом: платят-то по факту отработанного.
    assert hit["tiers"] == [
        {"minShifts": 15, "amount": 15000},
        {"minShifts": 20, "amount": 20000},
        {"minShifts": 30, "amount": 23000},
    ]


def test_ladder_without_known_vahta_says_so():
    hit = rates.resolve(LADDER, "vahtapro", "BMJ", "Грузчик", None)
    assert hit["amount"] == 15000
    assert "не указан" in hit["stepNote"]


def test_ladder_below_lowest_step_is_flagged():
    """Вахта 10 смен при нижней ступени 15 — рекрутёр должен это видеть."""
    hit = rates.resolve(LADDER, "vahtapro", "BMJ", "Грузчик", 10)
    assert hit["amount"] == 15000
    assert "ниже нижней ступени" in hit["stepNote"]


def test_object_rule_beats_counterparty_rule():
    rules = [rule(amount=20000), rule(client="ДНС Пушкино", amount=25000)]
    assert rates.resolve(rules, "vahtapro", "ДНС Пушкино", "Грузчик", 20)["amount"] == 25000
    assert rates.resolve(rules, "vahtapro", "Ламода", "Грузчик", 20)["amount"] == 20000


def test_vacancy_rule_beats_object_rule():
    """Лист ЯППИ: у ТЭЦ Чульман своя ставка на каждый разряд."""
    rules = [
        rule(source="yappi", amount=25000),
        rule(source="yappi", client="ТЭЦ Чульман", amount=35000),
        rule(source="yappi", client="ТЭЦ Чульман", vacancy="Электромонтёр 6 разряда", amount=45000),
    ]
    assert rates.resolve(rules, "yappi", "ТЭЦ Чульман",
                         "Электромонтёр 6 разряда", 35)["amount"] == 45000
    assert rates.resolve(rules, "yappi", "ТЭЦ Чульман", "Слесарь", 35)["amount"] == 35000
    assert rates.resolve(rules, "yappi", "Старый Оскол", "Слесарь", 35)["amount"] == 25000


def test_object_ladder_beats_counterparty_ladder():
    rules = LADDER + [
        rule(client="Молком", min_shifts=20, amount=30000),
        rule(client="Молком", min_shifts=30, amount=35000),
    ]
    hit = rates.resolve(rules, "vahtapro", "Молком", "Комплектовщик", 30)
    assert hit["amount"] == 35000
    assert len(hit["tiers"]) == 2      # чужая лестница в карточку не попадает


def test_expired_rule_is_shown_but_marked():
    """Сетка недельная: просроченную показываем, но помечаем.

    Спрятать — значит написать «ставка не задана» там, где её просто не
    обновили в понедельник.
    """
    rules = [rule(amount=20000, valid_to="2026-07-31")]
    hit = rates.resolve(rules, "vahtapro", "BMJ", "Грузчик", 20, today=date(2026, 8, 14))
    assert hit["amount"] == 20000 and hit["expired"] is True
    hit = rates.resolve(rules, "vahtapro", "BMJ", "Грузчик", 20, today=date(2026, 7, 20))
    assert hit["expired"] is False


def test_note_and_payout_travel_with_the_rate():
    rules = [rule(amount=20000, note="повторная вахта — 50%", payout="адаптация 5 смен")]
    hit = rates.resolve(rules, "vahtapro", "BMJ", "Грузчик", 20)
    assert hit["note"] == "повторная вахта — 50%"
    assert hit["payout"] == "адаптация 5 смен"


def test_client_key_prefers_object_name():
    """У Аметиста объект в object_name, у Градуса заказчик в counterparty."""
    assert rates.client_key("Аметист", "ДНС Пушкино") == "ДНС Пушкино"
    assert rates.client_key("BMJ", "") == "BMJ"


# -------------------------------------------------------------------- запись

def test_save_load_and_history(db_path):
    with db.connect(db_path) as conn:
        rates.save_rules(conn, LADDER, author="директор")
    with db.connect(db_path) as conn:
        saved = rates.load_rules(conn, "vahtapro")
        assert [r.amount for r in saved] == [15000, 20000, 23000]
        assert rates.history(conn)[0]["author"] == "директор"


def test_save_rewrites_same_scope(db_path):
    """Повторное выставление меняет сумму, а не плодит вторую строку."""
    with db.connect(db_path) as conn:
        rates.save_rules(conn, [rule(amount=20000)], author="директор")
        rates.save_rules(conn, [rule(amount=22000)], author="директор")
        rules = rates.load_rules(conn, "vahtapro")
    assert len(rules) == 1 and rules[0].amount == 22000


def test_clear_scope_removes_old_ladder(db_path):
    """Новая сетка короче старой — лишние ступени не должны остаться висеть."""
    with db.connect(db_path) as conn:
        rates.save_rules(conn, LADDER, author="директор")
        rates.clear_scope(conn, "vahtapro", author="директор")
        rates.save_rules(conn, [rule(min_shifts=20, amount=21000)], author="директор")
        rules = rates.load_rules(conn, "vahtapro")
    assert [(r.min_shifts, r.amount) for r in rules] == [(20, 21000)]


def test_delete_rule_writes_history(db_path):
    with db.connect(db_path) as conn:
        rates.save_rules(conn, [rule(amount=20000)], author="директор")
        rule_id = rates.load_rules(conn, "vahtapro")[0].id
        assert rates.delete_rule(conn, rule_id, author="директор") is True
        assert rates.load_rules(conn, "vahtapro") == []
        assert rates.history(conn)[0]["action"] == "delete"


def test_delete_missing_rule_is_not_an_error(db_path):
    with db.connect(db_path) as conn:
        assert rates.delete_rule(conn, 999) is False
