"""
Настройка логирования через loguru.

Использование:
    from logging_config import setup_logging
    setup_logging()
    from loguru import logger
    logger.info("Привет")

Один вызов setup_logging() настраивает:
- Консольный handler (stderr) с INFO+, цветной формат.
- Файл logs/eltera.log с DEBUG+, ротация раз в месяц, UTF-8.

Ротация: раз в месяц, старый файл сжимается в .zip, храним 12 месяцев.
Ротация у loguru отсчитывается от времени создания файла (месяц ≈ 30.4 суток),
а не от первого числа календарного месяца.

Повторные вызовы безопасны — конфигурация ставится один раз.
"""

import sys
from pathlib import Path

from loguru import logger

_configured = False
LOG_DIR = Path(__file__).parent / "logs"


def setup_logging(log_dir: Path = LOG_DIR) -> None:
    global _configured
    if _configured:
        return

    log_dir.mkdir(exist_ok=True)

    # Сбрасываем дефолтный handler loguru
    logger.remove()

    # Консоль: коротко, цветной, INFO+
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <7}</level> | "
            "<cyan>{name}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # Файл: подробный, с line:func, DEBUG+, помесячная ротация.
    # Сжатие обязательно: на VPS диск 14 ГБ и занят на три четверти,
    # а месячный лог DEBUG-уровня весит десятки мегабайт.
    logger.add(
        log_dir / "eltera.log",
        level="DEBUG",
        rotation="1 month",
        retention="12 months",
        compression="zip",
        encoding="utf-8",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} - {message}"
        ),
        enqueue=True,  # потокобезопасная очередь — важно для async + threads
    )

    _configured = True
