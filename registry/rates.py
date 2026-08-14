"""Ставки рекрутёра: правила, подбор под позицию, история.

Из заявок этот слой не приходит и прийти не может: сколько мы платим
рекрутёру — внутренняя договорённость с контрагентом. Раз в неделю контрагент
присылает картинку («сетка оплаты», «расчёты с рекрутерами»), и руководитель
переносит её сюда руками.

Три способа выставления, о которых просили, — это одна и та же строка правила
с разной областью действия:

    на всего контрагента      client = '',  vacancy = '',  min_shifts = 0
    лестница по сменам        client = '',  vacancy = '',  min_shifts = 15/20/30
    исключения по объектам    client = 'ДНС Пушкино', vacancy = '' или должность

Подбор идёт от частного к общему: правило по объекту и должности бьёт правило
по объекту, оно — правило по контрагенту. Ничего не нашлось — «ставка не
задана», и это честный ответ, а не ноль.

Надбавки («повторная вахта = 50%», «физлицам ниже на 5 000») сознательно
хранятся текстом: реестр не знает, повторная ли это вахта и кто рекрутёр,
поэтому посчитать их нельзя, а показать рекрутёру — нужно.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

FIELDS = ("source", "client", "vacancy", "min_shifts", "amount",
          "note", "payout", "valid_from", "valid_to")


@dataclass
class RateRule:
    """Одна строка сетки: сколько платим и за что именно."""

    source: str
    client: str = ""
    vacancy: str = ""
    min_shifts: int = 0
    amount: int = 0
    note: str = ""
    payout: str = ""
    valid_from: str = ""
    valid_to: str = ""
    id: Optional[int] = None
    created_at: str = ""
    author: str = ""

    @property
    def scope_rank(self) -> int:
        """Насколько правило конкретное. Больше — важнее при подборе."""
        return (2 if self.client else 0) + (1 if self.vacancy else 0)

    @property
    def scope_title(self) -> str:
        if self.client and self.vacancy:
            return f"{self.client} · {self.vacancy}"
        if self.client:
            return self.client
        return "все объекты контрагента"

    def expired(self, today: Optional[date] = None) -> bool:
        """Срок действия истёк. Правило при этом остаётся видимым.

        Спрятать его — значит показать рекрутёру «ставка не задана» там, где
        она просто не обновлена. Лучше показать сумму и сказать, что она
        просрочена.
        """
        if not self.valid_to:
            return False
        try:
            return _parse_date(self.valid_to) < (today or date.today())
        except ValueError:
            return False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "client": self.client,
            "vacancy": self.vacancy,
            "minShifts": self.min_shifts,
            "amount": self.amount,
            "note": self.note,
            "payout": self.payout,
            "validFrom": self.valid_from,
            "validTo": self.valid_to,
            "scope": self.scope_title,
            "expired": self.expired(),
            "author": self.author,
            "createdAt": self.created_at,
        }


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(str(value).strip()[:10]).date()


def _row_to_rule(row: sqlite3.Row) -> RateRule:
    return RateRule(
        id=row["id"],
        source=row["source"],
        client=row["client"] or "",
        vacancy=row["vacancy"] or "",
        min_shifts=int(row["min_shifts"] or 0),
        amount=int(row["amount"] or 0),
        note=row["note"] or "",
        payout=row["payout"] or "",
        valid_from=row["valid_from"] or "",
        valid_to=row["valid_to"] or "",
        created_at=row["created_at"] or "",
        author=row["author"] or "",
    )


# ------------------------------------------------------------------- чтение

def load_rules(conn: sqlite3.Connection, source: Optional[str] = None) -> List[RateRule]:
    if source:
        rows = conn.execute(
            "SELECT * FROM recruiter_rate_rules WHERE source = ? "
            "ORDER BY client, vacancy, min_shifts", (source,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM recruiter_rate_rules ORDER BY source, client, vacancy, min_shifts"
        ).fetchall()
    return [_row_to_rule(row) for row in rows]


def client_key(counterparty: str, object_name: str) -> str:
    """Что в этой системе называется объектом.

    У Аметиста и КНК объект лежит в object_name («ДНС Пушкино»), у Градуса и
    ЯППИ парсер кладёт заказчика в counterparty («BMJ», «Молком»). Для ставок
    это одно и то же понятие — то, что контрагент перечисляет в своей сетке.
    """
    return (object_name or "").strip() or (counterparty or "").strip()


def resolve(
        rules: Sequence[RateRule],
        source: str,
        client: str,
        vacancy: str,
        min_shifts: Optional[int],
        today: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Ставка для позиции: сумма, вся лестница и чем она обоснована."""
    applicable = [
        rule for rule in rules
        if rule.source == source
        and rule.client in ("", client)
        and rule.vacancy in ("", vacancy)
    ]
    if not applicable:
        return None

    best_rank = max(rule.scope_rank for rule in applicable)
    chosen = [rule for rule in applicable if rule.scope_rank == best_rank]

    ladder = sorted((r for r in chosen if r.min_shifts), key=lambda r: r.min_shifts)
    flat = next((r for r in chosen if not r.min_shifts), None)

    current, step_note = _pick_step(ladder, flat, min_shifts)
    if current is None:
        return None

    return {
        "amount": current.amount,
        "tier": current.min_shifts or None,
        "tiers": [{"minShifts": r.min_shifts, "amount": r.amount} for r in ladder],
        "note": current.note or (flat.note if flat else ""),
        "payout": current.payout or (flat.payout if flat else ""),
        "scope": current.scope_title,
        "byShifts": bool(ladder),
        "stepNote": step_note,
        "validTo": current.valid_to,
        "expired": current.expired(today),
    }


def _pick_step(
        ladder: Sequence[RateRule],
        flat: Optional[RateRule],
        min_shifts: Optional[int],
) -> tuple:
    """Ступень лестницы под вахту позиции.

    В заявке написано «вахта от 20 смен» — это и есть ориентир: рекрутёр
    увидит сумму за ту вахту, на которую он зовёт. Полная лестница едет рядом,
    потому что платят всё-таки по факту отработанного.
    """
    if not ladder:
        return flat, ""
    if min_shifts:
        fits = [rule for rule in ladder if rule.min_shifts <= min_shifts]
        if fits:
            return fits[-1], ""
        # Вахта короче самой низкой ступени: контрагент за неё не платит.
        return ladder[0], f"вахта от {min_shifts} смен — ниже нижней ступени"
    if flat is not None:
        return flat, ""
    return ladder[0], "срок вахты в заявке не указан"


# -------------------------------------------------------------------- запись

def save_rules(
        conn: sqlite3.Connection,
        rules: Iterable[RateRule],
        author: str = "",
) -> int:
    """Пишет пачку правил и отмечает каждое в истории."""
    now = datetime.now().isoformat(timespec="seconds")
    saved = 0
    for rule in rules:
        conn.execute(
            """
            INSERT INTO recruiter_rate_rules
                (source, client, vacancy, min_shifts, amount, note, payout,
                 valid_from, valid_to, created_at, author)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, client, vacancy, min_shifts) DO UPDATE SET
                amount = excluded.amount, note = excluded.note, payout = excluded.payout,
                valid_from = excluded.valid_from, valid_to = excluded.valid_to,
                created_at = excluded.created_at, author = excluded.author
            """,
            (rule.source, rule.client, rule.vacancy, rule.min_shifts, rule.amount,
             rule.note, rule.payout, rule.valid_from, rule.valid_to, now, author),
        )
        _log(conn, rule, "set", now, author)
        saved += 1
    return saved


def delete_rule(conn: sqlite3.Connection, rule_id: int, author: str = "") -> bool:
    row = conn.execute(
        "SELECT * FROM recruiter_rate_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if row is None:
        return False
    rule = _row_to_rule(row)
    conn.execute("DELETE FROM recruiter_rate_rules WHERE id = ?", (rule_id,))
    _log(conn, rule, "delete", datetime.now().isoformat(timespec="seconds"), author)
    return True


def clear_scope(
        conn: sqlite3.Connection,
        source: str,
        client: str = "",
        vacancy: str = "",
        author: str = "",
) -> int:
    """Убирает все правила одной области — например всю лестницу контрагента.

    Нужно при перевыставлении: новая сетка не всегда содержит те же ступени,
    и старая «30 смен» иначе осталась бы висеть рядом с новой.
    """
    rows = conn.execute(
        "SELECT * FROM recruiter_rate_rules WHERE source = ? AND client = ? AND vacancy = ?",
        (source, client, vacancy),
    ).fetchall()
    for row in rows:
        delete_rule(conn, row["id"], author)
    return len(rows)


def _log(conn: sqlite3.Connection, rule: RateRule, action: str, now: str, author: str) -> None:
    conn.execute(
        """
        INSERT INTO recruiter_rate_history
            (source, client, vacancy, min_shifts, amount, note, payout,
             valid_from, valid_to, action, changed_at, author)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (rule.source, rule.client, rule.vacancy, rule.min_shifts, rule.amount,
         rule.note, rule.payout, rule.valid_from, rule.valid_to, action, now, author),
    )


def history(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM recruiter_rate_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "source": row["source"],
            "scope": RateRule(
                source=row["source"], client=row["client"] or "",
                vacancy=row["vacancy"] or "",
            ).scope_title,
            "minShifts": row["min_shifts"],
            "amount": row["amount"],
            "action": row["action"],
            "at": (row["changed_at"] or "")[:16].replace("T", " "),
            "author": row["author"],
        }
        for row in rows
    ]
