"""Поиск и фильтры реестра."""

import pytest

from registry import db, queries as rq
from registry.ingest import RegistryIngestor
from registry.models import RawRequest

TEXTS = {
    "a": "Молком Пушкино, хостел Березка. Комплектовщик 3498 р/смена, вахта от 30 смен",
    "b": "Озон Тверь. Грузчик 3200 р/смена, вахта от 15 смен",
    "c": "Миксит Клин. Уборщица 2800 р/смена",
}

PARSED = {
    TEXTS["a"]: [{"counterparty": "Молком", "vacancy_name": "Комплектовщик", "city": "Пушкино",
                  "object_name": "Хостел Березка", "shift_rate": "3498 р/смена",
                  "schedule": "вахта от 30 смен", "need_men": 5, "need_women": 10,
                  "shift_type": "day"}],
    TEXTS["b"]: [{"counterparty": "Озон", "vacancy_name": "Грузчик", "city": "Тверь",
                  "shift_rate": "3200 р/смена", "schedule": "вахта от 15 смен", "need_men": 8}],
    TEXTS["c"]: [{"counterparty": "Миксит", "vacancy_name": "Уборщица", "city": "Клин",
                  "shift_rate": "2800 р/смена"}],
}


@pytest.fixture
async def filled_db(parser, db_path):
    parser.mapping = PARSED
    ingestor = RegistryIngestor(parser, db_path=db_path)
    await ingestor.ingest("ametist", [
        RawRequest(source="ametist", source_ref=f"row:{key}", raw_text=text,
                   source_name="Аметист")
        for key, text in TEXTS.items()
    ])
    return db_path


def search(db_path, filters, **kwargs):
    with db.connect(db_path) as conn:
        return rq.search(conn, filters, **kwargs)


@pytest.mark.asyncio
async def test_full_text_search_covers_source_text(filled_db):
    """Поиск идёт и по исходному тексту: «Березка» есть только в нём."""
    rows, total = search(filled_db, {"q": "Березка", "is_active": "true"})
    assert total == 1
    assert rows[0]["vacancy_name"] == "Комплектовщик"


@pytest.mark.asyncio
async def test_prefix_search(filled_db):
    rows, total = search(filled_db, {"q": "компл", "is_active": "true"})
    assert total == 1


@pytest.mark.asyncio
async def test_search_ignores_fts_syntax(filled_db):
    """Спецсимволы FTS5 в пользовательском вводе не должны ронять поиск.

    Менеджер спокойно введёт «Грузчик (Тверь)» или кавычки — синтаксической
    ошибки быть не должно, слова при этом ищутся как слова.
    """
    rows, total = search(filled_db, {"q": '"Грузчик"', "is_active": "true"})
    assert total == 1
    assert rows[0]["vacancy_name"] == "Грузчик"

    # Запрос из одних спецсимволов вырождается в пустой и просто не фильтрует.
    _, total = search(filled_db, {"q": '*(^"', "is_active": "true"})
    assert total == 3

    _, total = search(filled_db, {"q": "Грузчик (Тверь)", "is_active": "true"})
    assert total == 1


@pytest.mark.asyncio
async def test_rate_range_filter(filled_db):
    _, total = search(filled_db, {"rate_min": 3000, "is_active": "true"})
    assert total == 2
    _, total = search(filled_db, {"rate_min": 3000, "rate_max": 3300, "is_active": "true"})
    assert total == 1


@pytest.mark.asyncio
async def test_max_shifts_keeps_unknown(filled_db):
    """Позиция без указанного минимума смен не отсекается: неизвестно ≠ не подходит."""
    rows, total = search(filled_db, {"max_shifts": 20, "is_active": "true"})
    names = {row["vacancy_name"] for row in rows}
    assert names == {"Грузчик", "Уборщица"}


@pytest.mark.asyncio
async def test_has_gaps_filter(filled_db):
    rows, _ = search(filled_db, {"has_gaps": True, "is_active": "true"})
    # У всех трёх нет графика или потребности — кроме первой, где заполнено всё ключевое.
    assert "Уборщица" in {row["vacancy_name"] for row in rows}


@pytest.mark.asyncio
async def test_sorting_puts_empty_values_last(filled_db):
    rows, _ = search(filled_db, {"is_active": "true"}, sort="min_shifts", order="asc")
    values = [row["min_shifts"] for row in rows]
    assert values == [15, 30, None]


@pytest.mark.asyncio
async def test_facets_and_overview(filled_db):
    with db.connect(filled_db) as conn:
        facets = rq.facets(conn)
        overview = rq.overview(conn)
        ratio = rq.fill_ratio(conn)
    assert set(facets["counterparties"]) == {"Миксит", "Молком", "Озон"}
    assert overview["positions"] == 3
    assert overview["active"] == 3
    assert overview["requests"] == 3
    assert 0.0 <= ratio["shift_rate"] <= 1.0


@pytest.mark.asyncio
async def test_manager_fields_are_isolated_from_ingest(filled_db, parser):
    """Правка менеджера переживает повторный приём заявки."""
    with db.connect(filled_db) as conn:
        position_id = conn.execute(
            "SELECT position_id FROM positions WHERE vacancy_name = 'Грузчик'"
        ).fetchone()["position_id"]
        rq.update_manager_fields(conn, position_id, {
            "status": "в работе", "responsible_manager": "Иванова",
        })

    changed = TEXTS["b"].replace("3200", "3400")
    parser.mapping[changed] = [dict(PARSED[TEXTS["b"]][0], shift_rate="3400 р/смена")]
    ingestor = RegistryIngestor(parser, db_path=filled_db)
    await ingestor.ingest("ametist", [
        RawRequest(source="ametist", source_ref="row:b", raw_text=changed)
    ], snapshot=False)

    with db.connect(filled_db) as conn:
        row = conn.execute(
            "SELECT status, responsible_manager, shift_rate FROM positions WHERE position_id = ?",
            (position_id,),
        ).fetchone()
    assert row["status"] == "в работе"
    assert row["responsible_manager"] == "Иванова"
    assert row["shift_rate"] == 3400


@pytest.mark.asyncio
async def test_request_card_data(filled_db):
    with db.connect(filled_db) as conn:
        request_id = conn.execute("SELECT request_id FROM requests LIMIT 1").fetchone()["request_id"]
        request_row = rq.get_request(conn, request_id)
        positions = rq.positions_of_request(conn, request_id)
    assert request_row["parse_status"] == "ok"
    assert len(positions) == 1
