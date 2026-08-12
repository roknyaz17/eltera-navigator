"""Ретроспективное применение справочников.

Подтверждение записи в справочнике должно доезжать и до уже лежащих в реестре
позиций. Иначе свежие заявки нормализуются по новому справочнику, старые — по
старому, и фильтр опять показывает два варианта одной должности.
"""

import pytest

from registry import db
from registry import dictionaries as dicts
from registry.ingest import RegistryIngestor
from registry.models import RawRequest
from scripts.renormalize import renormalize


def parsed(vacancy_name, counterparty="ООО Молком", city="г. Пушкино"):
    return [{"counterparty": counterparty, "vacancy_name": vacancy_name,
             "city": city, "shift_rate": "3498 р/смена"}]


@pytest.fixture
async def seeded(parser, db_path):
    parser.mapping = {"t1": parsed("Комплектовщик СПК")}
    await RegistryIngestor(parser, db_path=db_path).ingest(
        "ametist", [RawRequest(source="ametist", source_ref="r1", raw_text="t1")]
    )
    return db_path


@pytest.mark.asyncio
async def test_confirmation_applies_to_existing_positions(seeded):
    with db.connect(seeded) as conn:
        before = conn.execute("SELECT * FROM positions").fetchone()
        dicts.confirm(conn, dicts.KIND_JOB_TITLE, "комплектовщик спк", "Комплектовщик")
        dicts.confirm(conn, dicts.KIND_COUNTERPARTY, "ооо молком", "Молком")

    with db.connect(seeded) as conn:
        result = renormalize(conn)

    assert result["updated"] == 1
    with db.connect(seeded) as conn:
        after = conn.execute("SELECT * FROM positions").fetchone()
        found = conn.execute(
            "SELECT COUNT(*) AS cnt FROM search_index WHERE search_index MATCH ?",
            ("Комплектовщик",),
        ).fetchone()["cnt"]

    assert after["vacancy_name"] == "Комплектовщик"
    assert after["counterparty"] == "Молком"
    assert after["position_id"] == before["position_id"], "ID позиции меняться не должен"
    assert after["fingerprint"] != before["fingerprint"], "ключ склейки пересчитан"
    assert found == 1, "поисковый индекс обновлён"


@pytest.mark.asyncio
async def test_dry_run_changes_nothing(seeded):
    with db.connect(seeded) as conn:
        dicts.confirm(conn, dicts.KIND_JOB_TITLE, "комплектовщик спк", "Комплектовщик")
    with db.connect(seeded) as conn:
        result = renormalize(conn, dry_run=True)
    assert result["updated"] == 1
    with db.connect(seeded) as conn:
        assert conn.execute("SELECT vacancy_name FROM positions").fetchone()[0] == "Комплектовщик СПК"


@pytest.mark.asyncio
async def test_manager_fields_untouched(seeded):
    with db.connect(seeded) as conn:
        position_id = conn.execute("SELECT position_id FROM positions").fetchone()[0]
        conn.execute("UPDATE positions SET status = 'в работе' WHERE position_id = ?", (position_id,))
        dicts.confirm(conn, dicts.KIND_JOB_TITLE, "комплектовщик спк", "Комплектовщик")
    with db.connect(seeded) as conn:
        renormalize(conn)
        assert conn.execute("SELECT status FROM positions").fetchone()[0] == "в работе"


@pytest.mark.asyncio
async def test_collision_is_reported_not_merged(parser, db_path):
    """Если после подтверждения две позиции сходятся в одну — не склеиваем молча."""
    parser.mapping = {"t1": parsed("Комплектовщик СПК"), "t2": parsed("КОМПЛЕКТОВЩИК")}
    await RegistryIngestor(parser, db_path=db_path).ingest("ametist", [
        RawRequest(source="ametist", source_ref="r1", raw_text="t1"),
        RawRequest(source="ametist", source_ref="r2", raw_text="t2"),
    ])

    with db.connect(db_path) as conn:
        dicts.confirm(conn, dicts.KIND_JOB_TITLE, "комплектовщик спк", "Комплектовщик")
        dicts.confirm(conn, dicts.KIND_JOB_TITLE, "комплектовщик", "Комплектовщик")

    with db.connect(db_path) as conn:
        result = renormalize(conn)

    assert result["collisions"] == 1
    with db.connect(db_path) as conn:
        names = [row[0] for row in conn.execute("SELECT vacancy_name FROM positions ORDER BY position_id")]
    assert names == ["Комплектовщик СПК", "Комплектовщик"], "обе позиции на месте"
