"""Холостой прогон канала МЕЛИОРа: что мы из него вытащим, ничего не записывая.

Повторяет путь реестра ровно так, как он пойдёт в проде — карточки объектов из
таблицы, сообщения канала, нарезка на блоки, справка на каждый блок, разбор
моделью, — но останавливается перед записью: ни одной строки в positions и
requests не появится. В базу пишутся только карточки объектов (disk_projects,
source='melior'): без них не проверить ни сопоставление, ни справку.

Запуск:
    python -m scripts.dry_run_melior                # 50 постов, с разбором
    python -m scripts.dry_run_melior --no-llm       # только сбор, без модели
    python -m scripts.dry_run_melior --posts 817    # один конкретный пост
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Dict, List, Optional

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from logging_config import setup_logging  # noqa: E402

setup_logging()

from loguru import logger  # noqa: E402

import melior_kb  # noqa: E402
from melior_message_processor import MeliorMessageProcessor  # noqa: E402
from telegram_userbot import TelegramUserbot  # noqa: E402
from vacancy_parser import VacancyParserService  # noqa: E402

CHAT_ID = int(os.getenv("TELEGRAM_MELIOR_CHAT_ID", "-1003439917213"))
LLM_CONCURRENCY = 5
DUMP_PATH = os.path.join("data", "melior_dry_run.json")


def _short(value, width: int = 28) -> str:
    text = "—" if value in (None, "") else str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _need(fields: Dict) -> str:
    parts = []
    for key, label in (("need_men", "М"), ("need_women", "Ж"), ("need_couples", "сем")):
        value = fields.get(key)
        if value:
            parts.append(f"{value}{label}")
    if not parts and fields.get("need_total"):
        parts.append(f"{fields['need_total']} чел")
    return ", ".join(parts) or "—"


async def main(limit: int, use_llm: bool, posts: Optional[List[int]]) -> int:
    print("=== 1. Карточки объектов из таблицы «Объекты Мелиор» ===")
    kb_stats = melior_kb.refresh()
    print(f"объектов: {kb_stats['objects']}, обновлено: {kb_stats['updated']}, "
          f"удалено: {kb_stats['removed']}")

    print("\n=== 2. Канал ===")
    async with TelegramUserbot() as bot:
        messages = await bot.get_messages(CHAT_ID, limit=limit)
    if posts:
        messages = [m for m in messages if m["id"] in set(posts)]
    print(f"сообщений с текстом: {len(messages)}")

    parser = VacancyParserService()
    processor = MeliorMessageProcessor(sheets_service=None, llm_parser=parser)
    album_stats = melior_kb.attach_disk_albums(messages, query_fn=processor.object_query)
    print(f"ссылок на диск в постах: {album_stats['links']}, "
          f"привязано альбомов: {album_stats['attached']} "
          f"({album_stats['photos']} фото), не опознано: {album_stats['skipped']}")

    print("\n=== 3. Сбор заявок ===")
    requests, had_snapshot = processor.collect_requests(
        messages=messages, source="melior",
        source_url=f"https://t.me/c/{abs(CHAT_ID) - 1000000000000}",
    )
    service = len(messages) - len(requests)
    chunks_total = sum(len(r.parse_chunks or [r.raw_text]) for r in requests)
    with_context = sum(
        1 for r in requests for ctx in r.context_for_chunks() if ctx
    )
    print(f"заявок: {len(requests)} | служебных постов: {service} | "
          f"блоков объектов: {chunks_total} | блоков со справкой: {with_context}")

    print("\nпост → блоки (и какой объект опознан)")
    for raw in requests:
        blocks = raw.parse_chunks or [raw.raw_text]
        kb = (raw.raw_payload or {}).get("kb") or []
        names = ", ".join(f"{k['project']} {k['score']:.2f}" for k in kb) or "справок нет"
        head = _short(raw.raw_text.split("\n")[0], 34)
        print(f"  {raw.source_ref:<10} блоков {len(blocks):>2}  {head:<36} | {_short(names, 90)}")

    if not use_llm:
        print("\n--no-llm: разбор моделью пропущен")
        return 0

    print("\n=== 4. Разбор моделью ===")
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    async def parse_chunk(text: str, context: Optional[str]):
        async with sem:
            return await parser.aparse_raw_ex(text, context)

    rows: List[Dict] = []
    failed: List[str] = []
    for raw in requests:
        chunks = raw.parse_chunks or [raw.raw_text]
        contexts = raw.context_for_chunks()
        results = await asyncio.gather(
            *[parse_chunk(chunk, ctx) for chunk, ctx in zip(chunks, contexts)]
        )
        for (parsed, _in, _out), chunk, ctx in zip(results, chunks, contexts):
            if parsed is None:
                failed.append(f"{raw.source_ref}: {_short(chunk, 60)}")
                continue
            for item in parsed:
                item["_ref"] = raw.source_ref
                item["_kb"] = bool(ctx)
                rows.append(item)

    # Разбор стоит денег, а смотреть на него хочется не один раз: складываем
    # результат рядом с базой, чтобы следующая проверка обошлась без модели.
    with open(DUMP_PATH, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    print(f"позиций распознано: {len(rows)} | блоков без разбора: {len(failed)} "
          f"| результат сохранён в {DUMP_PATH}")
    print(f"токены: {parser.usage.input} на вход, {parser.usage.output} на выход, "
          f"вызовов модели {parser.usage.requests}")

    print("\n{:<10} {:<24} {:<16} {:<20} {:<9} {:<12} {:<7} {}".format(
        "пост", "объект", "город", "должность", "потребн.", "ставка", "смены", "спр."))
    print("-" * 120)
    for item in sorted(rows, key=lambda r: (r.get("object_name") or "", r.get("vacancy_name") or "")):
        print("{:<10} {:<24} {:<16} {:<20} {:<9} {:<12} {:<7} {}".format(
            item["_ref"],
            _short(item.get("object_name") or item.get("counterparty"), 24),
            _short(item.get("city"), 16),
            _short(item.get("vacancy_name"), 20),
            _short(_need(item), 9),
            _short(item.get("shift_rate"), 12),
            _short(item.get("min_shifts"), 7),
            "да" if item["_kb"] else "—",
        ))

    print("\n=== 5. Полнота полей ===")
    keys = [
        "object_name", "object_address", "city", "vacancy_name", "shift_rate",
        "min_shifts", "schedule", "shift_type", "duties", "requirements",
        "citizenship_requirements", "age_to", "gender", "housing_available",
        "meals_available", "sb_policy", "advantages", "medical_book_required",
    ]
    for key in keys:
        filled = sum(1 for r in rows if r.get(key) not in (None, "", []))
        share = round(100 * filled / len(rows)) if rows else 0
        bar = "█" * (share // 5)
        print(f"  {key:<26} {filled:>3}/{len(rows):<3} {share:>3}% {bar}")

    # Один объект приезжает и постом-событием, и строкой в актуальной заявке.
    # В реестре такие позиции склеятся по fingerprint — прикидываем, сколько
    # строк реально появится у рекрутёра.
    keys = {
        tuple(
            " ".join(str(item.get(f) or "").lower().split())
            for f in ("counterparty", "city", "vacancy_name", "object_name", "shift_type")
        )
        for item in rows
    }
    print(f"\nпосле склейки одинаковых позиций останется примерно {len(keys)} из {len(rows)}")

    if failed:
        print("\nблоки, которые модель не разобрала:")
        for line in failed[:10]:
            print(f"  {line}")

    print("\nв реестр ничего не записано (это холостой прогон)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50, help="сколько последних постов взять")
    ap.add_argument("--no-llm", action="store_true", help="не звать модель, только сбор")
    ap.add_argument("--posts", type=str, default="", help="id постов через запятую")
    args = ap.parse_args()
    ids = [int(x) for x in args.posts.split(",") if x.strip()] if args.posts else None
    try:
        sys.exit(asyncio.run(main(args.limit, not args.no_llm, ids)))
    except KeyboardInterrupt:
        logger.warning("прервано")
        sys.exit(130)
