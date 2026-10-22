"""Вернуть в очередь сообщения, упавшие навсегда.

Отдельным файлом, а не строкой через ssh: одинарные и двойные кавычки при
передаче по ssh в docker exec съедаются, и запрос доезжает искалеченным —
молча, без ошибки, потому что SQLite принимает двойные кавычки за имя колонки
и в отсутствие такой колонки трактует их как строку.

    python requeue.py 69 71        вернуть конкретные сообщения
    python requeue.py --failed     всё, что упало навсегда
"""

from __future__ import annotations

import sqlite3
import sys

DB = "/app/data/registry.db"


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    if "--failed" in sys.argv:
        rows = conn.execute(
            "SELECT message_id FROM candidate_messages WHERE status = 'failed'").fetchall()
        ids = [r["message_id"] for r in rows]
    else:
        ids = [int(a) for a in sys.argv[1:] if a.isdigit()]

    if not ids:
        print("Нечего возвращать.")
        return 1

    marks = ", ".join("?" * len(ids))
    conn.execute(
        f"""UPDATE candidate_messages
            SET status = 'pending', attempts = 0, last_error = ''
            WHERE message_id IN ({marks})""", ids)
    conn.commit()

    for row in conn.execute(
            f"""SELECT message_id, phone, kind, channel, status
                FROM candidate_messages WHERE message_id IN ({marks})""", ids):
        print(" ", dict(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
