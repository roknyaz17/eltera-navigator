"""Описание вакансии из Telegram у Аметиста: разбор ссылок и сборка текста.

Сеть и Telethon тестам не нужны: фетчер подменяется заглушкой, а проверяется
то, что ломается на практике, — формат ссылок из таблицы и поведение, когда
Telegram недоступен.
"""

import asyncio
import json

import pytest

from ametist_sheet_extractor import AmetistSheetExtractor
from telegram_post_fetcher import parse_link

# Ссылки ровно в тех видах, в которых их кладёт Аметист в колонку
# «Ссылка на описание вакансии».
LINK_CASES = [
    ("https://t.me/c/2848712007/300/604", -1002848712007, 604),   # тема 300, сообщение 604
    ("https://t.me/c/2848712007/651", -1002848712007, 651),       # сообщение без темы
    ("https://t.me/c/2848712007/45/414", -1002848712007, 414),
    ("http://t.me/c/2848712007/26/610", -1002848712007, 610),
    ("https://t.me/eltera_vahta/128", "eltera_vahta", 128),       # публичный канал
]


@pytest.mark.parametrize("url, chat, message_id", LINK_CASES)
def test_parse_link(url, chat, message_id):
    assert parse_link(url) == (chat, message_id)


@pytest.mark.parametrize("value", ["", "   ", "не ссылка", "https://docs.google.com/spreadsheets/d/1", "https://t.me/joinchat/AAAA"])
def test_parse_link_rejects_garbage(value):
    assert parse_link(value) is None


class FakeFetcher:
    """Заглушка: отдаёт заранее известные посты, считает вызовы."""

    def __init__(self, posts):
        self.posts = posts
        self.calls = 0

    async def fetch_many(self, urls):
        self.calls += 1
        self.urls = sorted(urls)
        return {url: text for url, text in self.posts.items() if url in set(urls)}


ROW = {
    "Номер": "3",
    "Объект": "Торбеево (Изготовление Колбас)",
    "Потребность": "60 м/ж",
    "Должность": "Операторы на линию",
    "Ставка в смену": "3707",
    "Ссылка на описание вакансии": "https://t.me/c/2848712007/250/628",
}


def test_link_taken_from_named_column():
    extractor = AmetistSheetExtractor(sheets_service=None, llm_parser=None)
    assert extractor._row_link(ROW) == "https://t.me/c/2848712007/250/628"


def test_link_found_even_if_column_renamed():
    row = {k: v for k, v in ROW.items() if k != "Ссылка на описание вакансии"}
    row["Описание"] = "https://t.me/c/2848712007/250/628"
    extractor = AmetistSheetExtractor(sheets_service=None, llm_parser=None)
    assert extractor._row_link(row) == "https://t.me/c/2848712007/250/628"


def test_post_is_appended_to_row_text():
    post = "Вакансия: работа на линии\nАдрес: ПГТ Торбеево, ул. Весенняя 19\nСмены 08:00-20:00"
    fetcher = FakeFetcher({ROW["Ссылка на описание вакансии"]: post})
    extractor = AmetistSheetExtractor(None, None, fetcher)

    posts = asyncio.run(extractor._fetch_posts([("САРАНСКАЯ область", ROW)]))
    text = extractor._row_to_text(ROW, "САРАНСКАЯ область", posts.get(extractor._row_link(ROW), ""))

    assert "Источник: Аметист" in text
    assert "Регион: САРАНСКАЯ область" in text
    assert "Ставка в смену: 3707" in text          # строка таблицы на месте
    assert post in text                             # и пост добавлен целиком
    assert "верна строка таблицы" in text           # с пометкой, что главнее
    assert "ДОПОЛНЯЕТ" in text                      # и что пост её не заменяет


def test_without_fetcher_text_is_the_same_as_before():
    """Без фетчера (и когда Telegram недоступен) поведение прежнее."""
    plain = AmetistSheetExtractor(None, None)
    assert asyncio.run(plain._fetch_posts([("", ROW)])) == {}
    assert plain._row_to_text(ROW, "") == plain._row_to_text(ROW, "", "")


def test_links_are_fetched_once_for_the_whole_sheet():
    """Один и тот же пост у нескольких строк — одно обращение к Telegram."""
    fetcher = FakeFetcher({ROW["Ссылка на описание вакансии"]: "текст"})
    extractor = AmetistSheetExtractor(None, None, fetcher)
    rows = [("", ROW), ("", dict(ROW, Должность="Грузчик")), ("", dict(ROW, **{"Ссылка на описание вакансии": ""}))]

    posts = asyncio.run(extractor._fetch_posts(rows))

    assert fetcher.calls == 1
    assert fetcher.urls == [ROW["Ссылка на описание вакансии"]]   # пустая ссылка не поехала
    assert posts[ROW["Ссылка на описание вакансии"]] == "текст"


def test_cache_keeps_posts_when_telegram_is_down(tmp_path):
    """Telegram отвалился — описания берутся из кэша, а не исчезают.

    Это не про скорость: без кэша строка уехала бы в LLM без поста, и позиция
    потеряла бы поля, которые в прошлый раз из поста и извлекли.
    """
    from telegram_post_fetcher import TelegramPostFetcher

    cache = tmp_path / "posts.json"
    url = "https://t.me/c/2848712007/250/628"

    class DeadUserbot:
        async def __aenter__(self):
            raise RuntimeError("Connection to Telegram failed")

        async def __aexit__(self, *exc):
            return False

    cache.write_text(json.dumps({url: "старый текст поста"}, ensure_ascii=False), encoding="utf-8")
    fetcher = TelegramPostFetcher(userbot_factory=DeadUserbot, cache_path=str(cache))

    posts = asyncio.run(fetcher.fetch_many([url]))

    assert posts[url] == "старый текст поста"
