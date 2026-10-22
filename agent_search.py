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

import re

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import payouts
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


# Значения, которыми модель затыкает поле, когда кандидат ничего не сказал.
# Инструменту разрешено передавать null, но по дороге через query-строку он
# становится текстом «null», а сверх того агент подставляет то «без разницы»,
# то «не знаю». Всё это — не данные, а признак «кандидат не ответил», и
# считать их значением значит искать вакансию «оператор null».
_PLACEHOLDERS = {
    "", "null", "none", "nil", "undefined", "n/a", "-",
    "не знаю", "незнаю", "без разницы", "безразницы", "любая", "любой", "любое",
    "не важно", "неважно", "всё равно", "все равно", "что есть", "чё есть",
    "не указано", "не назвал", "неизвестно", "работа", "вахта", "вакансия",
}


def _said(value: Optional[str]) -> str:
    """Значение, названное кандидатом, или пустая строка."""
    cleaned = _norm(value)
    return "" if cleaned in _PLACEHOLDERS else cleaned


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


# ------------------------------------------------------------------- запрос

def _search_words(conn, profession: str) -> Tuple[List[Any], Dict[Any, int]]:
    """Выборка по фразе целиком и по каждому её слову отдельно.

    Второе значение — сколько слов запроса зацепила позиция; уходит в rank().
    Одно слово в запросе — один поход в SQL и пустой вес, поведение прежнее.
    """
    words = [w for w in _norm(profession).split() if len(w) > 3]
    queries = [profession] if len(words) < 2 else [profession] + words

    rows_by_id: Dict[Any, Any] = {}
    hits: Dict[Any, int] = {}
    for query in queries:
        found, _total = rq.search(
            conn,
            {"is_active": "true", "q": query},
            sort="last_seen_at",
            order="desc",
            page=1,
            per_page=FETCH_LIMIT,
        )
        # Совпадение по фразе целиком весит как все её слова: точное попадание
        # обязано остаться выше позиции, зацепившейся одним общим словом.
        weight = len(words) if query == profession and len(words) > 1 else 1
        for row in found:
            pid = row["position_id"]
            rows_by_id.setdefault(pid, row)
            hits[pid] = hits.get(pid, 0) + weight
    return list(rows_by_id.values()), hits


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


# Поле schedule заполняется руками из текста заявки, и там лежит что угодно:
# «РП – 6/1,7/0 по 12 ч. 8:00 – 20:00 / 20:00 - 8:00 МП - 3/2, 4/3, 6/1»,
# «12 ч. (11 раб. + 1 обед), с 8:00-20:00, 6/1, 4/1, 5/2, вахта от 30 смен».
# Рекрутёр это читает глазами, а агент — вслух, и получается «эр-пэ шесть один
# семь ноль по двенадцать че». Поэтому строку не зачитываем, а разбираем на
# факты и собираем заново. Что не распозналось — молча выбрасываем: лучше
# сказать меньше, чем произнести служебный мусор.
_SHIFT_RE = re.compile(r"\b([2-7])\s*/\s*([0-3])\b")
_HOURS_RE = re.compile(r"\b(\d{1,2})\s*ч", re.IGNORECASE)
_DAY_RE = re.compile(r"дневн|день", re.IGNORECASE)
_NIGHT_RE = re.compile(r"ночн|ночь", re.IGNORECASE)


def _shift_pattern_text(sched: str) -> str:
    """«6/1, 7/0» → «6 через 1 или без выходных».

    Больше двух вариантов вслух не называем: в заявках их бывает три-четыре,
    и перечисление превращается в считалку. Кандидату важно понять порядок
    работы, точный график ему назовёт менеджер объекта при согласовании.
    """
    seen: List[str] = []
    for work, rest in _SHIFT_RE.findall(sched or ""):
        item = "без выходных" if rest == "0" else f"{work} через {rest}"
        if item not in seen:
            seen.append(item)
    if not seen:
        return ""
    return " или ".join(seen[:2])


def _schedule_text(sched: str, hours: Any, min_shifts: Optional[int]) -> str:
    sched = sched or ""
    parts = []

    pattern = _shift_pattern_text(sched)
    if pattern:
        parts.append(pattern)

    # Колонка надёжнее текста: в тексте рядом с часами смены попадаются часы
    # обеда («11 раб. + 1 обед») и время начала.
    if not hours:
        found = _HOURS_RE.search(sched)
        if found and 4 <= int(found.group(1)) <= 24:
            hours = int(found.group(1))
    if hours:
        # Дробные часы в базе есть (12.5), и точка в них уезжает в синтез речи
        # как «двенадцать точка пять». Половину проговариваем словом.
        whole = int(hours)
        if abs(float(hours) - whole - 0.5) < 0.01:
            parts.append(f"смена {whole} с половиной часов")
        elif float(hours).is_integer():
            parts.append(f"смена {whole} часов")
        else:
            parts.append(f"смена около {whole} часов")

    day, night = _DAY_RE.search(sched), _NIGHT_RE.search(sched)
    if day and night:
        parts.append("смены дневные и ночные")
    elif night:
        parts.append("смены ночные")
    elif day:
        parts.append("смены дневные")

    # Длину вахты сюда не добавляем, даже когда в строке только она: min_shifts
    # уходит кандидату отдельным полем, и агент назовёт её сам. Дублировать —
    # значит услышать «вахта от двадцати смен» дважды в одной реплике.
    # Ничего не разобрали — пусть будет честное «не указан»: агент такие поля
    # вслух пропускает.
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

def priorities(conn, today: Optional[str] = None) -> List[tuple]:
    """Приоритетные объекты на сегодня: список (шаблон, уровень).

    Заказчик называет «горящие» объекты на неделю, и в выдаче они должны стоять
    выше прочих — но только пока срок не вышел. Протухший приоритет опаснее
    отсутствующего: он продолжает гнать людей на объект, который давно
    укомплектован, и заметить это по выдаче нельзя.
    """
    import datetime as _dt

    day = today or _dt.date.today().isoformat()
    try:
        rows = conn.execute(
            """SELECT pattern, tier FROM priority_objects
               WHERE valid_from <= ? AND valid_to >= ?""", (day, day)).fetchall()
    except Exception:  # noqa: BLE001 — таблицы может не быть на старой базе
        return []
    return [(_norm(r["pattern"]), int(r["tier"] or 1)) for r in rows if r["pattern"]]


def tier_of(row, marks: List[tuple]) -> int:
    """Уровень приоритета позиции. Больше — хуже; 9 значит «не в приоритете».

    Сопоставляем по названию объекта и контрагента, а не по идентификатору
    позиции: позиции заводятся и закрываются каждую неделю, а объект остаётся
    тем же. Заказчик называет объект словами — так и ищем.
    """
    if not marks:
        return 9
    haystack = " ".join(_norm(row[field] or "") for field in
                        ("object_name", "counterparty", "vacancy_name"))
    best = 9
    for pattern, tier in marks:
        if pattern and pattern in haystack:
            best = min(best, tier)
    return best


def rank(rows: List[Any], hits: Optional[Dict[Any, int]] = None,
         payouts: Optional[Dict[Any, int]] = None,
         tiers: Optional[Dict[Any, int]] = None) -> List[Any]:
    """Порядок выдачи: соответствие, потом наш заработок, потом ставка кандидата.

    Сортировка устойчивая, а из SQL строки приходят по last_seen_at DESC, так
    что при равенстве всего прочего свежая позиция окажется впереди сама собой.

    Полнота совпадения по словам запроса стоит первой и это не обсуждается:
    иначе на «оператор станка» первой встанет самая выгодная нам позиция,
    зацепившаяся одним словом, а точное совпадение уедет вниз. Плохо
    подобранная вакансия не приносит денег — она приносит отказ, и сегодняшние
    vacancy_mismatch в журнале обзвона это подтверждают.

    Приоритет заказчика идёт вторым, сразу за совпадением. Выше совпадения его
    ставить нельзя: «горящий» объект, зацепившийся одним общим словом, увёл бы
    выдачу от того, что человек действительно просил, — а плохо подобранная
    вакансия приносит не людей, а отказ. Зато среди одинаково подходящих
    вакансий горящая обязана быть первой, и наш заработок на это не влияет:
    укомплектовать объект в срок важнее лишних пяти тысяч за кандидата.

    Наш заработок идёт третьим: среди одинаково подходящих и одинаково
    приоритетных выше встаёт та, где у нас действует акция. Позиции без
    заведённой ставки получают ноль и опускаются — знание того, что мы
    заработаем сорок тысяч, весомее незнания.

    Ставка кандидата осталась последней: она решает при прочих равных.
    """
    hits = hits or {}
    payouts = payouts or {}
    tiers = tiers or {}
    return sorted(
        rows,
        key=lambda r: (
            -hits.get(r["position_id"], 0),
            tiers.get(r["position_id"], 9),
            -payouts.get(r["position_id"], 0),
            r["shift_rate"] is None,
            -(r["shift_rate"] or 0),
        ),
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
        profession: str = "",
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

    # Заглушки от модели приравниваем к «не сказано» по всем текстовым полям:
    # иначе «null» в городе отсечёт все позиции, а «без разницы» в профессии
    # уйдёт в полнотекстовый поиск как слово.
    profession = _said(profession)
    city, region = _said(city), _said(region)
    citizenship, gender = _said(citizenship), _said(gender)

    # Пустая профессия — это не ошибка вызова, а нормальный ход разговора:
    # кандидат сказал «мне без разницы». Раньше параметр был обязательным, и
    # голосовой агент, не желая оставлять обязательное поле пустым, подставлял
    # профессию сам — в тестовых звонках стабильно «грузчик», которого кандидат
    # ни разу не называл. Отвечаем честной причиной: агент по ней спрашивает,
    # а не выдумывает.
    if not _norm(profession):
        return {
            "found": 0,
            "total_matched": 0,
            "vacancies": [],
            "relaxed": [],
            "no_match_reason": "профессия не названа, спроси кандидата",
        }

    city_regions = dicts.load(conn, dicts.KIND_CITY_REGION)

    # В SQL уходит только то, что там дёшево и безопасно: активность и
    # полнотекстовый поиск по профессии. Остальное — в Python, потому что
    # LIKE в SQLite регистрозависим для кириллицы («грузчик» и «Грузчик» дают
    # разные выборки), а числовые фильтры движка молча отсекают позиции с NULL.
    #
    # Слова запроса queries.fts_query() склеивает через AND, и для экрана
    # рекрутёра это правильно: он видит выдачу и уточняет запрос сам. Для
    # телефонного разговора то же поведение — тупик, потому что переспросить
    # некому: «оператор станка» не находит НИ ОДНОГО оператора, хотя их в базе
    # десяток, достаточно чтобы слово «станка» не встретилось в тексте заявки.
    # Чем точнее кандидат назвал профессию, тем вероятнее он слышал «ничего не
    # нашлось». Поэтому ищем ещё и по каждому слову отдельно, а число совпавших
    # слов запоминаем: оно поднимает точные попадания над случайными.
    rows, hits = _search_words(conn, profession)

    fresh_after = (now - timedelta(days=MAX_AGE_DAYS)).isoformat()

    # Регион кандидата приводим к тому же написанию, в каком он лежит у
    # позиции. Без этого «Московская область» не совпадала с «Москва и
    # область» — а под этим названием в реестре стоят 133 активные позиции из
    # 177, то есть почти всё. Человек называл свою область и слышал, что
    # ничего нет.
    want_city = _norm(city)
    want_region = _norm(geo.region_title(region)) if region else ""

    # Назвал только город — регион берём из него же. Для вахты это и значит
    # «рядом»: объект в соседнем городе той же области человеку подходит, а
    # совпадения по названию города у него с этим объектом нет.
    if not want_region and want_city:
        want_region = _norm(geo.resolve_region(
            "", geo.normalize_city(city), city_regions))

    # Полнотекстовый индекс включает весь исходный текст заявки, поэтому
    # совпадение ещё не значит совпадение должности. Держим постфильтр по
    # названию вакансии.
    prof_words = [w for w in _norm(profession).split() if len(w) > 3]

    checks = {
        # Отсечение окончания здесь то же самое, что в полнотекстовом запросе:
        # раньше от слова отрезался ровно один символ, и «упаковщиком» не
        # проходило постфильтр даже тогда, когда индекс позицию нашёл.
        "profession": lambda r: (
            not prof_words
            or any(rq.loose_root(w) in _norm(r["vacancy_name"]) for w in prof_words)
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
        # Позиции без ставки до сюда не доходят — их отсекает базовый отбор,
        # поэтому проверять r["shift_rate"] на None здесь больше не нужно.
        "rate": lambda r: not min_rate or r["shift_rate"] >= min_rate,
    }

    # Базовые условия, которые не участвуют в объяснении причины: набор закрыт,
    # позиция протухла или в ней нет ставки.
    #
    # Про ставку. Вакансию без неё агенту отдавать нельзя: назвать деньги он не
    # может, выдумывать ему запрещено, и разговор выходит такой — «есть позиция
    # комплектовщика, проживание есть, питание бесплатное», а человек в ответ:
    # «Я что, за питание и жильё работать поеду?» Двое при этом успели
    # согласиться, не зная ни ставки, ни города. Отсутствие вакансии честнее
    # предложения, которое нечем описать.
    pool = [
        r for r in rows
        if (r["need_total"] or 0) > 0
        and (r["last_seen_at"] or "") >= fresh_after
        and r["shift_rate"]
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

    # Ставки читаем после всех отсечений: считать заработок по позициям,
    # которые кандидату всё равно не подойдут, незачем.
    rules = payouts.load_rules(conn, now.date().isoformat())
    payout_by_id = {r["position_id"]: payouts.payout_for(r, rules) for r in pool}
    marks = priorities(conn, now.date().isoformat())
    tier_by_id = {r["position_id"]: tier_of(r, marks) for r in pool}

    ranked = rank(pool, hits, payout_by_id, tier_by_id)
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
