"""Данные реестра в модели экрана «Навигатор».

Фронт /navigator описан макетом: у позиции там есть блоки «Проживание»,
«Питание», «Проезд», «Выплаты», «Удержания», «Материалы» и слой мотивации
рекрутера. Реестр хранит это иначе (и часть — не хранит вовсе), поэтому весь
перевод собран здесь, в одном месте, а не размазан по шаблону.

Главное правило: ничего не додумываем. Если поля в заявке нет — оно уезжает
на фронт как «не указано» (st='na'), а не как ноль, «нет» или значение по
умолчанию. Пустая ставка рекрутера — «не задана», а не процент от ставки
кандидата.

Чего в реестре сегодня нет и что поэтому всегда 'na':
    выплаты (периодичность, аванс), фото и материалы, тестирование,
    человек в комнате, постельное бельё, транспорт до объекта.
"""

import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from registry import geo, rates
from registry.labels import SOURCE_TITLES

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# Обязательные поля заявки. Ровно те, которые источник реально присылает и
# парсер реально извлекает: по ним имеет смысл запрашивать заказчика.
REQUIRED_FIELDS = {
    "cits": "Гражданство",
    "house": "Проживание",
    "meals": "Питание",
    "rate": "Ставка кандидата",
    "sched": "График",
    "need": "Потребность",
}

# Первая строка запроса. Список позиций и вопросы по пробелам собираются из
# самих заявок, поэтому настраивается ровно то, что не вычисляется, — просьба.
DEFAULT_TEMPLATE = (
    "Уточните, пожалуйста, по вакансиям ниже — без этих данных "
    "мы не можем подобрать людей."
)

# Расписание прогонов по источникам — из JOBS в app.py. Держим здесь копией
# строкой, потому что на экране это подпись, а не управление планировщиком.
SOURCE_SCHEDULE = {
    "vahtapro": "09:30, 13:00",
    "aaaplus": "09:30",
    "kpk": "12:00",
    "yappi": "12:00",
    "marketstaff": "12:00",
    "ametist": "13:30",
    "manual": "вручную",
}

SOURCE_KIND = {
    "vahtapro": "Telegram-чат",
    "aaaplus": "Telegram-чат",
    "ametist": "Telegram-чат",
    "kpk": "Google-таблица",
    "yappi": "SeaTable",
    "marketstaff": "Google-таблица",
    "manual": "Ручной ввод",
}


# --------------------------------------------------------------- мелкие хелперы

def _s(value) -> str:
    return "" if value is None else str(value).strip()


def _col(row, name: str):
    """Колонка строки, которой в выборке может не быть.

    Позиция приходит и из основного запроса (с присоединённой папкой проекта),
    и из выборок попроще — например из тестов. Отсутствие колонки не должно
    ронять сборку карточки.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _i(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _f(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _b(value) -> Optional[bool]:
    """NULL остаётся None: «не указано» и «нет» — разные вещи."""
    if value is None or value == "":
        return None
    return bool(value)


def _parse_dt(value) -> Optional[datetime]:
    text = _s(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def fmt_seen(value, now: Optional[datetime] = None) -> str:
    """Отметка времени в том виде, в каком её читает рекрутер."""
    moment = _parse_dt(value)
    if not moment:
        return ""
    now = now or datetime.now()
    today = now.date()
    day = moment.date()
    clock = moment.strftime("%H:%M")
    midnight = clock == "00:00"
    if day == today:
        return "сегодня" if midnight else f"сегодня {clock}"
    if day == today - timedelta(days=1):
        return "вчера" if midnight else f"вчера {clock}"
    label = f"{moment.day} {MONTHS_GEN[moment.month - 1]}"
    if moment.year != now.year:
        label += f" {moment.year}"
    return label


# ------------------------------------------------------------------- разбор поля

_CIT_MAP = [
    (("рф", "россия", "российское", "россияне"), "РФ"),
    (("рб", "беларус", "белорус"), "РБ"),
    (("кзх", "кз", "казах"), "КЗ"),
    (("крг", "кир", "кыргыз"), "КРГ"),
    (("еаэс",), "ЕАЭС"),
    (("снг",), "СНГ"),
    (("узб",), "УЗБ"),
    (("тадж",), "ТДЖ"),
    (("арм",), "АРМ"),
]


def parse_citizenship(raw: Optional[str]) -> List[str]:
    """«РФ/РБ (Кавказ по согласованию)» → ['РФ', 'РБ'].

    Скобки и оговорки не выбрасываем: они остаются в исходном значении, которое
    видно в карточке «как пришло». Здесь нужны только коды для фильтра.
    """
    text = _s(raw)
    if not text:
        return []
    text = re.sub(r"\(.*?\)", " ", text).lower().replace("ё", "е")
    out: List[str] = []
    for chunk in re.split(r"[\/,;+]|\sи\s|\sили\s", text):
        chunk = chunk.strip(" .")
        if not chunk:
            continue
        for needles, code in _CIT_MAP:
            if any(needle in chunk for needle in needles):
                if code not in out:
                    out.append(code)
                break
    return out


def normalize_gender(raw: Optional[str]) -> str:
    text = _s(raw).lower().replace("ё", "е")
    if not text:
        return ""
    has_m = "муж" in text
    has_w = "жен" in text
    if text in ("любые", "любой", "не важно", "неважно"):
        return "любые"
    if has_m and has_w:
        return "любые"
    if has_m:
        return "мужчины"
    if has_w:
        return "женщины"
    return _s(raw)


def sb_state(raw: Optional[str]) -> str:
    """Политика СБ → состояние карточки: none / part / full / na."""
    text = _s(raw).lower().replace("ё", "е")
    if not text:
        return "na"
    if text in ("нет", "без сб", "нет сб"):
        return "none"
    if "выбороч" in text or "тяж" in text or "легк" in text or "судим" in text:
        return "part"
    if "провер" in text or "полн" in text or "сб" in text:
        return "full"
    return "part"


def med_state(row) -> str:
    """Медкнижка: no / need / arranged / na."""
    required = _b(row["medical_book_required"])
    without = _b(row["can_start_without_medical_book"])
    if required is None:
        return "na"
    if not required:
        return "no"
    return "arranged" if without else "need"


def shift_type_text(raw: Optional[str]) -> str:
    return {"day": "дневная", "night": "ночная", "mixed": "дневная и ночная"}.get(_s(raw), _s(raw))


# ------------------------------------------------------------------ блоки быта

def housing_block(row) -> Dict[str, Any]:
    available = _b(row["housing_available"])
    if available is None:
        return {"st": "na"}
    if not available:
        return {"st": "no"}
    free = _b(row["housing_free"])
    deduction = _i(row["housing_deduction"])
    if free:
        cost = "free"
    elif deduction:
        cost = "deduct"
    elif free is False:
        cost = "paid"
    else:
        cost = "na"
    return {
        "st": "yes",
        "cost": cost,
        "type": _s(row["housing_conditions"]),
        # Человек в комнате, бельё и транспорт до объекта реестр не хранит.
        "per": None,
        "linen": None,
        "transfer": "",
    }


def meals_block(row) -> Dict[str, Any]:
    available = _b(row["meals_available"])
    if available is None:
        return {"st": "na"}
    if not available:
        return {"st": "no"}
    free = _b(row["meals_free"])
    deduction = _i(row["meals_deduction"])
    if free:
        st = "free"
    elif deduction:
        st = "deduct"
    elif free is False:
        st = "paid"
    else:
        st = "yes"
    return {"st": st, "times": _i(row["meals_times_per_day"]), "note": ""}


def travel_block(row) -> Dict[str, Any]:
    paid = _b(row["transport_paid"])
    terms = _s(row["transport_terms"])
    if paid is None:
        # Условия проезда без флага «оплачивается» не превращаем в «компенсируется»:
        # среди них есть прямо обратные («оплачивается сотрудником самостоятельно»).
        return {"st": "na", "terms": terms}
    if not paid:
        return {"st": "no", "terms": terms}
    full = _b(row["transport_fully_paid"])
    return {
        "st": "yes",
        "full": "full" if full else ("part" if full is False else ""),
        "amount": "",
        "when": "",
        "route": "",
        "terms": terms,
    }


def deductions(row) -> Dict[str, Any]:
    """Удержания. Единицу (в месяц / за смену) реестр не хранит — не выдумываем."""
    items = []
    known_zero = False
    for column, basis in (
            ("housing_deduction", "Проживание"),
            ("meals_deduction", "Питание"),
            ("uniform_deduction", "Спецодежда"),
    ):
        value = _i(row[column])
        if value:
            items.append({"basis": basis, "amount": value, "unit": "", "when": "", "refund": "na"})
        elif value == 0:
            known_zero = True
    if items:
        return {"ded": items, "dedSt": "yes"}
    return {"ded": [], "dedSt": "no" if known_zero else "na"}


# --------------------------------------------------------------------- позиции

def media_block(row) -> List[Dict[str, Any]]:
    """Материалы позиции. Сейчас это папка с фотографиями объекта.

    Ссылка берётся из таблицы `position_kb` — её заполняет обход Яндекс.Диска
    (см. project_kb.link_positions). Не опознали проект или в папке нет ни
    одной фотографии — материала нет, и кнопка в карточке не появляется.
    """
    url = _s(_col(row, "photos_url"))
    photos = _i(_col(row, "kb_photos")) or 0
    if not url or not photos:
        return []
    return [{
        "kind": "object_photo",
        "url": url,
        # Ссылка публичная: папка контрагента открыта по ссылке, её можно
        # отправить кандидату (рекрутёр решает это галочкой в тексте).
        "vis": "public",
        # Живость ссылки отдельно не проверяем: она из индекса, который
        # переобходит диск каждые несколько часов. Пропала папка — пропадёт и
        # строка в position_kb, а не появится битая кнопка.
        "alive": True,
        "upd": _s(_col(row, "linked_at"))[:10],
        "src": _s(_col(row, "kb_project")),
    }]


def position_row(row, now: datetime, rules: Optional[List] = None) -> Dict[str, Any]:
    """Строка реестра → позиция в модели макета."""
    city = geo.normalize_city(row["city"])
    region = geo.normalize_region(row["region"])
    district = region if region and region.lower() != city.lower() else ""

    schedule = _s(row["schedule"])
    hours = _f(row["shift_hours"])
    cits = parse_citizenship(row["citizenship_requirements"])
    house = housing_block(row)
    meals = meals_block(row)
    travel = travel_block(row)
    ded = deductions(row)

    # Объект в понимании ставок: то, что контрагент перечисляет в своей сетке.
    client = rates.client_key(_s(row["counterparty"]), _s(row["object_name"]))
    rec = rates.resolve(
        rules or [], _s(row["source"]), client,
        _s(row["vacancy_name"]), _i(row["min_shifts"]),
    )

    return {
        "id": _s(row["position_id"]),
        "ext": _s(row["last_request_id"]),
        "src": SOURCE_TITLES.get(_s(row["source"]), _s(row["source"])),
        "srcKey": _s(row["source"]),
        "seen": fmt_seen(row["last_seen_at"], now),
        "seenAt": _s(row["last_seen_at"]),

        "cp": _s(row["counterparty"]),
        # Публичного алиаса контрагента в системе нет: кандидату показываем
        # категорию объекта, а не название контрагента.
        "cpAlias": "",
        "cpType": _s(row["vacancy_category"]),
        "obj": _s(row["object_name"]),
        # Объект для ставок и ставка, подобранная под эту позицию.
        "client": client,
        "rec": rec,

        "prof": _s(row["vacancy_name"]),
        "city": city,
        "district": district,
        "region": region,
        "addr": _s(row["object_address"]),

        "rate": _i(row["shift_rate"]),
        "hourly": _i(row["hourly_rate"]),
        "unit": "смена",
        "net": "",
        "sched": schedule,
        "schedKind": _s(row["work_pattern"]),
        "hours": int(hours) if hours is not None and float(hours).is_integer() else hours,
        "shiftType": shift_type_text(row["shift_type"]),
        "minShifts": _i(row["min_shifts"]),
        "workFormat": _s(row["work_format"]),

        "m": _i(row["need_men"]),
        "w": _i(row["need_women"]),
        "c": _i(row["need_couples"]),
        "needTotal": _i(row["need_total"]),
        "gender": normalize_gender(row["gender"]),
        "ageFrom": _i(row["age_from"]),
        "ageTo": _i(row["age_to"]),
        "cits": cits,
        "citsRaw": _s(row["citizenship_requirements"]),
        "req": _s(row["requirements"]),
        "duties": _s(row["duties"]),
        "advantages": _s(row["advantages"]),
        "risks": _s(row["risks"]),

        "med": med_state(row),
        "medPayer": _s(row["medical_book_payer"]),
        "sb": sb_state(row["sb_policy"]),
        "sbRaw": _s(row["sb_policy"]),
        # Тестирования в реестре нет.
        "test": "na",
        "tsd": _b(row["requires_tsd"]),

        "house": house,
        "meals": meals,
        "travel": travel,
        # Периодичность выплат и аванс парсер не извлекает.
        "pay": {"st": "na"},
        "ded": ded["ded"],
        "dedSt": ded["dedSt"],
        # Видео и маршрутов реестр не хранит; фотографии объекта приходят из
        # папки проекта на Яндекс.Диске.
        "media": media_block(row),
    }


def _dupe_key(row: Dict[str, Any]) -> str:
    """Ключ тождества позиции.

    Смена входит в ключ обязательно: дневная и ночная — это две разные позиции
    одного объекта (реестр и сам разводит их по fingerprint), и без неё каждая
    такая пара выглядела бы дублем.
    """
    parts = [
        row["cp"].lower(),
        row["city"].lower(),
        row["prof"].lower(),
        row["obj"].lower(),
        row["shiftType"].lower(),
    ]
    return "|".join(re.sub(r"\s+", " ", part).strip() for part in parts)


# Поля, расхождение которых между дублями — это несостыковка, а не разные
# позиции. Ставка и потребность у одной и той же позиции обязаны совпадать.
CONFLICT_FIELDS = [
    ("rate", "Ставка кандидата"),
    ("sched", "График"),
    ("citsRaw", "Гражданство"),
    ("gender", "Пол"),
]


def find_dupes(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Дубли и расхождения между ними — реальные исключения для админа.

    Дублем считается одна и та же позиция, пришедшая из РАЗНЫХ источников:
    внутри одного источника реестр склеивает повторы сам, так что совпадение
    там — это не дубль, а две настоящие позиции.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not row["prof"]:
            continue
        groups.setdefault(_dupe_key(row), []).append(row)

    dupes: Dict[str, str] = {}
    conflicts: Dict[str, List[Dict[str, Any]]] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        if len({item["srcKey"] for item in group}) < 2:
            continue
        ids = sorted(item["id"] for item in group)
        for position_id in ids:
            dupes[position_id] = [other for other in ids if other != position_id][0]
        first = group[0]
        for field, label in CONFLICT_FIELDS:
            values = []
            for item in group:
                value = item.get(field)
                text = "" if value in (None, "") else str(value)
                if not text:
                    continue
                if all(text != existing["v"] for existing in values):
                    values.append({"v": text, "src": f"{item['src']} · {item['id']}"})
            if len(values) > 1:
                conflicts.setdefault(first["id"], []).append({
                    "field": field,
                    "label": label,
                    "what": (
                        f"Одна и та же позиция пришла в двух заявках с разными значениями поля "
                        f"«{label.lower()}». Выберите актуальное значение."
                    ),
                    "values": values,
                    "recommend": "Обычно верна более поздняя заявка",
                })
    return {"dupes": dupes, "conflicts": conflicts}


# ------------------------------------------------------------------- источники

def sources_block(conn, now: datetime) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.source,
               MAX(r.source_name) AS source_name,
               MAX(r.source_url) AS source_url,
               MAX(r.last_seen_at) AS last_seen,
               SUM(CASE WHEN r.parse_status != 'ok' THEN 1 ELSE 0 END) AS failed,
               COUNT(*) AS requests
        FROM requests r
        GROUP BY r.source
        ORDER BY r.source
        """
    ).fetchall()
    positions = {
        row["source"]: row["cnt"]
        for row in conn.execute(
            "SELECT source, COUNT(*) AS cnt FROM positions WHERE is_active = 1 GROUP BY source"
        )
    }
    out = []
    for index, row in enumerate(rows, start=1):
        key = _s(row["source"])
        failed = _i(row["failed"]) or 0
        out.append({
            "id": f"SRC-{index:02d}",
            "key": key,
            "cp": _s(row["source_name"]) or SOURCE_TITLES.get(key, key),
            "kind": SOURCE_KIND.get(key, "Источник"),
            "address": _s(row["source_url"]),
            "times": SOURCE_SCHEDULE.get(key, ""),
            "last": fmt_seen(row["last_seen"], now),
            "status": "error" if failed else "ok",
            "error": f"заявок с ошибкой разбора: {failed}" if failed else "",
            "pos": positions.get(key, 0),
            "requests": _i(row["requests"]) or 0,
        })
    return out


def counterparties_block(conn, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Карточки контрагентов. Настройки чатов и шаблонов в системе не хранятся —
    показываем то, что известно из заявок, без выдуманных доставок.

    Контрагент здесь — источник: ЯППИ, Градус, КНК, Аметист, ААА+. Их пять, и
    именно с ними есть договорённости, чаты и ставки. Раньше карточки строились
    по полю counterparty позиции, а туда парсер Градуса и ЯППИ кладёт заказчика
    («BMJ», «Молком»), и в панели оказывалось три десятка «контрагентов»,
    которых на самом деле нет. Заказчики теперь живут внутри карточки списком.
    """
    by_name: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = row["srcKey"]
        name = SOURCE_TITLES.get(key, key)
        if not name:
            continue
        card = by_name.setdefault(name, {
            "id": "",
            "name": name,
            "alias": "",
            "kind": SOURCE_KIND.get(row["srcKey"], "Источник"),
            "address": "",
            "times": SOURCE_SCHEDULE.get(row["srcKey"], ""),
            "required": list(REQUIRED_FIELDS.keys()),
            "contact": "",
            "bot": "",
            "token": "",
            "chat": "",
            "thread": "",
            "sendTime": "",
            "tpl": DEFAULT_TEMPLATE,
            "delivery": "отправка запросов не настроена",
            "deliveryOk": False,
            "sources": [],
            "positions": 0,
            # Заказчики этого контрагента: у Градуса это BMJ, Молком и прочие
            # бренды из постов, у Аметиста — объекты из его таблицы.
            "clients": [],
            "key": key,
        })
        card["positions"] += 1
        if row["src"] and row["src"] not in card["sources"]:
            card["sources"].append(row["src"])
        client = row.get("client") or ""
        if client and client not in card["clients"]:
            card["clients"].append(client)

    # Ключ — алиас источника, а не его название: название контрагента меняется
    # (ВахтаПро стал Градусом), алиас остаётся.
    urls = {
        _s(row["source"]): _s(row["source_url"])
        for row in conn.execute(
            "SELECT source, MAX(source_url) AS source_url FROM requests GROUP BY source"
        )
    }
    out = []
    for index, name in enumerate(sorted(by_name, key=lambda n: -by_name[n]["positions"]), start=1):
        card = by_name[name]
        card["id"] = f"CP-{index:02d}"
        card["address"] = urls.get(card["key"], "")
        card["clients"].sort()
        out.append(card)
    return out


def rates_block(conn, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Всё для вкладки «Ставки»: справочник контрагентов, правила и история.

    Справочник собирается из живых позиций, а не заводится руками: список
    объектов у контрагента меняется каждую неделю вместе с потребностью, и
    выбирать в форме нужно из того, что реально есть в реестре сегодня.
    """
    catalog: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = row["srcKey"]
        card = catalog.setdefault(key, {
            "key": key,
            "title": SOURCE_TITLES.get(key, key),
            "positions": 0,
            "clients": {},
        })
        card["positions"] += 1
        client = row.get("client") or ""
        if not client:
            continue
        entry = card["clients"].setdefault(client, {"name": client, "positions": 0,
                                                    "vacancies": [], "shifts": []})
        entry["positions"] += 1
        if row["prof"] and row["prof"] not in entry["vacancies"]:
            entry["vacancies"].append(row["prof"])
        if row["minShifts"] and row["minShifts"] not in entry["shifts"]:
            entry["shifts"].append(row["minShifts"])

    sources = []
    for card in sorted(catalog.values(), key=lambda c: -c["positions"]):
        clients = sorted(card["clients"].values(), key=lambda c: (-c["positions"], c["name"]))
        for client in clients:
            client["vacancies"].sort()
            client["shifts"].sort()
        card["clients"] = clients
        sources.append(card)

    rules = [rule.as_dict() for rule in rates.load_rules(conn)]
    return {
        "sources": sources,
        "rules": rules,
        "history": rates.history(conn, limit=30),
        "expired": sum(1 for rule in rules if rule["expired"]),
        "withRate": sum(1 for row in rows if row.get("rec")),
    }


def cities_block(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name = row["city"]
        if not name:
            continue
        item = seen.get(name)
        if not item:
            point = geo.coords(name)
            item = seen[name] = {
                "name": name,
                "region": row["region"],
                "lat": point[0] if point else None,
                "lon": point[1] if point else None,
                "count": 0,
            }
        item["count"] += 1
        if not item["region"] and row["region"]:
            item["region"] = row["region"]
    return sorted(seen.values(), key=lambda c: (-c["count"], c["name"]))


def raw_fields(row) -> Dict[str, str]:
    """«Как пришло» по полям — из *_raw колонок реестра."""
    pairs = (
        ("Должность", row["vacancy_name_raw"]),
        ("Город", row["city_raw"]),
        ("Регион", row["region_raw"]),
        ("График", row["schedule_raw"]),
        ("Ставка кандидата", row["shift_rate_raw"]),
        ("Требования", row["requirements_raw"]),
        ("Контрагент", row["counterparty_raw"]),
    )
    return {label: _s(value) for label, value in pairs if _s(value)}


def next_run(now: datetime) -> str:
    """Ближайший прогон по расписанию из app.JOBS."""
    times = [(9, 30), (12, 0), (13, 0), (13, 30)]
    for hour, minute in times:
        if (now.hour, now.minute) < (hour, minute):
            return f"{hour:02d}:{minute:02d}"
    hour, minute = times[0]
    return f"{hour:02d}:{minute:02d}"


# ----------------------------------------------------------- что видно рекрутёру
#
# Сумма в сетке — это цена контракта с заказчика за выведенного кандидата
# (20 000, 30 000, 40 000). Рекрутёру причитается доля от неё, и знать полную
# сумму ему не нужно: зная её, он знает и маржу агентства.
#
# Режется на сервере, до отдачи JSON. Спрятать сумму в вёрстке нельзя —
# /api/navigator открывается в браузере рекрутёра, и настоящая цифра лежала бы
# в ответе целиком.

DEFAULT_RECRUITER_SHARE_PERCENT = 20.0


def recruiter_share_percent() -> float:
    """Доля контракта, которую видит рекрутёр, в процентах.

    Настраивается RECRUITER_SHARE_PERCENT. Мусор и значения вне (0; 100]
    игнорируются: показать из-за опечатки в окружении больше, чем причитается,
    хуже, чем не применить настройку.
    """
    raw = os.getenv("RECRUITER_SHARE_PERCENT", "").strip()
    if not raw:
        return DEFAULT_RECRUITER_SHARE_PERCENT
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return DEFAULT_RECRUITER_SHARE_PERCENT
    if not 0 < value <= 100:
        return DEFAULT_RECRUITER_SHARE_PERCENT
    return value


def _share(amount: Any, percent: float) -> Any:
    """Доля от суммы, округлённая до рубля. «Не задана» остаётся «не задана»."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return amount
    return int(round(amount * percent / 100.0))


def apply_role_visibility(payload: Dict[str, Any], *, is_admin: bool) -> Dict[str, Any]:
    """Готовит payload под роль смотрящего. Администратору — всё как есть."""
    percent = recruiter_share_percent()
    # Процент едет и администратору: по нему экран показывает, какую сумму
    # увидит рекрутёр, — иначе выставленную сетку не с чем сверить.
    payload["ratePercent"] = percent
    payload["ratesMasked"] = not is_admin
    if is_admin:
        return payload

    for row in payload.get("rows") or []:
        rec = row.get("rec")
        if not rec:
            continue
        rec["amount"] = _share(rec.get("amount"), percent)
        rec["tiers"] = [
            dict(tier, amount=_share(tier.get("amount"), percent))
            for tier in rec.get("tiers") or []
        ]

    # Сетка целиком и журнал её правок — рабочий стол администратора, где
    # суммы лежат неурезанными. Рекрутёру этот блок не нужен вовсе, а вкладку
    # «Ставки» ему всё равно не открыть.
    block = payload.get("rates")
    if isinstance(block, dict):
        block["rules"] = []
        block["history"] = []
    return payload


# ------------------------------------------------------------------ сборка всего

def build_payload(conn, active_only: bool = True) -> Dict[str, Any]:
    now = datetime.now()
    scope = "WHERE p.is_active = 1" if active_only else ""
    db_rows = conn.execute(
        f"""
        SELECT p.*, r.raw_text, r.source_name, r.source_url, r.parse_status,
               r.received_at, r.first_seen_at AS request_first_seen_at,
               k.photos_url, k.photos AS kb_photos, k.project AS kb_project,
               k.linked_at
        FROM positions p
        JOIN requests r ON r.request_id = p.last_request_id
        LEFT JOIN position_kb k ON k.position_id = p.position_id
        {scope}
        ORDER BY p.last_seen_at DESC, p.position_id
        """
    ).fetchall()

    rows: List[Dict[str, Any]] = []
    raw: Dict[str, str] = {}
    raws: Dict[str, Dict[str, str]] = {}
    titles: Dict[str, Dict[str, str]] = {}
    rules = rates.load_rules(conn)
    for db_row in db_rows:
        row = position_row(db_row, now, rules)
        rows.append(row)
        text = _s(db_row["raw_text"])
        if text:
            raw[row["id"]] = text
        fields = raw_fields(db_row)
        if fields:
            raws[row["id"]] = fields
        titles[row["id"]] = {
            "t": _s(db_row["vacancy_name_raw"]) or row["prof"],
            "ref": _s(db_row["last_request_id"]),
        }

    dupes = find_dupes(rows)
    last_seen = conn.execute("SELECT MAX(last_seen_at) AS ts FROM requests").fetchone()["ts"]
    schedules = sorted({row["schedKind"] for row in rows if row["schedKind"]})
    schedules.sort(key=lambda x: (x != "вахта", x))

    return {
        "rows": rows,
        "raw": raw,
        "rawFields": raws,
        "cpTitles": titles,
        "rates": rates_block(conn, rows),
        "cities": cities_block(rows),
        "sources": sources_block(conn, now),
        "cps": counterparties_block(conn, rows),
        "dupes": dupes["dupes"],
        "conflicts": dupes["conflicts"],
        "required": REQUIRED_FIELDS,
        "schedules": schedules,
        "template": DEFAULT_TEMPLATE,
        # Автоматической рассылки запросов заказчикам в системе пока нет.
        "outreachEnabled": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
        "lastRun": fmt_seen(last_seen, now),
        "nextRun": next_run(now),
        "updatedAt": _s(last_seen)[:16].replace("T", " "),
        "total": len(rows),
        # Заявки заказчика на подбор («под заявку») в системе нет как сущности.
        "activeRequest": None,
    }
