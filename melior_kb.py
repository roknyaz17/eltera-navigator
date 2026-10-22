"""База знаний по объектам МЕЛИОРа — Google-таблица «Объекты Мелиор».

У Градуса описание проекта лежит .docx-файлами в папке на Яндекс.Диске, у
МЕЛИОРа — листом таблицы: один лист = один объект, строки «поле → значение»
(адрес, операции и ставки, функционал, проживание, питание, выплаты ЗП,
спецодежда, медкнижка, способы оформления, особенности).

Хранилище разное, смысл один: пост в канале говорит, что нужно СЕГОДНЯ, а
карточка объекта — что это за объект вообще. Поэтому карточки кладём в ту же
таблицу `disk_projects` (source='melior') и дальше пользуемся готовым
`ProjectKB`: сопоставление поста с объектом, справка для модели, связь позиции
с карточкой и кнопки в /navigator — всё это уже написано под Градус и не знает,
откуда взялась карточка.

Фотографии объекта живут не в таблице, а ссылкой на Яндекс.Диск в самом посте
(«👇ФОТО ПРОЖИВАНИЯ НА ДИСКЕ👇»). Ссылка относится к объекту, а не к сообщению,
поэтому она запоминается в карточке — тогда кнопка есть у всех позиций объекта,
а не только у той, что приехала из этого поста.
"""

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import gspread
from loguru import logger
from oauth2client.service_account import ServiceAccountCredentials

from project_kb import (
    KBMatch,
    ProjectCard,
    ProjectKB,
    link_positions,
    normalize_tokens,
    save_card,
)
from registry import db, geo
from yandex_disk import PublicDisk, YandexDiskError, is_image

SOURCE = "melior"
CATEGORY = "Объекты Мелиор"

SHEET_ID = os.getenv(
    "MELIOR_SHEET_ID", "1c2bD5FXTjWfyhPk2kmv-kSd-VblHMK35txqwz-8XKmU",
)
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")

KB_ENABLED = os.getenv("MELIOR_KB_ENABLED", "1").strip().lower() not in ("0", "false", "no")

# Больше сорока строк в листе объекта не бывает: это карточка, а не таблица
# данных. Читаем с запасом, но не весь лист на тысячу пустых строк.
MAX_ROWS = 40
MAX_COLS = "Z"

# Ссылка на публичную папку Яндекс.Диска в тексте поста.
DISK_LINK_RE = re.compile(r"https://(?:disk\.yandex\.[a-z]+|yadi\.sk)/\S+")

PATH_PREFIX = "melior:"


def object_path(sheet_name: str) -> str:
    """Ключ карточки в базе: «melior:Летуаль»."""
    return f"{PATH_PREFIX}{sheet_name}"


def object_name(path: str) -> str:
    """Обратно: «melior:Летуаль» → «Летуаль».

    Это же имя стоит в сетке ставок контрагента, поэтому ставка рекрутёра
    привязывается к нему, а не к тому, как объект назвали в очередном посте:
    «ЛЕТУАЛЬ (склад косметики и парфюмерии)» и «Летуаль» — один объект, а для
    точного сравнения в rates это две разные строки.
    """
    path = (path or "").strip()
    return path[len(PATH_PREFIX):] if path.startswith(PATH_PREFIX) else ""


def _client() -> gspread.Client:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    return gspread.authorize(creds)


def _sheet_url(gid: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={gid}"


# «г. Пушкино», «г.о. Солнечногорск», «гор. Чехов» — город, названный прямо.
_CITY_PREFIX_RE = re.compile(
    # Двусоставные имена берём целиком: «Сергиев Посад», «Нижний Новгород».
    r"\bг(?:ор)?\.?\s*о?\.?\s*([А-ЯЁ][А-Яа-яЁё\-]{2,}(?:\s+[А-ЯЁ][а-яё]{2,})?)"
)
# Область, район, округ — это не город, и в названии карточки им не место.
_NOT_A_CITY_RE = re.compile(
    # «Наро-Фоминский г.о.» — это округ, а не город: точка после «о» отличает
    # его от «г. Обнинск», где за «г.» идёт само название.
    r"обл|район|округ|край|республик|федерал|\bг\.?\s*о\.|^\s*м\.?\s*о\.?\s*$",
    re.IGNORECASE,
)
# Улица, дом, корпус — адрес внутри города, а не сам город.
_STREET_RE = re.compile(
    r"\d|\bул\b|улиц|\bд\b|\bдом\b|\bстр\b|корп|шоссе|проезд|пр-д|просп|"
    r"влад|помещ|этаж|терр|ппх|мкр|метро|Ⓜ",
    re.IGNORECASE,
)


def city_from_address(address: str) -> str:
    """Город из адреса объекта — второй ключ сопоставления.

    Одного названия мало: пост «САНТЭНС (склад медикаментов и косметики)»
    ложится на карточку «Склад медикаментов» лучше, чем на свою собственную, —
    в скобках у него ровно её слова. Город это разводит: у Сантэнса Нахабино,
    у склада медикаментов Пушкино.

    Берём только то, в чём уверены: город из справочника координат. Область,
    район и посёлок не годятся — их в посте не пишут, а в названии карточки
    они размывают сопоставление: «Триол, Московская область» пост «🐾 ТРИОЛ /
    МО, Щелково» покрывает хуже, чем просто «Триол».
    """
    if not address:
        return ""
    chunks = [c.strip() for c in re.split(r"[,\n;]", address) if c.strip()]
    for chunk in chunks:
        if _NOT_A_CITY_RE.search(chunk):
            continue
        city = geo.normalize_city(chunk)
        if city and geo.coords(city):
            return city
    match = _CITY_PREFIX_RE.search(address)
    if match and not _NOT_A_CITY_RE.search(match.group(1)):
        city = geo.normalize_city(match.group(1))
        if geo.coords(city):
            return city
    # Дальше не гадаем. Посёлок из адреса («п. Новая Ольховка» у Кералита) в
    # постах никто не пишет — там стоит «МО, г.о. Наро-Фоминск», — и такое
    # название не помогает найти карточку, а мешает: объект перестаёт
    # узнаваться по имени и цепляется к соседу по городу. Объекты, названия
    # которых похожи, различает описание из шапки листа.
    return ""


def card_text(header: str, rows: Sequence[Tuple[str, str]]) -> str:
    """Лист объекта → текст карточки, как его увидит модель и человек."""
    parts = [header.strip()] if header.strip() else []
    for key, value in rows:
        value = re.sub(r"\s*\n\s*", "; ", value.strip())
        if key and value:
            parts.append(f"{key.strip().rstrip(':')}: {value}")
    return "\n".join(parts)


def _read_sheets() -> List[ProjectCard]:
    """Все листы таблицы → карточки объектов. Одним батч-запросом."""
    spreadsheet = _client().open_by_key(SHEET_ID)
    worksheets = spreadsheet.worksheets()
    ranges = [f"'{ws.title}'!A1:{MAX_COLS}{MAX_ROWS}" for ws in worksheets]
    blocks = spreadsheet.values_batch_get(ranges)["valueRanges"]

    cards: List[ProjectCard] = []
    for worksheet, block in zip(worksheets, blocks):
        values = [row for row in block.get("values", []) if any((c or "").strip() for c in row)]
        if not values:
            logger.warning(f"[melior-kb] лист «{worksheet.title}» пуст — пропускаю")
            continue
        header = (values[0][0] if values[0] else "").strip()
        rows = [
            ((row[0] if row else ""), (row[1] if len(row) > 1 else ""))
            for row in values[1:]
        ]
        text = card_text(header, rows)
        # Название объекта берём из имени листа: в шапке оно идёт с довеском
        # «(склад косметики), вахта от 21 смены», и в токенах этот довесок
        # только мешает — «склад» есть у половины объектов. К названию
        # добавляем город из адреса: по нему карточки и различаются, когда
        # названия похожи (как у папок Градуса — «BMJ, Шарапово»).
        # Имя листа — ключ объекта: по нему заводятся ставки рекрутёра и по
        # нему карточка остаётся собой между обходами. Всё остальное
        # (описание, город) — украшение названия и меняется вместе с таблицей.
        sheet_name = re.sub(r"\s+", " ", worksheet.title).strip()
        title = sheet_name
        # Скобка из шапки листа («Сантэнс (склад медикаментов и косметики)»)
        # идёт в название: в посте объект нередко называют одним описанием, без
        # имени — «💉 СКЛАД МЕДИКАМЕНТОВ И КОСМЕТИКИ, МО, Нахабино». Без этих
        # слов такой пост уходит к пушкинскому «Складу медикаментов».
        descr = re.search(r"\(([^)]{3,60})\)", header)
        if descr:
            title = f"{title} ({descr.group(1).strip()})"
        address = next((v for k, v in rows if k.strip().lower().startswith("адрес")), "")
        city = city_from_address(address)
        if city and city.lower() not in title.lower():
            title = f"{title}, {city}"
        cards.append(ProjectCard(
            path=object_path(sheet_name),
            name=worksheet.title,
            title=title,
            category=CATEGORY,
            url=_sheet_url(worksheet.id),
            doc_text=text,
            docs=[{"name": "Карточка объекта", "text": text, "text_len": len(text)}],
            albums=[],
            photos=0,
            # Токены при загрузке из базы пересчитываются из title (см.
            # project_kb._row_to_card), поэтому сокращения вроде «ЖБИ» живут
            # в общем словаре TOKEN_ALIASES, а не здесь.
            tokens=normalize_tokens(title),
            modified="",
            fingerprint=hashlib.sha1(text.encode("utf-8")).hexdigest(),
        ))
    return cards


def refresh(db_path: Optional[str] = None, keep_albums: bool = True) -> Dict[str, int]:
    """Перечитывает таблицу и обновляет карточки объектов в базе.

    :param keep_albums: сохранить уже найденные фотоальбомы. Ссылки на диск
        приходят из постов канала, а не из таблицы, и переобход таблицы не
        должен стирать кнопку «Фото проживания».
    """
    stats = {"objects": 0, "updated": 0, "removed": 0}
    cards = _read_sheets()
    now = datetime.now().isoformat(timespec="seconds")

    with db.connect(db_path) as conn:
        known = {
            row["path"]: row
            for row in conn.execute(
                "SELECT * FROM disk_projects WHERE source = ?", (SOURCE,)
            ).fetchall()
        }
        for card in cards:
            stats["objects"] += 1
            old = known.get(card.path)
            if old is not None and keep_albums:
                card.albums = _json_albums(old["albums"])
                card.photos = old["photos"]
            if old is None or old["fingerprint"] != card.fingerprint:
                stats["updated"] += 1
            save_card(conn, card, SOURCE, now)

        stale = [path for path in known if path not in {c.path for c in cards}]
        for path in stale:
            conn.execute(
                "DELETE FROM disk_projects WHERE source = ? AND path = ?", (SOURCE, path),
            )
        stats["removed"] = len(stale)

    stats["linked"] = link_positions(db_path, SOURCE)["linked"]
    logger.info(
        f"[melior-kb] объектов в таблице: {stats['objects']}, обновлено: {stats['updated']}, "
        f"удалено: {stats['removed']}, позиций с карточкой: {stats['linked']}"
    )
    return stats


def _json_albums(raw) -> List[Dict]:
    try:
        return json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []


# ----------------------------------------------------------- фото из постов

def attach_disk_albums(
        messages: Sequence[Dict],
        db_path: Optional[str] = None,
        query_fn=None,
) -> Dict[str, int]:
    """Привязывает папки Яндекс.Диска из постов к карточкам объектов.

    Пост с фотографиями выглядит так: блок объекта, ниже «ФОТО ПРОЖИВАНИЯ НА
    ДИСКЕ» и ссылка. Объект определяем тем же сопоставлением, что и справку, —
    по тексту поста. Не опознали объект (или в папке нет ни одной фотографии)
    — ссылку не записываем: кнопка, ведущая в чужой хостел, хуже отсутствующей.

    :param query_fn: как выделить из поста строки про объект. Передаёт
        процессор канала — он один знает разметку постов. Без него сопоставляем
        по тексту как есть.
    """
    stats = {"links": 0, "attached": 0, "photos": 0, "skipped": 0}
    kb = ProjectKB.load(db_path=db_path, source=SOURCE)
    if kb.is_empty:
        logger.warning("[melior-kb] карточек объектов нет — сначала нужен refresh()")
        return stats

    now = datetime.now().isoformat(timespec="seconds")
    found: Dict[str, Dict] = {}
    for msg in sorted(messages, key=lambda m: m.get("date") or ""):
        text = (msg.get("text") or "").strip()
        links = DISK_LINK_RE.findall(text)
        if not links:
            continue
        stats["links"] += len(links)
        hit = kb.match(query_fn(text) if query_fn else text)
        if hit is None:
            stats["skipped"] += len(links)
            preview = text.split("\n")[0][:60]
            logger.info(f"[melior-kb] ссылка на диск без опознанного объекта: {preview!r}")
            continue
        # Пост свежее — ссылка свежее: контрагент переезжает из хостела в
        # хостел («МЫ ПЕРЕЕХАЛИ»), и старый альбом уже не про это проживание.
        found[hit.card.path] = {"url": links[-1].rstrip(").,"), "title": hit.card.title}

    for path, item in found.items():
        photos = _count_photos(item["url"])
        if not photos:
            stats["skipped"] += 1
            continue
        with db.connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM disk_projects WHERE source = ? AND path = ?", (SOURCE, path),
            ).fetchone()
            if row is None:
                continue
            album = {"name": "Фото проживания", "url": item["url"], "photos": photos}
            conn.execute(
                "UPDATE disk_projects SET albums = ?, photos = ?, indexed_at = ? "
                "WHERE source = ? AND path = ?",
                (json.dumps([album], ensure_ascii=False), photos, now, SOURCE, path),
            )
        stats["attached"] += 1
        stats["photos"] += photos
        logger.info(f"[melior-kb] {item['title']}: фотоальбом на {photos} фото")

    if stats["attached"]:
        link_positions(db_path, SOURCE)
    return stats


def _count_photos(public_url: str) -> int:
    """Сколько фотографий в публичной папке. Диск недоступен — считаем, что нет."""
    try:
        items = PublicDisk(public_url).listdir("/")
    except YandexDiskError as exc:
        logger.warning(f"[melior-kb] папка {public_url}: {exc}")
        return 0
    return sum(1 for item in items if is_image(item.get("name", "")))


# ------------------------------------------------------------------ справка

CONTEXT_HEADER = (
    "КАРТОЧКА ОБЪЕКТА из таблицы контрагента (постоянное описание, не заявка):"
)

# Что делать с полями карточки, для которых в реестре нет своей колонки.
# Заводить их ради одного источника не станем: у остальных пяти источников
# этих данных нет, и колонка стояла бы пустой у сотни позиций, притворяясь
# пробелом в заявке. Поэтому — в текстовые поля, где менеджер их и ищет.
CONTEXT_HINTS = (
    "Способы оформления (ТК, СЗ, ГПХ, СМЗ) добавь в requirements. "
    "Условия выплаты ЗП и авансов добавь в advantages."
)


def build_context(card: ProjectCard) -> str:
    """Справка по объекту в том виде, в каком её видит модель."""
    return f"{CONTEXT_HEADER}\n«{card.title}»\n{card.doc_text}\n\n{CONTEXT_HINTS}"


def load_kb(db_path: Optional[str] = None) -> Optional[ProjectKB]:
    """Карточки объектов из базы. None — работаем без справок, как любой канал."""
    if not KB_ENABLED:
        return None
    try:
        kb = ProjectKB.load(db_path=db_path, source=SOURCE)
    except Exception as exc:  # noqa: BLE001 — без справок прогон полноценен
        logger.warning(f"[melior-kb] карточки объектов не загрузились: {exc}")
        return None
    if kb.is_empty:
        logger.info("[melior-kb] таблица объектов ещё не прочитана — разбор без справок")
        return None
    logger.info(f"[melior-kb] карточек объектов: {len(kb.cards)}")
    return kb
