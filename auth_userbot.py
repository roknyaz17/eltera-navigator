"""
Одноразовая авторизация Telegram-userbot.

Запусти из консоли:
    .venv\\Scripts\\python.exe auth_userbot.py

Скрипт спросит твой номер телефона, код из Telegram и (если включён) 2FA-пароль.
В конце выведет StringSession — скопируй её в .env как TELEGRAM_SESSION.

Запускать нужно ТОЛЬКО ОДИН РАЗ. Дальше userbot будет жить на этой строке.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]


async def main() -> None:
    print("=" * 60)
    print("Авторизация userbot. Сейчас Telegram пришлёт код в твой аккаунт.")
    print("Если у тебя включён облачный пароль (2FA) — будь готов его ввести.")
    print("=" * 60)

    async with TelegramClient(
            StringSession(),
            API_ID,
            API_HASH,
            lang_code="ru",
            system_lang_code="ru-RU",
    ) as client:
        me = await client.get_me()
        session_str = client.session.save()
        print()
        print(f"Вошёл как: {me.first_name} {me.last_name or ''} "
              f"(@{me.username or '—'}), id={me.id}")
        print()
        print("=== СОХРАНИ ЭТУ СТРОКУ В .env КАК TELEGRAM_SESSION ===")
        print(session_str)
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
