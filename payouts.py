"""Сколько мы зарабатываем за оформленного кандидата на конкретной позиции.

Правила лежат в recruiter_rate_rules и заполняются вручную раз в неделю:
контрагенты объявляют акции, и они живут примерно семь дней. Таблица была в
схеме с самого начала, но её никто не читал — выдача вакансий ранжировалась по
ставке кандидата, то есть по тому, сколько платят ему, а не нам.

Два правила, которые здесь важнее кода.

Протухшее правило не действует. Если акция кончилась, позиция теряет надбавку
и опускается сама. Иначе через неделю после акции система продолжала бы
толкать наверх проект с обычной доходностью, и заметить это было бы нечем.

Сумма никогда не звучит вслух. Она влияет только на порядок выдачи; в
project() её нет и быть не должно — это внутренняя кухня, как и контрагент,
адрес объекта и сырой текст заявки.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple


def load_rules(conn, today: Optional[str] = None) -> List[dict]:
    """Действующие сегодня правила. Протухшие не возвращаются вовсе.

    Пустой valid_to означает постоянную ставку, а не бессрочную акцию: так
    заведены базовые ступени у vahtapro и aaaplus, они не привязаны к неделе.
    """
    today = today or date.today().isoformat()
    rows = conn.execute(
        """SELECT source, client, vacancy, min_shifts, amount, valid_from, valid_to
           FROM recruiter_rate_rules
           WHERE (valid_to = '' OR valid_to >= ?)
             AND (valid_from = '' OR valid_from <= ?)""",
        (today, today),
    ).fetchall()
    return [dict(row) for row in rows]


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("ё", "е").split())


def _fits(rule: dict, source: str, client: str, vacancy: str, shifts: int) -> bool:
    if _norm(rule["source"]) != source:
        return False
    # Пустое поле в правиле означает «любой», а не «пустой».
    if rule["client"] and _norm(rule["client"]) != client:
        return False
    if rule["vacancy"] and _norm(rule["vacancy"]) not in vacancy:
        return False
    # min_shifts в правиле — порог «от N смен». Позиция с неизвестным числом
    # смен проходит только под правила без порога: домысливать тут нельзя,
    # ошибка в свою пользу означает завышенный приоритет у позиции, за которую
    # нам столько не заплатят.
    if rule["min_shifts"]:
        return shifts >= rule["min_shifts"]
    return True


def _specificity(rule: dict) -> Tuple[int, int, int]:
    """Чем правило конкретнее, тем оно главнее при совпадении нескольких."""
    return (1 if rule["client"] else 0,
            1 if rule["vacancy"] else 0,
            rule["min_shifts"] or 0)


def payout_for(row: Any, rules: List[dict]) -> int:
    """Рублей за кандидата на этой позиции. 0 — ставка не заведена.

    Ноль и «мы ничего не заработаем» — разные вещи, но различить их по базе
    нельзя, и оба одинаково означают «наверх не поднимать».
    """
    source = _norm(row["source"])
    client = _norm(row["object_name"])
    vacancy = _norm(row["vacancy_name"])
    shifts = row["min_shifts"] or 0

    fitting = [r for r in rules if _fits(r, source, client, vacancy, shifts)]
    if not fitting:
        return 0
    best = max(fitting, key=_specificity)
    return int(best["amount"] or 0)


def coverage(conn, today: Optional[str] = None) -> Dict[str, Any]:
    """Насколько актуальны ставки. Нужно, чтобы протухание было видно.

    Без этого отчёта фича тихо ломается: правила перестают действовать, все
    позиции получают ноль, ранжирование откатывается к прежнему, и внешне
    ничего не меняется.
    """
    today = today or date.today().isoformat()
    rules = load_rules(conn, today)
    positions = conn.execute(
        "SELECT source, object_name, vacancy_name, min_shifts FROM positions WHERE is_active = 1"
    ).fetchall()

    with_rate = [p for p in positions if payout_for(p, rules)]
    expiring = conn.execute(
        """SELECT source, client, amount, valid_to FROM recruiter_rate_rules
           WHERE valid_to != '' AND valid_to >= ? ORDER BY valid_to""",
        (today,),
    ).fetchall()
    expired = conn.execute(
        "SELECT COUNT(*) FROM recruiter_rate_rules WHERE valid_to != '' AND valid_to < ?",
        (today,),
    ).fetchone()[0]

    return {
        "дата": today,
        "активных позиций": len(positions),
        "со ставкой": len(with_rate),
        "без ставки": len(positions) - len(with_rate),
        "действующих правил": len(rules),
        "протухших правил": expired,
        "ближайшие окончания": [
            {"источник": r["source"], "объект": r["client"],
             "сумма": r["amount"], "по": r["valid_to"]}
            for r in expiring[:10]
        ],
    }
