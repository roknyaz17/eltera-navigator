"""Сетка ставок рекрутёра по МЕЛИОРу — из картинки контрагента в реестр.

Контрагент присылает сетку картинкой: объект и две суммы, за 33 смены и за 21.
Здесь она лежит таблицей, чтобы в следующий раз правилась одной цифрой, а не
двадцатью кликами в админке.

Объект называется так, как он назван листом в таблице «Объекты Мелиор», —
именно это имя приезжает в позицию из базы знаний (см. melior_kb.object_path)
и по нему rates подбирает ставку. Названия из картинки к листам приводятся в
RATES ниже; если лист переименуют, прогон об этом скажет.

Запуск:
    python -m scripts.melior_rates --check     # только сверка с реестром
    python -m scripts.melior_rates             # записать правила
"""

import argparse
import os
import sys
from typing import Dict, List, Tuple

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from registry import db, rates  # noqa: E402

SOURCE = "melior"
VALID_FROM = "2026-08-14"          # дата в шапке сетки
PAYOUT = "за адаптированного кандидата"
NOTE = "местный персонал — 10 000 ₽, адаптация 7 смен, строго по согласованию"

# Ступени сетки: за 21 смену и за 33. Позиция с вахтой 35 смен попадёт на
# верхнюю ступень (rates берёт последнюю подходящую), с 21 — на нижнюю.
TIERS = (21, 33)

# (имя листа в таблице объектов, 33 смены, 21 смена)
RATES: List[Tuple[str, int, int]] = [
    ("Склад медикаментов", 25000, 16000),
    ("Золотое яблоко", 25000, 16000),
    ("Форум авто", 25000, 16000),
    ("Летуаль", 25000, 16000),

    ("Производство кваса", 22000, 15000),
    ("Галамарт", 22000, 15000),
    ("Мотекс", 22000, 15000),

    ("РЦ Перекресток", 20000, 13000),
    ("Восток-Запад", 20000, 13000),
    ("Осетинские пироги", 20000, 13000),
    ("Аэропорт Шереметьево", 20000, 13000),
    ("Мираторг", 20000, 13000),
    ("Яндекс Тарный", 20000, 13000),
    ("Яндекс Кувекино", 20000, 13000),
    ("Фабрика мороженого", 20000, 13000),
    ("ФМ Логистик Чехов", 20000, 13000),
    # Листа в таблице объектов пока нет — правило ждёт объект.
    ("ФМ Логистик Долгопрудный", 20000, 13000),
    ("Карат", 20000, 13000),
    ("Холодильник.ру", 20000, 13000),
    ("Кералит", 20000, 13000),
    ("Производство холодильного и стеллажного оборудован", 20000, 13000),

    ("Сантенс", 16000, 10000),
    ("Производство хлеба", 16000, 10000),
    ("Тисс", 16000, 10000),
    # «Яндексы (регионы)» из сетки — это три листа.
    ("Яндекс Самара", 16000, 10000),
    ("Яндекс Екатеринбург", 16000, 10000),
    ("Яндекс Ростов-на-Дону", 16000, 10000),
]


def build_rules() -> List[rates.RateRule]:
    out: List[rates.RateRule] = []
    for client, high, low in RATES:
        for shifts, amount in zip(TIERS, (low, high)):
            out.append(rates.RateRule(
                source=SOURCE, client=client, vacancy="", min_shifts=shifts,
                amount=amount, note=NOTE, payout=PAYOUT, valid_from=VALID_FROM,
            ))
    return out


def report(conn) -> None:
    """Сверка сетки с тем, что реально есть в реестре и в базе знаний."""
    sheets = {
        row["path"].split(":", 1)[1]
        for row in conn.execute(
            "SELECT path FROM disk_projects WHERE source = ?", (SOURCE,)
        )
    }
    positions: Dict[str, int] = {}
    for row in conn.execute(
        """SELECT k.path, COUNT(*) AS c FROM positions p
           JOIN position_kb k ON k.position_id = p.position_id
           WHERE p.source = ? AND p.is_active = 1 GROUP BY k.path""", (SOURCE,),
    ):
        positions[row["path"].split(":", 1)[1]] = row["c"]

    named = {client for client, _, _ in RATES}
    print(f"{'объект из сетки':<52} {'лист':>6} {'позиций':>8}")
    print("-" * 70)
    for client, _, _ in RATES:
        print(f"{client[:52]:<52} {'есть' if client in sheets else 'НЕТ':>6} "
              f"{positions.get(client, 0):>8}")

    orphan = sorted(sheets - named)
    if orphan:
        print("\nобъекты в таблице, которых нет в сетке ставок "
              "(у их позиций ставка останется незаданной):")
        for name in orphan:
            print(f"  {name} — позиций {positions.get(name, 0)}")

    no_card = conn.execute(
        """SELECT COUNT(*) FROM positions p
           LEFT JOIN position_kb k ON k.position_id = p.position_id
           WHERE p.source = ? AND p.is_active = 1 AND k.position_id IS NULL""",
        (SOURCE,),
    ).fetchone()[0]
    if no_card:
        print(f"\nпозиций без карточки объекта: {no_card} — ставку им подобрать не по чему")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="только сверка, без записи")
    ap.add_argument("--author", default="сетка контрагента от 14.08.2026")
    args = ap.parse_args()

    with db.connect() as conn:
        report(conn)
        if args.check:
            print("\n--check: правила не записаны")
            return 0
        # Старую сетку по каждому объекту убираем целиком: ступени могли
        # смениться, и «33 смены» из прошлой сетки иначе осталась бы висеть.
        for client, _, _ in RATES:
            rates.clear_scope(conn, SOURCE, client, "", author=args.author)
        saved = rates.save_rules(conn, build_rules(), author=args.author)
        total = conn.execute(
            "SELECT COUNT(*) FROM recruiter_rate_rules WHERE source = ?", (SOURCE,)
        ).fetchone()[0]
    print(f"\nзаписано правил: {saved}; всего у источника: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
