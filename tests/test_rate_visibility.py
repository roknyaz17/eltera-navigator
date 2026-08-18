"""Ставка рекрутёра: доля вместо цены контракта.

Суть требования — реальная сумма из сетки не должна доезжать до рекрутёра.
Поэтому проверяется payload целиком, а не одно поле: сумма лежит ещё и в
лестнице по сменам, и в самой сетке, и в журнале её правок.
"""

import pytest

import navigator_api


def payload_with(amount=30000, tiers=None, rules=None, history=None):
    """Минимальный payload той же формы, что собирает build_payload."""
    return {
        "rows": [{
            "id": "P-1",
            "prof": "Комплектовщик",
            "rate": 3740,
            "rec": {
                "amount": amount,
                "tier": 20,
                "tiers": tiers if tiers is not None else [
                    {"minShifts": 15, "amount": 20000},
                    {"minShifts": 30, "amount": 40000},
                ],
                "note": "повторная вахта +50%",
                "payout": "после 30 смен",
                "scope": "все объекты контрагента",
            },
        }],
        "rates": {
            "sources": [],
            "rules": rules if rules is not None else [{"id": 1, "amount": 30000}],
            "history": history if history is not None else [{"amount": 30000}],
        },
    }


@pytest.fixture(autouse=True)
def default_percent(monkeypatch):
    monkeypatch.delenv("RECRUITER_SHARE_PERCENT", raising=False)


# ------------------------------------------------------------------ процент

def test_default_share_is_twenty():
    assert navigator_api.recruiter_share_percent() == 20.0


@pytest.mark.parametrize("raw,expected", [
    ("25", 25.0),
    ("12,5", 12.5),
    ("", 20.0),
    ("не число", 20.0),
    ("0", 20.0),      # ноль скрыл бы ставку целиком — это опечатка, не настройка
    ("-10", 20.0),
    ("140", 20.0),    # больше суммы контракта показывать нечего
    ("100", 100.0),
])
def test_share_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("RECRUITER_SHARE_PERCENT", raw)
    assert navigator_api.recruiter_share_percent() == expected


# ------------------------------------------------------------------ рекрутёр

def test_recruiter_sees_only_the_share():
    result = navigator_api.apply_role_visibility(payload_with(30000), is_admin=False)
    assert result["rows"][0]["rec"]["amount"] == 6000


@pytest.mark.parametrize("amount,expected", [(20000, 4000), (30000, 6000), (40000, 8000)])
def test_share_of_real_grid_amounts(amount, expected):
    result = navigator_api.apply_role_visibility(payload_with(amount), is_admin=False)
    assert result["rows"][0]["rec"]["amount"] == expected


def test_ladder_is_masked_too():
    """Лестница по сменам — та же сетка, только развёрнутая."""
    result = navigator_api.apply_role_visibility(payload_with(), is_admin=False)
    tiers = result["rows"][0]["rec"]["tiers"]
    assert [t["amount"] for t in tiers] == [4000, 8000]
    assert [t["minShifts"] for t in tiers] == [15, 30]


def test_grid_and_history_do_not_reach_recruiter():
    result = navigator_api.apply_role_visibility(payload_with(), is_admin=False)
    assert result["rates"]["rules"] == []
    assert result["rates"]["history"] == []


def test_no_real_amount_anywhere_in_payload():
    """Главная проверка: числа 30000 нет нигде в ответе."""
    result = navigator_api.apply_role_visibility(payload_with(30000), is_admin=False)
    assert "30000" not in repr(result)
    assert "40000" not in repr(result)


def test_candidate_rate_is_untouched():
    """Режется ставка рекрутёра, а не оплата кандидата за смену."""
    result = navigator_api.apply_role_visibility(payload_with(), is_admin=False)
    assert result["rows"][0]["rate"] == 3740


def test_notes_survive():
    """Надбавки и условия выплаты рекрутёру нужны — их не режем."""
    rec = navigator_api.apply_role_visibility(payload_with(), is_admin=False)["rows"][0]["rec"]
    assert rec["note"] == "повторная вахта +50%"
    assert rec["payout"] == "после 30 смен"


def test_custom_percent_applies(monkeypatch):
    monkeypatch.setenv("RECRUITER_SHARE_PERCENT", "35")
    result = navigator_api.apply_role_visibility(payload_with(30000), is_admin=False)
    assert result["rows"][0]["rec"]["amount"] == 10500


def test_masked_flag_is_set():
    result = navigator_api.apply_role_visibility(payload_with(), is_admin=False)
    assert result["ratesMasked"] is True
    assert result["ratePercent"] == 20.0


# -------------------------------------------------------------- администратор

def test_admin_sees_the_real_amount():
    result = navigator_api.apply_role_visibility(payload_with(30000), is_admin=True)
    assert result["rows"][0]["rec"]["amount"] == 30000
    assert [t["amount"] for t in result["rows"][0]["rec"]["tiers"]] == [20000, 40000]


def test_admin_keeps_grid_and_history():
    result = navigator_api.apply_role_visibility(payload_with(), is_admin=True)
    assert result["rates"]["rules"]
    assert result["rates"]["history"]


def test_admin_gets_percent_to_check_against():
    """Администратору процент нужен, чтобы сверить, что увидит рекрутёр."""
    result = navigator_api.apply_role_visibility(payload_with(), is_admin=True)
    assert result["ratePercent"] == 20.0
    assert result["ratesMasked"] is False


# ------------------------------------------------------------------ границы

def test_position_without_rate_stays_without_rate():
    """«Ставка не задана» — честный ответ, он не должен превратиться в 0 ₽."""
    payload = payload_with()
    payload["rows"][0]["rec"] = None
    result = navigator_api.apply_role_visibility(payload, is_admin=False)
    assert result["rows"][0]["rec"] is None


def test_missing_amount_is_not_invented():
    payload = payload_with()
    payload["rows"][0]["rec"]["amount"] = None
    result = navigator_api.apply_role_visibility(payload, is_admin=False)
    assert result["rows"][0]["rec"]["amount"] is None


def test_empty_payload_survives():
    assert navigator_api.apply_role_visibility({}, is_admin=False) == {
        "ratePercent": 20.0, "ratesMasked": True,
    }


def test_rounding_is_to_the_rouble():
    result = navigator_api.apply_role_visibility(payload_with(3333), is_admin=False)
    assert result["rows"][0]["rec"]["amount"] == 667
