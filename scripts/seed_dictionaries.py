"""Сбор справочников унификации из уже накопленных данных.

    python scripts/seed_dictionaries.py              # собрать предложения
    python scripts/seed_dictionaries.py --confirm    # сразу подтвердить всё

Готовых утверждённых списков должностей и городов нет, поэтому первичный
словарь строится из того, что реально приехало от контрагентов:

- варианты написания должностей и городов сводятся по нормализованному ключу,
  каноническим предлагается самый частый вариант;
- город → регион берётся большинством голосов среди позиций, где регион был
  указан явно. Дальше именно этот справочник подставляет регион строкам, где
  контрагент его не написал, — вместо прежней догадки модели.

Всё попадает в очередь на подтверждение (confirmed=0) и не применяется, пока
человек не согласится. --confirm подтверждает пачкой: удобно на первом
запуске, когда список просматривают целиком глазами.
"""

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from registry import db
from registry import dictionaries as dicts
from registry.dictionaries import norm_key
from registry.normalize import title_ru


def _most_common(values) -> str:
    counter = Counter(v for v in values if v)
    return counter.most_common(1)[0][0] if counter else ""


def seed_field(conn, kind: str, column: str, titled: bool) -> int:
    """Собирает варианты написания одной колонки в справочник."""
    rows = conn.execute(
        f"SELECT {column} AS value, COUNT(*) AS cnt FROM positions "
        f"WHERE {column} IS NOT NULL AND {column} != '' GROUP BY {column}"
    ).fetchall()

    variants = defaultdict(list)
    for row in rows:
        variants[norm_key(row["value"])].extend([row["value"]] * row["cnt"])

    added = 0
    for key, values in variants.items():
        if not key:
            continue
        canonical = _most_common(values)
        if titled:
            canonical = title_ru(canonical)
        dicts.upsert(conn, kind, key, canonical, confirmed=False)
        added += 1
    return added


def seed_city_region(conn) -> int:
    """Город → регион по фактическим данным.

    Берём только те пары, где регион был написан в самой заявке. Если у одного
    города встретились разные регионы, выбираем частотный, но помечаем это в
    примечании — такие строки надо смотреть глазами.
    """
    rows = conn.execute(
        "SELECT city, region, COUNT(*) AS cnt FROM positions "
        "WHERE city IS NOT NULL AND city != '' AND region IS NOT NULL AND region != '' "
        "GROUP BY city, region"
    ).fetchall()

    by_city = defaultdict(list)
    for row in rows:
        by_city[norm_key(row["city"])].append((row["region"], row["cnt"]))

    added = 0
    for city_key, pairs in by_city.items():
        pairs.sort(key=lambda x: -x[1])
        canonical = pairs[0][0]
        note = ""
        if len(pairs) > 1:
            note = "спорно: " + ", ".join(f"{region} ({cnt})" for region, cnt in pairs)
        dicts.upsert(conn, dicts.KIND_CITY_REGION, city_key, canonical, confirmed=False, note=note)
        added += 1
    return added


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--confirm", action="store_true",
                        help="подтвердить собранные записи сразу")
    args = parser.parse_args()

    with db.connect(args.db) as conn:
        stats = {
            dicts.KIND_JOB_TITLE: seed_field(conn, dicts.KIND_JOB_TITLE, "vacancy_name", True),
            dicts.KIND_CITY: seed_field(conn, dicts.KIND_CITY, "city", True),
            dicts.KIND_REGION: seed_field(conn, dicts.KIND_REGION, "region", True),
            dicts.KIND_COUNTERPARTY: seed_field(conn, dicts.KIND_COUNTERPARTY, "counterparty", False),
            dicts.KIND_VACANCY_CATEGORY: seed_field(conn, dicts.KIND_VACANCY_CATEGORY, "vacancy_category", False),
            dicts.KIND_CITIZENSHIP: seed_field(conn, dicts.KIND_CITIZENSHIP, "citizenship_requirements", False),
            dicts.KIND_CITY_REGION: seed_city_region(conn),
        }
        for kind, count in stats.items():
            logger.info(f"{dicts.KIND_TITLES.get(kind, kind)}: {count} записей")

        if args.confirm:
            confirmed = dicts.confirm_all(conn)
            logger.info(f"Подтверждено сразу: {confirmed}")
        else:
            pending = dicts.pending_counts(conn)
            logger.info(f"Ждут подтверждения: {sum(pending.values())} — /registry/dictionaries")


if __name__ == "__main__":
    main()
