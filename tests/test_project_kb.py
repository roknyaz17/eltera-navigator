"""Сопоставление поста Градуса с папкой проекта на Яндекс.Диске.

Сеть здесь не нужна: индекс собирается руками, проверяется логика — как
разбираются названия, когда совпадение считается уверенным и как справка
доезжает до разбора. Названия папок и шапки постов взяты настоящие.
"""

import asyncio
import hashlib
import json
import os
import sys
import zipfile
from io import BytesIO

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import navigator_api
from project_kb import (
    ProjectCard,
    ProjectKB,
    build_context,
    link_positions,
    normalize_tokens,
    photos_url,
)
from registry import db
from registry.ingest import RegistryIngestor
from registry.models import RawRequest
from telegram_channel_processor import TelegramChannelProcessor
from yandex_disk import docx_to_text

# Настоящие имена папок с диска Градуса — на них и держится сопоставление.
FOLDERS = [
    "BMJ, Шарапово ✅",
    "BMJ, Теплый Стан ✅",
    "Сбер Шарапово ✅",
    "Сбер Химки ✅",
    "Все инструменты ДМД ✅",
    "Все инструменты Чашниково ✅",
    "Все Инструменты ЕКБ ✅",
    "Все инструменты Казань",
    "Молком Пушкино ✅",
    "Самокат Пушкино ✅",
    "Кюхенленд, Химки  ✅",
    "Кюхенленд Т2✅",
    "Мултон, Солнцево ✅",
    "Спортмастер, Старая Купавна ✅",
    "РотФронт✅",
    "Балтика Хабаровск✅",
    "Балтика, Тула ✅",
    "Хофф, Домодедово ✅",
]


def make_kb(folders=FOLDERS, with_description=True) -> ProjectKB:
    cards = []
    for name in folders:
        title = name.replace("✅", "").strip(" ,")
        cards.append(ProjectCard(
            path=f"/1. Москва и МО Проекты /{name}",
            name=name,
            title=title,
            category="1. Москва и МО Проекты",
            url=f"https://disk.yandex.ru/d/XXX/{title}",
            doc_text=f"Описание проекта {title}" if with_description else "",
            docs=([{"name": "Описание.docx", "text": f"Описание проекта {title}",
                    "text_len": 20}] if with_description else []),
            tokens=normalize_tokens(title),
        ))
    return ProjectKB(cards)


# ------------------------------------------------------------------ токенизация

def test_normalize_drops_emoji_and_geo_noise():
    assert normalize_tokens("🚀BMJ, Шарапово (Мос. Обл)") == ["bmj", "шарапово"]


def test_normalize_splits_underscore():
    assert normalize_tokens("🚀Балтика_Хабаровск") == ["балтика", "хабаровск"]


def test_normalize_expands_city_abbreviation():
    assert normalize_tokens("Все инструменты ДМД") == ["все", "инструменты", "домодедово"]


# -------------------------------------------------------------- сопоставление

@pytest.mark.parametrize("post, expected", [
    ("🚀BMJ, Шарапово (Мос. Обл)", "BMJ, Шарапово"),
    ("🚀Сбер, Шарапово", "Сбер Шарапово"),
    ("🚀Молком, Пушкино", "Молком Пушкино"),
    # Сокращение города в названии папки против полного в посте.
    ("🚀Все инструменты Домодедово", "Все инструменты ДМД"),
    # Папка «Кюхенленд Т2» не должна перетягивать на себя «Химки».
    ("🚀Кюхенленд, Химки", "Кюхенленд, Химки"),
    # В папке слитно, в посте раздельно.
    ("🚀Рот Фронт, г Москва", "РотФронт"),
    # Город в посте и в папке разный, но проект с таким брендом один.
    ("🚀Мултон, г. Москва", "Мултон, Солнцево"),
    ("🚀Спортмастер (Мос.обл)", "Спортмастер, Старая Купавна"),
])
def test_match_finds_project(post, expected):
    hit = make_kb().match(post)
    assert hit is not None, f"не опознан: {post}"
    assert hit.card.title == expected


def test_match_refuses_when_two_projects_equally_likely():
    """«Все инструменты» без города — четыре одинаково похожих папки.

    Подсунуть описание чужого объекта хуже, чем не подсунуть ничего.
    """
    assert make_kb().match("🚀Все инструменты, вахта от 15 смен") is None


def test_match_refuses_unknown_project():
    assert make_kb().match("🚀Пятёрочка, Тверь") is None


def test_match_ignores_positions_below_the_header():
    """Должности и ставки не должны цеплять чужие проекты."""
    post = (
        "🚀Хофф, Домодедово\n"
        "👉комплектовщик - 5 м - 3500 руб/смена\n"
        "✅вахта от 20 смен, питание бесплатно"
    )
    hit = make_kb().match(post)
    assert hit is not None and hit.card.title == "Хофф, Домодедово"


def test_match_reads_header_below_first_line():
    """Первая строка бывает служебной («УВЕЛИЧЕНИЕ ТАРИФА»)."""
    post = "🔥УВЕЛИЧЕНИЕ ТАРИФА🔥\n\nМолком, Пушкино\n👉комплектовщик - 5 м"
    hit = make_kb().match(post)
    assert hit is not None and hit.card.title == "Молком Пушкино"


def test_context_is_empty_for_project_without_description():
    """Папка с одними фотографиями модели ничего не добавляет."""
    kb = make_kb(with_description=False)
    context, meta = kb.context_for("🚀BMJ, Шарапово (Мос. Обл)")
    assert context is None and meta is None


def test_context_names_project_and_documents():
    kb = make_kb()
    context, meta = kb.context_for("🚀BMJ, Шарапово (Мос. Обл)")
    assert "BMJ, Шарапово" in context
    assert "Описание проекта BMJ, Шарапово" in context
    assert meta["project"] == "BMJ, Шарапово"
    assert meta["url"].startswith("https://disk.yandex.ru/")


def test_build_context_lists_albums_and_other_documents():
    card = ProjectCard(
        path="/x", name="Молком Пушкино ✅", title="Молком Пушкино",
        category="1. Москва и МО Проекты", url="https://disk.yandex.ru/d/XXX",
        doc_text="Описание",
        docs=[
            {"name": "Описание Молком.docx", "text": "Склад, Пушкино", "text_len": 14},
            {"name": "Направление Хостел_Березка.docx", "text": "", "text_len": 0},
        ],
        albums=[{"name": "Фото проживания Хостел «Березка»", "photos": 9, "url": ""}],
        tokens=normalize_tokens("Молком Пушкино"),
    )
    context = build_context(card)
    assert "Склад, Пушкино" in context
    # Имя файла тоже данные: оно называет место проживания.
    assert "Направление Хостел_Березка.docx" in context
    assert "9 фото" in context


# ------------------------------------------------------- доставка до разбора

def test_processor_attaches_context_per_chunk():
    """У каждого проекта в посте своя справка, а не одна на всё сообщение."""
    kb = make_kb()
    processor = TelegramChannelProcessor(
        None, None, source_name="Градус", context_provider=kb.context_for,
    )
    text = (
        "🚀BMJ, Шарапово (Мос. Обл)\n👉грузчик - 3 м - 3520 руб/смена\n"
        "🚀Молком, Пушкино\n👉комплектовщик - 5 м - 3498 руб/смена\n"
        "🚀Пятёрочка, Тверь\n👉кассир - 2 ж - 3000 руб/смена"
    )
    requests, _ = processor.collect_requests(
        [{"id": 1, "date": "2026-08-14T10:00:00", "text": text, "channel_id": -100123}],
        source="vahtapro",
    )
    assert len(requests) == 1
    contexts = requests[0].context_for_chunks()
    assert len(contexts) == 3
    assert "BMJ, Шарапово" in contexts[0]
    assert "Молком Пушкино" in contexts[1]
    # Проекта нет на диске — заявка разбирается как раньше, без справки.
    assert contexts[2] is None
    assert [item["project"] for item in requests[0].raw_payload["kb"]] == [
        "BMJ, Шарапово", "Молком Пушкино",
    ]


def test_processor_without_kb_behaves_as_before():
    processor = TelegramChannelProcessor(None, None, source_name="Градус")
    requests, _ = processor.collect_requests(
        [{"id": 1, "date": "2026-08-14T10:00:00", "text": "🚀BMJ\n👉грузчик - 3 м - 3520 руб",
          "channel_id": -100123}],
        source="vahtapro",
    )
    assert requests[0].context_for_chunks() == [None]
    assert "kb" not in requests[0].raw_payload


def test_context_change_forces_reparse():
    """Контрагент дописал описание — пост тот же, а разобрать нужно заново."""
    before = RawRequest(source="vahtapro", source_ref="msg:1", raw_text="🚀BMJ")
    after = RawRequest(source="vahtapro", source_ref="msg:1", raw_text="🚀BMJ",
                       extra_context="Питание бесплатное")
    assert before.content_hash != after.content_hash


def test_hash_unchanged_for_requests_without_context():
    """Появление самого механизма не должно перепарсить весь реестр.

    У заявки без справки хэш обязан остаться прежним — иначе первый же прогон
    после выката отправит в LLM все источники целиком, а заодно поднимет
    ревизию у каждой заявки. Поэтому здесь зафиксирована ровно та формула,
    что была до появления справок.
    """
    request = RawRequest(source="ametist", source_ref="row:1", raw_text="Грузчик, 5 человек")
    legacy = hashlib.sha256(
        json.dumps(
            {"text": request.raw_text, "overrides": {}, "defaults": {}},
            ensure_ascii=False, sort_keys=True, default=str,
        ).encode("utf-8")
    ).hexdigest()
    assert request.content_hash == legacy


def test_ingest_passes_context_to_parser(db_path, parser):
    """Справка доезжает до модели ровно тем куском, к которому относится."""
    ingestor = RegistryIngestor(parser, db_path=db_path, learn=False)
    raw = RawRequest(
        source="vahtapro", source_ref="msg:7", raw_text="🚀BMJ\n🚀Молком",
        parse_chunks=["🚀BMJ", "🚀Молком"],
        chunk_contexts=["справка BMJ", "справка Молком"],
    )
    asyncio.run(ingestor.ingest("vahtapro", [raw], snapshot=False))
    assert sorted(parser.contexts) == ["справка BMJ", "справка Молком"]


# ----------------------------------------------- папка с фотографиями объекта

def test_match_position_uses_position_fields():
    """Проект позиции ищется по контрагенту, объекту и городу самой позиции."""
    hit = make_kb().match_position("BMJ", "", "Шарапово")
    assert hit is not None and hit.card.title == "BMJ, Шарапово"


def test_match_position_refuses_without_fields():
    assert make_kb().match_position("", "", "") is None


def test_match_requires_brand_not_just_city():
    """Город совпал, а компания другая — это чужой объект, а не наш проект.

    «Шарапово» встречается у BMJ и у Сбера; если папки BMJ на диске нет,
    позиция BMJ не должна получить фотографии сберовского склада.
    """
    kb = make_kb([f for f in FOLDERS if not f.startswith("BMJ")])
    assert kb.match_position("BMJ", "", "Шарапово") is None


def test_photos_url_prefers_single_album():
    """Один альбом — ведём сразу в него, а не в папку с документами."""
    card = ProjectCard(
        path="/x", name="Молком Пушкино", title="Молком Пушкино", category="",
        url="https://disk.yandex.ru/d/XXX/molkom",
        albums=[{"name": "Фото проживания", "photos": 9,
                 "url": "https://disk.yandex.ru/d/XXX/molkom/photos"}],
    )
    assert photos_url(card) == "https://disk.yandex.ru/d/XXX/molkom/photos"


def test_photos_url_falls_back_to_folder_when_albums_differ():
    """Два общежития — выбирать за человека нельзя, ведём в папку проекта."""
    card = ProjectCard(
        path="/x", name="Молком Пушкино", title="Молком Пушкино", category="",
        url="https://disk.yandex.ru/d/XXX/molkom",
        albums=[
            {"name": "Хостел Березка", "photos": 9, "url": "https://disk.yandex.ru/d/XXX/a"},
            {"name": "Хостел Центральная", "photos": 5, "url": "https://disk.yandex.ru/d/XXX/b"},
        ],
    )
    assert photos_url(card) == "https://disk.yandex.ru/d/XXX/molkom"


def test_link_positions_writes_and_removes_links(db_path):
    """Позиция получает папку проекта; исчез проект — исчезает и ссылка."""
    with db.connect(db_path) as conn:
        _seed_position(conn, "ELT-2026-000001-01", counterparty="BMJ", city="Шарапово")
        _seed_position(conn, "ELT-2026-000001-02", counterparty="Пятёрочка", city="Тверь")
        for card in make_kb().cards:
            conn.execute(
                "INSERT INTO disk_projects (path, source, category, name, title, tokens, url, "
                "doc_text, docs, albums, photos, modified, fingerprint, indexed_at) "
                "VALUES (?, 'vahtapro', ?, ?, ?, ?, ?, '', '[]', ?, 12, '', '', '2026-08-14')",
                (card.path, card.category, card.name, card.title, " ".join(card.tokens),
                 card.url, json.dumps([{"name": "Фото", "photos": 12, "url": card.url + "/ph"}])),
            )

    stats = link_positions(db_path=db_path)
    assert stats["linked"] == 1 and stats["unlinked"] == 1

    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM position_kb").fetchall()
        assert len(rows) == 1
        assert rows[0]["position_id"] == "ELT-2026-000001-01"
        assert rows[0]["project"] == "BMJ, Шарапово"
        assert rows[0]["photos_url"].endswith("/ph")

    # Контрагент убрал проект с диска — связь должна исчезнуть, а не остаться
    # битой. Убираем обе папки бренда: пока на диске остаётся ровно один
    # «BMJ», позиция сопоставится с ним (см. _by_unique_brand).
    with db.connect(db_path) as conn:
        conn.execute("DELETE FROM disk_projects WHERE title LIKE 'BMJ%'")
    link_positions(db_path=db_path)
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM position_kb").fetchone()["n"] == 0


def test_media_block_only_when_folder_has_photos():
    """Кнопка появляется только там, где действительно есть фотографии."""
    with_photos = {"photos_url": "https://disk.yandex.ru/d/X/p", "kb_photos": 12,
                   "kb_project": "BMJ, Шарапово", "linked_at": "2026-08-14T10:00:00"}
    assert navigator_api.media_block(with_photos)[0]["kind"] == "object_photo"
    assert navigator_api.media_block(with_photos)[0]["vis"] == "public"
    assert navigator_api.media_block({**with_photos, "kb_photos": 0}) == []
    assert navigator_api.media_block({**with_photos, "photos_url": ""}) == []
    # Позиция другого источника: колонок вовсе нет.
    assert navigator_api.media_block({}) == []


def _seed_position(conn, position_id: str, counterparty: str, city: str) -> None:
    # Обе позиции теста живут в одной заявке — второй раз её заводить не нужно.
    conn.execute(
        "INSERT OR IGNORE INTO requests (request_id, year, seq, source, source_ref, "
        "content_hash, first_seen_at, last_seen_at) VALUES (?, 2026, 1, 'vahtapro', ?, 'h', '', '')",
        (position_id[:-3], position_id[:-3]),
    )
    conn.execute(
        "INSERT INTO positions (position_id, seq, first_request_id, last_request_id, source, "
        "fingerprint, is_active, first_seen_at, last_seen_at, updated_at, counterparty, city) "
        "VALUES (?, 1, ?, ?, 'vahtapro', ?, 1, '', '', '', ?, ?)",
        (position_id, position_id[:-3], position_id[:-3], position_id, counterparty, city),
    )


# --------------------------------------------------------------- чтение docx

def test_docx_to_text_keeps_paragraphs():
    document = (
        '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
        "<w:p><w:r><w:t>Проект &quot;BMJ&quot;</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Питание — 140 руб</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    text = docx_to_text(buffer.getvalue())
    assert text.splitlines() == ['Проект "BMJ"', "Питание — 140 руб"]
