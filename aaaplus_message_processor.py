"""
Процессор канала AAA+.

Формат сообщений AAA+ отличается от Градуса:
- разделитель позиций — рандомный эмодзи в начале строки (🧼, 🍾, 🐶, ...),
  единого «🚀» нет → сегментировать regex'ом нельзя, отдаём сообщение целиком LLM;
- денежные обозначения чаще через «ставка 3300» (а не «3300 руб/смена»),
  поэтому signal_keywords расширен словом «ставк»;
- counterparty в тексте обычно не пишут (есть только локация — Шереметьево
  и т.п.), поэтому форсированно ставим counterparty="AAA+", а исходное
  значение (если LLM что-то вытащил) переезжает в object_name;
- ежедневная сводка узнаётся по характерному эмодзи-заголовку
  «🟡🟡👇🟡🟡🟡 DD.MM», маркер — «🟡🟡👇🟡🟡🟡».
"""

from telegram_channel_processor import TelegramChannelProcessor


class AaaPlusMessageProcessor(TelegramChannelProcessor):
    SOURCE_NAME = "AAA+"
    SOURCE_URL_FALLBACK = "https://t.me/c/3554828202"
    SNAPSHOT_MARKER = "🟡🟡👇🟡🟡🟡"
    COUNTERPARTY = "AAA+"

    # К общему словарю добавляем специфику AAA+
    SIGNAL_KEYWORDS = TelegramChannelProcessor.DEFAULT_SIGNAL_KEYWORDS + (
        "ставк",         # «ставка 3300», «ставок» (фразы про повышение тоже сюда уйдут)
    )
    PROFESSION_HINTS = TelegramChannelProcessor.DEFAULT_PROFESSION_HINTS + (
        "клининг", "клинер",
        "шеф", "повар", "официант", "бариста",
        "хостес", "ресепшн", "горничн",
        "консультант", "продавец", "мерчандайзер",
        "охранник", "сотрудник",
    )

    def __init__(self, sheets_service, llm_parser):
        super().__init__(
            sheets_service,
            llm_parser,
            source_name=self.SOURCE_NAME,
            source_url_fallback=self.SOURCE_URL_FALLBACK,
            snapshot_marker=self.SNAPSHOT_MARKER,
            signal_keywords=self.SIGNAL_KEYWORDS,
            profession_hints=self.PROFESSION_HINTS,
            segment_emoji=None,           # не режем по эмодзи, отдаём всё сообщение LLM
            counterparty_override=self.COUNTERPARTY,
        )
