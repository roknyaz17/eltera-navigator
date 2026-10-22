"""Помощник подбора в «Навигаторе»: вопрос словами → позиции реестра.

Рекрутёр пишет так, как говорит: «грузчики 35 лет женщины с питанием»,
«найди вакансии с сидячими местами». Модель раскладывает вопрос на условия и
зовёт поиск, а позиции отбирает код — по той же карточке, которую строит
«Навигатор» (navigator_api.position_row). Поэтому помощник не найдёт того,
чего рекрутёр не увидел бы в списке, и не опишет позицию иначе, чем карточка.

От голосового агента (agent_search.py) отличается адресатом. Тот говорит с
кандидатом и зачитывает три варианта; этот отвечает рекрутёру, который видит
карточки целиком. Отсюда разница: профессия не обязательна, выдача длиннее, а
условия без своей колонки («сидячая работа», «в тепле») ищутся по тексту
заявки, и найденный кусок модель обязана прочитать сама — «работа стоя,
присесть нельзя» тоже содержит слово «присесть».

С пустотой обращаемся как везде в проекте: пустое поле — «в заявке не
сказано», а не «не подходит». Такая позиция не отсеивается, но идёт после
точных совпадений с пометкой, что именно про неё неизвестно.

Каждый вопрос пишется в assistant_log вместе с ответом, показанными
позициями и вызовами поиска — и удачный, и упавший.
"""

import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

import navigator_api as nav
from registry import db as registry_db
from registry import dictionaries as dicts
from registry import geo

# Вызовов поиска на один вопрос. Двух хватает почти всегда: найти и, если
# пусто, ослабить одно условие. Остальное — запас на уточнения модели.
MAX_SEARCHES = 4
# Позиций в одном ответе поиска модели. Остальные она видит только числом:
# двадцать карточек — это уже ~3 тыс. токенов на вызов.
MAX_ITEMS = 20
# Сколько позиций помощник может показать рекрутёру за раз.
MAX_SHOWN = 60
# Прошлых вопросов разговора, которые уходят модели: на «а с жильём?» ей
# нужен прошлый поиск, а не вся смена рекрутёра.
HISTORY_TURNS = 4
MAX_QUESTION = 1000
# Символов после найденного слова во фрагменте для модели (до — вдвое меньше).
SNIPPET = 140


# ------------------------------------------------------------------ журнал
#
# Таблица заводится здесь, а не миграцией в registry/db.py: номера миграций на
# боевом сервере и в репозитории разошлись (там их семнадцать, здесь семь), и
# очередная «v8» означала бы на сервере совсем другую схему. CREATE IF NOT
# EXISTS одинаково безопасен на обеих базах.

LOG_DDL = """
CREATE TABLE IF NOT EXISTS assistant_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    user_email   TEXT NOT NULL DEFAULT '',
    session_id   TEXT NOT NULL DEFAULT '',      -- один разговор в окне помощника
    query        TEXT NOT NULL,
    answer       TEXT NOT NULL DEFAULT '',
    position_ids TEXT NOT NULL DEFAULT '[]',    -- JSON: что показали, по порядку
    searches     TEXT NOT NULL DEFAULT '[]',    -- JSON: условия каждого поиска и сколько нашлось
    status       TEXT NOT NULL DEFAULT 'ok',    -- ok | error
    error        TEXT NOT NULL DEFAULT '',
    llm_model    TEXT NOT NULL DEFAULT '',
    llm_calls    INTEGER NOT NULL DEFAULT 0,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_assistant_log_at ON assistant_log(created_at);
CREATE INDEX IF NOT EXISTS idx_assistant_log_session ON assistant_log(session_id);
CREATE INDEX IF NOT EXISTS idx_assistant_log_user ON assistant_log(user_email);
"""

_schema_lock = threading.Lock()
_schema_ready: set = set()


def ensure_schema(conn) -> None:
    key = conn.execute("PRAGMA database_list").fetchone()["file"]
    if key in _schema_ready:
        return
    with _schema_lock:
        conn.executescript(LOG_DDL)
        _schema_ready.add(key)


def write_log(conn, entry: Dict[str, Any]) -> int:
    ensure_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO assistant_log
            (created_at, user_email, session_id, query, answer, position_ids,
             searches, status, error, llm_model, llm_calls, tokens_in,
             tokens_out, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            entry.get("user_email", ""),
            entry.get("session_id", ""),
            entry.get("query", ""),
            entry.get("answer", ""),
            json.dumps(entry.get("position_ids") or [], ensure_ascii=False),
            json.dumps(entry.get("searches") or [], ensure_ascii=False),
            entry.get("status", "ok"),
            entry.get("error", ""),
            entry.get("llm_model", ""),
            int(entry.get("llm_calls") or 0),
            int(entry.get("tokens_in") or 0),
            int(entry.get("tokens_out") or 0),
            int(entry.get("duration_ms") or 0),
        ),
    )
    return int(cur.lastrowid)


def recent_log(conn, limit: int = 100, user: str = "") -> List[Dict[str, Any]]:
    """Последние записи журнала, новые сверху. user — только его вопросы."""
    ensure_schema(conn)
    where, params = "", []
    if user:
        where, params = "WHERE user_email = ?", [user]
    rows = conn.execute(
        f"SELECT * FROM assistant_log {where} ORDER BY id DESC LIMIT ?",
        params + [max(1, min(int(limit), 1000))],
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for name in ("position_ids", "searches"):
            try:
                item[name] = json.loads(item[name] or "[]")
            except ValueError:
                item[name] = []
        out.append(item)
    return out


def load_history(conn, session: str, user: str) -> List[Dict[str, Any]]:
    """Прошлые вопросы этого разговора, старые первыми.

    Только свои: идентификатор разговора приходит из браузера, и по чужому
    подобранному id нельзя прочитать чужую переписку с помощником.
    """
    if not session:
        return []
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT query, answer, searches FROM assistant_log
        WHERE session_id = ? AND user_email = ? AND status = 'ok'
        ORDER BY id DESC LIMIT ?
        """,
        (session, user, HISTORY_TURNS),
    ).fetchall()
    out = []
    for row in reversed(rows):
        try:
            searches = json.loads(row["searches"] or "[]")
        except ValueError:
            searches = []
        out.append({
            "query": row["query"],
            "answer": row["answer"],
            "args": searches[-1]["args"] if searches else None,
        })
    return out


# ------------------------------------------------------------ набор позиций

def load_pool(conn) -> List[Dict[str, Any]]:
    """Активные позиции в модели карточки «Навигатора».

    Ставка рекрутёра (rec) сюда не попадает вовсе: у рекрутёра она урезана до
    его доли (navigator_api.apply_role_visibility), и модель, знающая полную
    сумму, пересказала бы её в ответе. Правила ставок поэтому не грузим.
    """
    now = datetime.now()
    city_regions = dicts.load(conn, dicts.KIND_CITY_REGION)
    pool = []
    for db_row in nav.load_rows(conn, active_only=True):
        row = nav.position_row(db_row, now, None, city_regions)
        row.pop("rec", None)
        # Название как в заявке: «Упаковщица» нормализуется в «Упаковщик», а
        # рекрутёр ищет ровно так, как написал заказчик.
        row["_title"] = db_row["vacancy_name_raw"] or ""
        # Тексты о позиции — те же, по которым карточка читает оформление и
        # выплаты: поля позиции, текст заявки (если в ней один объект) и
        # описание объекта у контрагента.
        row["_texts"] = nav.pay_sources(db_row)
        pool.append(row)
    return pool


def facets(pool: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Значения справочников, как они лежат в реестре, — для подсказки модели."""
    def distinct(key: str) -> List[str]:
        return sorted({str(r.get(key) or "").strip() for r in pool} - {""})
    return {
        "regions": distinct("region"),
        "categories": distinct("cpType"),
        "schedules": distinct("schedKind"),
    }


# ------------------------------------------------------------------ условия

def _low(text: Any) -> str:
    return str(text or "").lower().replace("ё", "е")


_ENDING = re.compile(
    r"(?:ами|ями|ими|ыми|ого|его|ому|ему|ов|ев|ей|ий|ый|ой|ая|яя|ое|ее|ые|ие|ую|юю"
    r"|ам|ям|ах|ях|ом|ем|а|я|ы|и|у|ю|е|о|ь|й)$"
)


def root(word: str) -> str:
    """Слово → корень для поиска подстрокой: «грузчики» → «грузчик».

    Морфологии в проекте нет и ради этого заводить её незачем: модель и так
    присылает корни, а здесь только страховка от «грузчики» и «упаковщицы»,
    которые иначе не нашли бы «Грузчик» и «Упаковщица». Короткое слово не
    режем: от «швея» осталось бы «шве».
    """
    word = _low(word).strip(" .,;:!?«»\"'()")
    cut = _ENDING.sub("", word)
    return cut if len(cut) >= 4 else word


def find_root(text: str, key: str) -> int:
    """Где в тексте слово, начинающееся с корня; -1 — нигде.

    Только с начала слова: подстрокой «грузчик» находился в «Водитель
    погрузчика», а «стул» — в «наступлении».
    """
    found = re.search(r"(?<![а-яa-z0-9])" + re.escape(key), _low(text))
    return found.start() if found else -1


_PLACEHOLDERS = {
    "", "null", "none", "nil", "undefined", "n/a", "-", "любой", "любая", "любое",
    "любые", "не важно", "неважно", "без разницы", "все", "всё", "не указано",
}


def _text_arg(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return "" if _low(text) in _PLACEHOLDERS else text


def _int_arg(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(float(str(value).replace(" ", "").replace(",", ".")))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _list_arg(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _text_arg(item)
        if text and text not in out:
            out.append(text)
    return out[:8]


def _bool_arg(value: Any) -> bool:
    if isinstance(value, str):
        return _low(value) in ("true", "да", "1", "yes")
    return value is True


def clean_args(raw: Any) -> Dict[str, Any]:
    """Аргументы от модели → проверенные условия. Мусор и заглушки выкидываем.

    Модель шлёт то «35», то 35, то «любой» вместо пропуска поля — всё это не
    должно ни ронять поиск, ни искать позицию «любой».
    """
    raw = raw if isinstance(raw, dict) else {}
    args: Dict[str, Any] = {}
    for key in ("professions", "keywords"):
        items = _list_arg(raw.get(key))
        if items:
            args[key] = items
    for key in ("category", "city", "region", "citizenship", "schedule"):
        text = _text_arg(raw.get(key))
        if text:
            args[key] = text
    for key in ("age", "rate_min", "shifts_max"):
        number = _int_arg(raw.get(key))
        if number:
            args[key] = number
    if args.get("age") and not 14 <= args["age"] <= 80:
        args.pop("age")
    enums = {
        "gender": ("мужчины", "женщины", "пары"),
        "housing": ("есть", "бесплатно"),
        "meals": ("есть", "бесплатно"),
        "shift": ("дневная", "ночная"),
        "hire": ("ТК", "ГПХ", "СЗ"),
        "pay": ("третье лицо", "наличные"),
    }
    for key, allowed in enums.items():
        value = _text_arg(raw.get(key))
        match = next((a for a in allowed if _low(a) == _low(value)), None)
        if match is None and value:
            # «женщина», «муж», «пара» — достаточно начала слова.
            match = next((a for a in allowed if _low(a)[:3] == _low(value)[:3]), None)
        if match:
            args[key] = match
    for key in ("no_medical_book", "travel_paid"):
        if _bool_arg(raw.get(key)):
            args[key] = True
    return args


def _best(states: List[str]) -> str:
    """Город ИЛИ регион — объединением, как в «Навигаторе» (passGeo)."""
    if "yes" in states:
        return "yes"
    return "na" if "na" in states else "no"


def _c_professions(r, value: List[str]) -> str:
    names = r["prof"] + " | " + r["_title"]
    return "yes" if any(find_root(names, root(p)) >= 0 for p in value) else "no"


def _c_category(r, value: str) -> str:
    if not r["cpType"]:
        return "na"
    return "yes" if find_root(r["cpType"], root(value)) >= 0 else "no"


def _c_geo(r, value: Tuple[str, str]) -> str:
    want_city, want_region = value
    states = []
    if want_city:
        city = _low(r["city"])
        want = _low(geo.normalize_city(want_city))
        states.append(("yes" if want in city or city in want else "no") if city else "na")
    if want_region:
        region = _low(r["region"])
        want = _low(geo.region_title(want_region))
        # «Тульская» без «области» — по первому слову: модель не всегда
        # пишет название так, как оно лежит в справочнике.
        head = root(want.split()[0]) if want.split() else want
        states.append(("yes" if want == region or head in region else "no") if region else "na")
    return _best(states)


EAEU = {"РФ", "РБ", "КЗ", "АРМ", "КРГ"}
CIS = EAEU | {"УЗБ", "ТДЖ"}


def _c_citizenship(r, value: str) -> str:
    codes = nav.parse_citizenship(value)
    want = codes[0] if codes else value.upper()
    cits = r["cits"]
    if not cits:
        return "na"
    if want in cits:
        return "yes"
    if "ЕАЭС" in cits and want in EAEU:
        return "yes"
    if "СНГ" in cits and want in CIS:
        return "yes"
    return "no"


def _c_age(r, value: int) -> str:
    # Нет границы — нет ограничения: так же считает passAge в «Навигаторе».
    if r["ageFrom"] and value < r["ageFrom"]:
        return "no"
    if r["ageTo"] and value > r["ageTo"]:
        return "no"
    return "yes"


def _c_gender(r, value: str) -> str:
    """Потребность — тоже ответ: «Упаковщик 15Ж» без графы «пол» — это для женщин."""
    men, women, couples = r["m"] or 0, r["w"] or 0, r["c"] or 0
    if value == "пары":
        return "yes" if couples else "na"
    count = men if value == "мужчины" else women
    if r["gender"] in (value, "любые") or count > 0:
        return "yes"
    if r["gender"] in ("мужчины", "женщины") or men or women:
        return "no"
    return "na"


def _c_housing(r, value: str) -> str:
    house = r["house"]
    if house["st"] != "yes":
        return {"no": "no"}.get(house["st"], "na")
    if value == "бесплатно":
        return {"free": "yes", "na": "na"}.get(house.get("cost"), "no")
    return "yes"


def _c_meals(r, value: str) -> str:
    state = r["meals"]["st"]
    if value == "бесплатно":
        # «Есть», а за чей счёт — не сказано: это вопрос к заказчику, не отказ.
        return {"free": "yes", "yes": "na", "na": "na"}.get(state, "no")
    return {"na": "na", "no": "no"}.get(state, "yes")


def _c_rate(r, value: int) -> str:
    if not r["rate"]:
        return "na"
    return "yes" if r["rate"] >= value else "no"


def _c_shifts(r, value: int) -> str:
    if not r["minShifts"]:
        return "na"
    return "yes" if r["minShifts"] <= value else "no"


def _c_schedule(r, value: str) -> str:
    """Режим работы. «Вахта» — это ещё и формат: у вахты на 6/1 в режиме
    стоит «6/1», и по одному режиму «вахты до 15 смен» теряли больше половины
    вахт реестра."""
    kind, want = _low(r["schedKind"]), _low(value)
    if any(want in _low(text) for text in (kind, r["sched"], r["workFormat"])):
        return "yes"
    return "no" if kind else "na"


def _c_shift(r, value: str) -> str:
    kind = _low(r["shiftType"])
    if not kind:
        return "na"
    return "yes" if value in kind else "no"


def _c_med(r, value: bool) -> str:
    return {"no": "yes", "arranged": "yes", "need": "no"}.get(r["med"], "na")


def _c_hire(r, value: str) -> str:
    forms = r.get("hire") or []
    if not forms:
        return "na"
    return "yes" if value in forms else "no"


def _c_pay(r, value: str) -> str:
    state = r["pay"].get("third" if value == "третье лицо" else "cash") or ""
    return {"yes": "yes", "no": "no"}.get(state, "na")


def _c_travel(r, value: bool) -> str:
    return {"yes": "yes", "no": "no"}.get(r["travel"]["st"], "na")


# Ключ условия → (как назвать его рекрутёру, проверка). Проверка отвечает
# "yes" — подходит, "no" — не подходит, "na" — в заявке не сказано.
CRITERIA: List[Tuple[str, str, Callable[[Dict[str, Any], Any], str]]] = [
    ("professions", "профессия", _c_professions),
    ("category", "категория объекта", _c_category),
    ("geo", "город/регион", _c_geo),
    ("age", "возраст", _c_age),
    ("gender", "пол", _c_gender),
    ("citizenship", "гражданство", _c_citizenship),
    ("housing", "проживание", _c_housing),
    ("meals", "питание", _c_meals),
    ("rate_min", "ставка", _c_rate),
    ("shifts_max", "длина вахты", _c_shifts),
    ("schedule", "график", _c_schedule),
    ("shift", "дневная/ночная смена", _c_shift),
    ("no_medical_book", "медкнижка", _c_med),
    ("hire", "оформление", _c_hire),
    ("pay", "способ выплаты", _c_pay),
    ("travel_paid", "проезд", _c_travel),
]


def _criteria_values(args: Dict[str, Any]) -> Dict[str, Any]:
    values = dict(args)
    city, region = values.pop("city", ""), values.pop("region", "")
    if city or region:
        values["geo"] = (city, region)
    return values


def find_snippet(texts: List[str], keywords: List[str]) -> str:
    """Первый кусок текста о позиции, где встретился любой из корней."""
    roots = [root(k) for k in keywords]
    roots = [k for k in roots if len(k) >= 3]
    for text in texts:
        for key in roots:
            at = find_root(text, key)
            if at < 0:
                continue
            # После слова — вдвое больше, чем до: «Сидячая работа — только
            # для упаковщиц» объясняется справа, а не слева.
            start = max(0, at - SNIPPET // 2)
            end = min(len(text), at + len(key) + SNIPPET)
            piece = " ".join(text[start:end].split())
            return ("…" if start else "") + piece + ("…" if end < len(text) else "")
    return ""


# ------------------------------------------------------------------- поиск

def _need_text(r) -> str:
    parts = [f"{label}{r[key]}" for key, label in (("m", "м"), ("w", "ж"), ("c", "пары "))
             if r[key]]
    total = r["needTotal"]
    if total and parts:
        return f"{total} ({', '.join(parts)})"
    return str(total) if total else ", ".join(parts)


def _age_text(r) -> str:
    lo, hi = r["ageFrom"], r["ageTo"]
    if lo and hi:
        return f"{lo}–{hi}"
    if lo:
        return f"от {lo}"
    return f"до {hi}" if hi else ""


_HOUSE_TEXT = {"free": "бесплатно", "deduct": "с удержанием", "paid": "платно", "na": "есть"}
_MEALS_TEXT = {"free": "бесплатно", "deduct": "с удержанием", "paid": "платно",
               "yes": "есть", "no": "нет"}
_MED_TEXT = {"no": "не нужна", "need": "нужна", "arranged": "нужна, можно оформить на месте"}


def brief(r: Dict[str, Any], unknown: List[str], snippet: str = "") -> Dict[str, Any]:
    """Позиция для модели: только то, о чём её могут спросить, и без пустот."""
    house = r["house"]
    item = {
        "id": r["id"],
        "prof": r["prof"],
        "city": r["city"],
        "region": r["region"],
        "obj": r["obj"],
        "rate": r["rate"],
        "hourly": r["hourly"],
        "sched": r["sched"][:80],
        "min_shifts": r["minShifts"],
        "need": _need_text(r),
        "gender": r["gender"],
        "age": _age_text(r),
        "cits": ", ".join(r["cits"]),
        "housing": ("нет" if house["st"] == "no"
                    else _HOUSE_TEXT.get(house.get("cost"), "есть") if house["st"] == "yes" else ""),
        "meals": _MEALS_TEXT.get(r["meals"]["st"], ""),
        "med": _MED_TEXT.get(r["med"], ""),
        "unknown": unknown,
        "snippet": snippet,
    }
    return {key: value for key, value in item.items() if value not in (None, "", [])}


def search(pool: List[Dict[str, Any]], raw_args: Any) -> Dict[str, Any]:
    """Подбор по условиям. Возвращает ответ инструмента для модели."""
    args = clean_args(raw_args)
    values = _criteria_values(args)
    active = [(key, label, check) for key, label, check in CRITERIA if key in values]
    keywords = args.get("keywords") or []

    excluded: Dict[str, int] = {}
    passed = []
    for r in pool:
        unknown, failed = [], []
        for key, label, check in active:
            state = check(r, values[key])
            if state == "no":
                failed.append(label)
            elif state == "na":
                unknown.append(label)
        snippet = ""
        if keywords:
            snippet = find_snippet(r["_texts"] + [r["prof"], r["obj"]], keywords)
            if not snippet:
                failed.append("слова в тексте заявки")
        # Считаем только позиции, которые споткнулись ровно об одно условие:
        # это ответ на «что будет, если его снять». Простой счёт отказов
        # здесь бесполезен — «профессия: 290» есть в любом поиске и ничего не
        # говорит модели о том, какое условие ослабить.
        if len(failed) == 1:
            excluded[failed[0]] = excluded.get(failed[0], 0) + 1
        if not failed:
            passed.append((r, unknown, snippet))

    # Сначала точные, затем с неизвестными; внутри — по ставке. Из реестра
    # строки приходят свежими вперёд, сортировка устойчивая.
    passed.sort(key=lambda x: (len(x[1]), x[0]["rate"] is None, -(x[0]["rate"] or 0)))
    exact = sum(1 for _, unknown, _ in passed if not unknown)
    return {
        "total": len(passed),
        "exact": exact,
        "with_unknown": len(passed) - exact,
        "items": [brief(r, unknown, snippet) for r, unknown, snippet in passed[:MAX_ITEMS]],
        "excluded": excluded if not passed else {},
        "_ids": [r["id"] for r, _, _ in passed],
        "_args": args,
    }


def describe(args: Dict[str, Any]) -> List[str]:
    """Условия поиска словами — их видит рекрутёр под ответом помощника."""
    out = []
    if args.get("professions"):
        out.append(", ".join(args["professions"]))
    if args.get("category"):
        out.append(args["category"])
    for key in ("city", "region"):
        if args.get(key):
            out.append(args[key])
    if args.get("gender"):
        out.append(args["gender"])
    if args.get("age"):
        out.append(f"{args['age']} лет")
    if args.get("citizenship"):
        out.append(args["citizenship"])
    for key, title in (("housing", "жильё"), ("meals", "питание")):
        if args.get(key):
            out.append(title + (" бесплатно" if args[key] == "бесплатно" else ""))
    if args.get("rate_min"):
        out.append(f"от {args['rate_min']} ₽/смена")
    if args.get("shifts_max"):
        out.append(f"до {args['shifts_max']} смен")
    if args.get("schedule"):
        out.append(args["schedule"])
    if args.get("shift"):
        out.append(f"{args['shift']} смена")
    if args.get("no_medical_book"):
        out.append("без медкнижки")
    if args.get("hire"):
        out.append(f"оформление {args['hire']}")
    if args.get("pay"):
        out.append(f"выплата: {args['pay']}")
    if args.get("travel_paid"):
        out.append("проезд оплачивается")
    if args.get("keywords"):
        out.append("в тексте: " + " / ".join(args["keywords"]))
    return out


# ------------------------------------------------------------------- модель

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_vacancies",
        "description": (
            "Поиск по активным позициям реестра. Все параметры необязательны, "
            "заданные складываются через И. Пустое поле позиции её не отсеивает: "
            "такая позиция приходит с пометкой unknown. Ответ: total — сколько "
            "подошло, exact — без неизвестных, items — до 20 лучших (сначала точные), "
            "excluded — сколько позиций добавится, если снять только это условие "
            "(приходит, когда не нашлось ничего). Поля позиции: prof, city, region, obj — объект, rate — ₽ за смену, "
            "hourly — ₽/ч, sched, min_shifts — минимум смен, need — потребность, gender, "
            "age, cits — гражданство, housing, meals, med — медкнижка, snippet — где в "
            "тексте нашлись keywords."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "professions": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Должности или их корни, подходит любая: [\"грузчик\"], "
                                   "[\"комплектовщ\", \"сборщик\"]. Не задавай, если профессию не назвали.",
                },
                "category": {"type": "string", "description": "Категория объекта из справочника."},
                "keywords": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Условие, для которого нет параметра, — 3–6 коротких корней "
                                   "и синонимов так, как пишут в заявках: [\"сидя\", \"сидяч\", "
                                   "\"стул\", \"присест\"]. Совпадение с любым.",
                },
                "city": {"type": "string", "description": "Город объекта."},
                "region": {"type": "string", "description": "Регион из справочника."},
                "age": {"type": "integer", "description": "Возраст кандидата."},
                "gender": {"type": "string", "enum": ["мужчины", "женщины", "пары"],
                           "description": "Кто кандидат."},
                "citizenship": {"type": "string",
                                "description": "Гражданство кандидата: РФ, РБ, Казахстан, "
                                               "Киргизия, Армения, Узбекистан, Таджикистан."},
                "housing": {"type": "string", "enum": ["есть", "бесплатно"],
                            "description": "Нужно проживание."},
                "meals": {"type": "string", "enum": ["есть", "бесплатно"],
                          "description": "Нужно питание."},
                "rate_min": {"type": "integer", "description": "Ставка за смену не ниже, ₽."},
                "shifts_max": {"type": "integer",
                               "description": "Кандидат готов отработать не больше N смен."},
                "schedule": {"type": "string",
                             "description": "Режим из справочника графиков: вахта, 6/1, 5/2…"},
                "shift": {"type": "string", "enum": ["дневная", "ночная"]},
                "no_medical_book": {"type": "boolean",
                                    "description": "true — у кандидата нет медкнижки."},
                "hire": {"type": "string", "enum": ["ТК", "ГПХ", "СЗ"],
                         "description": "Нужная форма оформления."},
                "pay": {"type": "string", "enum": ["третье лицо", "наличные"],
                        "description": "Нужен способ выплаты."},
                "travel_paid": {"type": "boolean", "description": "true — нужен оплачиваемый проезд."},
            },
        },
    },
}

REPLY_TOOL = {
    "type": "function",
    "function": {
        "name": "reply",
        "description": "Финальный ответ рекрутёру. Вызывается один раз, когда подбор готов.",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {"type": "string",
                           "description": "1–4 предложения по-русски, без markdown и списков."},
                "position_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "id подходящих позиций в порядке пригодности."},
            },
            "required": ["answer", "position_ids"],
        },
    },
}

SYSTEM_PROMPT = """Ты — помощник рекрутёра в «Навигаторе» Eltera. Подбираешь вакансии (позиции) из реестра заявок под запрос рекрутёра. Сегодня {today}.

Правила:
1. Данные о вакансиях бери только из search_vacancies. Ставки, условия и объекты называй только из результатов, ничего не додумывай.
2. Разложи запрос по параметрам поиска. Возраст, пол и гражданство — это кандидат: «женщины 35 лет» → gender=женщины, age=35. Профессию дай корнем: «грузчики» → «грузчик». «Склад», «производство» — это category, а не профессия.
3. Условие без своего параметра (сидячая работа, в тепле, без подъёма тяжестей, рядом с метро…) передай в keywords: 3–6 коротких корней и синонимов самого условия, как его пишут в заявках («сидя», «сидяч», «присест»). Профессии и догадки туда не добавляй: «оператор» — не синоним сидячей работы. Прочитай snippet каждой найденной позиции и оставь в position_ids только те, где условие действительно про эту работу: «стулья в комнате приёма пищи» или «стоя, присесть нельзя» — не подходит. Если условие в заявке про другую должность — оговори.
4. Пустое поле в заявке значит «не сказано», а не «нет». Позиции с unknown показывай после точных и скажи, что уточнить у заказчика.
5. Ничего не нашлось — посмотри excluded, ослабь одно условие и поищи ещё раз; в ответе скажи, что ослабил. Не больше {max_searches} поисков на вопрос.
6. Если вопрос продолжает прошлый («а с жильём?», «а в Туле?»), повтори прошлый поиск с изменённым условием.
7. В конце обязательно вызови reply. answer — 1–3 коротких предложения: сколько подошло, чем выделяются лучшие, что уточнить у заказчика. Коды позиций и подробности карточек не перечисляй: рекрутёр видит карточки списком под ответом. position_ids — подходящие позиции, сначала лучшие.
8. Ставку рекрутёра и вознаграждение за кандидата не обсуждай: этих данных у тебя нет.

Значения справочников в реестре:
- регионы: {regions}
- категории объектов: {categories}
- графики: {schedules}"""


def make_llm():
    """Модель того же провайдера, что и разбор заявок (vacancy_parser.py).

    Отдельный адрес — ASSISTANT_BASE_URL: на Timeweb это может быть свой
    агент с отдельным лимитом, чтобы разговоры рекрутёров не съели бюджет
    ночного разбора.
    """
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        base_url=os.getenv("ASSISTANT_BASE_URL") or os.getenv("TIMEWEB_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        model=os.getenv("ASSISTANT_MODEL", "gpt-4.1"),
        temperature=0,
        max_tokens=1500,
        timeout=60,
        max_retries=1,
    )


def _usage(message) -> Tuple[int, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _for_model(result: Dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in result.items() if not k.startswith("_")},
                      ensure_ascii=False)


def run_agent(
        question: str,
        pool: List[Dict[str, Any]],
        llm,
        history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Цикл «модель → поиск → модель» до вызова reply."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    known = facets(pool)
    messages = [SystemMessage(SYSTEM_PROMPT.format(
        today=datetime.now().strftime("%d.%m.%Y"),
        max_searches=MAX_SEARCHES,
        regions=", ".join(known["regions"]) or "—",
        categories=", ".join(known["categories"]) or "—",
        schedules=", ".join(known["schedules"]) or "—",
    ))]
    for turn in history or []:
        messages.append(HumanMessage(turn["query"]))
        note = ""
        if turn.get("args"):
            note = "\n(условия последнего поиска: " + json.dumps(turn["args"], ensure_ascii=False) + ")"
        messages.append(AIMessage((turn.get("answer") or "") + note))
    messages.append(HumanMessage(question))

    bound = llm.bind_tools([SEARCH_TOOL, REPLY_TOOL])
    out: Dict[str, Any] = {"searches": [], "llm_calls": 0, "tokens_in": 0, "tokens_out": 0,
                           "llm_model": ""}
    last: Optional[Dict[str, Any]] = None

    def finish(answer: str, ids: Any, fallback: bool = False) -> Dict[str, Any]:
        valid = {r["id"] for r in pool}
        picked: List[str] = []
        for pid in ids if isinstance(ids, list) else []:
            pid = str(pid).strip()
            # id, которого нет среди активных позиций, — выдумка модели: такой
            # карточки рекрутёр не откроет.
            if pid in valid and pid not in picked:
                picked.append(pid)
        # Пустой reply — это решение модели («по фрагментам ничего не
        # подошло»), его не перебиваем. Подставляем найденное, только когда
        # модель до reply не дошла.
        if fallback and not picked and last:
            picked = last["_ids"]
        out.update(answer=(answer or "").strip(), position_ids=picked[:MAX_SHOWN],
                   criteria=describe(last["_args"]) if last else [])
        return out

    # Каждый поиск — отдельный ход модели, плюс ход на reply и один запасной:
    # модель иногда отвечает текстом, а потом всё же зовёт инструмент.
    for _ in range(MAX_SEARCHES + 2):
        message = bound.invoke(messages)
        out["llm_calls"] += 1
        t_in, t_out = _usage(message)
        out["tokens_in"] += t_in
        out["tokens_out"] += t_out
        meta = getattr(message, "response_metadata", None) or {}
        out["llm_model"] = meta.get("model_name") or out["llm_model"]
        messages.append(message)

        calls = getattr(message, "tool_calls", None) or []
        if not calls:
            # Ответил текстом без reply — берём текст, а позиции из последнего
            # поиска: показать то, что он нашёл, честнее, чем ничего.
            text = message.content if isinstance(message.content, str) else str(message.content)
            return finish(text, [], fallback=True)

        for call in calls:
            name, call_args = call.get("name"), call.get("args") or {}
            if name == "reply":
                return finish(str(call_args.get("answer") or ""), call_args.get("position_ids") or [])
            if name != "search_vacancies":
                body = json.dumps({"error": f"нет инструмента {name}"}, ensure_ascii=False)
            elif len(out["searches"]) >= MAX_SEARCHES:
                body = json.dumps({"error": "лимит поисков исчерпан — ответь по найденному через reply"},
                                  ensure_ascii=False)
            else:
                last = search(pool, call_args)
                out["searches"].append({"args": last["_args"], "total": last["total"],
                                        "exact": last["exact"]})
                body = _for_model(last)
            messages.append(ToolMessage(body, tool_call_id=call.get("id") or ""))

    return finish("Не успел собрать ответ — показываю, что нашлось последним поиском.", [],
                  fallback=True)


# ------------------------------------------------------------------ вопрос

def ask(question: str, *, user: str, session: str = "", db_path: Optional[str] = None,
        llm=None) -> Dict[str, Any]:
    """Вопрос рекрутёра → ответ помощника. Пишет журнал при любом исходе."""
    started = time.monotonic()
    question = " ".join(str(question or "").split())[:MAX_QUESTION]
    session = re.sub(r"[^\w\-]", "", str(session or ""))[:64]
    entry: Dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "user_email": user or "",
        "session_id": session,
        "query": question,
    }
    try:
        with registry_db.connect(db_path) as conn:
            ensure_schema(conn)
            pool = load_pool(conn)
            history = load_history(conn, session, user)
        # Соединение закрыто до похода в модель: ответ идёт секунды, держать
        # базу всё это время незачем.
        result = run_agent(question, pool, llm or make_llm(), history)
        entry.update(result, status="ok")
    except Exception as exc:
        # Не logger.exception: loguru по умолчанию печатает значения
        # переменных в трейсбеке, а среди них весь набор позиций и аргументы
        # клиента модели вместе с ключом. Обычный трейсбек их не содержит.
        logger.error(f"[assistant] {user}: вопрос «{question[:80]}» упал: {type(exc).__name__}: {exc}")
        entry.update(status="error", error=traceback.format_exc()[-3000:])
    entry["duration_ms"] = int((time.monotonic() - started) * 1000)

    with registry_db.connect(db_path) as conn:
        entry_id = write_log(conn, entry)
    logger.info(
        f"[assistant] #{entry_id} {user} «{question[:80]}» → {entry['status']}, "
        f"позиций {len(entry.get('position_ids') or [])}, поисков {len(entry.get('searches') or [])}, "
        f"{entry['duration_ms']} мс, токенов {entry.get('tokens_in', 0)}/{entry.get('tokens_out', 0)}"
    )

    if entry["status"] != "ok":
        return {"id": entry_id, "status": "error", "answer": "", "ids": [], "criteria": [],
                "error": "Помощник сейчас не ответил. Вопрос записан — попробуйте ещё раз чуть позже."}
    return {
        "id": entry_id,
        "status": "ok",
        "answer": entry.get("answer") or "",
        "ids": entry.get("position_ids") or [],
        "criteria": entry.get("criteria") or [],
    }
