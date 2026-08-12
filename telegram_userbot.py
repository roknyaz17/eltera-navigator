"""
Async-userbot для чтения каналов от лица пользователя (Telethon).

Используется внутри активного asyncio loop (FastAPI / APScheduler).
Для CLI-сценариев тоже подходит — оборачивается через asyncio.run.
"""

import os
from datetime import datetime
from typing import List, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession


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
        entity = await self._client.get_entity(chat_id)
        result: List[dict] = []
        async for msg in self._client.iter_messages(entity, limit=limit, min_id=min_id):
            # msg.date — timezone-aware (UTC). after тоже должен быть aware.
            if after is not None and msg.date < after:
                break
            text = (msg.message or "").strip()
            if not text:
                continue
            result.append({
                "id": msg.id,
                "date": msg.date,
                "text": text,
                "channel_id": chat_id,
                "channel_title": getattr(entity, "title", ""),
            })
        return result
