"""
Async-userbot для чтения каналов от лица пользователя (Telethon).

Используется внутри активного asyncio loop (FastAPI / APScheduler).
Для CLI-сценариев тоже подходит — оборачивается через asyncio.run.
"""

import asyncio
import os
from datetime import datetime
from typing import List, Optional

from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

# Сколько ждать ответа Telegram, прежде чем считать вызов зависшим.
# Без него оборванное соединение держит прогон до таймаута ОС.
REQUEST_TIMEOUT_SEC = 60
# Повторы на сетевых ошибках. Три попытки с нарастающей паузой: этого хватает
# на переподключение и не растягивает утренний прогон.
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 5
# FloodWait дольше этого не пережидаем: прогон повторится по расписанию,
# а держать задачу полчаса ради одного канала смысла нет.
MAX_FLOOD_WAIT_SEC = 120


class TelegramUserbot:
    """Async-обёртка над Telethon: умеет читать сообщения канала."""

    def __init__(
            self,
            api_id: Optional[int] = None,
            api_hash: Optional[str] = None,
            session_string: Optional[str] = None,
    ):
        self.api_id = api_id or int(os.environ["TELEGRAM_API_ID"])
        self.api_hash = api_hash or os.environ["TELEGRAM_API_HASH"]
        self.session_string = session_string or os.environ.get("TELEGRAM_SESSION", "")
        if not self.session_string:
            raise RuntimeError(
                "TELEGRAM_SESSION пустой. Запусти auth_userbot.py, "
                "скопируй полученную строку и положи её в .env."
            )
        self._client: Optional[TelegramClient] = None

    # ------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "TelegramUserbot":
        self._client = TelegramClient(
            StringSession(self.session_string),
            self.api_id,
            self.api_hash,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "StringSession недействительна. Перезапусти auth_userbot.py."
            )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    # --------------------------------------------------------- methods
    async def whoami(self) -> dict:
        me = await self._client.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "username": me.username,
        }

    async def get_messages(
            self,
            chat_id: int,
            limit: int = 50,
            min_id: int = 0,
            after: Optional[datetime] = None,
    ) -> List[dict]:
        """
        Возвращает сообщения канала (от новых к старым).

        :param limit: верхний предел количества сообщений (cap).
        :param after: timezone-aware datetime. Если задан, берём только сообщения
                      НЕ старше него. Поскольку Telethon отдаёт от новых к старым,
                      как только встречаем сообщение старше after — прекращаем обход.
        """
        entity = await self._with_retry(f"get_entity({chat_id})", self._client.get_entity, chat_id)

        async def _collect() -> List[dict]:
            out: List[dict] = []
            skipped_no_text = 0
            async for msg in self._client.iter_messages(entity, limit=limit, min_id=min_id):
                # msg.date — timezone-aware (UTC). after тоже должен быть aware.
                if after is not None and msg.date < after:
                    break
                text = (msg.message or "").strip()
                if not text:
                    # Сообщение без текстовой части: фото, документ, альбом без
                    # подписи. Мы такое не разбираем — но раньше оно исчезало
                    # молча, и понять, теряется ли так потребность, было нельзя.
                    skipped_no_text += 1
                    logger.warning(
                        f"[telegram] пропущено сообщение без текста: "
                        f"chat={chat_id} msg_id={msg.id} media={type(msg.media).__name__ if msg.media else 'нет'} "
                        f"grouped_id={getattr(msg, 'grouped_id', None)}"
                    )
                    continue
                out.append({
                    "id": msg.id,
                    "date": msg.date,
                    "text": text,
                    "channel_id": chat_id,
                    "channel_title": getattr(entity, "title", ""),
                })
            if skipped_no_text:
                logger.warning(
                    f"[telegram] chat={chat_id}: пропущено без текста {skipped_no_text}, "
                    f"взято в разбор {len(out)}"
                )
            return out

        return await self._with_retry(f"iter_messages({chat_id})", _collect)

    # ----------------------------------------------------------- retry
    async def _with_retry(self, what: str, func, *args):
        """Вызов Telethon с таймаутом и повторами.

        Раньше вокруг сетевых вызовов не было ни одного try/except: обрыв связи
        или FloodWait роняли весь прогон источника, а вместе с ним и остальные
        источники в той же задаче планировщика.
        """
        last: Optional[BaseException] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await asyncio.wait_for(func(*args), timeout=REQUEST_TIMEOUT_SEC)
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 0) or 0)
                if wait > MAX_FLOOD_WAIT_SEC:
                    logger.error(
                        f"[telegram] {what}: FloodWait {wait} с — дольше допустимых "
                        f"{MAX_FLOOD_WAIT_SEC} с, прогон источника прерван"
                    )
                    raise
                logger.warning(f"[telegram] {what}: FloodWait {wait} с, ждём и повторяем")
                await asyncio.sleep(wait + 1)
                last = exc
            except (asyncio.TimeoutError, ConnectionError, OSError, RPCError) as exc:
                last = exc
                if attempt == MAX_RETRIES:
                    break
                pause = RETRY_BACKOFF_SEC * attempt
                logger.warning(
                    f"[telegram] {what}: {type(exc).__name__}: {exc}. "
                    f"Попытка {attempt} из {MAX_RETRIES}, повтор через {pause} с"
                )
                await asyncio.sleep(pause)
        logger.error(f"[telegram] {what}: не удалось за {MAX_RETRIES} попыток: {last!r}")
        raise last if last is not None else RuntimeError(f"{what}: неизвестная ошибка")
