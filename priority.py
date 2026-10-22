"""Приоритетные объекты: что показать агенту первым на этой неделе.

Заказчик каждую неделю называет «горящие» объекты. До этого приоритет жил в
переписке, а агент предлагал то, что выше по нашей выплате, — и звал людей на
объект, который уже укомплектован.

Срок обязателен и задаётся явно. Приоритет без даты окончания — это приоритет
навсегда: он тихо продолжает действовать, когда неделя прошла и горит уже
другое. Так протухли двадцать три правила выплат, и заметили мы это случайно.

    python priority.py                                    что действует сейчас
    python priority.py --set "Марс Лужники=1,Данон=1" --с 2026-10-12 --по 2026-10-18
    python priority.py --снять "Фэшн"
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import sys
from pathlib import Path

sys.path.insert(0, "/app")

import registry.db as registry_db


def show(conn, day: str) -> None:
    rows = conn.execute(
        """SELECT pattern, tier, valid_from, valid_to, note FROM priority_objects
           ORDER BY valid_to DESC, tier, pattern""").fetchall()
    if not rows:
        print("Приоритетов не заведено.")
        return

    print(f"{'объект':<22}{'уровень':>8}  {'срок':<25}{'статус':<12}активных позиций")
    print("-" * 86)
    for r in rows:
        live = r["valid_from"] <= day <= r["valid_to"]
        state = "действует" if live else ("не начался" if r["valid_from"] > day
                                          else "истёк")
        hits = conn.execute(
            """SELECT count(*) FROM positions WHERE is_active = 1
               AND (lower(object_name) LIKE ? OR lower(counterparty) LIKE ?
                    OR lower(vacancy_name) LIKE ?)""",
            (f"%{r['pattern'].lower()}%",) * 3).fetchone()[0]
        mark = "" if hits else "   ← ничего не найдено"
        print(f"{r['pattern'][:20]:<22}{r['tier']:>8}  "
              f"{r['valid_from']}–{r['valid_to']}  {state:<12}{hits}{mark}")


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Приоритетные объекты обзвона")
    parser.add_argument("--set", dest="pairs", default="",
                        help="«Объект=уровень» через запятую; уровень 1 — самый высокий")
    parser.add_argument("--с", dest="since", default="")
    parser.add_argument("--по", dest="until", default="")
    parser.add_argument("--снять", dest="drop", default="")
    parser.add_argument("--автор", dest="author", default="")
    args = parser.parse_args()

    today = dt.date.today().isoformat()
    with registry_db.connect() as conn:
        if args.drop:
            n = conn.execute("DELETE FROM priority_objects WHERE pattern = ?",
                             (args.drop,)).rowcount
            conn.commit()
            print(f"Снято записей: {n}")

        if args.pairs:
            if not (args.since and args.until):
                raise SystemExit("Нужны --с и --по: приоритет без срока не ставим.")
            if args.until < args.since:
                raise SystemExit("Дата «по» раньше даты «с».")
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            for chunk in args.pairs.split(","):
                name, _, tier = chunk.partition("=")
                name = name.strip()
                if not name:
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO priority_objects
                       (pattern, tier, valid_from, valid_to, note, created_at, author)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (name, int(tier or 1), args.since, args.until, "", stamp,
                     args.author or "панель"))
                print(f"  {name} → уровень {tier or 1}, {args.since}–{args.until}")
            conn.commit()
            print()

        show(conn, today)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
