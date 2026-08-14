"""Ставки рекрутёра из командной строки — то же, что вкладка «Ставки» в админке.

Основной способ — форма в `/navigator` → «Нормализация» → «Ставки»: раз в
неделю руководитель переносит туда сетку, присланную контрагентом. Здесь то же
самое без интерфейса: посмотреть, что выставлено, и залить пачку из файла,
когда сетка большая (у КНК это два десятка объектов).

    python -m scripts.recruiter_rates list
    python -m scripts.recruiter_rates list --source vahtapro

    # на всего контрагента
    python -m scripts.recruiter_rates set vahtapro --amount 20000 --to 2026-07-31

    # лестница по сменам
    python -m scripts.recruiter_rates set vahtapro --tier 15:15000 --tier 20:20000 \\
        --tier 30:23000 --payout "адаптация 5 смен" --note "повторная вахта — 50%"

    # исключение по объекту (и, если нужно, по должности)
    python -m scripts.recruiter_rates set ametist --client "ДНС Пушкино" --amount 20000
    python -m scripts.recruiter_rates set yappi --client "ТЭЦ Чульман" \\
        --vacancy "Электромонтёр 5 разряда" --amount 40000

    # пачкой из CSV: source,client,vacancy,min_shifts,amount,note,payout,valid_from,valid_to
    python -m scripts.recruiter_rates import rates.csv

    python -m scripts.recruiter_rates delete 12
"""

import argparse
import csv
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry import db, rates
from registry.labels import SOURCE_TITLES


def cmd_list(args) -> int:
    with db.connect() as conn:
        rules = rates.load_rules(conn, args.source)
        active = {
            row["source"]: row["n"]
            for row in conn.execute(
                "SELECT source, COUNT(*) AS n FROM positions WHERE is_active = 1 GROUP BY source"
            )
        }
    if not rules:
        print("Правил нет — в карточках честно написано «ставка не задана».")
        return 0

    current = None
    for rule in rules:
        if rule.source != current:
            current = rule.source
            title = SOURCE_TITLES.get(current, current)
            print(f"\n{title} ({current}) — активных позиций: {active.get(current, 0)}")
        scope = rule.scope_title
        if rule.min_shifts:
            scope += f" · от {rule.min_shifts} смен"
        period = ""
        if rule.valid_to:
            period = f"по {rule.valid_to}" + (" — ИСТЁК" if rule.expired() else "")
        tail = " · ".join(x for x in (rule.payout, rule.note) if x)
        print(f"  #{rule.id:<4} {rule.amount:>7} ₽  {scope:<44} {period} {tail}")
    return 0


def cmd_set(args) -> int:
    tiers = []
    for item in args.tier or []:
        shifts, _, amount = item.partition(":")
        if not shifts.strip().isdigit() or not amount.strip().isdigit():
            print(f"Ступень «{item}» не разобрана: нужно СМЕНЫ:СУММА", file=sys.stderr)
            return 2
        tiers.append((int(shifts), int(amount)))
    if not tiers and not args.amount:
        print("Укажите --amount или хотя бы одну --tier", file=sys.stderr)
        return 2

    common = {
        "note": args.note or "",
        "payout": args.payout or "",
        "valid_from": getattr(args, "from") or "",
        "valid_to": args.to or "",
    }
    client, vacancy = args.client or "", args.vacancy or ""
    if tiers:
        rules = [
            rates.RateRule(source=args.source, client=client, vacancy=vacancy,
                           min_shifts=shifts, amount=amount, **common)
            for shifts, amount in tiers
        ]
    else:
        rules = [rates.RateRule(source=args.source, client=client, vacancy=vacancy,
                                amount=args.amount, **common)]

    with db.connect() as conn:
        # Область переписывается целиком — как и в админке: в новой сетке
        # ступеней может оказаться меньше, чем было в прошлой.
        rates.clear_scope(conn, args.source, client, vacancy, author="cli")
        saved = rates.save_rules(conn, rules, author="cli")
    print(f"Сохранено правил: {saved}")
    return 0


def cmd_import(args) -> int:
    with open(args.path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print("В файле нет строк", file=sys.stderr)
        return 2
    rules = [
        rates.RateRule(
            source=(row.get("source") or "").strip(),
            client=(row.get("client") or "").strip(),
            vacancy=(row.get("vacancy") or "").strip(),
            min_shifts=int(row.get("min_shifts") or 0),
            amount=int(row.get("amount") or 0),
            note=(row.get("note") or "").strip(),
            payout=(row.get("payout") or "").strip(),
            valid_from=(row.get("valid_from") or "").strip(),
            valid_to=(row.get("valid_to") or "").strip(),
        )
        for row in rows
    ]
    bad = [rule for rule in rules if not rule.source or rule.amount <= 0]
    if bad:
        print(f"Строк без источника или суммы: {len(bad)} — исправьте файл", file=sys.stderr)
        return 2
    with db.connect() as conn:
        saved = rates.save_rules(conn, rules, author="cli:import")
    print(f"Сохранено правил: {saved}")
    return 0


def cmd_delete(args) -> int:
    with db.connect() as conn:
        ok = rates.delete_rule(conn, args.id, author="cli")
    print("Убрано" if ok else f"Правила #{args.id} нет")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Ставки рекрутёра для «Навигатора»")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="показать выставленные ставки")
    p_list.add_argument("--source", help="алиас контрагента")
    p_list.set_defaults(func=cmd_list)

    p_set = sub.add_parser("set", help="выставить ставку или лестницу")
    p_set.add_argument("source", help="алиас контрагента: vahtapro, yappi, kpk, ametist…")
    p_set.add_argument("--amount", type=int, help="сумма за кандидата, ₽")
    p_set.add_argument("--tier", action="append", metavar="СМЕНЫ:СУММА",
                       help="ступень лестницы, можно несколько раз")
    p_set.add_argument("--client", help="объект/заказчик; пусто — весь контрагент")
    p_set.add_argument("--vacancy", help="должность; пусто — все должности объекта")
    p_set.add_argument("--note", help="надбавки и оговорки текстом")
    p_set.add_argument("--payout", help="когда выплачивается")
    p_set.add_argument("--from", dest="from", help="действует с (ГГГГ-ММ-ДД)")
    p_set.add_argument("--to", help="действует по (ГГГГ-ММ-ДД)")
    p_set.set_defaults(func=cmd_set)

    p_imp = sub.add_parser("import", help="залить пачку из CSV")
    p_imp.add_argument("path")
    p_imp.set_defaults(func=cmd_import)

    p_del = sub.add_parser("delete", help="убрать правило по номеру из list")
    p_del.add_argument("id", type=int)
    p_del.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
