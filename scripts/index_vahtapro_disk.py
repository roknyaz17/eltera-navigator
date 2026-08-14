"""Индексация публичного Яндекс.Диска Градуса в реестр.

Диск — база знаний контрагента: раздел → папка проекта → описание, направления
на заселение, фотоальбомы. Скрипт обходит его и складывает в таблицу
`disk_projects`, откуда разбор заявок берёт справки, не выходя в сеть.

    python -m scripts.index_vahtapro_disk            # инкрементально
    python -m scripts.index_vahtapro_disk --force    # перечитать все описания
    python -m scripts.index_vahtapro_disk --stats    # что уже лежит в индексе
    python -m scripts.index_vahtapro_disk --match "🚀BMJ, Шарапово (Мос. Обл)"

Обход инкрементальный: у каждой папки считается отпечаток содержимого, и
документы перекачиваются только у тех, где он изменился. Полный первый обход —
несколько минут, последующие — меньше минуты.

Отдельная команда, а не только шаг прогона: диск обновляют вручную и в любое
время, и иногда индекс нужно поднять «прямо сейчас», не дожидаясь пайплайна.
"""

import argparse
import os
import sys

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from project_kb import DEFAULT_DISK_URL, DiskIndexer, ProjectKB, build_context, last_indexed_at
from yandex_disk import PublicDisk


def show_stats(db_path: str) -> int:
    kb = ProjectKB.load(db_path=db_path)
    if kb.is_empty:
        print("Индекс пуст — запустите без --stats")
        return 1
    with_docs = [c for c in kb.cards if c.has_description]
    photos = sum(c.photos for c in kb.cards)
    print(f"Проектов в индексе: {len(kb.cards)}")
    print(f"С текстовым описанием: {len(with_docs)} "
          f"({len(with_docs) / len(kb.cards):.0%})")
    print(f"Фотографий в альбомах: {photos}")
    print(f"Последний обход: {last_indexed_at(db_path) or '—'}")
    print()
    for card in kb.cards:
        mark = "+" if card.has_description else " "
        print(f" {mark} {card.title[:44]:<46}{len(card.doc_text):>6} симв.  "
              f"{card.photos:>3} фото  {card.category}")
    print("\n«+» — есть описание проекта, оно и уходит в разбор заявки.")
    return 0


def show_match(db_path: str, text: str) -> int:
    kb = ProjectKB.load(db_path=db_path)
    if kb.is_empty:
        print("Индекс пуст")
        return 1
    hit = kb.match(text)
    if not hit:
        print(f"Проект не опознан: {text[:80]!r}")
        print("Так и должно быть, если проекта нет на диске: заявка разберётся без справки.")
        return 0
    print(f"Проект: {hit.card.title}  (совпадение {hit.score:.2f}, "
          f"следующий кандидат — {hit.runner_up or '—'} {hit.runner_up_score:.2f})")
    print(f"Папка:  {hit.card.url}")
    print()
    print(build_context(hit.card))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="перечитать описания всех проектов, даже неизменившихся")
    parser.add_argument("--stats", action="store_true", help="показать состояние индекса")
    parser.add_argument("--match", metavar="ТЕКСТ",
                        help="проверить, с каким проектом сопоставится пост")
    parser.add_argument("--url", default=DEFAULT_DISK_URL, help="публичная ссылка на диск")
    parser.add_argument("--db", default=os.getenv("REGISTRY_DB_PATH"), help="путь к базе реестра")
    args = parser.parse_args()

    if args.stats:
        return show_stats(args.db)
    if args.match:
        return show_match(args.db, args.match)

    stats = DiskIndexer(PublicDisk(args.url), db_path=args.db).refresh(force=args.force)
    print(f"Проектов: {stats['projects']}, обновлено: {stats['updated']}, "
          f"описаний прочитано: {stats['docs_read']}, удалено: {stats['removed']}, "
          f"ошибок: {stats['errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
