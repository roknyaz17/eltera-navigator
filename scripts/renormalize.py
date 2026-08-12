"""Пересчёт нормализации по текущим справочникам.

    python scripts/renormalize.py --dry-run
    python scripts/renormalize.py

Зачем: подтверждение записи в справочнике меняет каноническое значение, но уже
лежащие в реестре позиции об этом не знают. Без пересчёта получается расхождение
— свежие заявки нормализуются по новому справочнику, старые остаются со старыми
формулировками, и фильтр снова показывает два варианта одной должности.

Запускать после каждой заметной правки справочников и обязательно — между
миграцией из Sheets и первым боевым прогоном: миграция нормализует данные, когда
справочники ещё пусты.

Пересчитываются только поля, которые зависят от справочников и разбора строк
(должность, город, регион, контрагент, ставка, график). Всё остальное и работа
менеджера не трогаются.
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from registry import db
from registry.ingest import SEARCHABLE_FIELDS
from registry.normalize import Normalizer, fingerprint

# Поля, которые пересчитываются. Для каждого — откуда брать исходное значение:
# сохранённый при приёме *_raw, а если его нет (позиция приехала до появления
# колонки) — текущее значение.
RECOMPUTED = {
    "counterparty": "counterparty_raw",
    "vacancy_name": "vacancy_name_raw",
    "city": "city_raw",
    "region": "region_raw",
    "schedule": "schedule_raw",
    "shift_rate": "shift_rate_raw",
    "requirements": "requirements_raw",
}
# Производные от тех же исходников.
DERIVED = ["work_pattern", "min_shifts", "shift_hours", "hourly_rate", "vacancy_category",
           "citizenship_requirements", "sb_policy", "work_format", "shift_type", "gender"]


def source_value(row, field: str):
    raw_field = RECOMPUTED.get(field)
    if raw_field:
        value = row[raw_field]
        if value not in (None, ""):
            return value
    return row[field]


def renormalize(conn, dry_run: bool = False) -> dict:
    """Пересчитывает позиции по текущим справочникам.

    Возвращает сводку: сколько обновлено, какие поля изменились, сколько
    пропущено из-за схлопывания.
    """
    changed_fields = Counter()
    updated = collisions = 0

    normalizer = Normalizer(conn, learn=False)
    rows = list(conn.execute("SELECT * FROM positions"))
    logger.info(f"Позиций к пересчёту: {len(rows)}")

    for row in rows:
        vac = {field: source_value(row, field) for field in RECOMPUTED}
        for field in DERIVED:
            vac.setdefault(field, row[field])
        # Числа потребности и признаки нормализация не меняет — переносим как есть.
        for field in ("need_men", "need_women", "need_couples", "need_total",
                      "age_from", "age_to", "object_name", "object_address", "duties"):
            vac[field] = row[field]

        fields = normalizer.normalize(vac)
        diff = {
            name: value for name, value in fields.items()
            if name in RECOMPUTED or name in DERIVED
            if (value or None) != (row[name] or None)
        }
        new_fp = fingerprint(fields)
        if not diff and new_fp == row["fingerprint"]:
            continue

        if new_fp != row["fingerprint"]:
            clash = conn.execute(
                "SELECT position_id FROM positions WHERE source = ? AND fingerprint = ? "
                "AND position_id != ?",
                (row["source"], new_fp, row["position_id"]),
            ).fetchone()
            if clash:
                # Две позиции сошлись в одну после подтверждения справочника.
                # Молча склеивать нельзя — это потеря данных, показываем пару.
                logger.warning(
                    f"{row['position_id']} после пересчёта совпала с {clash['position_id']} "
                    f"({row['counterparty']} / {row['city']} / {row['vacancy_name']}) — "
                    f"пропускаю, разведите их вручную"
                )
                collisions += 1
                continue

        for name in diff:
            changed_fields[name] += 1
        updated += 1
        if dry_run:
            continue

        assignments = ", ".join(f"{name} = ?" for name in diff)
        params = list(diff.values())
        if assignments:
            assignments += ", "
        conn.execute(
            f"UPDATE positions SET {assignments}fingerprint = ? WHERE position_id = ?",
            params + [new_fp, row["position_id"]],
        )
        request_row = conn.execute(
            "SELECT raw_text FROM requests WHERE request_id = ?",
            (row["last_request_id"],),
        ).fetchone()
        body = [request_row["raw_text"] if request_row else ""]
        body += [str(fields[name]) for name in SEARCHABLE_FIELDS if fields.get(name)]
        conn.execute("DELETE FROM search_index WHERE position_id = ?", (row["position_id"],))
        conn.execute(
            "INSERT INTO search_index (position_id, request_id, body) VALUES (?, ?, ?)",
            (row["position_id"], row["last_request_id"], "\n".join(body)),
        )

    return {"updated": updated, "collisions": collisions, "fields": dict(changed_fields)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with db.connect(args.db) as conn:
        result = renormalize(conn, dry_run=args.dry_run)

    logger.info(f"{'Изменилось бы' if args.dry_run else 'Обновлено'} позиций: {result['updated']}")
    for name, count in sorted(result["fields"].items(), key=lambda kv: -kv[1]):
        logger.info(f"  {name}: {count}")
    if result["collisions"]:
        logger.warning(f"Пропущено из-за совпадения после пересчёта: {result['collisions']}")
    if args.dry_run:
        logger.info("dry-run: ничего не записано")


if __name__ == "__main__":
    main()
