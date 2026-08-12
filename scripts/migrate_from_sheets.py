"""Разовый перенос текущего листа Google Sheets в реестр.

    python scripts/migrate_from_sheets.py --dry-run      # только посчитать
    python scripts/migrate_from_sheets.py                # перенести

Что делает:
- строки с одинаковым original_message собираются в одну заявку — так
  восстанавливается тот документ, из которого они когда-то приехали;
- каждая заявка получает внутренний ID (ELT-…), каждая строка — ID позиции;
- значения прогоняются через ту же нормализацию, что и новые заявки, поэтому
  fingerprint у перенесённых позиций считается по новым правилам и первый же
  боевой прогон узнаёт их, а не заводит дубли;
- прежний vacancy_id сохраняется в positions.legacy_id для сверки;
- поля менеджера (статус, приоритет, комментарии) переносятся как есть.

Скрипт идемпотентен: повторный запуск не создаёт вторых копий, потому что
заявки ищутся по (source, source_ref).
"""

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from loguru import logger

from registry import db
from registry.ids import next_position_id, next_request_id, sync_counters
from registry.models import DATA_FIELDS, MANAGER_FIELDS
from registry.normalize import Normalizer, fingerprint, to_int
from sheets_adapter import GoogleSheetsService

# Значения колонки source в старой таблице → алиасы источников реестра.
LEGACY_SOURCE_ALIASES = {
    "кпк": "kpk",
    "кпк матрица": "kpk",
    "yappi": "yappi",
    "вахтапро": "vahtapro",
    "aaa+": "aaaplus",
    "аметист": "ametist",
    "маркетстафф": "marketstaff",
}


def alias_for(source_name: str) -> str:
    """Алиас источника. Незнакомые (отключённые провайдеры) остаются историей.

    Такие строки попадают в реестр под собственным алиасом и никогда не
    трогаются снапшот-лайфциклом: коллектора с таким алиасом нет, гасить их
    некому — ровно то поведение, что нужно для архива.
    """
    key = (source_name or "").strip().lower()
    if key in LEGACY_SOURCE_ALIASES:
        return LEGACY_SOURCE_ALIASES[key]
    return "legacy:" + (key or "unknown")


def parse_bool(value: str):
    text = (value or "").strip().upper()
    if text == "TRUE":
        return True
    if text == "FALSE":
        return False
    return None


def row_to_vac(row: Dict[str, str]) -> Dict:
    """Строка таблицы → словарь в том виде, какой ждёт Normalizer."""
    vac: Dict = {}
    for key, value in row.items():
        if key in ("vacancy_id", "source", "source_url", "original_message",
                   "is_active", "needs_review", "created_at", "updated_at",
                   "last_updated_at"):
            continue
        text = (value or "").strip()
        if not text:
            continue
        parsed = parse_bool(text)
        vac[key] = parsed if parsed is not None else text
    # В старой таблице ставка лежала числом, а нормализатор ждёт исходную
    # строку — числу это не мешает.
    return vac


def to_iso(value: str, fallback: str) -> str:
    text = (value or "").strip()
    if not text:
        return fallback
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return fallback


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="путь к базе реестра")
    parser.add_argument("--spreadsheet", default=None, help="id исходной таблицы")
    parser.add_argument("--sheet", default=None, help="имя листа")
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать, ничего не писать")
    args = parser.parse_args()

    from pipeline import TARGET_SHEET_NAME, TARGET_SPREADSHEET_ID

    spreadsheet_id = args.spreadsheet or TARGET_SPREADSHEET_ID
    sheet_name = args.sheet or TARGET_SHEET_NAME

    logger.info(f"Читаю {spreadsheet_id}/{sheet_name}")
    sheets = GoogleSheetsService(args.credentials)
    worksheet = sheets.client.open_by_key(spreadsheet_id).worksheet(sheet_name)
    all_values = worksheet.get_all_values()
    if len(all_values) < 2:
        logger.error("В листе нет данных")
        return

    headers = [(h or "").strip() for h in all_values[0]]
    rows: List[Dict[str, str]] = []
    for raw in all_values[1:]:
        entry = {col: (raw[i] if i < len(raw) else "") for i, col in enumerate(headers) if col}
        if any((v or "").strip() for v in entry.values()):
            rows.append(entry)
    logger.info(f"Строк с данными: {len(rows)}")

    # Группируем в заявки: одинаковый исходный текст = один документ.
    groups: Dict[tuple, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        source = alias_for(row.get("source"))
        message = (row.get("original_message") or "").strip()
        if message:
            ref = "legacy:" + hashlib.md5(message.encode("utf-8")).hexdigest()[:16]
        else:
            # Без исходника заявку не восстановить — считаем документом саму строку.
            ref = "legacy-row:" + (row.get("vacancy_id") or "").strip()
        groups[(source, ref)].append(row)

    logger.info(f"Заявок получится: {len(groups)}")
    if args.dry_run:
        by_source: Dict[str, int] = defaultdict(int)
        requests_by_source: Dict[str, int] = defaultdict(int)
        for (source, _), items in groups.items():
            by_source[source] += len(items)
            requests_by_source[source] += 1
        for source, count in sorted(by_source.items(), key=lambda x: -x[1]):
            logger.info(f"  {source}: позиций {count}, заявок {requests_by_source[source]}")

        # Источники, для которых нет коллектора, переносятся как архив и
        # больше никогда не обновляются. Это нормально для отключённых
        # провайдеров, но должно быть видно, а не теряться в списке.
        archived = {s: c for s, c in by_source.items() if s.startswith("legacy:")}
        if archived:
            logger.warning(
                "Источники без коллектора (переедут как история, обновляться не будут): "
                + ", ".join(f"{s.split(':', 1)[1]} — {c} позиций" for s, c in archived.items())
            )

        active = sum(
            1 for rows_of in groups.values() for row in rows_of
            if (row.get("is_active") or "").upper() == "TRUE"
        )
        logger.info(f"Из них активных позиций: {active}, в архиве: {len(rows) - active}")
        logger.info("dry-run: ничего не записано")
        return

    now = datetime.now().isoformat(timespec="seconds")
    created_requests = created_positions = merged = 0

    with db.connect(args.db) as conn:
        normalizer = Normalizer(conn, learn=True)

        for (source, ref), items in groups.items():
            existing = conn.execute(
                "SELECT request_id FROM requests WHERE source = ? AND source_ref = ?",
                (source, ref),
            ).fetchone()
            if existing:
                continue

            first = items[0]
            first_seen = to_iso(first.get("created_at"), now)
            last_seen = max(to_iso(row.get("last_updated_at"), first_seen) for row in items)
            request_id, year, seq = next_request_id(
                conn, int(first_seen[:4]) if first_seen[:4].isdigit() else None
            )
            conn.execute(
                """
                INSERT INTO requests (
                    request_id, year, seq, source, source_ref, source_name, source_url,
                    counterparty, counterparty_raw, raw_text, raw_payload, content_hash,
                    revision, received_at, first_seen_at, last_seen_at, parsed_at, parse_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, 1, ?, ?, ?, ?, 'ok')
                """,
                (
                    request_id, year, seq, source, ref,
                    (first.get("source") or source), (first.get("source_url") or ""),
                    (first.get("counterparty") or ""), (first.get("counterparty") or ""),
                    (first.get("original_message") or ""),
                    hashlib.sha256((first.get("original_message") or ref).encode("utf-8")).hexdigest(),
                    first_seen, first_seen, last_seen, first_seen,
                ),
            )
            created_requests += 1

            for row in items:
                fields = normalizer.normalize(row_to_vac(row))
                fp = fingerprint(fields)
                clash = conn.execute(
                    "SELECT position_id FROM positions WHERE source = ? AND fingerprint = ?",
                    (source, fp),
                ).fetchone()
                if clash:
                    # В старой таблице две строки могли описывать одну позицию
                    # (id «плыл» при правке города или названия). Схлопываем.
                    conn.execute(
                        "INSERT OR IGNORE INTO request_positions (request_id, position_id) "
                        "VALUES (?, ?)",
                        (request_id, clash["position_id"]),
                    )
                    merged += 1
                    continue

                position_id, position_seq = next_position_id(conn, request_id)
                columns = ["position_id", "seq", "first_request_id", "last_request_id",
                           "source", "fingerprint", "legacy_id", "is_active",
                           "first_seen_at", "last_seen_at", "updated_at"]
                values = [
                    position_id, position_seq, request_id, request_id, source, fp,
                    (row.get("vacancy_id") or "").strip() or None,
                    1 if (row.get("is_active") or "").upper() == "TRUE" else 0,
                    to_iso(row.get("created_at"), first_seen),
                    to_iso(row.get("last_updated_at"), first_seen),
                    to_iso(row.get("updated_at"), first_seen),
                ]
                for name in DATA_FIELDS:
                    columns.append(name)
                    values.append(fields.get(name))
                # Работа менеджера переносится дословно.
                for name in MANAGER_FIELDS:
                    columns.append(name)
                    raw_value = (row.get(name) or "").strip()
                    if name in ("market_rate",):
                        values.append(to_int(raw_value))
                    elif name == "market_deviation":
                        values.append(float(raw_value.replace(",", ".")) if raw_value else None)
                    else:
                        values.append(raw_value or None)

                conn.execute(
                    f"INSERT INTO positions ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    values,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO request_positions (request_id, position_id) VALUES (?, ?)",
                    (request_id, position_id),
                )
                body = [row.get("original_message") or ""]
                body += [str(fields[name]) for name in fields if fields.get(name)]
                conn.execute(
                    "INSERT INTO search_index (position_id, request_id, body) VALUES (?, ?, ?)",
                    (position_id, request_id, "\n".join(body)),
                )
                created_positions += 1

        sync_counters(conn)

    logger.info(
        f"Перенесено: заявок {created_requests}, позиций {created_positions}, "
        f"схлопнуто дублей {merged}"
    )
    logger.info(
        "Дальше: python scripts/seed_dictionaries.py — собрать справочники "
        "и просмотреть очередь на /registry/dictionaries"
    )


if __name__ == "__main__":
    main()
