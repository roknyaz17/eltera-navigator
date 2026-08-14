"""Сравнение разбора Аметиста: только строка таблицы против «строка + пост из Telegram».

Одни и те же строки листа прогоняются через LLM дважды и сравниваются по
заполненности полей реестра. Это ответ на вопрос «а помогло ли вообще»,
измеренный, а не на глаз.

    python -m scripts.ametist_ab_test          # 12 строк
    python -m scripts.ametist_ab_test 30       # все строки со ссылкой
"""

import asyncio
import json
import os
import sys
import time
from typing import Dict, List

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

from ametist_sheet_extractor import AmetistSheetExtractor
from sheets_adapter import GoogleSheetsService
from telegram_post_fetcher import TelegramPostFetcher
from vacancy_parser import VacancyParserService

AMETIST_SPREADSHEET_ID = "1mnupysp96GdAZ5UEHFafOdyiqyqMgZt3-z0WBFM2Akg"
SHEET_NAME = "Потребность "
# Посты кэшируются на диск: MTProto доступен не всегда, а сравнение разбора
# должно быть воспроизводимым и не зависеть от сети.
CACHE_PATH = "data/telegram_posts.json"   # тот же кэш, что у пайплайна

# Поля реестра, по которым считаем пользу. Служебные (source, original_message)
# и административные не в счёт — их заполняем не мы.
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


def filled(vacancies: List[Dict], field: str) -> bool:
    """Поле считается заполненным, если хотя бы у одной позиции строки оно есть."""
    for vac in vacancies or []:
        value = vac.get(field)
        if value is None or value == "":
            continue
        return True
    return False


async def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    sheets = GoogleSheetsService("credentials.json")
    parser = VacancyParserService()
    extractor = AmetistSheetExtractor(sheets, parser, TelegramPostFetcher())

    rows = await asyncio.to_thread(extractor._read_sheet, AMETIST_SPREADSHEET_ID, SHEET_NAME)

    cached: Dict[str, str] = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as handle:
            cached = json.load(handle)
    posts = await extractor._fetch_posts(rows)
    if posts:
        merged = {**cached, **posts}
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=1)
        posts = merged
    else:
        posts = cached
        if cached:
            print(f"Telegram недоступен — беру {len(cached)} постов из кэша {CACHE_PATH}")

    cases = []
    for region, row in rows:
        need = (row.get("Потребность") or "").strip().lower()
        if need in extractor.EMPTY_NEEDS:
            continue
        if not (row.get("Объект") or "").strip():
            continue
        post = posts.get(extractor._row_link(row), "")
        if not post:
            continue
        cases.append({
            "label": f"{(row.get('Объект') or '').strip()} · {(row.get('Должность') or '').strip()}",
            "plain": extractor._row_to_text(row, region),
            "rich": extractor._row_to_text(row, region, post),
            "post_len": len(post),
        })
        if len(cases) >= limit:
            break

    if not cases:
        print("Нет строк с доступным описанием из Telegram")
        return 1

    print(f"Строк в тесте: {len(cases)}; средняя длина поста: "
          f"{sum(c['post_len'] for c in cases) // len(cases)} символов\n")

    sem = asyncio.Semaphore(4)

    async def parse(text: str):
        async with sem:
            try:
                return await parser.aparse(text, source="Аметист", source_url="")
            except Exception as exc:
                logger.warning(f"разбор упал: {exc}")
                return []

    started = time.time()
    plain_results = await asyncio.gather(*[parse(c["plain"]) for c in cases])
    rich_results = await asyncio.gather(*[parse(c["rich"]) for c in cases])
    elapsed = time.time() - started

    stats = {field: [0, 0] for field in FIELDS}
    gained_rows, lost_rows = [], []
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
        case["plain_n"], case["rich_n"] = len(plain or []), len(rich or [])
        case["gained"], case["lost"] = gained, lost
        if gained:
            gained_rows.append(case)
        if lost:
            lost_rows.append(case)

    total_plain = sum(v[0] for v in stats.values())
    total_rich = sum(v[1] for v in stats.values())
    possible = len(FIELDS) * len(cases)

    print("Поле                            без ТГ   с ТГ   разница")
    print("-" * 58)
    for field in FIELDS:
        plain_count, rich_count = stats[field]
        if plain_count == rich_count:
            continue
        diff = rich_count - plain_count
        print(f"{field:<32}{plain_count:>5}{rich_count:>7}{diff:>+9}")
    print("-" * 58)
    print(f"{'ИТОГО заполнено полей':<32}{total_plain:>5}{total_rich:>7}{total_rich - total_plain:>+9}")
    print(f"Заполненность: {total_plain / possible:.0%} → {total_rich / possible:.0%} "
          f"(из {possible} возможных)")
    print(f"Позиций извлечено: {sum(c['plain_n'] for c in cases)} → {sum(c['rich_n'] for c in cases)}")
    print(f"Время: {elapsed:.0f} с на {len(cases) * 2} вызовов LLM · {parser.usage.summary()}")

    print("\nЧто добавилось по строкам:")
    for case in gained_rows[:12]:
        print(f"  + {case['label'][:46]:<48}{', '.join(case['gained'])}")
    if lost_rows:
        print("\nЧто пропало (проверить):")
        for case in lost_rows[:12]:
            print(f"  - {case['label'][:46]:<48}{', '.join(case['lost'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
