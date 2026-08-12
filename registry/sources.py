"""Общие мелочи для экстракторов, отдающих заявки в реестр."""

from typing import Set

from registry.dictionaries import norm_key

# Алиасы источников. Совпадают с ключами SOURCE_NAMES в pipeline.py — это то,
# что лежит в колонке positions.source и по чему работает снапшот-лайфцикл.
SOURCE_KPK = "kpk"
SOURCE_YAPPI = "yappi"
SOURCE_VAHTAPRO = "vahtapro"
SOURCE_AAAPLUS = "aaaplus"
SOURCE_AMETIST = "ametist"
SOURCE_MARKETSTAFF = "marketstaff"
SOURCE_MANUAL = "manual"


def unique_ref(base: str, seen: Set[str]) -> str:
    """Гарантирует уникальность ключа документа внутри одного прогона.

    Естественный ключ строки таблицы («объект + должность») изредка
    повторяется: у одного объекта бывают две строки с одной должностью, но
    разными сменами. Второй такой строке даём суффикс, иначе она затрёт
    первую и половина потребности потеряется.
    """
    key = norm_key(base) or "row"
    candidate = key
    index = 2
    while candidate in seen:
        candidate = f"{key}#{index}"
        index += 1
    seen.add(candidate)
    return candidate
