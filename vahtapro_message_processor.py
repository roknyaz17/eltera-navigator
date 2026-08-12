"""
Процессор канала ВахтаПро.

Это тонкая надстройка над универсальным TelegramChannelProcessor,
с предзаданным source_name и snapshot_marker под формат ВахтаПро.
"""

from telegram_channel_processor import TelegramChannelProcessor


class VahtaProMessageProcessor(TelegramChannelProcessor):
    """Сохраняется как отдельный класс ради читаемости и обратной совместимости."""

    SOURCE_NAME = "ВахтаПро"
    SOURCE_URL_FALLBACK = "https://t.me/c/2610083978"
    SNAPSHOT_MARKER = "Описание проектов и актуальная потребность"

    def __init__(self, sheets_service, llm_parser):
        super().__init__(
            sheets_service,
            llm_parser,
            source_name=self.SOURCE_NAME,
            source_url_fallback=self.SOURCE_URL_FALLBACK,
            snapshot_marker=self.SNAPSHOT_MARKER,
        )
