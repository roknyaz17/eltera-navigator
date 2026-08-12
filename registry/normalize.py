"""Приведение распознанных данных к единому виду.

Порядок такой: LLM только ИЗВЛЕКАЕТ то, что написано в заявке, а унификацией
занимается этот модуль — детерминированно и по справочникам. Так одинаковые по
смыслу значения из разных источников («Комплектовщик», «комплектовщик»,
«КОМПЛЕКТОВЩИК») склеиваются одинаково при каждом прогоне, а не как решит
модель в этот раз.

Главное ограничение: ничего не додумывать. Нет данных в заявке — поле остаётся
None. Единственное исключение — регион по городу, и тот подставляется
исключительно из подтверждённого человеком справочника city_region, а не
догадкой модели.
"""

import re
import sqlite3
from typing import Any, Dict, Optional, Tuple

from registry import dictionaries as dicts
from registry.dictionaries import norm_key
from registry.models import (
    BOOL_FIELDS,
    DATA_FIELDS,
    INT_FIELDS,
    REAL_FIELDS,
    TEXT_FIELDS,
)

# --------------------------------------------------------------- перечисления
#
# Значения фиксированы: всё, что не опознано, остаётся пустым, а не попадает в
# поле «как есть» — иначе фильтр в интерфейсе снова обрастёт вариантами.

SHIFT_TYPES = {"day", "night", "mixed"}
SHIFT_TYPE_ALIASES = {
    "день": "day", "дневная": "day", "дневные": "day", "дневная смена": "day",
    "ночь": "night", "ночная": "night", "ночные": "night", "ночная смена": "night",
    "смешанная": "mixed", "смешанные": "mixed", "день/ночь": "mixed",
    "день и ночь": "mixed", "любая": "mixed",
}

GENDERS = {"мужчины", "женщины", "любые"}
GENDER_ALIASES = {
    "м": "мужчины", "муж": "мужчины", "мужчина": "мужчины", "мужской": "мужчины",
    "ж": "женщины", "жен": "женщины", "женщина": "женщины", "женский": "женщины",
    "любой": "любые", "любые": "любые", "м/ж": "любые", "м ж": "любые",
    "не важно": "любые", "неважно": "любые", "все": "любые",
}

WORK_FORMATS = {"вахта", "смена", "постоянная", "подработка"}

# Аббревиатуры и короткие обозначения должностей, к которым нельзя применять
# «Первая Заглавная»: СПК и ВЭШ — это не «Спк» и «Вэш».
_KEEP_UPPER_RE = re.compile(r"^[A-ZА-ЯЁ]{2,4}$")

_CITY_PREFIX_RE = re.compile(r"^(?:г\.?|гор\.?|город)\s+", re.IGNORECASE)
# Группы после запятой повторяемы, чтобы «1,234,567» разобралось целиком,
# а не превратилось в 1234 (см. _clean_number).
_NUM_RE = re.compile(r"-?\d[\d\s ]*(?:[.,]\d+)*")

# Ставка: «3 498 р/смена», «320 руб/час», «45000 ₽ в месяц».
_RATE_RE = re.compile(
    r"(\d[\d\s ]*(?:[.,]\d+)?)\s*"
    r"(?:₽|руб(?:лей|ля)?\.?|р\.?)?\s*"
    r"(?:/|\bв\b|\bза\b)?\s*"
    r"(смену|смена|смен|час(?:а|ов)?|мес(?:яц)?|month)",
    re.IGNORECASE,
)
# График сменности: 2/2, 5/2, 6/1.
_PATTERN_RE = re.compile(r"\b([1-7])\s*/\s*([1-7])\b")
_MIN_SHIFTS_RE = re.compile(r"(?:от|мин(?:имум)?\.?)\s*(\d{1,3})\s*смен", re.IGNORECASE)
_SHIFTS_LIST_RE = re.compile(r"\b(\d{1,3})(?:\s*/\s*\d{1,3})+\s*смен", re.IGNORECASE)
_HOURS_RE = re.compile(r"(?:по\s*)?(\d{1,2}(?:[.,]\d)?)\s*(?:ч\.?|час(?:а|ов)?)\b", re.IGNORECASE)

# Нижние границы правдоподобности для денег (₽). Нужны, чтобы «первые 3 смены»
# не прочиталось как ставка 3 ₽ за смену.
MIN_SHIFT_RATE = 100
MIN_HOURLY_RATE = 20

# Поля, которые заполняет исключительно parse_rate — мимо общего разбора чисел.
MONEY_FIELDS = {"shift_rate", "hourly_rate"}


# ------------------------------------------------------------------ примитивы

def clean_text(value: Any) -> Optional[str]:
    """Строка без мусора по краям или None. Пустая строка — это тоже None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = str(value).replace(" ", " ").strip()
    text = re.sub(r"[\s​]+", " ", text)
    text = text.strip(" \t\r\n-–—·•;,")
    return text or None


_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+$")


def _clean_number(raw: str) -> str:
    """Приводит найденное число к виду, понятному float().

    Запятая коварна: в «3,5» это дробная часть, а в «3,960 руб фикс.» —
    разделитель тысяч, и наивное чтение даёт ставку 3 ₽ за смену вместо 3960.
    Различаем по форме: группы ровно по три цифры после запятой — тысячи.
    """
    text = raw.replace(" ", "").replace(" ", "")
    if _THOUSANDS_RE.match(text):
        return text.replace(",", "")
    return text.replace(",", ".")


def to_int(value: Any) -> Optional[int]:
    """Аккуратный разбор чисел: «3 498», «3498 р», «3,960», 3498.0 → 3498."""
    result = to_float(value)
    return None if result is None else int(result)


def to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUM_RE.search(str(value))
    if not match:
        return None
    try:
        return float(_clean_number(match.group(0)))
    except ValueError:
        return None



def to_bool(value: Any) -> Optional[int]:
    """0/1/None. None — «в заявке не сказано», и это не то же самое, что «нет»."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in ("true", "да", "1", "yes", "есть", "y"):
        return 1
    if text in ("false", "нет", "0", "no", "n", "-", "—"):
        return 0
    return None


def title_ru(value: str) -> str:
    """«КОМПЛЕКТОВЩИК» → «Комплектовщик», «СПК» → «СПК».

    Заглавная только у первого слова: «сборщик/комплектовщик» должен остаться
    «Сборщик/комплектовщик», а не превратиться в подобие имени собственного.
    """
    parts = re.split(r"(\s+|/|-)", value)
    out = []
    first_done = False
    for part in parts:
        if not part.strip() or part in ("/", "-"):
            out.append(part)
            continue
        if _KEEP_UPPER_RE.match(part):
            out.append(part)
        elif not first_done:
            out.append(part[:1].upper() + part[1:].lower())
        else:
            out.append(part.lower())
        first_done = True
    return "".join(out)


# ---------------------------------------------------------------- канонизация

def canon_sb_policy(raw: Optional[str]) -> Optional[str]:
    """Сводит варианты политики СБ к канонической форме.

    Перенесено из app.py: «легкие» и «лёгкие», «Проверка СБ» и «проверка СБ»,
    «Без тяжких» и «без тяж.статей» — одно и то же, но в фильтре выглядели как
    разные значения. Теперь канон считается один раз при приёме, а не при
    каждом рендере страницы.
    """
    text = clean_text(raw)
    if not text:
        return None
    s = text.lower().replace("ё", "е")
    if "тяж" in s:
        return "без тяж.статей"
    if "легк" in s:
        return "лёгкие статьи допускаются"
    if "судим" in s and ("без" in s or "стро" in s):
        return "без судимостей"
    if "выбороч" in s:
        return "выборочная"
    if "провер" in s and ("сб" in s or "безопас" in s):
        return "проверка СБ"
    if s in ("нет", "без сб", "нет сб"):
        return "нет"
    return text


def canon_shift_type(raw: Any) -> Optional[str]:
    text = clean_text(raw)
    if not text:
        return None
    low = text.lower()
    if low in SHIFT_TYPES:
        return low
    return SHIFT_TYPE_ALIASES.get(low)


def canon_gender(raw: Any) -> Optional[str]:
    text = clean_text(raw)
    if not text:
        return None
    low = text.lower().replace("ё", "е")
    if low in GENDERS:
        return low
    return GENDER_ALIASES.get(low)


def canon_work_format(raw: Any) -> Optional[str]:
    text = clean_text(raw)
    if not text:
        return None
    low = text.lower()
    if "вахт" in low:
        return "вахта"
    if "подработ" in low:
        return "подработка"
    if "постоян" in low:
        return "постоянная"
    if "смен" in low:
        return "смена"
    return None


# Служебные слова в составных названиях: «Ростов-на-Дону», а не «Ростов-На-Дону».
_CITY_LOWER_PARTS = {"на", "в", "у", "при", "над", "под", "за", "им", "де"}


def title_city(value: str) -> str:
    """«САНКТ-ПЕТЕРБУРГ» → «Санкт-Петербург», «ростов-на-дону» → «Ростов-на-Дону».

    В отличие от должностей, у городов с заглавной пишется каждая
    самостоятельная часть составного названия.
    """
    parts = re.split(r"(\s+|-)", value)
    out = []
    first_done = False
    for part in parts:
        if not part.strip() or part == "-":
            out.append(part)
            continue
        if first_done and part.lower() in _CITY_LOWER_PARTS:
            out.append(part.lower())
        elif _KEEP_UPPER_RE.match(part):
            out.append(part)
        else:
            out.append(part[:1].upper() + part[1:].lower())
        first_done = True
    return "".join(out)


def canon_city(raw: Any) -> Optional[str]:
    text = clean_text(raw)
    if not text:
        return None
    text = _CITY_PREFIX_RE.sub("", text).strip()
    if not text:
        return None
    return title_city(text)


def parse_rate(raw: Any) -> Tuple[Optional[int], Optional[int]]:
    """Разбирает строку про деньги. Возвращает (за смену, за час).

    Пересчёт час↔смена намеренно НЕ делается: «320 ₽/ч» при 12-часовой смене
    не всегда даёт 3840 — бывает неоплачиваемый перерыв. Интерфейс показывает
    то, что было в заявке, и не выдумывает третье число. Всё, что не разобрано
    («первые 3 смены, далее %»), целиком лежит в shift_rate_raw.
    """
    text = clean_text(raw)
    if not text:
        return None, None

    shift_rate: Optional[int] = None
    hourly_rate: Optional[int] = None
    for match in _RATE_RE.finditer(text):
        amount = to_int(match.group(1))
        unit = match.group(2).lower()
        if amount is None:
            continue
        # Отсечка от чисел, которые деньгами быть не могут: «первые 3 смены»
        # синтаксически неотличимо от «3 ₽/смена», отличается только порядком.
        if unit.startswith("смен"):
            if amount >= MIN_SHIFT_RATE and shift_rate is None:
                shift_rate = amount
        elif unit.startswith("час"):
            if amount >= MIN_HOURLY_RATE and hourly_rate is None:
                hourly_rate = amount

    # Ставка без единицы измерения: «Ставка: 3498». По величине понятно, что
    # это смена, а не час, но угадывать не станем — кладём только если число
    # единственное и в тексте нет слова «час».
    if shift_rate is None and hourly_rate is None:
        low = text.lower()
        if "час" not in low and "мес" not in low:
            candidate = to_int(text)
            if candidate is not None and candidate >= MIN_SHIFT_RATE:
                shift_rate = candidate

    return shift_rate, hourly_rate


def parse_schedule(raw: Any) -> Tuple[Optional[str], Optional[int], Optional[float]]:
    """Из строки графика достаёт (шаблон, мин. смен, часов в смене).

    Исходная строка при этом сохраняется целиком в schedule_raw — она часто
    содержит нюансы, которые в три поля не укладываются.
    """
    text = clean_text(raw)
    if not text:
        return None, None, None

    pattern: Optional[str] = None
    match = _PATTERN_RE.search(text)
    if match:
        pattern = f"{match.group(1)}/{match.group(2)}"
    elif "вахт" in text.lower():
        pattern = "вахта"

    min_shifts = None
    match = _MIN_SHIFTS_RE.search(text)
    if match:
        min_shifts = to_int(match.group(1))
    else:
        # «20/30/45 смен» — минимальный срок это первое число.
        match = _SHIFTS_LIST_RE.search(text)
        if match:
            min_shifts = to_int(match.group(1))

    hours = None
    match = _HOURS_RE.search(text)
    if match:
        hours = to_float(match.group(1))
        # «вахта от 30 смен» не должна прочитаться как 30 часов
        if hours is not None and not (1 <= hours <= 24):
            hours = None

    return pattern, min_shifts, hours


# ------------------------------------------------------------------ нормализатор

class Normalizer:
    """Приводит один распознанный набор полей к канону.

    :param conn: соединение с базой — из него читаются справочники, в него же
        уходят незнакомые варианты написания (очередь на подтверждение).
    :param learn: писать ли встреченные варианты в очередь. Выключается в
        тестах и при повторной нормализации задним числом.
    """

    def __init__(self, conn: sqlite3.Connection, learn: bool = True):
        self.conn = conn
        self.learn = learn
        self._dicts = dicts.load_all(conn)

    def reload(self) -> None:
        self._dicts = dicts.load_all(self.conn)

    def _lookup(self, kind: str, value: Optional[str], proposed: Optional[str] = None) -> Optional[str]:
        """Ищет канон в справочнике.

        Нет в справочнике — возвращаем значение как есть и ставим его в
        очередь. Молча приклеивать к похожему нельзя: «Комплектовщик СПК» и
        «Комплектовщик» могут быть разными позициями с разной ставкой.
        """
        if not value:
            return None
        key = norm_key(value)
        if not key:
            return None
        canonical = self._dicts.get(kind, {}).get(key)
        fallback = proposed or value
        if canonical:
            if self.learn:
                dicts.record_seen(self.conn, kind, key, canonical)
            return canonical
        if self.learn:
            dicts.record_seen(self.conn, kind, key, fallback)
        return fallback

    def normalize(self, vac: Dict[str, Any]) -> Dict[str, Any]:
        """LLM-выхлоп (плюс overrides источника) → набор полей позиции."""
        out: Dict[str, Any] = {name: None for name in DATA_FIELDS}

        # --- исходники: то, что видно в панели «Как пришло»
        out["counterparty_raw"] = clean_text(vac.get("counterparty"))
        out["vacancy_name_raw"] = clean_text(vac.get("vacancy_name"))
        out["city_raw"] = clean_text(vac.get("city"))
        out["region_raw"] = clean_text(vac.get("region"))
        out["schedule_raw"] = clean_text(vac.get("schedule"))
        out["shift_rate_raw"] = clean_text(vac.get("shift_rate"))
        out["requirements_raw"] = clean_text(vac.get("requirements"))

        # --- справочники
        out["counterparty"] = self._lookup(dicts.KIND_COUNTERPARTY, out["counterparty_raw"])
        job_title = out["vacancy_name_raw"]
        out["vacancy_name"] = self._lookup(
            dicts.KIND_JOB_TITLE, job_title, title_ru(job_title) if job_title else None
        )
        city = canon_city(out["city_raw"])
        out["city"] = self._lookup(dicts.KIND_CITY, city, city)
        out["vacancy_category"] = self._lookup(
            dicts.KIND_VACANCY_CATEGORY, clean_text(vac.get("vacancy_category"))
        )

        # Регион: сперва то, что реально написано в заявке. Если не написано —
        # подставляем из справочника «город → регион», и только из
        # подтверждённой его части. Никаких «Москва, значит Московская
        # область» (кстати, неверного) на стороне модели.
        region = self._lookup(dicts.KIND_REGION, out["region_raw"]) if out["region_raw"] else None
        if not region and out["city"]:
            region = self._dicts.get(dicts.KIND_CITY_REGION, {}).get(norm_key(out["city"]))
        out["region"] = region

        # --- перечисления
        out["work_format"] = canon_work_format(vac.get("work_format"))
        out["shift_type"] = canon_shift_type(vac.get("shift_type"))
        out["gender"] = canon_gender(vac.get("gender"))
        out["sb_policy"] = canon_sb_policy(vac.get("sb_policy"))
        out["citizenship_requirements"] = self._lookup(
            dicts.KIND_CITIZENSHIP, clean_text(vac.get("citizenship_requirements"))
        )

        # --- график
        schedule_source = out["schedule_raw"] or clean_text(vac.get("work_format"))
        pattern, min_shifts, hours = parse_schedule(schedule_source)
        out["schedule"] = out["schedule_raw"]
        out["work_pattern"] = pattern
        out["min_shifts"] = to_int(vac.get("min_shifts"))
        if out["min_shifts"] is None:
            out["min_shifts"] = min_shifts
        out["shift_hours"] = to_float(vac.get("shift_hours"))
        if out["shift_hours"] is None:
            out["shift_hours"] = hours
        if out["work_format"] is None and pattern == "вахта":
            out["work_format"] = "вахта"

        # --- деньги
        shift_rate, hourly_rate = parse_rate(vac.get("shift_rate"))
        out["shift_rate"] = shift_rate
        out["hourly_rate"] = to_int(vac.get("hourly_rate")) or hourly_rate

        # --- остальные текстовые/числовые поля как есть
        for name in TEXT_FIELDS:
            if out.get(name) is None and name in vac:
                out[name] = clean_text(vac.get(name))
        for name in INT_FIELDS:
            # Денежные поля заполняет только parse_rate: у неё есть отсечка от
            # бессмысленных величин, а этот общий проход её обходил — так в
            # реестр приехали ставки 10 и 20 ₽ за смену из старых строк, где в
            # колонке лежала вовсе не ставка.
            if name in MONEY_FIELDS:
                continue
            if out.get(name) is None:
                out[name] = to_int(vac.get(name))
        for name in REAL_FIELDS:
            if out.get(name) is None:
                out[name] = to_float(vac.get(name))
        for name in BOOL_FIELDS:
            out[name] = to_bool(vac.get(name))

        out["need_total"] = self._need_total(vac, out)
        return out

    @staticmethod
    def _need_total(vac: Dict[str, Any], out: Dict[str, Any]) -> Optional[int]:
        """Считает общую потребность.

        Раньше при полностью пустой потребности получался 0 — то есть заявка
        выглядела так, будто людей не требуется вовсе. Теперь: явное значение
        уважаем, иначе суммируем то, что есть, а если нет ничего — оставляем
        пустым.
        """
        explicit = to_int(vac.get("need_total"))
        if explicit is not None:
            return explicit
        parts = [out.get("need_men"), out.get("need_women"), out.get("need_couples")]
        known = [p for p in parts if p is not None]
        if not known:
            return None
        return sum(known)


def fingerprint(fields: Dict[str, Any]) -> str:
    """Ключ склейки одной и той же позиции между прогонами.

    Это НЕ идентификатор заявки (им служит ELT-…, см. registry/ids.py), а
    только способ понять, что вчерашняя строка и сегодняшняя — про одну
    позицию. Считается по уже нормализованным полям, поэтому «Москва» и
    «москва» больше не дают двух разных записей.
    """
    import hashlib

    from registry.models import FINGERPRINT_FIELDS

    parts = [norm_key(fields.get(name)) for name in FINGERPRINT_FIELDS]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:12]
