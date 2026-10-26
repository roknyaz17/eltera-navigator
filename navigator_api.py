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
from typing import Any, Dict, List, Optional, Tuple

from registry import dictionaries as dicts
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
    "tabiya": "12:00",
    "ametist": "13:30",
    "manual": "вручную",
    "internal": "вручную",
}

SOURCE_KIND = {
    "vahtapro": "Telegram-чат",
    "aaaplus": "Telegram-чат",
    "ametist": "Telegram-чат",
    "kpk": "Google-таблица",
    "yappi": "SeaTable",
    "marketstaff": "Google-таблица",
    "tabiya": "Google-таблица",
    "manual": "Ручной ввод",
    "internal": "Шпаргалка Avito",
}

# За сколько дней считаем заявки с ошибкой разбора в здоровье источника.
# Неделя: разовый сбой сервиса разбора не должен висеть красным месяцами, но и
# гаснуть за сутки ему рано — источник чинят не в тот же час.
FAILED_WINDOW_DAYS = 7


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


# ------------------------------------------- ограничения по национальности
#
# Заказчики пишут это прямым текстом: «строго не Кавказ», «СТРОГО НЕ ЦЫГАН»,
# «Кавказ и Цыгане запрет», «запрет на кандидатов кавказской национальности».
# Гражданством это не является — и Кавказ, и цыгане в заявках это граждане РФ,
# поэтому в citizenship_requirements оно почти не попадает (3 позиции из 874) и
# в parse_citizenship ему делать нечего. Живёт ограничение в «Требованиях» и
# «Рисках» — их и читаем.
#
# Зачем вообще: рекрутёру нужно знать заранее, куда кандидата не возьмут, чтобы
# не гнать человека на объект, где ему откажут на проходной. Наружу, в текст
# кандидату, это не уходит никогда (см. candidateText в шаблоне).
#
# Состояния: ban — запрет, women — можно только женщин, ok — прямо разрешено,
# na — в заявке молчок. Молчание НЕ значит «можно»: две трети позиций про это
# просто не пишут, и выдавать их за разрешённые было бы враньём.

_ETH_GROUPS = (("kav", "кавказ"), ("gyp", "цыган"))

# Заявка — это перечисление через запятую, и полярность («не», «запрет»,
# «можно») относится к своему пункту, а не ко всей строке: в «Кавказ МОЖНО,
# Цыгане запрет» запрет только на вторых. Поэтому режем на пункты. Скобки и
# слэш режут так же, как запятая. Двоеточие — нет: «Запрет на трудоустройство:
# кавказцы» должно остаться одним пунктом.
_ETH_SENTENCE = re.compile(r"[.;\n]+")
_ETH_CLAUSE = re.compile(r"[,()/]+")

_ETH_BAN = re.compile(r"\bне\b|запрет", re.I)
# «допускается», «разрешено» рядом с этими группами в данных не встречаются, а
# «допускаются административные правонарушения» — встречается сплошь. Поэтому
# разрешающих маркеров ровно два, оба наблюдаемые.
_ETH_OK = re.compile(r"можно|по\s+согласовани", re.I)

# «ж можно Кавказ», «женщинам можно Кавказ» и обратная запись того же самого —
# «запрет на кавказцев (мужчин)». Обе означают: женщину возьмут, мужчину нет.
_ETH_WOMEN = re.compile(r"\bж(?:ен\w*)?\s+можно\s+кавказ\w*|кавказц\w*\s*\(\s*мужчин\w*\s*\)?", re.I)

# Отдельный от «не Кавказ» способ сказать то же самое. Ставим запрет обеим
# группам, но в подпись кладём дословную формулировку: вывод здесь наш, и
# рекрутёр должен видеть, из чего он сделан.
_ETH_SLAV = re.compile(r"славянск\w*\s+внешност\w*", re.I)


def _join_note(note: str, clause: str) -> str:
    """Подпись без повторов: одна и та же фраза лежит и в «Требованиях», и в
    «Гражданстве», и дважды в карточке она выглядит опечаткой."""
    parts = [p for p in note.split("; ") if p]
    if clause.lower() not in [p.lower() for p in parts]:
        parts.append(clause)
    return "; ".join(parts)


def parse_ethnic_limits(*sources: Optional[str]) -> Dict[str, Dict[str, str]]:
    """«РФ (Кавказ МОЖНО, Цыгане запрет)» → kav=ok, gyp=ban.

    Возвращает по блоку на группу: {'st': ban|women|ok|na, 'note': дословно}.
    Дословная формулировка нужна не для красоты: разбор здесь эвристический, и
    рекрутёр должен иметь возможность перечитать оригинал и не поверить нам.
    """
    out = {key: {"st": "na", "note": ""} for key, _ in _ETH_GROUPS}
    text = " ; ".join(_s(src) for src in sources if _s(src))
    if not text:
        return out

    for sentence in _ETH_SENTENCE.split(text):
        # Пункт со своим маркером решает сам. Пункт без маркера — под общим
        # запретом предложения: он пишется и слева («запрет на трудоустройство
        # кавказцев (мужчин), цыган, пенсионеров»), и справа («Кавказ, Цыгане
        # запрет на трудоустройство»). Поэтому запрет разливается по
        # предложению в обе стороны, а разрешение — никуда: перенести «можно»
        # на соседний пункт значит выдумать разрешение, которого не давали.
        parsed = []
        loose_ban = False
        # «Женскую» оговорку ищем по всему предложению, а не по пункту: скобка
        # режет пункты, а «запрет на кавказцев (мужчин)» без скобки не читается.
        women = _ETH_WOMEN.search(sentence)
        for clause in _ETH_CLAUSE.split(sentence):
            clause = clause.strip()
            if not clause:
                continue
            # Регистр в подписи сохраняем — «СТРОГО НЕ ЦЫГАН» заказчик написал
            # именно так, и рекрутёру полезно это видеть. Сравниваем по копии.
            probe = clause.lower().replace("ё", "е")
            ban = bool(_ETH_BAN.search(probe))
            ok = bool(_ETH_OK.search(probe))
            groups = [key for key, stem in _ETH_GROUPS if stem in probe]
            if groups:
                # И запрет, и разрешение в одном пункте — берём запрет: ошибка
                # в эту сторону стоит дешевле несостоявшегося выхода.
                parsed.append((clause, groups, "ban" if ban else ("ok" if ok else "")))
                loose_ban = loose_ban or ban
            elif "запрет" in probe:
                loose_ban = True

        for clause, groups, state in parsed:
            if not state and loose_ban:
                state = "ban"
            if not state:
                continue
            for key in groups:
                verdict = "women" if (key == "kav" and women) else state
                # Запрет не перебиваем разрешением из другого поля: в данных
                # есть позиция, где «Требования» говорят «Кавказ можно», а
                # «Риски» — «Кавказ и Цыгане запрет». Это расхождение источника,
                # и безопасное чтение — запрет, а подпись покажет оба куска.
                if out[key]["st"] == "ban" and verdict != "ban":
                    verdict = "ban"
                note = _join_note(out[key]["note"], clause)
                # Оговорку про пол дописываем в подпись отдельно: без неё пункт
                # «Запрет на трудоустройство: кавказцы» рядом с «можно женщин»
                # читается как ошибка разбора, хотя в скобках стояло «мужчин».
                if verdict == "women" and women:
                    note = _join_note(note, women.group(0))
                out[key] = {"st": verdict, "note": note}

    slav = _ETH_SLAV.search(text)
    if slav:
        for key, _ in _ETH_GROUPS:
            if out[key]["st"] == "na":
                out[key] = {"st": "ban", "note": slav.group(0)}
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


# ------------------------------------------------------- оформление и выплаты
#
# Своей колонки у этих двух вещей в реестре нет и заводить её ради одного
# источника не стали: у половины контрагентов такой строки в заявке нет вовсе,
# и колонка стояла бы пустой, притворяясь пробелом. Поэтому читаем текстом — и
# из распознанных полей, и из самой заявки: «Оформление ТК, СЗ» контрагент
# пишет одной строкой, а парсер кладёт её в требования не всегда.

# Поля позиции, где эти формулировки реально встречаются.
_HIRE_FIELDS = (
    "requirements", "advantages", "risks", "duties", "schedule",
    "housing_conditions", "transport_terms", "requirements_raw",
)

# «т.к.» («так как») из поиска исключаем: иначе каждая вторая заявка
# превращается в «оформление по ТК».
HIRE_FORMS = (
    ("ТК", re.compile(r"(?<![а-яёa-z.])тк(?![а-яё])|трудов\w*\s+договор", re.I)),
    ("ГПХ", re.compile(r"(?<![а-яё])гпх(?![а-яё])|гражданско[- ]правов", re.I)),
    ("СЗ", re.compile(r"(?<![а-яё])сз(?![а-яё])|(?<![а-яё])смз(?![а-яё])|самозанят|(?<![а-яё])нпд(?![а-яё])", re.I)),
)

def hire_text(row) -> str:
    """Весь текст позиции, где может быть сказано про оформление и выплаты."""
    parts = [_s(_col(row, name)) for name in _HIRE_FIELDS]
    parts.append(_s(_col(row, "raw_text")))
    return "\n".join(part for part in parts if part)


# Форма оформления, известная про контрагента целиком, а не из заявки
# (со слов руководителя 26.10.2026). Табия оформляет всех по ТК, хотя в её
# таблице попадаются и ставки «по СЗ», — это HIRE_FIXED, заявка его не
# перебивает. КНК работает с самозанятыми («процент оплачивают люди сами»),
# но в заявке это чаще видно только по условию выплаты третьему лицу, —
# это HIRE_FALLBACK: только когда заявка формы не называет, позиции КНК по
# ТК так и остаются ТК.
HIRE_FIXED = {"tabiya": ("ТК",)}
HIRE_FALLBACK = {"kpk": ("СЗ",)}


def _hire_sources(row) -> List[str]:
    """Тексты, где ищем оформление и налог, — в порядке доверия.

    Первый — поля позиции вместе с постом, как и раньше. Порознь их не
    разделить: поля бывают старше поста (риски ингест не затирает, и у
    Кюхенленда в них с августа «Оформление по СМЗ, налог 6% компенсируется»,
    а в свежем посте «✅Оформление по ТК»), а пост бывает списком объектов
    (Градус пишет «❗️Внимание, на всех проектах оформление ПО ТК ❗️» одной
    строкой на девять объектов). Второй — карточка объекта: постоянные
    условия отстают от постов (в описаниях проектов Градуса на диске у
    объектов, которые теперь по ТК, ещё СМЗ), так что карточка только
    заполняет молчание заявки.
    """
    return [text for text in (hire_text(row), _s(_col(row, "kb_text"))) if text]


def _forms_in(text: str) -> List[str]:
    out = []
    for name, pattern in HIRE_FORMS:
        for m in pattern.finditer(text):
            # «3-е лицо должно быть совершеннолетним и САМОЗАНЯТЫМ» (КНК) —
            # условие выплаты третьему лицу, а не оформление кандидата: без
            # этой оговорки позиции КНК по ТК получали ещё и СЗ. Сокращения
            # не трогаем: «выплаты по СМЗ — на себя или на третье лицо» у
            # Аметиста — это и есть оформление.
            if name == "СЗ" and m.group(0).lower().startswith("самозанят"):
                left, right = _bounds(text, m.start(), m.end(), ".;\n")
                if _THIRD_PERSON.search(text[left:right]):
                    continue
            out.append(name)
            break
    return out


def hire_forms(row) -> List[str]:
    """Формы оформления, названные в заявке: ТК, ГПХ, СЗ.

    Их может быть несколько сразу — контрагент нередко даёт выбор
    («Оформление ТК, СЗ»), и это не противоречие, а две возможности.

    Решает первый источник, где форма названа (см. _hire_sources).
    Карточка объекта читается, только если ни поля, ни пост об оформлении не
    говорят: у Градуса форма часто есть лишь в описании проекта на диске
    («Оформление по СЗ, оплата налога за счет компании!»). Пост МЕЛИОРа —
    список объектов, и формы из него по-прежнему расходятся по соседям: у
    выплат от этого спасает _whole_request, здесь — пока нет.
    """
    source = _s(_col(row, "source"))
    if source in HIRE_FIXED:
        return list(HIRE_FIXED[source])
    for text in _hire_sources(row):
        forms = _forms_in(text)
        if forms:
            return forms
    return list(HIRE_FALLBACK.get(source, ()))


# ------------------------------------------------------------------ поле «%»
#
# Ставку самозанятому контрагенты пишут по-разному. У КНК она «грязная»:
# «Налог самозанятого оплачивается сотрудником самостоятельно (4-6%)», и из
# 3 700 за смену на руки остаётся 3 700 − 6%. Градус на части проектов налог
# берёт на себя — «налог 6% компенсируется», «СМЗ = 3960 + 6%», — и тогда
# 3 700 доходят целиком. Рекрутёру разница видна только из такой строки,
# поэтому у позиции есть поле «%» (попросил руководитель 26.10.2026):
#   ТК, ГПХ — оформление не самозанятое, 6% тут ни при чём;
#   +6%     — сказано, что налог самозанятого платит контрагент;
#   −6%     — сказано, что платит сам человек, или не сказано ничего.
# Последнее — сознательное исключение из «ничего не додумываем»: молчание у
# нас значит «налог на человеке», и карточка помечает такое значение как
# принятое по умолчанию.

_TAX = r"(?:налог\w*|(?<![а-яё])нпд(?![а-яё]))"
_TAX_PAYER = r"(?:компани\w*|работодател\w*|организаци\w*|заказчик\w*|фирм\w*|нами|мы)"
_TAX_PCT = r"\s*[46](?:[.,]0)?\s*%"
_SMZ_WORD = r"(?<![а-яё])(?:смз|сз)(?![а-яё])"
# Кто платит, ищем в своём куске фразы — до запятой: в «налог 6% —
# сотрудник, компенсация проезда до 2000» компенсация не про налог.
_TAX_EMPLOYER = re.compile(
    _TAX + r"[^.;,\n]{0,40}?(?:компенс\w*|за\s+сч[её]т\s+" + _TAX_PAYER
    + r"|(?:оплачива|плат)\w*\s+" + _TAX_PAYER + r"|бер[её]\w*\s+на\s+себя)"
    + r"|компенс\w*[^.;,\n]{0,25}?" + _TAX
    + r"|(?:\d{3,}|" + _SMZ_WORD + r")[^.;,\n]{0,12}?\+" + _TAX_PCT,
    re.I,
)
_TAX_WORKER = re.compile(
    _TAX + r"[^.;,\n]{0,60}?(?:сотрудник\w*|работник\w*|кандидат\w*|исполнител\w*|"
    r"самостоятельн\w*|сами?(?![а-яё])|за\s+свой\s+сч[её]т|удерж\w*|вычит\w*)"
    + r"|(?:\d{3,}|" + _SMZ_WORD + r")[^.;,\n]{0,12}?[-−–]" + _TAX_PCT,
    re.I,
)
# Налог назван, а кто платит — нет: «Ставка налога 4-6%» (КНК). Ставку
# называют кандидату, чтобы он знал, сколько отдаст сам, — значит, на нём.
_TAX_BARE = re.compile(_TAX + r"[^.;,\n]{0,20}?\d\s*%", re.I)


def tax_terms(row) -> Tuple[str, str]:
    """Кто платит налог самозанятого: employer / worker / "" — и где сказано.

    Решает первый источник, где о налоге есть слово (порядок тот же, что у
    оформления). Внутри источника «платит сам» сильнее «компенсируем», как
    «нет» сильнее «да» у выплат: недоплаченные 6% рекрутёру объяснять
    дороже, чем приятная неожиданность.
    """
    for text in _hire_sources(row):
        found = (_TAX_WORKER.search(text) or _TAX_EMPLOYER.search(text)
                 or _TAX_BARE.search(text))
        if found:
            who = "employer" if found.re is _TAX_EMPLOYER else "worker"
            return who, _note(text, found.start(), found.end())
    return "", ""


def tax_block(row, forms: Optional[List[str]] = None) -> Dict[str, Any]:
    """Поле «%»: «ТК», «+6%», «−6%» или несколько через « / »."""
    forms = hire_forms(row) if forms is None else forms
    smz = "СЗ" in forms or not forms
    who, note = tax_terms(row) if smz else ("", "")
    options = [form for form in ("ТК", "ГПХ") if form in forms]
    if smz:
        options.append("+6%" if who == "employer" else "−6%")
    return {
        "v": " / ".join(options),
        # def — минус шесть процентов взяты по умолчанию: про налог в заявке
        # ни слова.
        "st": "def" if smz and not who else "yes",
        "who": who,
        "note": note,
    }


# Как получить деньги: на карту третьего лица и наличными. Рекрутёру это
# спрашивают прямо — у кандидата арестована карта или её нет вовсе, — и ответ
# «в заявке не сказано» тут хуже, чем «нельзя»: человек поедет и не получит
# зарплату. Поэтому три состояния, а не флаг: yes / no / «не сказано».
#
# Пишут это по-разному (срез на 07.10.2026):
#   КНК      «Получение ЗП на 3-е лицо: 3-е лицо должно быть … САМОЗАНЯТЫМ»,
#            и там же «НЕТ ВЫПЛАТ НА 3-Е ЛИЦО»;
#   МЕЛИОР   «можно на карту 3-го лица», «на карту 3 лица», «возможен
#            НАЛИЧНЫЙ РАСЧЕТ», «получать ЗП наличными»;
#   Аметист  «выплаты по СМЗ (можно оформить на себя или на третье лицо)
#            через «Консоль» (в Консоли можно внести реквизиты карты 3-го
#            лица)» — самозанятость на третье лицо и есть выплата ему;
#   Градус   «разрешены выплаты на карту третьего лица (все банки, кроме
#            цифровых …)» — в описании проекта на диске.

_THIRD_PERSON = re.compile(
    r"(?<![а-яё\d])(?:треть\w*|3\s*-?\s*(?:е|ье|го|ьего|ему|ьему|м|х|их)?)\s+лиц\w*",
    re.I,
)
# «3 лица» само по себе — не про деньги. Рядом должно стоять, что именно на
# это лицо делают: платят, переводят, оформляют самозанятость. «Передача
# данных третьим лицам» под это не попадает.
_THIRD_CONTEXT = re.compile(
    r"(?<![а-яё])(?:карт\w*|выплат\w*|оплат\w*|перевод\w*|перечисл\w*|зп|з/п|"
    r"зарплат\w*|деньг\w*|получ\w*|оформ\w*|смз|самозанят\w*|консол\w*|реквизит\w*)",
    re.I,
)
# Наличные — только как способ получить зарплату. «Иметь при себе наличкой
# 500 руб за бельё» (Черкизово у Градуса) — это платит кандидат, а не ему;
# «безналичный расчёт» и «наличие СНИЛС» отсекает граница слова.
_CASH_WORD = r"(?<![а-яё])(?:наличн\w*|наличк\w*|налом|кэш\w*)"
_CASH = re.compile(
    _CASH_WORD + r"[^.;\n]{0,15}?расч[её]т"
    r"|расч[её]т\w*[^.;\n]{0,15}?" + _CASH_WORD
    + r"|(?<![а-яё])(?:зп|з/п|зарплат\w*|выплат\w*|выплач\w*|аванс\w*|получ\w*)"
    r"[^.;\n]{0,50}?" + _CASH_WORD
    + r"|" + _CASH_WORD + r"[^.;\n]{0,15}?(?:зп|з/п|зарплат|выплат|аванс)",
    re.I,
)
# Отрицание ищем только в своём куске фразы, до ближайшей запятой: в
# «Возможен наличный расчёт, …, спецодежда при доработке не возвращается»
# «не» относится к спецодежде, а не к наличным.
_PAY_NEGATION = re.compile(
    r"(?<![а-яё])(?:нет|нельзя|без|невозможн\w*|запрещ\w*|отсутств\w*|"
    r"не(?!\s+(?:менее|более|позднее|ранее|раньше))(?=\s))(?![а-яё])",
    re.I,
)
_FRAGMENT_STOP = ".;,\n"
_NOTE_LONG = 120
_NOTE_MAX = 200


def _bounds(text: str, start: int, end: int, stops: str):
    left = max(text.rfind(ch, 0, start) for ch in stops) + 1
    rights = [i for i in (text.find(ch, end) for ch in stops) if i != -1]
    return left, min(rights) if rights else len(text)


def _note(text: str, start: int, end: int) -> str:
    """Фраза заявки, где это сказано, — как написал контрагент.

    Условия здесь важнее самого «да»: у КНК третье лицо обязано быть
    самозанятым, у Градуса не годятся цифровые банки. Эмодзи и маркеры
    списка в начале срезаем. Поля позиции бывают одной фразой через запятую
    на полэкрана («Питание …, проживание …, возможен наличный расчет, …») —
    тогда цитату начинаем с того куска, где сказано нужное.
    """
    left, right = _bounds(text, start, end, ".;\n")
    if right - left > _NOTE_LONG:
        left = max(left, text.rfind(",", 0, start) + 1)
    note = text[left:right].strip()
    if len(note) > _NOTE_MAX:
        note = note[:_NOTE_MAX - 1].rstrip() + "…"
    return re.sub(r"^[^\w«(]+", "", note)


def _pay_hits(text: str):
    """Упоминания в одном тексте: (вид, yes/no, строка-первоисточник)."""
    for m in _THIRD_PERSON.finditer(text):
        sentence = _bounds(text, m.start(), m.end(), ".;\n")
        before = text[max(sentence[0], m.start() - 50):m.start()]
        after = text[m.end():min(sentence[1], m.end() + 35)]
        if not (_THIRD_CONTEXT.search(before) or _THIRD_CONTEXT.search(after)):
            continue
        left, right = _bounds(text, m.start(), m.end(), _FRAGMENT_STOP)
        state = "no" if _PAY_NEGATION.search(text[left:right]) else "yes"
        yield "third", state, _note(text, m.start(), m.end())
    for m in _CASH.finditer(text):
        word = re.search(_CASH_WORD, m.group(0), re.I)
        at = m.start() + word.start()
        left, right = _bounds(text, at, at + len(word.group(0)), _FRAGMENT_STOP)
        state = "no" if _PAY_NEGATION.search(text[left:right]) else "yes"
        yield "cash", state, _note(text, at, at + len(word.group(0)))


def _whole_request(row) -> bool:
    """Относится ли текст заявки к позиции целиком.

    Пост МЕЛИОРа — список объектов: «💰 Возможен НАЛИЧНЫЙ РАСЧЕТ» стоит под
    тремя из девятнадцати, а текст заявки у всех девятнадцати позиций один.
    Читать его целиком — раздать наличные ФМ Логистик Чехову и Кералиту. Так
    что текст заявки берём, только когда в ней один объект; иначе остаются
    поля самой позиции и карточка объекта.
    """
    positions = _i(_col(row, "req_positions"))
    if positions is None or positions <= 1:
        return True
    return _i(_col(row, "req_objects")) == 1 and not _i(_col(row, "req_blank_objects"))


def pay_sources(row) -> List[str]:
    """Тексты о позиции в порядке доверия.

    Первыми — поля самой позиции: их разбирали под неё. Затем текст заявки,
    если он про один объект: он сегодняшний. Последней — карточка объекта из
    базы знаний контрагента (лист «Объекты Мелиор», описание проекта Градуса):
    это постоянные условия объекта, и у МЕЛИОРа «есть возможность получать на
    карту третьего лица» написано только там — в постах этого почти нет.
    """
    fields = "\n".join(
        part for part in (_s(_col(row, name)) for name in _HIRE_FIELDS) if part
    )
    out = [fields]
    if _whole_request(row):
        out.append(_s(_col(row, "raw_text")))
    out.append(_s(_col(row, "kb_text")))
    return [text for text in out if text]


def payment_block(row) -> Dict[str, Any]:
    """Способы выплаты: на карту третьего лица и наличными.

    Каждый вид решает первый источник, где о нём сказано (см. pay_sources), а
    внутри одного источника «нет» сильнее «да»: у КНК в одной заявке стоит и
    «Получение ЗП на 3-е лицо: Оформление по ТК РФ», и «НЕТ ВЫПЛАТ НА 3-Е
    ЛИЦО».
    """
    found: Dict[str, tuple] = {}
    for text in pay_sources(row):
        here: Dict[str, tuple] = {}
        for kind, state, note in _pay_hits(text):
            if kind in found:
                continue
            if kind not in here or (state == "no" and here[kind][0] == "yes"):
                here[kind] = (state, note)
        found.update(here)
    third = found.get("third", ("", ""))
    cash = found.get("cash", ("", ""))
    notes = []
    for note in (third[1], cash[1]):
        if note and note not in notes:
            notes.append(note)
    return {
        "st": "yes" if third[0] or cash[0] else "na",
        # Периодичность выплат и аванс парсер не извлекает.
        "third": third[0],
        "cash": cash[0],
        "note": " · ".join(notes),
    }


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

def kb_object_key(row) -> str:
    """Имя объекта из базы знаний контрагента — ключ для ставок рекрутёра.

    Пока такое имя есть только у МЕЛИОРа: там карточка объекта — лист таблицы,
    и её путь (`melior:Летуаль`) устойчив, в отличие от названия, которое
    контрагент правит вместе с адресом. У Градуса ключ карточки — путь папки
    на диске, он в сетке ставок не фигурирует, поэтому там всё по-прежнему.
    """
    path = _s(_col(row, "kb_path"))
    if not path.startswith("melior:"):
        return ""
    return path[len("melior:"):].strip()


def media_block(row) -> List[Dict[str, Any]]:
    """Материалы позиции: фотографии объекта и его описание.

    Ссылки берутся из таблицы `position_kb` — её заполняет обход базы знаний
    контрагента (см. project_kb.link_positions): у Градуса это папки
    Яндекс.Диска, у МЕЛИОРа — листы таблицы «Объекты Мелиор». Не опознали
    объект — материалов нет, и кнопок в карточке не появляется.
    """
    out: List[Dict[str, Any]] = []
    source = _s(_col(row, "source"))
    photos_url = _s(_col(row, "photos_url"))
    photos = _i(_col(row, "kb_photos")) or 0
    folder_url = _s(_col(row, "kb_folder_url"))
    updated = _s(_col(row, "linked_at"))[:10]
    project = _s(_col(row, "kb_project"))

    if photos_url and photos:
        out.append({
            # У МЕЛИОРа контрагент выкладывает именно фотографии проживания —
            # так подписан и сам пост со ссылкой.
            "kind": "housing_photo" if source == "melior" else "object_photo",
            "url": photos_url,
            # Ссылка публичная: папка контрагента открыта по ссылке, её можно
            # отправить кандидату (рекрутёр решает это галочкой в тексте).
            "vis": "public",
            # Живость ссылки отдельно не проверяем: она из индекса, который
            # переобходит диск каждые несколько часов. Пропала папка — пропадёт и
            # строка в position_kb, а не появится битая кнопка.
            "alive": True,
            "upd": updated,
            "src": project,
        })

    # Описание объекта — внутренний документ контрагента: там ставки по всем
    # операциям и условия выплат. Кандидату его не отправляют, поэтому vis
    # не public: кнопка в карточке есть, в текст кандидату материал не уедет.
    #
    # Пока только МЕЛИОР: у него карточка объекта — отдельный лист таблицы, и
    # ссылка ведёт ровно на описание. У Градуса folder_url — это папка
    # проекта, где лежат те же фотографии, и вторая кнопка вела бы туда же.
    #
    # Сверяемся с уже показанным, а не с photos_url: когда фотоальбома у
    # объекта нет, photos_url тоже указывает на лист (так устроен
    # project_kb.photos_url), кнопки с фотографиями при этом не появляется — и
    # сравнение с ним прятало бы описание у всех объектов без фото.
    shown = {item["url"] for item in out}
    if source == "melior" and folder_url and folder_url not in shown:
        out.append({
            "kind": "object_card",
            "url": folder_url,
            "vis": "internal",
            "alive": True,
            "upd": updated,
            "src": project,
        })
    return out


def _same_place(region: str, city: str) -> bool:
    """Регион и город — одно и то же место?

    «Москва» в городе и «Москва и область» в регионе — не два уровня адреса,
    а одна строка дважды: в подписи карточки это читалось бы как «Москва ·
    Москва и область».
    """
    if not region or not city:
        return False
    left, right = region.lower(), city.lower()
    return left == right or left.startswith(right + " и ")


def position_row(
        row,
        now: datetime,
        rules: Optional[List] = None,
        city_regions: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Строка реестра → позиция в модели макета."""
    city = geo.normalize_city(row["city"])
    # Регион — единым написанием (см. registry/geo.py): по нему на экране
    # собирается выбор «показать всё, что есть в области», а «Мордовия» и
    # «Республика мордовия» развалили бы одну область на две строки списка.
    region = geo.resolve_region(row["region"], city, city_regions)
    district = region if region and not _same_place(region, city) else ""

    schedule = _s(row["schedule"])
    hours = _f(row["shift_hours"])
    cits = parse_citizenship(row["citizenship_requirements"])
    # Читаем три поля: в «Гражданстве» это лежит у трёх позиций из 874, всё
    # остальное — в «Требованиях» и «Рисках». sb_policy не смотрим: там этого
    # не бывает ни разу.
    eth = parse_ethnic_limits(
        row["requirements"], row["citizenship_requirements"], row["risks"],
    )
    house = housing_block(row)
    meals = meals_block(row)
    travel = travel_block(row)
    ded = deductions(row)
    pay = payment_block(row)
    hire = hire_forms(row)

    # Объект в понимании ставок: то, что контрагент перечисляет в своей сетке.
    # Если объект опознан в базе знаний контрагента — берём имя оттуда: в
    # сетке ставок стоит «Летуаль», а парсер в разных постах пишет то
    # «ЛЕТУАЛЬ (склад косметики и парфюмерии)», то «Летуаль», и правило,
    # заведённое на один вариант, для другого не сработало бы (rates сравнивает
    # строки точно). На 55 позициях МЕЛИОРа таких написаний было 34 на 24
    # объекта.
    client = kb_object_key(row) or rates.client_key(
        _s(row["counterparty"]), _s(row["object_name"]),
    )
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
        "eth": eth,
        "req": _s(row["requirements"]),
        "duties": _s(row["duties"]),
        "advantages": _s(row["advantages"]),
        "risks": _s(row["risks"]),

        # Оформление и выплаты — не поля реестра, а то, что удалось вычитать
        # из заявки. Пусто значит «в заявке не сказано», а не «нельзя».
        "hire": hire,
        # Поле «%»: как читать ставку — ТК, «+6%» (налог самозанятого
        # платит контрагент) или «−6%» (платит сам; так же, если не сказано).
        "tax": tax_block(row, hire),
        "pay3": pay["third"] == "yes",
        "payCash": pay["cash"] == "yes",

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
        "pay": pay,
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
    # Сбой считаем только по заявкам, которые ещё приходят из источника.
    # Строка может упасть один раз и назавтра исчезнуть у контрагента —
    # повторять её станет нечем, а «источник недоступен» висело бы вечно: так
    # ЯППИ шесть недель числился сломанным из-за одной заявки от 17.08.2026,
    # у которой разбор упал по 404 от LLM. Живой сбой сам себя покажет —
    # такая заявка приезжает каждый прогон и падает заново, поэтому её
    # last_seen_at всегда свежий.
    cutoff = (now - timedelta(days=FAILED_WINDOW_DAYS)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT r.source,
               MAX(r.source_name) AS source_name,
               MAX(r.source_url) AS source_url,
               MAX(r.last_seen_at) AS last_seen,
               SUM(CASE WHEN r.parse_status != 'ok'
                         AND COALESCE(r.last_seen_at, '') >= ?
                        THEN 1 ELSE 0 END) AS failed,
               COUNT(*) AS requests
        FROM requests r
        GROUP BY r.source
        ORDER BY r.source
        """,
        (cutoff,),
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


def regions_block(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Области для панели выбора: сколько позиций и сколько городов в каждой.

    Позиции без региона идут отдельной строкой с пустым name и стоят в конце
    списка: «регион не указан» — это состояние данных, а не область, но и
    прятать такие позиции из выбора нельзя, иначе они видны только тогда,
    когда не выбрано вообще ничего.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name = row["region"]
        item = seen.get(name)
        if not item:
            item = seen[name] = {"name": name, "count": 0, "cities": 0, "_cities": set()}
        item["count"] += 1
        if row["city"]:
            item["_cities"].add(row["city"])

    out = []
    for item in seen.values():
        item["cities"] = len(item.pop("_cities"))
        out.append(item)
    out.sort(key=lambda r: (not r["name"], -r["count"], r["name"]))
    return out


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
    # Процент — такой же секрет, как и сама сумма: зная долю, рекрутёр
    # восстанавливает цену контракта обратным умножением, и всё сокрытие
    # теряет смысл. Поэтому доля уезжает только администратору — ему она
    # нужна, чтобы видеть, какую сумму получит рекрутёр из выставленной сетки.
    payload["ratePercent"] = percent if is_admin else None
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

def load_rows(conn, active_only: bool = True) -> List[Any]:
    """Строки реестра со всем, что нужно position_row() и pay_sources().

    Отдельно от build_payload: тем же набором пользуется помощник подбора
    (nav_assistant.py), и его выдача обязана совпадать с тем, что рекрутёр
    видит в «Навигаторе».
    """
    scope = "WHERE p.is_active = 1" if active_only else ""
    return conn.execute(
        f"""
        SELECT p.*, r.raw_text, r.source_name, r.source_url, r.parse_status,
               r.received_at, r.first_seen_at AS request_first_seen_at,
               k.photos_url, k.photos AS kb_photos, k.project AS kb_project,
               k.folder_url AS kb_folder_url, k.path AS kb_path, k.linked_at,
               d.doc_text AS kb_text,
               g.req_positions, g.req_objects, g.req_blank_objects
        FROM positions p
        JOIN requests r ON r.request_id = p.last_request_id
        LEFT JOIN position_kb k ON k.position_id = p.position_id
        LEFT JOIN disk_projects d ON d.path = k.path
        -- Сколько объектов в заявке: в посте-списке текст заявки общий на
        -- все объекты, и условия одного нельзя раздавать остальным
        -- (см. _whole_request). Считаем по request_positions — что заявка
        -- породила, — а не по last_request_id: на нём висят и погашенные
        -- старые версии той же позиции.
        LEFT JOIN (
            SELECT rp.request_id,
                   COUNT(*) AS req_positions,
                   COUNT(DISTINCT TRIM(COALESCE(s.object_name, ''))) AS req_objects,
                   SUM(TRIM(COALESCE(s.object_name, '')) = '') AS req_blank_objects
            FROM request_positions rp
            JOIN positions s ON s.position_id = rp.position_id
            GROUP BY rp.request_id
        ) g ON g.request_id = p.last_request_id
        {scope}
        ORDER BY p.last_seen_at DESC, p.position_id
        """
    ).fetchall()


def build_payload(conn, active_only: bool = True) -> Dict[str, Any]:
    now = datetime.now()
    db_rows = load_rows(conn, active_only)

    rows: List[Dict[str, Any]] = []
    raw: Dict[str, str] = {}
    raws: Dict[str, Dict[str, str]] = {}
    titles: Dict[str, Dict[str, str]] = {}
    rules = rates.load_rules(conn)
    # Подтверждённый справочник «город → регион»: им приём заполняет пустой
    # регион (registry/normalize.py), им же он заполняется и здесь — для строк,
    # принятых до того, как справочник пополнили. Берём только подтверждённые
    # соответствия: неподтверждённые ждут человека в /registry/dictionaries.
    city_regions = dicts.load(conn, dicts.KIND_CITY_REGION)
    for db_row in db_rows:
        row = position_row(db_row, now, rules, city_regions)
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
        "regions": regions_block(rows),
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
