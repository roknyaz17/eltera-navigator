"""Общая обвязка тестов реестра.

Тесты специально не тянут langchain/gspread: проверяется логика приёма и
унификации, а разбор подменяется заглушкой. Иначе для запуска понадобились бы
ключи и сеть.
"""

import os
import sys
from typing import Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry import db
from registry.models import RawRequest


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "registry.db")
    db.reset_schema_cache()
    yield path
    db.reset_schema_cache()


@pytest.fixture
def conn(db_path):
    with db.connect(db_path) as connection:
        yield connection


class StubParser:
    """Подменяет LLM: возвращает заранее заданный разбор по тексту заявки.

    Считает вызовы — на этом держится проверка, что неизменившаяся заявка
    повторно не разбирается.
    """

    def __init__(self, mapping: Optional[Dict[str, List[Dict]]] = None):
        self.mapping = mapping or {}
        self.calls = 0
        self.fail_on: set = set()
        # Справки, с которыми звали разбор: по ним проверяется, что база
        # знаний доезжает до модели и что каждому куску досталась своя.
        self.contexts: List[Optional[str]] = []

    async def aparse_raw_ex(
            self, text: str, context: Optional[str] = None,
    ) -> Tuple[Optional[List[Dict]], int, int]:
        self.calls += 1
        self.contexts.append(context)
        if text in self.fail_on:
            return None, 10, 0
        return list(self.mapping.get(text, [])), 100, 50


@pytest.fixture
def parser():
    return StubParser()


def make_request(source: str, ref: str, text: str, **kwargs) -> RawRequest:
    return RawRequest(source=source, source_ref=ref, raw_text=text, **kwargs)
