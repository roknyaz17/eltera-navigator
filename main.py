"""
CLI-обёртка над run_pipeline.

Запуск:
    python main.py                                # все источники, без сброса
    python main.py --sources kpk,yappi,marketstaff --reset
    python main.py --sources vahtapro

Для сервера (с планировщиком + FastAPI) используется app.py.
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

from logging_config import setup_logging

setup_logging()

from loguru import logger

from pipeline import ALL_SOURCES, parse_sources, run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sources",
        type=str,
        default=None,
        help=f"Список источников через запятую: {', '.join(ALL_SOURCES)}. По умолчанию — все.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Перед прогоном пометить is_active=FALSE для указанных source.",
    )
    args = parser.parse_args()

    try:
        sources = parse_sources(args.sources)
    except ValueError as exc:
        logger.error(str(exc))
        sys.exit(1)

    asyncio.run(run_pipeline(sources, reset=args.reset))


if __name__ == "__main__":
    main()
