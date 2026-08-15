"""Резервная копия перед миграцией и скрипт backup_db.

Откатов у схемы нет: SQLite не умеет DROP COLUMN в старых сборках, обратных
скриптов в проекте тоже нет. Единственный способ вернуться назад — файл копии,
поэтому проверяем, что он действительно появляется.
"""

import os
import sqlite3

from registry import db


def test_no_backup_on_first_run(tmp_path):
    """Пустая база — это первый запуск, а не апгрейд: копировать нечего."""
    path = str(tmp_path / "registry.db")
    backups = tmp_path / "backups"
    os.environ["BACKUP_DIR"] = str(backups)
    try:
        db.reset_schema_cache()
        with db.connect(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert not backups.exists() or not list(backups.iterdir())
    finally:
        os.environ.pop("BACKUP_DIR", None)
        db.reset_schema_cache()


def test_backup_is_made_before_upgrade(tmp_path):
    """База со старой версией схемы копируется до наката недостающих миграций."""
    path = str(tmp_path / "registry.db")
    backups = tmp_path / "backups"
    os.environ["BACKUP_DIR"] = str(backups)
    try:
        db.reset_schema_cache()
        with db.connect(path) as conn:
            conn.execute(
                "INSERT INTO id_counters (scope, value) VALUES ('request:2026', 7)"
            )
        db.reset_schema_cache()

        # Откатываем версию: как будто база осталась на схеме постарше.
        raw = sqlite3.connect(path)
        raw.execute(f"PRAGMA user_version={len(db.MIGRATIONS) - 1}")
        raw.commit()
        raw.close()

        with db.connect(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)

        files = sorted(backups.iterdir())
        assert files, "копия перед миграцией не создана"

        # Копия читается и содержит данные, которые были до наката.
        copy = sqlite3.connect(str(files[0]))
        try:
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            value = copy.execute(
                "SELECT value FROM id_counters WHERE scope = 'request:2026'"
            ).fetchone()[0]
            assert value == 7
        finally:
            copy.close()
    finally:
        os.environ.pop("BACKUP_DIR", None)
        db.reset_schema_cache()


def test_backup_script_makes_readable_copy(tmp_path):
    import scripts.backup_db as backup

    path = str(tmp_path / "registry.db")
    db.reset_schema_cache()
    try:
        with db.connect(path) as conn:
            conn.execute("INSERT INTO id_counters (scope, value) VALUES ('position:X', 3)")

        out = str(tmp_path / "copy.db")
        backup.make_backup(path, out)

        copy = sqlite3.connect(out)
        try:
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert copy.execute(
                "SELECT value FROM id_counters WHERE scope = 'position:X'"
            ).fetchone()[0] == 3
        finally:
            copy.close()
    finally:
        db.reset_schema_cache()
