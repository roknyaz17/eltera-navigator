"""Резервная копия базы реестра.

    python scripts/backup_db.py                  # копия в BACKUP_DIR
    python scripts/backup_db.py --keep 30        # оставить последние 30
    python scripts/backup_db.py --out /mnt/x.db  # в конкретный файл
    python scripts/backup_db.py --list           # что уже накоплено
    python scripts/backup_db.py --restore FILE   # восстановить из копии

Зачем отдельным скриптом, а не `cp`: база работает в режиме WAL, и простое
копирование файла во время прогона даёт битую копию — часть транзакций
останется в `-wal`. Штатный backup API SQLite снимает согласованный снимок,
не останавливая приложение.

Копия перед миграциями снимается автоматически (registry/db.py), этот скрипт —
для регулярных копий по расписанию и для ручных перед рискованными операциями.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.db import DEFAULT_DB_PATH

SUFFIX = ".bak"


def backup_dir() -> str:
    configured = os.getenv("BACKUP_DIR", "").strip()
    if configured:
        return configured
    return os.path.join(os.path.dirname(os.path.abspath(DEFAULT_DB_PATH)) or ".", "backups")


def existing(directory: str, db_path: str) -> List[str]:
    if not os.path.isdir(directory):
        return []
    base = os.path.basename(db_path)
    names = [n for n in os.listdir(directory) if n.startswith(base) and n.endswith(SUFFIX)]
    return sorted(names, reverse=True)


def make_backup(db_path: str, out: str = "") -> str:
    if not os.path.exists(db_path):
        raise SystemExit(f"База не найдена: {db_path}")

    if out:
        target = out
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    else:
        directory = backup_dir()
        os.makedirs(directory, exist_ok=True)
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            version = source.execute("PRAGMA user_version").fetchone()[0]
        finally:
            source.close()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(directory, f"{os.path.basename(db_path)}.v{version}-{stamp}{SUFFIX}")

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    size = os.path.getsize(target)
    print(f"Копия готова: {target} ({size / 1024 / 1024:.1f} МБ)")
    return target


def prune(db_path: str, keep: int) -> None:
    directory = backup_dir()
    names = existing(directory, db_path)
    extra = names[keep:]
    for name in extra:
        os.remove(os.path.join(directory, name))
    if extra:
        print(f"Удалено старых копий: {len(extra)} (оставлено {keep})")


def restore(db_path: str, source_path: str) -> None:
    if not os.path.exists(source_path):
        raise SystemExit(f"Копия не найдена: {source_path}")

    # Проверяем, что копия читается и это действительно наша база.
    probe = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        ok = probe.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise SystemExit(f"Копия повреждена: integrity_check = {ok}")
        version = probe.execute("PRAGMA user_version").fetchone()[0]
        tables = probe.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='positions'"
        ).fetchone()[0]
        if not tables:
            raise SystemExit("В копии нет таблицы positions — это не база реестра")
    finally:
        probe.close()

    if os.path.exists(db_path):
        aside = f"{db_path}.replaced-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.move(db_path, aside)
        print(f"Текущая база отложена в {aside}")
        for tail in ("-wal", "-shm"):
            leftover = db_path + tail
            if os.path.exists(leftover):
                os.remove(leftover)

    shutil.copy2(source_path, db_path)
    print(f"Восстановлено из {source_path} → {db_path} (схема v{version})")
    print("Перезапустите приложение: открытые соединения ещё смотрят на прежний файл.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Резервная копия базы реестра")
    parser.add_argument("--db", default=None, help=f"путь к базе (по умолчанию {DEFAULT_DB_PATH})")
    parser.add_argument("--out", default="", help="записать копию в конкретный файл")
    parser.add_argument("--keep", type=int, default=0, help="оставить N последних копий, остальные удалить")
    parser.add_argument("--list", action="store_true", help="показать накопленные копии")
    parser.add_argument("--restore", default="", help="восстановить базу из указанной копии")
    args = parser.parse_args()

    db_path = args.db or DEFAULT_DB_PATH

    if args.list:
        directory = backup_dir()
        names = existing(directory, db_path)
        if not names:
            print(f"Копий нет: {directory}")
            return
        print(f"Копии в {directory}:")
        for name in names:
            full = os.path.join(directory, name)
            print(f"  {name}  {os.path.getsize(full) / 1024 / 1024:>7.1f} МБ")
        return

    if args.restore:
        restore(db_path, args.restore)
        return

    make_backup(db_path, args.out)
    if args.keep > 0:
        prune(db_path, args.keep)


if __name__ == "__main__":
    main()
