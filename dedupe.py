"""Разовая сверка воронки: находит дубли по телефону и схлопывает их.

Дубли копились год: один человек откликался на несколько вакансий, и каждый
отклик заводил отдельный лид. Менеджер видит три карточки одного Иванова и не
знает, в какой писать, а робот при звонке поднимает одну из трёх — остальные
продолжают лежать в очереди и однажды будут набраны заново.

Объединение необратимо, поэтому здесь те же три защиты, что и при звонке:
лид в сделке, лид со свежей правкой и чёрный список не трогаем вовсе. Группу
пропускаем целиком, если хоть один её лид под защитой, — разбирать такие
случаи должен человек.

    python dedupe.py               посмотреть, что нашлось
    python dedupe.py --apply       слить
    python dedupe.py --apply --limit 50
"""

from __future__ import annotations

import argparse
import collections
import io
import re
import sys
import time

sys.path.insert(0, "/app")

import bitrix

# Стадии, в которых человек нам уже отказал. Слияние сводит группу к стадии
# выжившего, и если выживет свежий отклик, отказ исчезнет — а вместе с ним
# и причина не звонить. Такие группы по умолчанию не трогаем.
REFUSAL = {"2", "UC_0DFVI6"}

# Пользователь, под которым работает наш вебхук. Его правки за «работу
# менеджера» не считаем: выравнивание полей перед слиянием само делает лид
# свежим, и следующий заход обошёл бы группу стороной ещё неделю.
OUR_USER = "13557"


def digits(raw) -> str:
    only = re.sub(r"\D", "", str(raw or ""))
    return only[-10:] if len(only) >= 10 else ""


def sweep() -> list[dict]:
    """Все лиды портала с телефоном, стадией и датой правки."""
    items, start = [], 0
    while True:
        answer = bitrix._call("crm.lead.list", {
            "select": ["ID", "PHONE", "STATUS_ID", "DATE_MODIFY",
                       "DATE_CREATE", "MODIFY_BY_ID"],
            "order": {"ID": "ASC"},
            "start": start,
        }, 60)
        # crm.lead.list со start отдаёт список, а постраничность прячет в
        # заголовке ответа; _call её не возвращает, поэтому листаем вручную.
        batch = answer or []
        items += batch
        if len(batch) < 50:
            return items
        start += 50
        if start % 2000 == 0:
            print(f"  прочитано {len(items)}...", end="\r", flush=True)


def groups(leads: list[dict]) -> dict[str, list[dict]]:
    by_phone: dict[str, list[dict]] = collections.defaultdict(list)
    for lead in leads:
        touched = str(lead.get("DATE_MODIFY") or "")[:10]
        if str(lead.get("MODIFY_BY_ID") or "") == OUR_USER:
            touched = str(lead.get("DATE_CREATE") or "")[:10]
        brief = {
            "id": int(lead["ID"]),
            "status": str(lead.get("STATUS_ID") or ""),
            "modified": touched,
            "title": "",
        }
        for phone in (lead.get("PHONE") or []):
            number = digits(phone.get("VALUE"))
            if number:
                by_phone[number].append(brief)
                break
    return {p: rows for p, rows in by_phone.items() if len(rows) > 1}


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Схлопывание дублей в воронке")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--с-отказами", dest="refusals", action="store_true",
                        help="сливать и те группы, где есть отказ, оставляя "
                             "выжившим именно отказ")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    leads = sweep()
    print(f"Лидов в воронке: {len(leads)}        ")

    dupes = groups(leads)
    extra = sum(len(rows) - 1 for rows in dupes.values())
    print(f"Номеров с дублями: {len(dupes)}, лишних карточек: {extra}\n")

    ready, guarded, blocked, refused = [], 0, 0, []
    for phone, rows in dupes.items():
        if any(r["status"] in bitrix.BLACKLIST for r in rows):
            blocked += 1
            continue
        if any(r["status"] in bitrix.KEEP_AS_IS or bitrix._is_fresh(r["modified"])
               for r in rows):
            guarded += 1
            continue
        refusal = [r for r in rows if r["status"] in REFUSAL]
        newest = max(rows, key=lambda r: r["modified"])
        if refusal and newest["status"] not in REFUSAL:
            refused.append((phone, rows, refusal[0]["id"]))
            continue
        ready.append((phone, rows, None))

    if args.refusals:
        ready += refused
        refused = []

    print(f"  можно слить:       {len(ready)}")
    print(f"  под защитой:       {guarded}  (в сделке или правлены за неделю)")
    print(f"  чёрный список:     {blocked}")
    print(f"  отложено с отказом:{len(refused)}  (слить: --с-отказами)")

    if not args.apply:
        print("\nПримеры:")
        for phone, rows, _ in ready[:8]:
            print(f"   +7{phone}: " + ", ".join(f"{r['id']}/{r['status']}" for r in rows))
        print(f"\nСлить: python dedupe.py --apply")
        return 0

    todo = ready[: args.limit] if args.limit else ready
    print(f"\nСливаю {len(todo)} групп...\n")
    done = failed = 0
    gone = 0
    for n, (phone, rows, keep) in enumerate(todo, 1):
        try:
            alive, note = bitrix.merge_twins(rows, 30, keep=keep)
        except Exception as exc:  # noqa: BLE001 — портал, сеть
            failed += 1
            print(f"  [!] +7{phone}: {exc}")
            continue
        if note.startswith("слито"):
            done += 1
            gone += len(rows) - 1
        else:
            failed += 1
            print(f"  [!] +7{phone}: {note}")
        if n % 25 == 0:
            print(f"  {n}/{len(todo)}: слито {done}, не вышло {failed}", flush=True)
        time.sleep(0.3)

    print(f"\nСлито групп: {done}, лишних карточек убрано: {gone}")
    print(f"Не вышло: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
