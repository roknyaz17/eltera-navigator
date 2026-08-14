"""Сравнение разбора Градуса: только пост из канала против «пост + справка с Яндекс.Диска».

Одни и те же заявки прогоняются через LLM дважды и сравниваются по
заполненности полей реестра. Это ответ на вопрос «а помогло ли вообще»,
измеренный, а не на глаз.

    python -m scripts.vahtapro_ab_test           # 20 заявок
    python -m scripts.vahtapro_ab_test 40        # 40 заявок
    python -m scripts.vahtapro_ab_test --all     # все, у которых нашёлся проект

Тексты берутся из реестра (таблица requests, source=vahtapro) — это реальные
посты канала, а не выдуманные примеры. Справки — из индекса Яндекс.Диска, его
надо один раз собрать: python -m scripts.index_vahtapro_disk

Отдельно считается то, что важнее прироста: не поехали ли поля, за которые
отвечает пост (ставка, потребность, возраст) и не наплодила ли справка лишних
позиций. Справка описывает проект целиком, в ней перечислены все должности
объекта — если модель начнёт заводить их без спроса, реестр наполнится
потребностью, которой никто не заявлял.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional

from dotenv import load_dotenv
from loguru import logger

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from project_kb import ProjectKB
from telegram_channel_processor import TelegramChannelProcessor
from vacancy_parser import VacancyParserService

# Поля реестра, по которым считаем пользу. Служебные и административные не в
# счёт — их заполняем не мы.
FIELDS = [
    "vacancy_name", "vacancy_category", "city", "region", "object_name", "object_address",
    "work_format", "shift_type", "schedule", "min_shifts", "shift_hours", "shift_rate",
    "duties", "requirements", "requires_tsd",
    "gender", "age_from", "age_to", "citizenship_requirements",
    "need_men", "need_women", "need_couples", "need_total",
    "housing_available", "housing_free", "housing_deduction", "housing_conditions",
    "meals_available", "meals_free", "meals_deduction", "meals_times_per_day",
    "medical_book_required", "medical_book_payer", "can_start_without_medical_book",
    "uniform_available", "uniform_free", "transport_paid", "transport_terms",
    "advantages", "risks", "sb_policy",
]

# За эти поля отвечает пост и только пост. Их расхождение между прогонами —
# не прирост, а тревога: справка не должна их трогать.
POST_OWNED = [
    "shift_rate", "need_men", "need_women", "need_couples", "need_total",
    "age_from", "age_to", "min_shifts",
]


def filled(vacancies: List[Dict], field: str) -> bool:
    """Поле заполнено, если оно есть хотя бы у одной позиции заявки."""
    for vac in vacancies or []:
        value = vac.get(field)
        if value is None or value == "":
            continue
        return True
    return False


def by_position(vacancies: List[Dict]) -> Dict[str, Dict]:
    """Позиции по ключу «должность + смена» — чтобы сравнивать сопоставимое.

    Без этого «расхождение» ловится там, где его нет: у заявки с двумя
    должностями достаточно поменяться порядку, и сравнение первых значений
    покажет, что ставка «поехала».
    """
    out: Dict[str, Dict] = {}
    for vac in vacancies or []:
        key = "|".join([
            str(vac.get("vacancy_name") or "").strip().lower(),
            str(vac.get("shift_type") or ""),
        ])
        out.setdefault(key, vac)
    return out


def drifted_fields(plain: List[Dict], rich: List[Dict]) -> List[tuple]:
    """Поля поста, которые изменились у ОДНОЙ И ТОЙ ЖЕ позиции."""
    left, right = by_position(plain), by_position(rich)
    out = []
    for key in left.keys() & right.keys():
        for field in POST_OWNED:
            before, after = left[key].get(field), right[key].get(field)
            if before in (None, "") or after in (None, ""):
                continue
            if str(before).strip() != str(after).strip():
                out.append((key.split("|")[0], field, str(before), str(after)))
    return out


def load_cases(db_path: str, limit: Optional[int]) -> List[Dict]:
    """Заявки Градуса из реестра со справками по проектам."""
    kb = ProjectKB.load(db_path=db_path)
    if kb.is_empty:
        print("Индекс диска пуст: сначала python -m scripts.index_vahtapro_disk")
        return []

    proc = TelegramChannelProcessor(None, None, source_name="Градус")
    conn = sqlite3.connect(db_path or "data/registry.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT request_id, raw_text FROM requests WHERE source = 'vahtapro' "
        "ORDER BY request_id"
    ).fetchall()
    conn.close()

    cases: List[Dict] = []
    seen_projects: set = set()
    for row in rows:
        for chunk in proc._iter_segments(row["raw_text"]):
            context, meta = kb.context_for(chunk)
            if not context:
                continue
            cases.append({
                "label": f"{row['request_id']} · {meta['project']}",
                "project": meta["project"],
                "text": chunk,
                "context": context,
                "context_len": len(context),
            })
            seen_projects.add(meta["project"])
        if limit and len(cases) >= limit:
            break
    if limit:
        cases = cases[:limit]
    print(f"Заявок в тесте: {len(cases)}, разных проектов: {len(seen_projects)}; "
          f"средняя длина справки: "
          f"{sum(c['context_len'] for c in cases) // max(len(cases), 1)} символов\n")
    return cases


async def main() -> int:
    parser_args = argparse.ArgumentParser(description=__doc__)
    parser_args.add_argument("limit", nargs="?", type=int, default=20)
    parser_args.add_argument("--all", action="store_true", help="взять все заявки со справкой")
    parser_args.add_argument("--db", default=os.getenv("REGISTRY_DB_PATH", "data/registry.db"))
    parser_args.add_argument("--concurrency", type=int, default=4)
    parser_args.add_argument("--save", metavar="ФАЙЛ",
                             help="сохранить оба разбора в JSON для разбора глазами")
    args = parser_args.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    cases = load_cases(args.db, None if args.all else args.limit)
    if not cases:
        return 1

    parser = VacancyParserService()
    sem = asyncio.Semaphore(args.concurrency)

    async def parse(text: str, context: Optional[str]) -> Optional[List[Dict]]:
        """Разбор одной заявки. None — модель не ответила (сеть, битый JSON).

        Отличать это от «вакансий в тексте нет» обязательно: сбой, посчитанный
        за пустой результат, выглядит в отчёте как «справка добавила позиции»,
        хотя добавила их не она, а вторая попытка.
        """
        async with sem:
            for attempt in range(2):
                try:
                    parsed, _, _ = await parser.aparse_raw_ex(text, context)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"разбор упал (попытка {attempt + 1}): {exc}")
                    await asyncio.sleep(2)
                    continue
                if parsed is not None:
                    return parser._enrich(parsed, "Градус", "")
            return None

    started = time.time()
    plain_results = await asyncio.gather(*[parse(c["text"], None) for c in cases])
    rich_results = await asyncio.gather(*[parse(c["text"], c["context"]) for c in cases])
    elapsed = time.time() - started

    failed = [
        case for case, plain, rich in zip(cases, plain_results, rich_results)
        if plain is None or rich is None
    ]
    if failed:
        # Из статистики выбывают обе стороны пары: сравнивать разбор с
        # несостоявшимся разбором нельзя.
        keep = [
            (case, plain, rich)
            for case, plain, rich in zip(cases, plain_results, rich_results)
            if plain is not None and rich is not None
        ]
        cases = [item[0] for item in keep]
        plain_results = [item[1] for item in keep]
        rich_results = [item[2] for item in keep]
        print(f"Исключено из сравнения (модель не ответила): {len(failed)} заявок\n")
    if not cases:
        print("Не осталось ни одной пары для сравнения")
        return 1

    stats = {field: [0, 0] for field in FIELDS}
    gained_rows, lost_rows, drifted = [], [], []
    for case, plain, rich in zip(cases, plain_results, rich_results):
        gained, lost = [], []
        for field in FIELDS:
            in_plain, in_rich = filled(plain, field), filled(rich, field)
            stats[field][0] += int(in_plain)
            stats[field][1] += int(in_rich)
            if in_rich and not in_plain:
                gained.append(field)
            if in_plain and not in_rich:
                lost.append(field)
        for vacancy, field, before, after in drifted_fields(plain, rich):
            drifted.append((f"{case['label']} · {vacancy}", field, before, after))
        case["plain_n"], case["rich_n"] = len(plain), len(rich)
        case["gained"], case["lost"] = gained, lost
        if gained:
            gained_rows.append(case)
        if lost:
            lost_rows.append(case)

    total_plain = sum(v[0] for v in stats.values())
    total_rich = sum(v[1] for v in stats.values())
    possible = len(FIELDS) * len(cases)

    print("Поле                            без диска  с диском  разница")
    print("-" * 62)
    for field in FIELDS:
        plain_count, rich_count = stats[field]
        if plain_count == rich_count:
            continue
        print(f"{field:<32}{plain_count:>7}{rich_count:>10}{rich_count - plain_count:>+9}")
    print("-" * 62)
    print(f"{'ИТОГО заполнено полей':<32}{total_plain:>7}{total_rich:>10}"
          f"{total_rich - total_plain:>+9}")
    print(f"Заполненность: {total_plain / possible:.0%} → {total_rich / possible:.0%} "
          f"(из {possible} возможных)")
    print(f"Позиций извлечено: {sum(c['plain_n'] for c in cases)} → "
          f"{sum(c['rich_n'] for c in cases)}  "
          f"(рост здесь — плохо: справка не должна создавать потребность)")
    print(f"Время: {elapsed:.0f} с на {len(cases) * 2} вызовов LLM · {parser.usage.summary()}")

    print("\nЧто добавилось по заявкам:")
    for case in gained_rows[:15]:
        print(f"  + {case['label'][:44]:<46}{', '.join(case['gained'])}")
    if lost_rows:
        print("\nЧто пропало (проверить):")
        for case in lost_rows[:15]:
            print(f"  - {case['label'][:44]:<46}{', '.join(case['lost'])}")
    changed_counts = [c for c in cases if c["plain_n"] != c["rich_n"]]
    if changed_counts:
        print("\nГде изменилось число позиций (0 → N — это заявка, которую разбор "
              "раньше не осилил вовсе):")
        for case in changed_counts[:15]:
            print(f"  ~ {case['label'][:44]:<46}{case['plain_n']} → {case['rich_n']}")

    print(f"\nПоля, за которые отвечает пост, разошлись у одной и той же позиции: "
          f"{len(drifted)}")
    for label, field, before, after in drifted[:15]:
        print(f"  ! {label[:44]:<46}{field:<14}{before[:30]!r} → {after[:30]!r}")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as handle:
            json.dump(
                [
                    {"label": c["label"], "text": c["text"], "context": c["context"],
                     "plain": p, "rich": r}
                    for c, p, r in zip(cases, plain_results, rich_results)
                ],
                handle, ensure_ascii=False, indent=1,
            )
        print(f"\nРазборы сохранены: {args.save} — можно пересчитать метрики без LLM")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
