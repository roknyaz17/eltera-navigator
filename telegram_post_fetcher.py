"""Тексты постов Telegram по ссылке из таблицы контрагента.

Аметист завёл в своей таблице колонку «Ссылка на описание вакансии»: строка
даёт структуру (объект, потребность, ставка), а пост в канале — подробности,
которых в строке нет физически (адрес, часы смен, оплата по ролям, что выдают,
условия заселения). Забираем пост до вызова LLM и отдаём разбору вместе со
строкой.

Ссылки бывают двух видов:
    https://t.me/c/2848712007/300/604   — закрытый канал, тема 300, сообщение 604
    https://t.me/c/2848712007/651       — закрытый канал, сообщение 651
    https://t.me/eltera_channel/128     — публичный канал по username

Пост может быть альбомом: текст лежит на одном сообщении группы, остальные —
фотографии. Поэтому у альбома собираем подписи всех сообщений группы.
"""

import json
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from loguru import logger

# t.me/c/<internal_id>/<...>/<message_id> — у закрытых каналов id без префикса -100
_PRIVATE_RE = re.compile(r"t\.me/c/(\d+)((?:/\d+)+)", re.IGNORECASE)
_PUBLIC_RE = re.compile(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,})((?:/\d+)+)", re.IGNORECASE)

# Насколько далеко от найденного сообщения искать соседей по альбому.
ALBUM_SPAN = 9


def parse_link(url: str) -> Optional[Tuple[object, int]]:
    """Ссылка → (адресат канала, id сообщения). None, если это не ссылка на пост."""
    text = (url or "").strip()
    if not text:
        return None

    match = _PRIVATE_RE.search(text)
    if match:
        parts = [int(x) for x in match.group(2).strip("/").split("/")]
        return int("-100" + match.group(1)), parts[-1]

    match = _PUBLIC_RE.search(text)
    if match:
        username = match.group(1)
        if username.lower() in ("c", "s", "joinchat", "addstickers", "proxy"):
            return None
        parts = [int(x) for x in match.group(2).strip("/").split("/")]
        return username, parts[-1]
    return None


class TelegramPostFetcher:
    """Читает посты по ссылкам одним подключением userbot'а.

    Ошибка доступа к каналу или удалённое сообщение — не повод ронять прогон:
    в таком случае позиция разбирается по одной строке таблицы, как раньше,
    а в лог уходит предупреждение.
    """

    def __init__(self, userbot_factory=None, cache_path: Optional[str] = None):
        # Фабрика, а не готовый клиент: подключение живёт ровно на время выборки.
        self._factory = userbot_factory
        # Тексты постов переживают прогон на диске. Это не оптимизация, а защита
        # данных: без Telegram (а он отваливается) строка уехала бы в LLM без
        # описания, и позиция потеряла бы уже извлечённые поля.
        self._cache_path = cache_path
        self._cache: Dict[str, str] = self._load_cache()
        # Что уже забрали в этом прогоне — второй раз в Telegram не ходим.
        self._fetched: set = set()

    def _load_cache(self) -> Dict[str, str]:
        if not self._cache_path or not os.path.exists(self._cache_path):
            return {}
        try:
            with open(self._cache_path, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            logger.warning(f"[tg-описания] кэш не читается ({exc}), начинаем с пустого")
            return {}

    def _save_cache(self) -> None:
        if not self._cache_path:
            return
        try:
            directory = os.path.dirname(os.path.abspath(self._cache_path))
            os.makedirs(directory, exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle, ensure_ascii=False, indent=1)
        except Exception as exc:
            logger.warning(f"[tg-описания] кэш не сохранён: {exc}")

    def _make_userbot(self):
        if self._factory is not None:
            return self._factory()
        from telegram_userbot import TelegramUserbot

        return TelegramUserbot()

    async def fetch_many(self, urls: Iterable[str]) -> Dict[str, str]:
        """{ссылка: текст поста}: свежие из Telegram, недостающие — из кэша."""
        wanted: Dict[str, Tuple[object, int]] = {}
        for url in urls:
            key = (url or "").strip()
            if not key or key in wanted or key in self._fetched:
                continue
            target = parse_link(key)
            if target:
                wanted[key] = target

        if not wanted:
            return dict(self._cache)

        try:
            async with self._make_userbot() as bot:
                by_chat: Dict[object, List[Tuple[str, int]]] = {}
                for url, (chat, message_id) in wanted.items():
                    by_chat.setdefault(chat, []).append((url, message_id))

                for chat, items in by_chat.items():
                    try:
                        entity = await bot._client.get_entity(chat)
                    except Exception as exc:
                        logger.warning(f"[tg-описания] нет доступа к каналу {chat}: {exc}")
                        continue
                    await self._fetch_chat(bot, entity, items)
        except Exception as exc:
            stale = sum(1 for url in wanted if url in self._cache)
            logger.warning(
                f"[tg-описания] userbot недоступен ({exc}); "
                f"берём {stale} описаний из кэша, остальные строки разбираем без них"
            )

        self._save_cache()
        return dict(self._cache)

    async def _fetch_chat(self, bot, entity, items: List[Tuple[str, int]]) -> None:
        ids = [message_id for _, message_id in items]
        messages = await bot._client.get_messages(entity, ids=ids)
        albums: Dict[int, str] = {}

        for (url, message_id), message in zip(items, messages):
            if message is None:
                logger.warning(f"[tg-описания] сообщение не найдено: {url}")
                continue
            text = (message.message or "").strip()
            grouped = getattr(message, "grouped_id", None)
            if grouped:
                if grouped not in albums:
                    albums[grouped] = await self._album_text(bot, entity, message)
                text = albums[grouped] or text
            if not text:
                logger.warning(f"[tg-описания] пустой текст поста: {url}")
                continue
            self._cache[url] = text
            self._fetched.add(url)

    async def _album_text(self, bot, entity, message) -> str:
        """Подписи всех сообщений альбома, по порядку.

        У альбома текст обычно на одном сообщении, а ссылка в таблице может
        вести на любое из них.
        """
        grouped = message.grouped_id
        start = max(1, message.id - ALBUM_SPAN)
        ids = list(range(start, message.id + ALBUM_SPAN + 1))
        try:
            neighbours = await bot._client.get_messages(entity, ids=ids)
        except Exception as exc:
            logger.warning(f"[tg-описания] не удалось собрать альбом {message.id}: {exc}")
            return (message.message or "").strip()

        parts = [
            (item.message or "").strip()
            for item in neighbours
            if item is not None and getattr(item, "grouped_id", None) == grouped
        ]
        return "\n".join(part for part in parts if part).strip()
