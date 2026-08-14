"""Процессор канала Градуса (бывш. ВахтаПро).

Это тонкая надстройка над универсальным TelegramChannelProcessor,
с предзаданным source_name и snapshot_marker под формат Градуса.

Отличие от остальных каналов одно: у Градуса есть публичная папка на
Яндекс.Диске с описанием каждого проекта, и перед разбором пост сопоставляется
с этой папкой (см. project_kb.py). Пост говорит, что нужно сегодня; папка —
что это за объект вообще. Диск недоступен или проект не опознан — процессор
работает ровно как раньше.
"""

import os

from loguru import logger

from project_kb import KB_ENABLED, ProjectKB
from telegram_channel_processor import TelegramChannelProcessor


class VahtaProMessageProcessor(TelegramChannelProcessor):
    """Сохраняется как отдельный класс ради читаемости и обратной совместимости."""

    SOURCE_NAME = "Градус"
    SOURCE_URL_FALLBACK = "https://t.me/c/2610083978"
    SNAPSHOT_MARKER = "Описание проектов и актуальная потребность"

    def __init__(self, sheets_service, llm_parser, db_path=None, use_kb=None):
        super().__init__(
            sheets_service,
            llm_parser,
            source_name=self.SOURCE_NAME,
            source_url_fallback=self.SOURCE_URL_FALLBACK,
            snapshot_marker=self.SNAPSHOT_MARKER,
            context_provider=_make_context_provider(db_path, use_kb),
        )


def _make_context_provider(db_path=None, use_kb=None):
    """Загружает индекс диска один раз на процессор.

    Индекс читается из SQLite (в сеть за диском ходит отдельный шаг, см.
    scripts/index_vahtapro_disk.py и project_kb.refresh_if_stale), поэтому это
    дешёвая операция. Пустой индекс — это не ошибка: значит, диск ещё ни разу
    не обходили, и справок просто не будет.
    """
    enabled = KB_ENABLED if use_kb is None else use_kb
    if not enabled:
        return None
    try:
        kb = ProjectKB.load(db_path=db_path or os.getenv("REGISTRY_DB_PATH"))
    except Exception as exc:  # noqa: BLE001 — без справок прогон полноценен
        logger.warning(f"[Градус] база знаний по проектам не загрузилась: {exc}")
        return None
    if kb.is_empty:
        logger.info("[Градус] индекс Яндекс.Диска пуст — разбор без справок")
        return None
    logger.info(f"[Градус] база знаний: проектов в индексе {len(kb.cards)}")
    return kb.context_for
