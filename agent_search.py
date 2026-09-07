"""Подбор вакансий для голосового агента.

Отдельный модуль, а не расширение registry/queries.py: тот обслуживает экраны
рекрутёра, и менять его семантику ради телефонного бота — верный способ уронить
реестр. Отсюда мы queries.py только вызываем.

Разница с экранными фильтрами одна, но принципиальная — обращение с пустотой.
В реестре пустое поле значит «парсер не извлёк», а не «не подходит»: правило
всего проекта — ничего не додумывать. Рекрутёру это неважно, он видит карточку
целиком и сам решает. Агенту, который зачитывает вариант вслух, разница стоит
дорого в обе стороны: отсечь позицию по неизвестному признаку — потерять живую
вакансию, а выдать её как подходящую — пообещать человеку то, чего никто не
проверял, и получить сорванный выезд.

Компромисс здесь такой: неизвестное **никогда не отсеивает**, но и никогда не
утверждается. В ответ уходит честное "неизвестно", а ранжирование опускает такие
позиции вниз — они всплывут, только если ничего определённого не нашлось.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from registry import dictionaries as dicts
from registry import geo
from registry import queries as rq

# Сборку карточки position_row() намеренно НЕ используем: она тянет расчёт
# гонорара рекрутёра (rec) и обход базы знаний. Кандидату это показывать нельзя,
# а надёжнее не «забыть выкинуть поле», а не получать его вовсе. Берём только
# нормализаторы.
from navigator_api import (
    housing_block,
    meals_block,
    med_state,
    normalize_gender,
    parse_citizenship,
)

# Позицию, которой давно не было в выгрузке источника, кандидату не предлагаем.
# is_active для этого не годится: это флаг «встретилась в последнем прогоне», и
# у источника, который сам перестал обновляться, все строки так и остаются
# активными. На живых данных так висят позиции месячной давности.
MAX_AGE_DAYS = 21

# Союзы поверх стран: в требованиях пишут то «ЕАЭС», то перечисление.
EAEU = {"РФ", "РБ", "КЗ", "АРМ", "КРГ"}
CIS = EAEU | {"УЗБ", "ТДЖ"}

# Сколько строк вытаскиваем из SQL до фильтрации в Python. Активных позиций
# меньше сотни, так что берём с запасом и не городим пагинацию.
FETCH_LIMIT = 500


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


# ----------------------------------------------------------------- отсечения

def citizenship_ok(cits: List[str], want: Optional[str]) -> bool:
    """Проходит ли кандидат по гражданству."""
    if not want:
        return True
    if not cits:
        return True  # требований не извлекли — не отсекаем
    want = want.upper()
    if want in cits:
        return True
    if "ЕАЭС" in cits and want in EAEU:
        return True
    if "СНГ" in cits and want in CIS:
        return True
    return False


def age_ok(age_from: Optional[int], age_to: Optional[int], want: Optional[int]) -> bool:
    if not want:
        return True
    if age_from is not None and want < age_from:
        return False
    if age_to is not None and want > age_to:
        return False
    return True


def gender_ok(pos_gender: str, want: Optional[str]) -> bool:
    """pos_gender — уже нормализованный: мужчины / женщины / любые / пусто."""
    if not want or not pos_gender or pos_gender == "любые":
        return True
    want = _norm(want)
    if want.startswith("муж"):
        return pos_gender == "мужчины"
    if want.startswith("жен"):
        return pos_gender == "женщины"
    return True


def medical_ok(med: str, has_book: Optional[bool]) -> bool:
    """med: no / need / arranged / na.

    Отсекаем единственный однозначный случай — книжка нужна, а её нет и
    оформлять её работодатель не берётся.
    """
    if has_book is not False:
        return True
    return med != "need"


def geo_ok(pos_city: str, pos_region: str, want_city: str, want_region: str) -> bool:
    """Город ИЛИ регион — объединением, а не пересечением.

    У четверти активных позиций города нет вовсе. Пересечение выкинуло бы их
    все; в интерфейсе рекрутёра ровно поэтому тоже объединение
    (templates/navigator.html, passGeo).
    """
    if not want_city and not want_region:
        return True
    if not pos_city and not pos_region:
        return True  # позиция без географии — не отсекаем
    for want in (want_city, want_region):
        if not want:
            continue
        if pos_city and want in pos_city:
            return True
        if pos_region and want in pos_region:
            return True
    return False


# ---------------------------------------------------------------- проекция

def _pay_text(rate: Optional[int]) -> str:
    """Готовая к произношению строка про деньги.

    Число дублируется текстом сознательно: считать модель не должна ничего.
    Именно на пересчётах (за смену → за месяц, «примерно столько-то») LLM и
    начинает выдумывать суммы, которых в карточке нет.
    """
    if not rate:
        return "ставка не указана"
    return f"{rate} рублей за смену на руки"


def _schedule_text(sched: str, hours: Any, min_shifts: Optional[int]) -> str:
    parts = []
    if sched:
        parts.append(sched)
    elif min_shifts:
        parts.append(f"вахта от {min_shifts} смен")
    if hours:
        parts.append(f"смена {hours} часов")
    return ", ".join(parts) or "график не указан"


def _housing_text(block: Dict[str, Any]) -> str:
    """Только факт и цена, без описания из карточки.

    В housing_conditions лежит свободный текст, и у части позиций это полный
    адрес общежития («г.о. Подольск, д. Коледино, д. Бережки, 2г»). Зачитывать
    такое вслух незачем, а отдавать агенту адрес объекта — тем более: это
    внутренняя информация, кандидат получает её от координатора после согласия.
    Подробности про этажи и количество мест в комнате уходят в Telegram
    карточкой, а не в телефонный разговор.

    У блока меняется набор ключей: при st='na' других ключей нет вовсе,
    поэтому только .get().
    """
    state = block.get("st")
    if state == "no":
        return "не предоставляется"
    if state != "yes":
        return "неизвестно"
    return {
        "free": "есть, бесплатно",
        "deduct": "есть, с удержанием из зарплаты",
        "paid": "есть, платно",
    }.get(block.get("cost"), "есть")


def _meals_text(block: Dict[str, Any]) -> str:
    state = block.get("st")
    return {
        "free": "бесплатное",
        "paid": "платное",
        "deduct": "с удержанием",
        "yes": "есть",
        "no": "не предоставляется",
    }.get(state, "неизвестно")


def _age_text(age_from: Optional[int], age_to: Optional[int]) -> str:
    if age_from and age_to:
        return f"от {age_from} до {age_to} лет"
    if age_to:
        return f"до {age_to} лет"
    if age_from:
        return f"от {age_from} лет"
    return "без ограничений по возрасту"


MED_TEXT = {
    "no": "не нужна",
    "need": "нужна",
    "arranged": "нужна, работодатель помогает оформить",
    "na": "неизвестно",
}


def project(row, city_regions: Dict[str, str]) -> Dict[str, Any]:
    """Строка реестра → то немногое, что можно назвать кандидату вслух.

    Белый список полей, а не «взять карточку и убрать лишнее»: контрагент,
    объект, адрес, ставка рекрутёра и сырой текст заявки — внутренняя кухня.
    """
    city = geo.normalize_city(row["city"])
    region = geo.resolve_region(row["region"], city, city_regions)
    rate = row["shift_rate"]
    hours = row["shift_hours"]
    if hours is not None and float(hours).is_integer():
        hours = int(hours)

    return {
        "id": row["position_id"],
        "profession": row["vacancy_name"] or "",
        "city": city,
        "region": region,
        "rate_per_shift": rate,
        "pay_text": _pay_text(rate),
        "min_shifts": row["min_shifts"],
        "schedule_text": _schedule_text(row["schedule"] or "", hours, row["min_shifts"]),
        "housing": _housing_text(housing_block(row)),
        "meals": _meals_text(meals_block(row)),
        "citizenship": parse_citizenship(row["citizenship_requirements"]),
        "age_limit": _age_text(row["age_from"], row["age_to"]),
        "gender": normalize_gender(row["gender"]) or "любые",
        "medical_book": MED_TEXT.get(med_state(row), "неизвестно"),
    }


# ------------------------------------------------------------------- подбор

def rank(rows: List[Any]) -> List[Any]:
    """Сначала деньги, известные — выше неизвестных.

    Сортировка устойчивая, а из SQL строки приходят по last_seen_at DESC, так
    что при равных ставках свежая позиция окажется впереди сама собой.
    """
    return sorted(
        rows,
        key=lambda r: (r["shift_rate"] is None, -(r["shift_rate"] or 0)),
    )


# Порядок важен: причина «не нашлось» называется по первому отсечению, которое
# обнулило выборку, поэтому от самого частого к самому редкому.
_STAGES = [
    ("profession", "по профессии ничего не нашлось"),
    ("geo", "нет вакансий в этом городе или регионе"),
    ("citizenship", "нет вакансий под это гражданство"),
    ("age", "нет вакансий под этот возраст"),
    ("gender", "нет вакансий под этот пол"),
    ("medical", "во всех подходящих вакансиях нужна медкнижка"),
    ("rate", "нет вакансий с такой ставкой"),
]


def search(
        conn,
        *,
        profession: str,
        city: str = "",
        region: str = "",
        citizenship: str = "",
        age: Optional[int] = None,
        gender: str = "",
        has_medical_book: Optional[bool] = None,
        min_rate: Optional[int] = None,
        limit: int = 3,
        now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Подбор под кандидата. Возвращает готовое тело ответа ручки."""
    now = now or datetime.now()
    city_regions = dicts.load(conn, dicts.KIND_CITY_REGION)

    # В SQL уходит только то, что там дёшево и безопасно: активность и
    # полнотекстовый поиск по профессии. Остальное — в Python, потому что
    # LIKE в SQLite регистрозависим для кириллицы («грузчик» и «Грузчик» дают
    # разные выборки), а числовые фильтры движка молча отсекают позиции с NULL.
    rows, _total = rq.search(
        conn,
        {"is_active": "true", "q": profession},
        sort="last_seen_at",
        order="desc",
        page=1,
        per_page=FETCH_LIMIT,
    )

    fresh_after = (now - timedelta(days=MAX_AGE_DAYS)).isoformat()
    want_city, want_region = _norm(city), _norm(region)

    # Полнотекстовый индекс включает весь исходный текст заявки, поэтому
    # совпадение ещё не значит совпадение должности. Держим постфильтр по
    # названию вакансии.
    prof_words = [w for w in _norm(profession).split() if len(w) > 3]

    checks = {
        "profession": lambda r: (
            not prof_words
            or any(w[:-1] in _norm(r["vacancy_name"]) for w in prof_words)
        ),
        "geo": lambda r: geo_ok(
            _norm(geo.normalize_city(r["city"])),
            _norm(geo.resolve_region(r["region"], geo.normalize_city(r["city"]), city_regions)),
            want_city, want_region,
        ),
        "citizenship": lambda r: citizenship_ok(
            parse_citizenship(r["citizenship_requirements"]), citizenship
        ),
        "age": lambda r: age_ok(r["age_from"], r["age_to"], age),
        "gender": lambda r: gender_ok(normalize_gender(r["gender"]), gender),
        "medical": lambda r: medical_ok(med_state(r), has_medical_book),
        "rate": lambda r: not min_rate or r["shift_rate"] is None or r["shift_rate"] >= min_rate,
    }

    # Базовые условия, которые не участвуют в объяснении причины: набор закрыт
    # или позиция протухла.
    pool = [
        r for r in rows
        if (r["need_total"] or 0) > 0 and (r["last_seen_at"] or "") >= fresh_after
    ]

    reason = None
    if not pool:
        # Полнотекстовый поиск не дал ничего, либо всё найденное протухло или
        # уже укомплектовано. Для агента это тот же исход, что и отсечение по
        # фильтру, и молчать о причине нельзя: без неё он скажет «ничего нет»
        # там, где надо предложить другую профессию.
        reason = "по профессии ничего не нашлось"
    for key, message in _STAGES:
        if not pool:
            break
        survived = [r for r in pool if checks[key](r)]
        if not survived:
            reason = message
            pool = []
            break
        pool = survived

    ranked = rank(pool)
    limit = max(1, min(limit, 5))
    return {
        "found": min(len(ranked), limit),
        "total_matched": len(ranked),
        "vacancies": [project(r, city_regions) for r in ranked[:limit]],
        # Задел под деградацию фильтров из ТЗ: когда она появится, сюда лягут
        # снятые условия, чтобы агент проговорил компромисс вслух.
        "relaxed": [],
        "no_match_reason": reason if not ranked else None,
    }
