"""
CLI-обёртка для прогона только AAA+.
По сути алиас: `python main.py --sources aaaplus [--reset]`.
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

from logging_config import setup_logging

setup_logging()

from loguru import logger

from pipeline import run_pipeline
from telegram_userbot import TelegramUserbot


async def _check_only(limit: int) -> None:
    chat_id = int(os.environ["TELEGRAM_AAAPLUS_CHAT_ID"])
    async with TelegramUserbot() as bot:
        me = await bot.whoami()
        logger.info(f"Userbot: {me['first_name']} {me.get('last_name') or ''} "
                    f"(@{me.get('username') or '—'}, id={me['id']})")
        logger.info(f"Канал AAA+: chat_id={chat_id}")
        messages = await bot.get_messages(chat_id, limit=limit)
        logger.info(f"Получено сообщений: {len(messages)}")
        for m in messages[:10]:
            preview = m["text"][:120].replace("\n", " ")
            logger.info(f"  #{m['id']} {m['date']:%Y-%m-%d %H:%M}: {preview}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50,
                        help="(только для --check-only) сколько сообщений показать")
    parser.add_argument("--check-only", action="store_true",
                        help="Только проверить доступ к каналу, не парсить")
    parser.add_argument("--reset", action="store_true",
                        help="Сбросить is_active=FALSE для AAA+ перед прогоном")
    args = parser.parse_args()

    if args.check_only:
        asyncio.run(_check_only(args.limit))
    else:
        asyncio.run(run_pipeline(["aaaplus"], reset=args.reset))


if __name__ == "__main__":
    main()
