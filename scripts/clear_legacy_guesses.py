"""Очистка следов удалённых правил-догадок у перенесённых позиций.

    python scripts/clear_legacy_guesses.py --dry-run
    python scripts/clear_legacy_guesses.py

Разовая операция после миграции. В прежнем промпте были правила, которые
достраивали данные вместо контрагента:

- смена по умолчанию «day», даже если про смены в заявке ни слова;
- «вахта → питание есть и бесплатное»;
- «пищевое производство → нужна медкнижка»;
- регион определялся моделью по городу.

Правила убраны, но перенесённые из таблицы строки хранят их результат:
миграция нормализует формат, а текст заново не разбирает. Хуже того, значение
закрепляется — при обновлении позиции пустое новое не затирает известное
старое, так что «day» пережил бы и будущие разборы.

ВАЖНО, как именно чистим. Поле стирается только если в исходном тексте заявки
нет ничего, на чём оно могло быть основано. Иначе легко снести настоящие
данные: у «Миксита» дневная и ночная смены — это две разные позиции с разной
потребностью, и смена там не подставлена по умолчанию, а прямо написана. Из 113
активных позиций питание указано у 104, а додумано было лишь у 29 — стирать все
104 значило бы потерять реальные условия.

Знание про регионы не теряется: seed_dictionaries.py собирает справочник
«город → регион» ровно из этих значений, и после подтверждения человеком регион
подставляется обратно (см. renormalize.py). Поэтому скрипт отказывается чистить
регионы, пока справочник пуст.

Чистятся только позиции, приехавшие из миграции (legacy_id не пуст). Данные,
принятые новым путём, догадок не содержат по определению.
"""

import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime
from typing import Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from registry import db
from registry import dictionaries as dicts

# Метка в истории изменений: чтобы потом было видно, что поле опустошила
# чистка, а не контрагент передумал.
CLEANUP_MARKER = "cleanup:legacy-guesses"

# Слова, наличие которых в тексте заявки означает, что поле заполнено по делу.
SHIFT_MARKERS = ("ночн", "ночь", "в ночь", "дневн", "день", "смешан", "суточ",
                 "2 смены", "две смены", "день/ночь", "д/н")
MEALS_MARKERS = ("питан", "обед", "кормят", "столов", "еда", "завтрак", "ужин", "🍽")
MEDBOOK_MARKERS = ("медкниж", "мед.книж", "мед книж", "медицинск книж", "лмк",
                   "санкниж", "санитарн книж", "мед книжк", "мк ")


class Rule:
    """Правило очистки одного поля.

    :param markers: если хоть одно слово встретилось в тексте заявки — поле
        основано на тексте, не трогаем.
    :param only_values: чистить только эти значения. Для смены — только «day»:
        «night» и «mixed» по умолчанию не подставлялись никогда, они всегда
        приходили из текста.
    """

    def __init__(self, description: str, markers: Sequence[str],
                 only_values: Optional[set] = None):
        self.description = description
        self.markers = markers
        self.only_values = only_values

    def should_clear(self, value, text: str) -> bool:
        if value is None:
            return False
        if self.only_values is not None and str(value) not in self.only_values:
            return False
        return not any(marker in text for marker in self.markers)


RULES = {
    "shift_type": Rule("смена по умолчанию «day»", SHIFT_MARKERS, only_values={"day"}),
    "meals_available": Rule("вахта → питание есть", MEALS_MARKERS),
    "meals_free": Rule("вахта → питание бесплатное", MEALS_MARKERS),
    "medical_book_required": Rule("пищевое производство → нужна медкнижка", MEDBOOK_MARKERS),
}

_WORD_RE = re.compile(r"[\w]{4,}", re.UNICODE)


def normalize_text(value: Optional[str]) -> str:
    return (value or "").lower().replace("ё", "е")


def region_from_text(region: Optional[str], text: str) -> bool:
    """Похоже ли, что регион действительно написан в заявке.

    Ищем любое значимое слово названия: «Московская область» подтверждается
    словом «московская», а если в тексте только «Москва» — значит, регион
    вывела модель, и это ровно тот случай, который мы убираем.
    """
    if not region:
        return True
    words = _WORD_RE.findall(normalize_text(region))
    ignore = {"область", "обл", "край", "республика", "респ", "округ", "район"}
    words = [w for w in words if w not in ignore]
    if not words:
        return True
    return any(word[:6] in text for word in words)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--scope", choices=("active", "all"), default="active",
                        help="active — только действующие позиции (по умолчанию), "
                             "all — включая архив")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-region", action="store_true",
                        help="чистить регионы даже без справочника «город → регион»")
    args = parser.parse_args()

    now = datetime.now().isoformat(timespec="seconds")
    cleared = Counter()
    kept = Counter()
    touched = 0

    with db.connect(args.db) as conn:
        clean_region = True
        city_region = dicts.load(conn, dicts.KIND_CITY_REGION, include_pending=True)
        if not city_region and not args.force_region:
            clean_region = False
            logger.warning(
                "Справочник «город → регион» пуст — регионы не трогаю, иначе знание "
                "будет потеряно. Сначала: python scripts/seed_dictionaries.py "
                "(или --force-region, если так и задумано)"
            )
        else:
            confirmed = len(dicts.load(conn, dicts.KIND_CITY_REGION))
            logger.info(
                f"Справочник «город → регион»: {len(city_region)} записей, "
                f"подтверждено {confirmed}"
            )
            if not confirmed and not args.force_region:
                logger.warning(
                    "Ни одна запись справочника не подтверждена — после чистки регионы "
                    "останутся пустыми, пока их не подтвердят на /registry/dictionaries"
                )

        where = "p.legacy_id IS NOT NULL AND p.legacy_id != ''"
        if args.scope == "active":
            where += " AND p.is_active = 1"
        rows = list(conn.execute(
            f"SELECT p.*, r.raw_text FROM positions p "
            f"JOIN requests r ON r.request_id = p.last_request_id WHERE {where}"
        ))
        logger.info(f"Позиций из миграции в области видимости: {len(rows)}")

        # Должность «Работник» — тоже подстановка, но снести её нельзя: без
        # названия позиция станет неразличимой. Показываем список отдельно.
        stubs = [row["position_id"] for row in rows
                 if (row["vacancy_name"] or "").strip().lower() == "работник"]
        if stubs:
            logger.warning(
                f"Позиции с подставленной должностью «Работник» ({len(stubs)}): "
                f"{', '.join(stubs[:10])}{'…' if len(stubs) > 10 else ''} — "
                f"название придётся уточнить руками, автоматически стереть его нельзя"
            )

        for row in rows:
            text = normalize_text(row["raw_text"])
            changes = {}
            for name, rule in RULES.items():
                if rule.should_clear(row[name], text):
                    changes[name] = row[name]
                elif row[name] is not None:
                    kept[name] += 1
            if clean_region and row["region"] is not None:
                if region_from_text(row["region"], text):
                    kept["region"] += 1
                else:
                    changes["region"] = row["region"]

            if not changes:
                continue
            touched += 1
            for name in changes:
                cleared[name] += 1
            if args.dry_run:
                continue

            assignments = ", ".join(f"{name} = NULL" for name in changes)
            conn.execute(
                f"UPDATE positions SET {assignments}, updated_at = ? WHERE position_id = ?",
                (now, row["position_id"]),
            )
            conn.executemany(
                "INSERT INTO position_history "
                "(position_id, request_id, field, old_value, new_value, changed_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                [(row["position_id"], CLEANUP_MARKER, name, str(old), now)
                 for name, old in changes.items()],
            )

    logger.info(f"{'Затронуло бы' if args.dry_run else 'Затронуто'} позиций: {touched}")
    logger.info("Поле: стёрто (догадка) / оставлено (есть в тексте заявки)")
    for name in list(RULES) + (["region"] if cleared.get("region") or kept.get("region") else []):
        if cleared.get(name) or kept.get(name):
            description = RULES[name].description if name in RULES else "регион по городу"
            logger.info(f"  {name}: {cleared.get(name, 0)} / {kept.get(name, 0)}  ({description})")
    if args.dry_run:
        logger.info("dry-run: ничего не записано")
    else:
        logger.info("Стёртые значения видны в истории изменений позиции")


if __name__ == "__main__":
    main()
