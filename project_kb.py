"""База знаний по проектам: папка контрагента на Яндекс.Диске → справка для разбора.

Зачем это нужно. Пост в канале Градуса — это телеграмма: «BMJ, Шарапово, РФ
до 55, грузчик 3520, вахта от 20». В нём принципиально нет того, что не
меняется день ото дня: адреса объекта, часов смены, что за склад, сколько
человек в комнате, есть ли форма и медкнижка. Всё это лежит у контрагента на
Яндекс.Диске — по папке на проект, с описанием и фотографиями проживания.
Раньше эти данные до реестра не доезжали вообще: модель видела только пост, а
додумывать ей запрещено (и правильно).

Схема:

    пост из канала
       ↓  первая строка — «шапка» проекта
    ProjectKB.match — папка проекта на диске (по токенам с весами)
       ↓
    ProjectKB.context_for — справка: текст «Описания проекта» + перечень
    документов и фотоальбомов
       ↓
    vacancy_parser — разбирает пост, СПРАВКА только дополняет молчащие поля

Границы, которые здесь важны:

* Совпадение должно быть уверенным. Не нашли — работаем как раньше, без
  справки: пустое поле честнее, чем условия чужого проекта.
* Справка не создаёт позиций. Потребность, ставки и возраст берутся только из
  поста — за это отвечает промпт (см. vacancy_parser.SYSTEM_PROMPT), а
  проверяется это скриптом scripts/vahtapro_ab_test.py.
* Индекс живёт в SQLite и обновляется отдельно от разбора: во время прогона в
  сеть за диском не ходим.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from loguru import logger

from registry import db, geo
from yandex_disk import PublicDisk, YandexDiskError, file_to_text, is_image, is_readable

# Публичная ссылка на диск Градуса. Вынесена в env, чтобы у следующего
# контрагента с таким же диском не пришлось править код.
DEFAULT_DISK_URL = os.getenv(
    "VAHTAPRO_DISK_URL", "https://disk.yandex.ru/d/mThqHeyoM1rDGw"
)
KB_ENABLED = os.getenv("VAHTAPRO_KB_ENABLED", "1").strip().lower() not in ("0", "false", "no")
KB_REFRESH_HOURS = float(os.getenv("VAHTAPRO_KB_REFRESH_HOURS", "6"))

# Сколько символов описания уходит в промпт. Описания проектов — 1–3 КБ, так
# что предел скорее страховка от документа-простыни, чем реальная отсечка.
MAX_DOC_CHARS = 4000

# ------------------------------------------------------------------ токенизация

# Шум написания адреса, который есть в одних названиях и отсутствует в других.
STOP_TOKENS = frozenset({
    "обл", "область", "областная", "мос", "мо", "рф",
    "г", "гор", "город", "пгт", "рп", "д", "дер", "с", "село", "ул",
    "проект", "проекты", "проектов",
})

# Сокращения, которыми пишут города. Без них «Все инструменты ДМД» и «Все
# инструменты Домодедово» — четыре одинаково похожие папки и ни одного
# уверенного совпадения.
TOKEN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "дмд": ("домодедово",),
    "екб": ("екатеринбург",),
    "нн": ("нижний", "новгород"),
    "нч": ("набережные", "челны"),
    "спб": ("санкт", "петербург"),
    "питер": ("санкт", "петербург"),
    "мск": ("москва",),
    # Латиница в названии папки против кириллицы в посте: транслитерация тут
    # не помогает («бмж» → «bmzh»), поэтому пара задана явно.
    "бмж": ("bmj",),
}

# Слова, которые описывают тип предприятия, а не проект. Брендом работать не
# могут, даже если в индексе встретились однажды: «фабрика» в посте не значит,
# что это та самая фабрика.
GENERIC_TOKENS = frozenset({
    "склад", "склады", "производство", "фабрика", "кондитерская", "комбинат",
    "мясокомбинат", "логистик", "логистика", "персонал", "местный", "местные",
    "смены", "смена", "ночные", "дневные",
})

# Подчёркивание тоже разделитель: в канале пишут «Балтика_Хабаровск».
_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize_tokens(text: str) -> List[str]:
    """Название проекта → список значимых токенов.

    Эмодзи, знаки препинания и «(Мос. обл)» уходят: в папке «BMJ, Шарапово ✅»
    и в посте «🚀BMJ, Шарапово (Мос. Обл)» значимого одно и то же.
    """
    if not text:
        return []
    low = text.lower().replace("ё", "е")
    tokens: List[str] = []
    for chunk in _WORD_RE.split(low):
        if not chunk or chunk in STOP_TOKENS:
            continue
        if chunk.isdigit() and len(chunk) <= 2:
            # Нумерация папок («1. Москва и МО») и одиночные цифры смысла не несут.
            continue
        tokens.extend(TOKEN_ALIASES.get(chunk, (chunk,)))
    return tokens


def _glue(tokens: Sequence[str]) -> List[str]:
    """Склейки соседних токенов: «Рот Фронт» ↔ «РотФронт»."""
    return [tokens[i] + tokens[i + 1] for i in range(len(tokens) - 1)]


def _geo_tokens() -> frozenset:
    """Слова из названий, которые обозначают место, а не проект.

    Берутся из справочника городов реестра: он и так знает, что «Шарапово» —
    населённый пункт, а «Сбер» — нет.
    """
    out = set()
    for name in list(geo.CITY_COORDS) + list(geo.CITY_ALIASES):
        out.update(normalize_tokens(name))
    out.update({"тульская", "московская", "липецкая", "воронежская", "нижний", "новгород"})
    return frozenset(out)


GEO_TOKENS = _geo_tokens()


def _brands(tokens: Iterable[str]) -> set:
    """Токены названия, которые не являются географией, — то есть бренд."""
    return {token for token in tokens if token not in GEO_TOKENS}


def _similar(a: str, b: str) -> bool:
    """Совпадение токенов с поправкой на склонения и опечатки.

    Общее начало засчитывается только при близкой длине: «солнечногорск» и
    «солнечногорске» — одно слово, а «инструменты» и «инструментычашниково» —
    нет (второе тут появляется из склейки соседних токенов).
    """
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    if a.startswith(b) or b.startswith(a):
        return min(len(a), len(b)) >= 5 and abs(len(a) - len(b)) <= 3
    return SequenceMatcher(None, a, b).ratio() >= 0.86


# ---------------------------------------------------------------------- модели

@dataclass
class ProjectCard:
    """Папка проекта на диске в том виде, в каком её использует разбор."""

    path: str
    name: str
    title: str
    category: str
    url: str
    doc_text: str = ""
    docs: List[Dict] = field(default_factory=list)
    albums: List[Dict] = field(default_factory=list)
    photos: int = 0
    tokens: List[str] = field(default_factory=list)
    modified: str = ""
    indexed_at: str = ""
    # Отпечаток содержимого папки — по нему решается, перекачивать ли документы.
    fingerprint: str = ""

    @property
    def has_description(self) -> bool:
        return bool(self.doc_text.strip())


@dataclass
class KBMatch:
    """Результат сопоставления поста с папкой проекта."""

    card: ProjectCard
    score: float
    runner_up: str = ""
    runner_up_score: float = 0.0
    query: str = ""

    def as_dict(self) -> Dict:
        return {
            "project": self.card.title,
            "path": self.card.path,
            "url": self.card.url,
            "score": round(self.score, 3),
            "runner_up": self.runner_up,
            "runner_up_score": round(self.runner_up_score, 3),
            "docs": [d["name"] for d in self.card.docs if d.get("text_len")],
            "photos": self.card.photos,
        }


# ------------------------------------------------------------------- сама база

class ProjectKB:
    """Индекс проектов в памяти + сопоставление поста с проектом.

    Пороги подобраны так, чтобы промах был дешевле ошибки: не уверены —
    возвращаем None, и заявка разбирается ровно как до появления диска.
    """

    MIN_COVERAGE = 0.55        # какую долю (по весам) названия папки покрыл пост
    MIN_MARGIN = 0.12          # отрыв от второго кандидата
    MIN_BRAND_COVERAGE = 0.30  # тот же порог для совпадения по уникальному бренду
    MAX_QUERY_LINES = 3        # сколько первых строк поста считаем «шапкой»

    def __init__(self, cards: Optional[Iterable[ProjectCard]] = None):
        self.cards: List[ProjectCard] = list(cards or [])
        self._weights: Dict[str, float] = {}
        self._doc_freq: Dict[str, int] = {}
        self._recompute_weights()

    # ------------------------------------------------------------ загрузка
    @classmethod
    def load(cls, db_path: Optional[str] = None, source: str = "vahtapro") -> "ProjectKB":
        with db.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM disk_projects WHERE source = ? ORDER BY path", (source,)
            ).fetchall()
        return cls(_row_to_card(row) for row in rows)

    @property
    def is_empty(self) -> bool:
        return not self.cards

    def _recompute_weights(self) -> None:
        """Вес токена — обратная частота.

        «Все» и «инструменты» встречаются в четырёх папках и почти ничего не
        различают, а «Чашниково» — ровно в одной. Без весов пост «Все
        инструменты Домодедово» одинаково похож на четыре разных проекта.
        """
        total = len(self.cards) or 1
        freq: Dict[str, int] = {}
        for card in self.cards:
            for token in set(card.tokens):
                freq[token] = freq.get(token, 0) + 1
        self._doc_freq = freq
        self._weights = {
            token: 1.0 + (total / count) ** 0.5 for token, count in freq.items()
        }

    def _weight(self, token: str) -> float:
        return self._weights.get(token, 1.0 + float(len(self.cards) or 1) ** 0.5)

    # ------------------------------------------------------------ сравнение
    def _coverage(self, card: ProjectCard, query_tokens: Sequence[str]) -> Tuple[float, List[str]]:
        if not card.tokens:
            return 0.0, []
        pool = set(query_tokens) | set(_glue(query_tokens))
        matched: List[str] = []
        for token in card.tokens:
            if any(_similar(token, q) for q in pool):
                matched.append(token)
        # Обратная склейка: в папке «Рот Фронт», в посте «РотФронт». Сравнение
        # здесь строгое: «кюхенлендт2» похоже на «кюхенленд» ровно настолько,
        # чтобы засчитать несуществующее совпадение по второму слову.
        for i, glued in enumerate(_glue(card.tokens)):
            if glued in pool:
                matched.extend(card.tokens[i:i + 2])
        matched = list(dict.fromkeys(matched))

        total = sum(self._weight(t) for t in set(card.tokens))
        got = sum(self._weight(t) for t in matched)
        return (got / total if total else 0.0), matched

    def match(self, text: str) -> Optional[KBMatch]:
        """Ищет проект по тексту поста. None — уверенного совпадения нет."""
        if self.is_empty or not text:
            return None
        for query in self._query_variants(text):
            hit = self._match_query(query)
            if hit:
                return hit
        return None

    def _query_variants(self, text: str) -> List[str]:
        """Шапка поста — первая строка; если не хватило, первые три.

        Дальше по тексту идут должности и ставки: «маркировка», «грузчик»,
        «питание» — они похожи на слова из названий чужих проектов и только
        мешают.
        """
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return []
        variants = [lines[0]]
        if len(lines) > 1:
            variants.append(" ".join(lines[:self.MAX_QUERY_LINES]))
        return variants

    def _match_query(self, query: str) -> Optional[KBMatch]:
        tokens = normalize_tokens(query)
        if not tokens:
            return None

        scored: List[Tuple[float, ProjectCard, List[str]]] = []
        for card in self.cards:
            coverage, matched = self._coverage(card, tokens)
            if coverage > 0:
                scored.append((coverage, card, matched))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)

        best_score, best_card, best_matched = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        second_name = scored[1][1].title if len(scored) > 1 else ""

        if self._accepts(best_score, second_score, best_card, best_matched, tokens):
            return KBMatch(
                card=best_card, score=best_score,
                runner_up=second_name, runner_up_score=second_score,
                query=query,
            )

        brand = self._by_unique_brand(scored, tokens)
        if brand is not None:
            score, card, _ = brand
            return KBMatch(
                card=card, score=score,
                runner_up=second_name if card is not best_card else "",
                runner_up_score=second_score if card is not best_card else 0.0,
                query=query,
            )

        logger.debug(
            f"[kb] не опознано: {query!r} → лучший {best_card.title} ({best_score:.2f}), "
            f"следующий {second_name} ({second_score:.2f})"
        )
        return None

    def _accepts(
            self,
            best_score: float,
            second_score: float,
            card: ProjectCard,
            matched: Sequence[str],
            query_tokens: Sequence[str],
    ) -> bool:
        if best_score < self.MIN_COVERAGE:
            return False
        # Две папки похожи одинаково — это «Все инструменты» без города.
        # Подсунуть любую из них хуже, чем не подсунуть ничего.
        if best_score - second_score < self.MIN_MARGIN:
            return False
        if not _brand_agrees(card, matched, query_tokens):
            return False
        # Самый редкий токен названия (обычно бренд) обязан найтись в посте:
        # иначе «Хофф, Домодедово» подберёт себе любой проект в Домодедово.
        key_weight = max(self._weight(t) for t in set(card.tokens))
        return any(abs(self._weight(t) - key_weight) < 1e-9 for t in matched)

    def _by_unique_brand(
            self,
            scored: Sequence[Tuple[float, ProjectCard, List[str]]],
            query_tokens: Sequence[str],
    ) -> Optional[Tuple[float, ProjectCard, List[str]]]:
        """Запасной путь: бренд, который есть ровно у одной папки на диске.

        Пост сплошь и рядом называет место иначе, чем папка: «Мултон, г.
        Москва» против «Мултон, Солнцево», «Спортмастер (Мос.обл)» против
        «Спортмастер, Старая Купавна». По доле покрытия названия это мимо, но
        проект с таким названием на диске один-единственный — и это он.

        Именно поэтому проверка идёт по редкости токена, а не по позиции в
        названии: «склад» и «производство» встречаются у многих и брендом быть
        не могут, а «мултон» — только здесь.
        """
        candidates = [
            item for item in scored
            if item[0] >= self.MIN_BRAND_COVERAGE
            and self._brand_tokens(item[2])
            and _brand_agrees(item[1], item[2], query_tokens)
        ]
        if len(candidates) != 1:
            return None
        return candidates[0]

    def _brand_tokens(self, matched: Sequence[str]) -> List[str]:
        """Совпавшие токены, которые встречаются ровно в одной папке индекса."""
        return [
            token for token in matched
            if len(token) >= 4
            and token not in GENERIC_TOKENS
            and self._doc_freq.get(token, 0) == 1
        ]

    # -------------------------------------------------------------- справка
    def context_for(self, text: str) -> Tuple[Optional[str], Optional[Dict]]:
        """(текст справки для модели, что записать в заявку) или (None, None)."""
        hit = self.match(text)
        if not hit or not hit.card.has_description:
            # Папка без описания (одни фотографии) полезна человеку, но модели
            # добавить нечего — контекст не подмешиваем.
            if hit:
                logger.debug(f"[kb] {hit.card.title}: нет описания, справка не собрана")
            return None, None
        return build_context(hit.card), hit.as_dict()


    # ------------------------------------------------------------- позиции
    def match_position(self, counterparty: str, object_name: str, city: str) -> Optional[KBMatch]:
        """Проект позиции — по её собственным полям, а не по заявке.

        Пост-сводка описывает десяток проектов сразу, и какая позиция из
        какого куска — после нормализации уже не восстановить. Зато у позиции
        есть ровно то, из чего состоят названия папок: контрагент, объект,
        город («BMJ» + «Шарапово» → папка «BMJ, Шарапово»).
        """
        query = " ".join(part for part in (counterparty, object_name, city) if part)
        return self.match(query) if query.strip() else None


def _brand_agrees(card: ProjectCard, matched: Sequence[str], query_tokens: Sequence[str]) -> bool:
    """Совпал ли бренд, а не только город.

    Без этой проверки позиция «BMJ, Шарапово» цепляется к папке «Сбер
    Шарапово»: город редкий, покрытия хватает, а то, что это вообще другая
    компания, по весам не видно. Правило срабатывает, только когда бренд есть
    с обеих сторон: у «Атяшево Торбеево» название целиком географическое, и
    требовать от него бренд нечестно.
    """
    card_brands = _brands(card.tokens)
    query_brands = _brands(query_tokens)
    if not card_brands or not query_brands:
        return True
    return bool(card_brands & set(matched))


def photos_url(card: ProjectCard) -> str:
    """Куда ведёт кнопка «Фото объекта».

    Один альбом — сразу в него, чтобы рекрутёр попадал на фотографии, а не в
    папку с документами. Несколько — в папку проекта: выбирать за человека,
    какое из двух общежитий ему нужно, мы не можем.
    """
    albums = [a for a in card.albums if a.get("photos") and a.get("url")]
    if len(albums) == 1:
        return albums[0]["url"]
    return card.url


def link_positions(db_path: Optional[str] = None, source: str = "vahtapro") -> Dict[str, int]:
    """Пересчитывает связь «позиция → папка проекта» для источника.

    Вызывается после обхода диска и после каждого прогона: папки на диске
    переименовывают, позиции появляются и меняют объект. Не опознали — строку
    удаляем, и кнопка «Фото объекта» у позиции просто пропадает.
    """
    kb = ProjectKB.load(db_path=db_path, source=source)
    stats = {"positions": 0, "linked": 0, "unlinked": 0}
    if kb.is_empty:
        return stats

    now = datetime.now().isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT position_id, counterparty, object_name, city FROM positions WHERE source = ?",
            (source,),
        ).fetchall()
        for row in rows:
            stats["positions"] += 1
            hit = kb.match_position(
                row["counterparty"] or "", row["object_name"] or "", row["city"] or "",
            )
            if hit is None:
                conn.execute(
                    "DELETE FROM position_kb WHERE position_id = ?", (row["position_id"],)
                )
                stats["unlinked"] += 1
                continue
            conn.execute(
                """
                INSERT INTO position_kb (
                    position_id, source, project, path, folder_url, photos_url,
                    photos, score, linked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    source = excluded.source, project = excluded.project,
                    path = excluded.path, folder_url = excluded.folder_url,
                    photos_url = excluded.photos_url, photos = excluded.photos,
                    score = excluded.score, linked_at = excluded.linked_at
                """,
                (
                    row["position_id"], source, hit.card.title, hit.card.path,
                    hit.card.url, photos_url(hit.card), hit.card.photos,
                    round(hit.score, 3), now,
                ),
            )
            stats["linked"] += 1

    logger.info(
        f"[kb] позиции {source}: с папкой проекта {stats['linked']} из {stats['positions']}"
    )
    return stats


def build_context(card: ProjectCard) -> str:
    """Справка по проекту в том виде, в каком её видит модель."""
    parts = [f"Папка проекта: «{card.title}» (раздел «{card.category}»)"]
    for doc in card.docs:
        if not doc.get("text_len"):
            continue
        parts.append(f"\nДокумент «{doc['name']}»:\n{doc.get('text', '').strip()}")
    others = [d["name"] for d in card.docs if not d.get("text_len")]
    if others:
        # Имена файлов сами по себе информативны: «Направление Хостел
        # Березка.docx» называет место проживания.
        parts.append("\nДругие документы проекта: " + ", ".join(others))
    if card.albums:
        albums = ", ".join(f"«{a['name']}» ({a.get('photos', 0)} фото)" for a in card.albums)
        parts.append(f"\nФотоальбомы проекта: {albums}")
    text = "\n".join(parts)
    return text[:MAX_DOC_CHARS * 2]


# --------------------------------------------------------------- индексация

# Документы, которые идут в справку текстом. Остальные (направления на
# заселение) — только именами: там инструкции по заселению и телефоны
# коменданта, модели они не нужны, а в реестр телефоны людей тянуть незачем.
DESCRIPTION_HINTS = ("опис", "проект", "инфо", "памятка", "услови")


def _is_description(name: str) -> bool:
    low = name.lower()
    if "направлени" in low or "заселени" in low:
        return False
    return any(hint in low for hint in DESCRIPTION_HINTS)


class DiskIndexer:
    """Обход диска и запись индекса в базу.

    Инкрементально: у каждой папки проекта считается отпечаток содержимого
    (имена, размеры, md5 файлов). Не изменился — документы не перекачиваются,
    и полный обход стоит один листинг на проект.
    """

    def __init__(
            self,
            disk: Optional[PublicDisk] = None,
            db_path: Optional[str] = None,
            source: str = "vahtapro",
    ):
        self.disk = disk or PublicDisk(DEFAULT_DISK_URL)
        self.db_path = db_path
        self.source = source

    def refresh(self, force: bool = False) -> Dict[str, int]:
        stats = {"projects": 0, "updated": 0, "docs_read": 0, "removed": 0, "errors": 0}
        now = datetime.now().isoformat(timespec="seconds")

        with db.connect(self.db_path) as conn:
            known = {
                row["path"]: row
                for row in conn.execute(
                    "SELECT * FROM disk_projects WHERE source = ?", (self.source,)
                ).fetchall()
            }

            seen: List[str] = []
            for category in self._categories():
                for folder in self._safe_listdir(category["path"], stats):
                    if folder.get("type") != "dir":
                        continue
                    stats["projects"] += 1
                    seen.append(folder["path"])
                    try:
                        card, changed, docs_read = self._read_project(
                            folder, category["name"], known.get(folder["path"]), force
                        )
                    except YandexDiskError as exc:
                        stats["errors"] += 1
                        logger.warning(f"[kb] {folder['name']}: {exc}")
                        continue
                    stats["docs_read"] += docs_read
                    if changed:
                        stats["updated"] += 1
                    self._save(conn, card, now)

            stale = [path for path in known if path not in seen]
            for path in stale:
                conn.execute(
                    "DELETE FROM disk_projects WHERE source = ? AND path = ?",
                    (self.source, path),
                )
            stats["removed"] = len(stale)

        # Индекс изменился — пересчитываем, у каких позиций есть папка с
        # фотографиями. Переименовали папку на диске — кнопка в /navigator
        # начнёт вести по новому адресу уже после этого обхода.
        stats["linked"] = link_positions(self.db_path, self.source)["linked"]

        logger.info(
            f"[kb] индекс диска: проектов={stats['projects']}, обновлено={stats['updated']}, "
            f"документов прочитано={stats['docs_read']}, удалено={stats['removed']}, "
            f"ошибок={stats['errors']}, позиций с папкой={stats['linked']}"
        )
        return stats

    # --------------------------------------------------------------- обход
    def _categories(self) -> List[Dict]:
        """Разделы верхнего уровня («1. Москва и МО Проекты», «2. Регионы»)."""
        return [item for item in self.disk.listdir("/") if item.get("type") == "dir"]

    def _safe_listdir(self, path: str, stats: Dict[str, int]) -> List[Dict]:
        try:
            return self.disk.listdir(path)
        except YandexDiskError as exc:
            stats["errors"] += 1
            logger.warning(f"[kb] не прочитал папку {path}: {exc}")
            return []

    def _read_project(
            self,
            folder: Dict,
            category: str,
            row,
            force: bool,
    ) -> Tuple[ProjectCard, bool, int]:
        items = self.disk.listdir(folder["path"])
        fingerprint = _fingerprint(items)
        unchanged = row is not None and row["fingerprint"] == fingerprint and not force

        if unchanged:
            card = _row_to_card(row)
            card.modified = folder.get("modified") or card.modified
            card.fingerprint = fingerprint
            return card, False, 0

        docs: List[Dict] = []
        albums: List[Dict] = []
        docs_read = 0
        failed = 0
        for item in sorted(items, key=lambda i: i.get("name", "")):
            name = item.get("name", "")
            if item.get("type") == "dir":
                albums.append({
                    "name": name,
                    "photos": self._count_photos(item["path"]),
                    "url": self.disk.folder_url(item["path"]),
                })
                continue
            if not is_readable(name):
                continue
            entry = {"name": name, "size": item.get("size", 0), "text": "", "text_len": 0}
            if _is_description(name):
                try:
                    blob = self.disk.download(item["path"])
                except YandexDiskError as exc:
                    failed += 1
                    logger.warning(f"[kb] {folder['name']}: не скачал «{name}»: {exc}")
                    docs.append(entry)
                    continue
                text = file_to_text(name, blob)[:MAX_DOC_CHARS]
                entry["text"] = text
                entry["text_len"] = len(text)
                docs_read += 1
            docs.append(entry)

        title = _clean_title(folder["name"])
        card = ProjectCard(
            path=folder["path"],
            name=folder["name"],
            title=title,
            category=category.strip(),
            url=self.disk.folder_url(folder["path"]),
            doc_text="\n\n".join(d["text"] for d in docs if d["text_len"]),
            docs=docs,
            albums=albums,
            photos=sum(a.get("photos", 0) for a in albums),
            tokens=normalize_tokens(title),
            modified=folder.get("modified", ""),
            # Отпечаток пустой, если хоть один документ не скачался: иначе
            # проект навсегда останется без описания — папка ведь не менялась.
            fingerprint="" if failed else fingerprint,
        )
        return card, True, docs_read

    def _count_photos(self, path: str) -> int:
        try:
            return sum(1 for item in self.disk.listdir(path) if is_image(item.get("name", "")))
        except YandexDiskError:
            return 0

    def _save(self, conn, card: ProjectCard, now: str) -> None:
        conn.execute(
            """
            INSERT INTO disk_projects (
                path, source, category, name, title, tokens, url,
                doc_text, docs, albums, photos, modified, fingerprint, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                category = excluded.category, name = excluded.name, title = excluded.title,
                tokens = excluded.tokens, url = excluded.url, doc_text = excluded.doc_text,
                docs = excluded.docs, albums = excluded.albums, photos = excluded.photos,
                modified = excluded.modified, fingerprint = excluded.fingerprint,
                indexed_at = excluded.indexed_at
            """,
            (
                card.path, self.source, card.category, card.name, card.title,
                " ".join(card.tokens), card.url, card.doc_text,
                json.dumps(card.docs, ensure_ascii=False),
                json.dumps(card.albums, ensure_ascii=False),
                card.photos, card.modified,
                card.fingerprint, now,
            ),
        )


def _clean_title(name: str) -> str:
    """Имя папки без галочек и хвостовых пробелов: «BMJ, Шарапово ✅» → «BMJ, Шарапово»."""
    title = re.sub(r"[✅✔️☑️❗️🔥⛔️]+", " ", name)
    return re.sub(r"\s+", " ", title).strip(" ,")


def _fingerprint(items: Sequence[Dict]) -> str:
    payload = sorted(
        (i.get("name", ""), i.get("type", ""), i.get("size", 0),
         i.get("md5", ""), i.get("modified", ""))
        for i in items
    )
    return json.dumps(payload, ensure_ascii=False)


def _row_to_card(row) -> ProjectCard:
    return ProjectCard(
        path=row["path"],
        name=row["name"],
        title=row["title"],
        category=row["category"],
        url=row["url"],
        doc_text=row["doc_text"],
        docs=json.loads(row["docs"] or "[]"),
        albums=json.loads(row["albums"] or "[]"),
        photos=row["photos"],
        # Токены считаются заново из названия, а не читаются из колонки:
        # правка normalize_tokens должна действовать сразу, не дожидаясь
        # переобхода диска. В колонке они лежат, чтобы индекс можно было
        # посмотреть глазами прямо в базе.
        tokens=normalize_tokens(row["title"]),
        modified=row["modified"],
        indexed_at=row["indexed_at"],
        fingerprint=row["fingerprint"],
    )


# ------------------------------------------------------------------- фасады

def last_indexed_at(db_path: Optional[str] = None, source: str = "vahtapro") -> Optional[datetime]:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(indexed_at) AS ts FROM disk_projects WHERE source = ?", (source,)
        ).fetchone()
    if not row or not row["ts"]:
        return None
    try:
        return datetime.fromisoformat(row["ts"])
    except ValueError:
        return None


def refresh_if_stale(
        db_path: Optional[str] = None,
        max_age_hours: Optional[float] = None,
        disk_url: Optional[str] = None,
) -> bool:
    """Обновляет индекс, если он старше max_age_hours. Ошибки не пробрасывает.

    Диск — сторонний сервис, и его недоступность не повод ронять прогон:
    молча работаем на прошлом индексе (или вовсе без справок).
    """
    if not KB_ENABLED:
        return False
    age_limit = KB_REFRESH_HOURS if max_age_hours is None else max_age_hours
    last = last_indexed_at(db_path)
    if last and datetime.now() - last < timedelta(hours=age_limit):
        return False
    try:
        DiskIndexer(PublicDisk(disk_url or DEFAULT_DISK_URL), db_path=db_path).refresh()
        return True
    except Exception as exc:  # noqa: BLE001 — см. докстроку
        logger.warning(f"[kb] индекс диска не обновлён: {exc}")
        return False
