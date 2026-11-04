"""Снятый контрагент (КНК, 04.11.2026) не запускается и не показывается.

Заявки и позиции снятого источника остаются в базе — на них ссылается история
кандидатов, — поэтому прятать его надо в коде, а не удалением данных.
"""

from datetime import datetime

import pytest

from registry import db
from registry.ingest import RegistryIngestor
from registry.models import RawRequest
from registry.sources import RETIRED_SOURCES, SOURCE_KPK


def test_кнк_снята():
    assert SOURCE_KPK in RETIRED_SOURCES


def test_снятый_источник_не_запускается():
    pipeline = pytest.importorskip("pipeline")
    assert SOURCE_KPK not in pipeline.ALL_SOURCES
    assert SOURCE_KPK not in pipeline.parse_sources(None), "«все источники» не должны включать КНК"
    with pytest.raises(ValueError):
        pipeline.parse_sources("kpk")
    assert pipeline.parse_sources("yappi,marketstaff") == ["yappi", "marketstaff"]


@pytest.mark.asyncio
async def test_снятого_источника_нет_на_вкладке_источники(parser, db_path):
    import navigator_api

    ingestor = RegistryIngestor(parser, db_path=db_path)
    await ingestor.ingest("kpk", [RawRequest(source="kpk", source_ref="city:Тверь",
                                             raw_text="Тверь, упаковщики", source_name="КНК")])
    await ingestor.ingest("yappi", [RawRequest(source="yappi", source_ref="row:1",
                                               raw_text="Тамбов, обвальщик", source_name="ЯППИ")])

    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE source = 'kpk'").fetchone()[0] == 1
        keys = [s["key"] for s in navigator_api.sources_block(conn, datetime.now())]

    assert keys == ["yappi"]
    assert SOURCE_KPK not in navigator_api.SOURCE_SCHEDULE
