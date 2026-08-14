"""Разовая сверка на одной строке: строка таблицы против «строка + пост».

Пост подставляется из файла (data/telegram_posts.json или аргументом),
чтобы прогон не требовал доступа к Telegram.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from loguru import logger

load_dotenv()
logger.remove()
logger.add(sys.stderr, level="ERROR")

from ametist_sheet_extractor import AmetistSheetExtractor
from sheets_adapter import GoogleSheetsService
from vacancy_parser import VacancyParserService

SHEET_ID = "1mnupysp96GdAZ5UEHFafOdyiqyqMgZt3-z0WBFM2Akg"
CACHE = "data/telegram_posts.json"

FIELDS = [
    "vacancy_name", "vacancy_category", "city", "region", "object_name", "object_address",
    "work_format", "shift_type", "schedule", "min_shifts", "shift_hours", "shift_rate",
    "duties", "requirements", "requires_tsd", "gender", "age_from", "age_to",
    "citizenship_requirements", "need_men", "need_women", "need_couples", "need_total",
    "housing_available", "housing_free", "housing_deduction", "housing_conditions",
    "meals_available", "meals_free", "meals_deduction", "meals_times_per_day",
    "medical_book_required", "medical_book_payer", "can_start_without_medical_book",
    "uniform_available", "uniform_free", "transport_paid", "transport_terms",
    "advantages", "risks", "sb_policy",
]


def values(vacancies, field):
    out = []
    for vac in vacancies or []:
        value = vac.get(field)
        if value not in (None, ""):
            out.append(str(value))
    return out


async def main() -> int:
    marker = sys.argv[1] if len(sys.argv) > 1 else "/651"
    posts = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

    sheets = GoogleSheetsService("credentials.json")
    parser = VacancyParserService()
    extractor = AmetistSheetExtractor(sheets, parser)
    rows = await asyncio.to_thread(extractor._read_sheet, SHEET_ID, "Потребность ")

    target = None
    for region, row in rows:
        link = extractor._row_link(row)
        if link.endswith(marker) and (row.get("Потребность") or "").strip().lower() not in extractor.EMPTY_NEEDS:
            target = (region, row, link)
            break
    if not target:
        print(f"Строка со ссылкой {marker} не найдена")
        return 1

    region, row, link = target
    post = posts.get(link, "")
    if not post:
        print(f"Нет текста поста для {link} — положите его в {CACHE}")
        return 1

    plain = extractor._row_to_text(row, region)
    rich = extractor._row_to_text(row, region, post)
    print(f"Строка: {row.get('Объект')} · {row.get('Должность')}")
    print(f"Текст для LLM: {len(plain)} символов без поста → {len(rich)} с постом\n")

    plain_result, rich_result = await asyncio.gather(
        parser.aparse(plain, source="Аметист"),
        parser.aparse(rich, source="Аметист"),
    )

    print(f"Позиций извлечено: {len(plain_result or [])} → {len(rich_result or [])}\n")
    print(f"{'поле':<32}{'без поста':<26}с постом")
    print("-" * 92)
    gained = 0
    for field in FIELDS:
        a, b = values(plain_result, field), values(rich_result, field)
        if a == b:
            continue
        if not a and b:
            gained += 1
        print(f"{field:<32}{('; '.join(a) or '—')[:24]:<26}{('; '.join(b) or '—')[:40]}")
    print("-" * 92)
    print(f"Заполнено полей: {sum(1 for f in FIELDS if values(plain_result, f))} → "
          f"{sum(1 for f in FIELDS if values(rich_result, f))} (новых: {gained})")
    print(parser.usage.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
