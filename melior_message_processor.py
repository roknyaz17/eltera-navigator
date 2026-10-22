"""Процессор канала МЕЛИОРа.

Отличий от Градуса и AAA+ четыре, и все они про то, как канал устроен.

1. Заголовок поста ничего не значит. «АКТУАЛЬНАЯ ЗАЯВКА», «🚀ЗАПУСКАЕМ
   ПРОИЗВОДСТВО В Г. ВЛАДИМИР🚀» и «МЫ ПЕРЕЕХАЛИ🏠» — это всё заявки, просто
   поводы разные. Поэтому snapshot_marker нет: вакансию узнаём по телу поста,
   а не по шапке.

2. Блок объекта размечен эмодзи, но каждый раз своим: 💊 склад медикаментов,
   🚘 автозапчасти, 🍏 косметика. Нарезать по одному символу, как «🚀» у
   Градуса, нельзя — режем по строке-заголовку объекта: эмодзи, НАЗВАНИЕ
   заглавными, следом строка с городом. В посте-сводке таких блоков дюжина, и
   отдавать их модели одним куском — верный способ потерять половину.

3. Контрагента в тексте нет вовсе, есть только объекты («ЛЕТУАЛЬ», «ФОРУМ
   АВТО»). Контрагент — сам МЕЛИОР, объект переезжает в object_name (та же
   механика, что у AAA+).

4. Текущее состояние — только «Актуальная заявка». Каждое утро (кроме
   воскресенья) МЕЛИОР перепощивает два списка, «АКТУАЛЬНАЯ ЗАЯВКА
   (Москва/МО)» и «Актуальная заявка (Ленинградская область/Регионы)», а
   вчерашние удаляет. Анонсы же («ЗАПУСК НОВОГО ОБЪЕКТА», «УВЕЛИЧЕНИЕ СТАВКИ»)
   не удаляет никогда: в октябре 2026 в канале лежали посты с апреля, и
   закрытые объекты (Комбинат шампанских вин, Ягодная долина) держались на них
   живыми. Поэтому разбираем списки и то, что вышло с утра того же дня, а
   более старые анонсы отбрасываем — см. current_messages.

Справку по объекту (адрес, функционал, проживание, выплаты, оформление) даёт
melior_kb: она приходит из таблицы «Объекты Мелиор» и подмешивается к разбору,
как описание проекта с диска у Градуса.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from loguru import logger

import melior_kb
from telegram_channel_processor import TelegramChannelProcessor

MSK = timezone(timedelta(hours=3))

# Ежедневный список: «АКТУАЛЬНАЯ ЗАЯВКА\n(Москва/МО):», «Актуальная заявка
# \n(Ленинградская область/Регионы)». Пишут то заглавными, то строчными.
DAILY_LIST_RE = re.compile(r"^\W*АКТУАЛЬНАЯ\s+ЗАЯВКА", re.IGNORECASE)

# Маркеры полей внутри блока объекта. Строка, начинающаяся с любого из них, —
# это продолжение блока, а не новый объект.
FIELD_MARKERS = (
    "👥", "🪪", "💵", "🥘", "🚨", "📑", "🔹", "💸", "💰", "🏠", "🚌", "📍", "⏰",
    "⚠️", "✅", "👇", "🔸", "▪️", "❗", "🆕", "🔝", "🆙",
)

# Строка с городом под названием объекта: «МО, г. Пушкино», «г. Москва, метро
# Южная», «Москва, Ⓜ️Алексеевская», «МО, г.о. Наро-Фоминск».
CITY_LINE_RE = re.compile(
    r"^\s*(МО\b|М\.?О\.?,|г\.|г\.о\.|Москва|Санкт|[А-ЯЁ][а-яё]+\s+обл|[А-ЯЁ][а-яё]+ская\s+обл)",
    re.IGNORECASE,
)

# Начало названия объекта: строка открывается не буквой и не цифрой (значит,
# эмодзи), а дальше идёт слово заглавными.
_LEAD_RE = re.compile(r"^[^\w(]+")

# Заглавная строка бывает не объектом, а поводом: «🚀ЗАПУСК ОБЪЕКТА🚀»,
# «‼️ЗАСЕЛЕНИЕ С 09.07‼️». Объектом такие строки считать нельзя — пост
# развалится по ним на куски, в которых нет ни одной вакансии.
EVENT_WORDS = (
    "ЗАПУСК", "ЗАПУСКАЕМ", "ВОЗОБНОВ", "УВЕЛИЧЕН", "ПОВЫШЕН", "ЗАСЕЛЕНИЕ",
    "ВНИМАНИЕ", "ВАЖНО", "АКТУАЛЬН", "ПЕРЕЕХАЛИ", "ВЕРНУЛИ", "ПРИНИМАЕМ",
    "СТОП", "ПАУЗА", "НОВЫЙ", "НОВОЕ", "ЕЩЁ ОДИН", "ЕЩЕ ОДИН", "КОЛЛЕГИ",
    "ПАРТНЕРЫ", "ПАРТНЁРЫ", "СРОЧНО", "ФОТО",
)


class MeliorMessageProcessor(TelegramChannelProcessor):
    SOURCE_NAME = "МЕЛИОР"
    SOURCE_URL_FALLBACK = "https://t.me/c/3439917213"
    COUNTERPARTY = "МЕЛИОР"

    # Профессии, которых нет в общем словаре: у МЕЛИОРа много пищевых
    # производств, и «лепщик» с «печником» больше нигде не встречаются.
    PROFESSION_HINTS = TelegramChannelProcessor.DEFAULT_PROFESSION_HINTS + (
        "стикеровщи", "лепщик", "печник", "котломой", "пекар", "тестовод",
        "подсобный", "производственной линии", "стропальщик", "погрузчик",
    )

    def __init__(self, sheets_service, llm_parser, db_path=None, use_kb=None):
        self.kb = melior_kb.load_kb(db_path) if (use_kb is None or use_kb) else None
        provider = self._make_context_provider() if self.kb is not None else None
        super().__init__(
            sheets_service,
            llm_parser,
            source_name=self.SOURCE_NAME,
            source_url_fallback=self.SOURCE_URL_FALLBACK,
            snapshot_marker=None,
            profession_hints=self.PROFESSION_HINTS,
            segment_emoji=None,          # режем по заголовку объекта, см. _iter_segments
            counterparty_override=self.COUNTERPARTY,
            context_provider=provider,
        )

    # ------------------------------------------------------ текущее состояние
    def current_messages(self, messages: List[Dict]) -> List[Dict]:
        """Посты, которые описывают то, что нужно сейчас.

        Это «Актуальные заявки», лежащие в канале, и всё, что вышло с начала
        дня (МСК) самой старой из них. Порог — день, а не минута списка: анонс
        нового объекта, вышедший утром до списков, в них ещё не попал (новый
        объект появляется в списке назавтра — Яндекс Казань, Ногинск, Армтек),
        и терять его на сутки незачем. Порог — самый старый список, а не самый
        свежий: если прогон попал между перепостами и Москва уже сегодняшняя, а
        Регионы ещё вчерашние, вчерашний список тоже в силе. В воскресенье
        постов нет, и в силе остаются субботние.

        Если списков в канале нет вовсе, отдаём всё как есть: значит, МЕЛИОР
        поменял формат, и гасить по такой пачке опаснее, чем держать лишнее.
        """
        daily = [m for m in messages if DAILY_LIST_RE.match(m.get("text") or "")]
        if not daily:
            logger.warning(
                f"[{self.source_name}] в канале нет ни одной «Актуальной заявки» — "
                f"разбираем все {len(messages)} сообщений, старые анонсы не отсекаются"
            )
            return list(messages)

        oldest = min(_msk(m["date"]) for m in daily)
        since = oldest.replace(hour=0, minute=0, second=0, microsecond=0)
        current = [m for m in messages if _msk(m["date"]) >= since]
        logger.info(
            f"[{self.source_name}] в силе {len(daily)} списка(ов) и посты с "
            f"{since:%d.%m %H:%M} МСК: {len(current)} из {len(messages)} сообщений, "
            f"старые анонсы отброшены"
        )
        return current

    def collect_requests(self, messages: List[Dict], source: str, source_url: str = ""):
        # Пайплайн считает пачку МЕЛИОРа снимком и гасит всё, чего в ней нет,
        # поэтому старые анонсы надо отсечь до разбора, а не после.
        return super().collect_requests(self.current_messages(messages), source, source_url)

    # -------------------------------------------------------------- справка
    def object_query(self, text: str) -> str:
        """Строки, по которым ищем объект в таблице: его название и город.

        Сопоставлять весь кусок нельзя: `ProjectKB` берёт первые три строки, а
        у поста это шапка повода — «МЫ ПЕРЕЕХАЛИ», пустая строка и восклицание
        про новый хостел. Название объекта стоит ниже, и по шапке карточка
        либо не находится, либо находится чужая.
        """
        lines = [line.strip() for line in text.split("\n")]
        heads = self._object_heads(text)
        if not heads:
            return text
        start = heads[0]
        return "\n".join(line for line in lines[start:start + 3] if line)

    def _make_context_provider(self):
        def provide(text: str) -> Tuple[Optional[str], Optional[Dict]]:
            hit = self.kb.match(self.object_query(text))
            if hit is None or not hit.card.doc_text.strip():
                return None, None
            return melior_kb.build_context(hit.card), hit.as_dict()

        return provide

    # ------------------------------------------------------------ детекция
    def _has_vacancy_signals(self, text: str) -> bool:
        """Заявка — это пост, в котором есть блок объекта с потребностью и ставкой.

        Общий словарь сигналов («руб», «смен») здесь слишком мягкий: им
        проходят и «КОЛЛЕГИ, ПРОСИМ ПРОВЕРИТЬ ОБЪЯВЛЕНИЯ», и поздравления с
        Новым годом, где смены упоминаются мимоходом. Требуем разметку: есть
        👥 (кого и сколько) и 💵 (почём) — значит это заявка.
        """
        if not text:
            return False
        if "👥" in text and "💵" in text:
            return True
        # Блок без разметки, но с названием объекта и городом, — тоже заявка:
        # изредка пост пишут прозой. Тогда полагаемся на общий словарь.
        return bool(self._object_heads(text)) and super()._has_vacancy_signals(text)

    # -------------------------------------------------------- сегментация
    def _object_heads(self, text: str) -> List[int]:
        """Номера строк, с которых начинается блок объекта."""
        lines = [line.strip() for line in text.split("\n")]
        heads: List[int] = []
        for i, line in enumerate(lines):
            if not line or line[0].isalnum() or line.startswith(FIELD_MARKERS):
                continue
            body = _LEAD_RE.sub("", line).strip()
            core = re.sub(r"\(.*?\)", "", body).strip()
            if len(core) < 3 or not re.match(r"^[А-ЯЁA-Z]", core):
                continue
            if core.upper().startswith(EVENT_WORDS):
                continue
            letters = [ch for ch in core if ch.isalpha()]
            if not letters or sum(1 for ch in letters if ch.isupper()) / len(letters) < 0.7:
                continue
            # Название объекта подтверждается тем, что идёт следом: город или
            # сразу потребность. Иначе «🔥УВЕЛИЧЕНИЕ ЗАЯВКИ🔥» стало бы объектом.
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if CITY_LINE_RE.match(nxt) or nxt.startswith(("👥", "🪪", "💵")) or "(" in body:
                heads.append(i)
        return heads

    def _iter_segments(self, text: str) -> List[str]:
        """Пост → куски по одному объекту.

        Шапку до первого объекта («АКТУАЛЬНАЯ ЗАЯВКА (Москва/МО):») не теряем,
        а приклеиваем к первому блоку: в ней бывает регион, общий для всего
        поста. Хвост после последнего блока («ФОТО ПРОЖИВАНИЯ НА ДИСКЕ» со
        ссылкой) остаётся в последнем куске — он про него и написан.

        Кусок без потребности и ставки объектом не считается и приклеивается к
        следующему: строка «‼️ЗАСЕЛЕНИЕ С 09.07‼️» выглядит как заголовок
        объекта ровно так же, как «✈️ АЭРОПОРТ», и, разрезав по ней, мы
        оставили бы аэропорт без единой вакансии, а все три должности — под
        заголовком «Заселение».
        """
        heads = self._object_heads(text)
        if len(heads) < 2:
            return [text]

        lines = text.split("\n")
        bounds = heads + [len(lines)]
        segments: List[str] = []
        pending = ""
        for index, start in enumerate(heads):
            block = "\n".join(lines[start:bounds[index + 1]]).strip()
            if pending:
                block = f"{pending}\n{block}".strip()
                pending = ""
            if not block:
                continue
            if "👥" not in block and "💵" not in block:
                pending = block
                continue
            segments.append(block)
        if pending:
            if segments:
                segments[-1] = f"{segments[-1]}\n{pending}"
            else:
                segments.append(pending)

        if len(segments) < 2:
            return [text]
        head_text = "\n".join(lines[: heads[0]]).strip()
        if head_text:
            segments[0] = f"{head_text}\n\n{segments[0]}"
        return segments


def _msk(value) -> datetime:
    """Дата сообщения в МСК. Telethon отдаёт aware-UTC, тесты — строку без
    пояса; её тоже считаем UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MSK)
