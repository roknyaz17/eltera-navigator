"""Приём заявок: дедупликация, ревизии, история, снапшот-лайфцикл."""

from datetime import datetime

import pytest

from registry import db
from registry.ingest import RegistryIngestor
from registry.models import RawRequest

# ID содержит год выдачи, поэтому ожидания строим от текущего года,
# а не от зашитой константы.
YEAR = datetime.now().year

TEXT_A = "Молком Пушкино. Комплектовщик - 10 ж 5 м - 3498 р/смена. вахта от 30 смен"
TEXT_A2 = TEXT_A.replace("3498", "3600")
TEXT_B = "Озон Тверь. Грузчик - 8 м - 3200 р/смена"

PARSED = {
    TEXT_A: [{"counterparty": "Молком", "vacancy_name": "Комплектовщик", "city": "Пушкино",
              "shift_rate": "3498 р/смена", "schedule": "вахта от 30 смен",
              "need_men": 5, "need_women": 10}],
    TEXT_A2: [{"counterparty": "Молком", "vacancy_name": "Комплектовщик", "city": "Пушкино",
               "shift_rate": "3600 р/смена", "schedule": "вахта от 30 смен",
               "need_men": 5, "need_women": 10}],
    TEXT_B: [{"counterparty": "Озон", "vacancy_name": "Грузчик", "city": "Тверь",
              "shift_rate": "3200 р/смена", "need_men": 8}],
}


def request_for(ref: str, text: str, **kwargs) -> RawRequest:
    return RawRequest(source="ametist", source_ref=ref, raw_text=text,
                      source_name="Аметист", **kwargs)


@pytest.fixture
def ingestor(parser, db_path):
    parser.mapping = PARSED
    return RegistryIngestor(parser, db_path=db_path)


def fetch_all(db_path, sql, params=()):
    with db.connect(db_path) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


@pytest.mark.asyncio
async def test_first_run_creates_requests_and_positions(ingestor, db_path, parser):
    stats = await ingestor.ingest("ametist", [
        request_for("row:1", TEXT_A), request_for("row:2", TEXT_B),
    ])
    assert (stats.requests_new, stats.positions_added) == (2, 2)
    assert parser.calls == 2

    positions = fetch_all(db_path, "SELECT position_id, vacancy_name, shift_rate FROM positions ORDER BY position_id")
    assert [p["position_id"] for p in positions] == [
        f"ELT-{YEAR}-000001-01", f"ELT-{YEAR}-000002-01",
    ]
    assert positions[0]["shift_rate"] == 3498


@pytest.mark.asyncio
async def test_unchanged_request_skips_llm(ingestor, parser):
    batch = [request_for("row:1", TEXT_A), request_for("row:2", TEXT_B)]
    await ingestor.ingest("ametist", batch)
    calls_after_first = parser.calls

    stats = await ingestor.ingest("ametist", [request_for("row:1", TEXT_A),
                                              request_for("row:2", TEXT_B)])
    assert parser.calls == calls_after_first, "неизменившаяся заявка не должна разбираться заново"
    assert stats.requests_unchanged == 2
    assert stats.llm_calls_saved == 2
    assert stats.positions_added == 0
    assert stats.positions_deactivated == 0


@pytest.mark.asyncio
async def test_changed_request_bumps_revision_and_records_diff(ingestor, db_path):
    await ingestor.ingest("ametist", [request_for("row:1", TEXT_A), request_for("row:2", TEXT_B)])
    stats = await ingestor.ingest("ametist", [request_for("row:1", TEXT_A2),
                                              request_for("row:2", TEXT_B)])

    assert stats.requests_changed == 1
    assert stats.positions_updated == 1
    assert stats.positions_added == 0

    request = fetch_all(db_path, "SELECT revision FROM requests WHERE source_ref = 'row:1'")[0]
    assert request["revision"] == 2

    revisions = fetch_all(db_path, "SELECT raw_text FROM request_revisions")
    assert revisions[0]["raw_text"] == TEXT_A, "прежний текст заявки должен сохраниться"

    history = fetch_all(db_path, "SELECT field, old_value, new_value FROM position_history "
                                 "WHERE field = 'shift_rate'")
    assert history == [{"field": "shift_rate", "old_value": "3498", "new_value": "3600"}]


@pytest.mark.asyncio
async def test_position_id_survives_changes(ingestor, db_path):
    """ID позиции не меняется при правке данных — в этом вся суть замены хэша."""
    await ingestor.ingest("ametist", [request_for("row:1", TEXT_A)])
    before = fetch_all(db_path, "SELECT position_id FROM positions")[0]["position_id"]
    await ingestor.ingest("ametist", [request_for("row:1", TEXT_A2)])
    after = fetch_all(db_path, "SELECT position_id FROM positions")
    assert len(after) == 1
    assert after[0]["position_id"] == before


@pytest.mark.asyncio
async def test_snapshot_deactivates_missing_positions(ingestor, db_path):
    await ingestor.ingest("ametist", [request_for("row:1", TEXT_A), request_for("row:2", TEXT_B)])
    stats = await ingestor.ingest("ametist", [request_for("row:1", TEXT_A)])

    assert stats.positions_deactivated == 1
    active = fetch_all(db_path, "SELECT position_id, is_active FROM positions ORDER BY position_id")
    assert [row["is_active"] for row in active] == [1, 0]


@pytest.mark.asyncio
async def test_partial_batch_without_snapshot_keeps_others(ingestor, db_path):
    """Отдельное сообщение канала — не полный список потребностей."""
    await ingestor.ingest("ametist", [request_for("row:1", TEXT_A), request_for("row:2", TEXT_B)])
    stats = await ingestor.ingest("ametist", [request_for("row:1", TEXT_A)], snapshot=False)

    assert stats.positions_deactivated == 0
    assert all(row["is_active"] == 1 for row in fetch_all(db_path, "SELECT is_active FROM positions"))


@pytest.mark.asyncio
async def test_failed_parse_keeps_previous_positions(ingestor, parser, db_path):
    await ingestor.ingest("ametist", [request_for("row:1", TEXT_A)])
    parser.fail_on = {TEXT_A2}

    stats = await ingestor.ingest("ametist", [request_for("row:1", TEXT_A2)])
    assert stats.requests_failed == 1
    assert stats.positions_deactivated == 0, "сбой разбора не значит, что потребность закрылась"

    request = fetch_all(db_path, "SELECT parse_status FROM requests")[0]
    assert request["parse_status"] == "failed"


@pytest.mark.asyncio
async def test_failed_request_is_retried_next_run(ingestor, parser):
    parser.fail_on = {TEXT_A}
    await ingestor.ingest("ametist", [request_for("row:1", TEXT_A)])
    calls_after_failure = parser.calls

    parser.fail_on = set()
    stats = await ingestor.ingest("ametist", [request_for("row:1", TEXT_A)])
    assert parser.calls > calls_after_failure, "заявку со сбоем нужно разобрать повторно"
    assert stats.positions_added == 1


@pytest.mark.asyncio
async def test_multi_chunk_request_stays_single(ingestor, parser, db_path):
    """Длинный пост режется для модели, но заявка остаётся одна."""
    parser.mapping = dict(PARSED)
    whole = TEXT_A + "\n" + TEXT_B
    raw = RawRequest(source="vahtapro", source_ref="msg:10", raw_text=whole,
                     source_name="Градус", parse_chunks=[TEXT_A, TEXT_B])

    stats = await ingestor.ingest("vahtapro", [raw])
    assert stats.requests_new == 1
    assert stats.positions_added == 2

    requests = fetch_all(db_path, "SELECT request_id, raw_text FROM requests")
    assert len(requests) == 1
    assert requests[0]["raw_text"] == whole, "для сверки хранится пост целиком"


@pytest.mark.asyncio
async def test_field_overrides_win_defaults_fill_gaps(ingestor, parser, db_path):
    parser.mapping = {"текст": [{"vacancy_name": "Что-то своё", "city": "Тверь"}]}
    raw = RawRequest(
        source="marketstaff", source_ref="row:1", raw_text="текст",
        field_overrides={"vacancy_name": "Комплектовщик"},
        field_defaults={"object_name": "Склад №2", "city": "Москва"},
    )
    await ingestor.ingest("marketstaff", [raw])

    row = fetch_all(db_path, "SELECT vacancy_name, object_name, city FROM positions")[0]
    assert row["vacancy_name"] == "Комплектовщик", "колонка таблицы главнее разбора"
    assert row["object_name"] == "Склад №2", "подстановка заполняет пропуск"
    assert row["city"] == "Тверь", "подстановка не затирает то, что нашлось в тексте"


@pytest.mark.asyncio
async def test_rescue_match_keeps_position_when_fingerprint_drifts(ingestor, parser, db_path):
    """Смена перестала определяться — позиция должна остаться той же.

    Это ровно то, что произойдёт при первом прогоне после отказа от догадок:
    раньше shift_type всегда был "day", теперь его часто нет.
    """
    parser.mapping = {
        "v1": [{"counterparty": "Молком", "vacancy_name": "Комплектовщик",
                "city": "Пушкино", "shift_type": "day"}],
        "v2": [{"counterparty": "Молком", "vacancy_name": "Комплектовщик",
                "city": "Пушкино"}],
    }
    await ingestor.ingest("ametist", [request_for("row:1", "v1")])
    before = fetch_all(db_path, "SELECT position_id, fingerprint FROM positions")[0]

    await ingestor.ingest("ametist", [request_for("row:1", "v2")])
    after = fetch_all(db_path, "SELECT position_id, fingerprint FROM positions")

    assert len(after) == 1, "не должно появиться второй позиции"
    assert after[0]["position_id"] == before["position_id"]
    assert after[0]["fingerprint"] != before["fingerprint"]


@pytest.mark.asyncio
async def test_rescue_match_when_field_becomes_empty(ingestor, parser, db_path):
    """Поле опустело — позиция всё равно должна найтись.

    Случай с боевого прогона: у Аметиста в таблице нет колонки «город», а
    выводить его из названия объекта модели больше нельзя. Город стал пустым,
    и сравнение «пусто = пусто» позицию не находило — заводился дубль.
    """
    parser.mapping = {
        "v1": [{"counterparty": "Аметист", "vacancy_name": "Грузчик-комплектовщик",
                "object_name": "ДНС Пушкино", "city": "Пушкино"}],
        "v2": [{"counterparty": "Аметист", "vacancy_name": "Грузчик-комплектовщик",
                "object_name": "ДНС Пушкино"}],
    }
    await ingestor.ingest("ametist", [request_for("row:1", "v1")])
    before = fetch_all(db_path, "SELECT position_id FROM positions")[0]["position_id"]

    stats = await ingestor.ingest("ametist", [request_for("row:1", "v2")])
    positions = fetch_all(db_path, "SELECT position_id, city FROM positions")

    assert len(positions) == 1, "дубля быть не должно"
    assert positions[0]["position_id"] == before
    assert positions[0]["city"] == "Пушкино", "известное старое значение не затирается пустым"
    assert stats.positions_deactivated == 0


@pytest.mark.asyncio
async def test_rescue_match_refuses_ambiguous_candidates(ingestor, parser, db_path):
    """Два кандидата — значит, различие в смене; склеивать нельзя."""
    parser.mapping = {
        "v1": [{"counterparty": "Миксит", "vacancy_name": "Упаковщик",
                "object_name": "Склад", "city": "Москва", "shift_type": "day"},
               {"counterparty": "Миксит", "vacancy_name": "Упаковщик",
                "object_name": "Склад", "city": "Москва", "shift_type": "night"}],
        "v2": [{"counterparty": "Миксит", "vacancy_name": "Упаковщик",
                "object_name": "Склад", "city": "Москва"}],
    }
    await ingestor.ingest("ametist", [request_for("row:1", "v1")])
    assert len(fetch_all(db_path, "SELECT 1 FROM positions")) == 2

    await ingestor.ingest("ametist", [request_for("row:1", "v2")], snapshot=False)
    positions = fetch_all(db_path, "SELECT position_id, shift_type FROM positions ORDER BY position_id")
    assert len(positions) == 3, "неоднозначность разрешается новой позицией, а не склейкой"
    assert [p["shift_type"] for p in positions] == ["day", "night", None]


@pytest.mark.asyncio
async def test_empty_parse_result_creates_no_positions(ingestor, parser, db_path):
    """Служебное сообщение — это не ошибка разбора."""
    parser.mapping = {"с добрым утром": []}
    stats = await ingestor.ingest("vahtapro", [
        RawRequest(source="vahtapro", source_ref="msg:1", raw_text="с добрым утром")
    ])
    assert stats.requests_failed == 0
    assert stats.positions_added == 0
    assert fetch_all(db_path, "SELECT parse_status FROM requests")[0]["parse_status"] == "ok"


@pytest.mark.asyncio
async def test_search_index_covers_raw_text(ingestor, db_path):
    await ingestor.ingest("ametist", [request_for("row:1", TEXT_A)])
    with db.connect(db_path) as conn:
        found = conn.execute(
            "SELECT position_id FROM search_index WHERE search_index MATCH ?", ("Пушкино",)
        ).fetchall()
    assert len(found) == 1
