"""Что потерялось при разборе заявок — читаемо, из мёртвой очереди.

Сам файл logs/parse_dlq.log — JSON Lines: машине удобно, человеку не очень.
Здесь то же самое, но глазами.

    python -m scripts.parse_dlq_show                    # последние 20 потерь
    python -m scripts.parse_dlq_show --source yappi     # только по ЯППИ
    python -m scripts.parse_dlq_show --kind vacancy_empty
    python -m scripts.parse_dlq_show --request ELT-2026-000526 --full
    python -m scripts.parse_dlq_show --since 2026-09-01 --stats

Старые месяцы лежат рядом в .zip — их тоже читаем, файл указывать не нужно.
"""

import argparse
import json
import os
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_DIR = Path(__file__).parent.parent / "logs"
KINDS = {
    "request_failed": "заявка не разобралась целиком",
    "chunks_failed": "потерян фрагмент заявки",
    "vacancy_empty": "вакансия отброшена как пустышка",
    "request_empty": "строка с потребностью разобралась в пустой список",
}


def _lines() -> Iterator[str]:
    """Все строки мёртвой очереди, включая заархивированные месяцы."""
    for path in sorted(LOG_DIR.glob("parse_dlq*.log")):
        yield from path.read_text(encoding="utf-8").splitlines()
    for path in sorted(LOG_DIR.glob("parse_dlq*.log.zip")):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                yield from archive.read(name).decode("utf-8").splitlines()


def load(args) -> List[Dict]:
    out = []
    for line in _lines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # обрезанная строка на границе ротации — не повод падать
        if args.source and entry.get("source") != args.source:
            continue
        if args.kind and entry.get("kind") != args.kind:
            continue
        if args.request and args.request not in entry.get("request_id", ""):
            continue
        if args.since and entry.get("at", "") < args.since:
            continue
        out.append(entry)
    return sorted(out, key=lambda e: e.get("at", ""))


def show(entry: Dict, full: bool) -> None:
    kind = entry.get("kind", "?")
    print(f"\n{'─' * 72}")
    print(f"{entry.get('at', '?')}  {entry.get('source', '?')}  {entry.get('request_id', '?')}")
    print(f"  что: {KINDS.get(kind, kind)}")
    print(f"  причина: {entry.get('error') or '—'}")
    origin = entry.get("origin") or {}
    if origin.get("source_ref"):
        print(f"  строка источника: {origin['source_ref'][:100]}")
    if origin.get("source_url"):
        print(f"  источник: {origin['source_url'][:100]}")
    for key in ("chunk_index", "chunks_total", "chunks_failed"):
        if key in entry:
            print(f"  {key}: {entry[key]}")
    if entry.get("exc_type"):
        print(f"  исключение: {entry['exc_type']}")
    raw = entry.get("raw_text") or {}
    if raw:
        note = f" (обрезано с {raw['chars']})" if raw.get("clipped") else ""
        body = raw["text"] if full else raw["text"][:400]
        print(f"  текст заявки{note}:")
        print("    " + body.replace("\n", "\n    "))
    if full:
        for key in ("vacancy_raw", "chunk"):
            if key in entry:
                val = entry[key]
                text = val["text"] if isinstance(val, dict) else json.dumps(val, ensure_ascii=False)
                print(f"  {key}:\n    " + text.replace("\n", "\n    "))
        if entry.get("fields"):
            print(f"  после нормализации: {json.dumps(entry['fields'], ensure_ascii=False)[:600]}")
        if entry.get("traceback"):
            print("  трассировка:\n    " + entry["traceback"].replace("\n", "\n    "))


def main() -> int:
    parser = argparse.ArgumentParser(description="Мёртвая очередь разбора заявок")
    parser.add_argument("--source", help="алиас источника: yappi, vahtapro, melior…")
    parser.add_argument("--kind", choices=sorted(KINDS), help="вид потери")
    parser.add_argument("--request", help="номер заявки или его часть")
    parser.add_argument("--since", help="с даты, ГГГГ-ММ-ДД")
    parser.add_argument("--limit", type=int, default=20, help="сколько показать (по умолчанию 20)")
    parser.add_argument("--full", action="store_true", help="целиком: текст, трассировка, поля")
    parser.add_argument("--stats", action="store_true", help="только сводка")
    args = parser.parse_args()

    if not LOG_DIR.exists():
        print(f"Каталога логов нет: {LOG_DIR}", file=sys.stderr)
        return 1
    entries = load(args)
    if not entries:
        print("Потерь не найдено — мёртвая очередь пуста.")
        return 0

    if args.stats:
        print(f"Всего записей: {len(entries)}  "
              f"({entries[0]['at'][:10]} … {entries[-1]['at'][:10]})\n")
        for title, counter in (("по источникам", Counter(e.get("source") for e in entries)),
                               ("по виду потери", Counter(e.get("kind") for e in entries))):
            print(f"{title}:")
            for key, count in counter.most_common():
                print(f"  {str(key):<18} {count}")
            print()
        return 0

    shown = entries[-args.limit:]
    for entry in shown:
        show(entry, args.full)
    print(f"\n{'─' * 72}\nпоказано {len(shown)} из {len(entries)}; "
          f"подробнее — с ключом --full")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
