"""Внутренние идентификаторы: выдаются один раз и не переиспользуются."""

from registry.ids import (
    format_position_id,
    format_request_id,
    next_position_id,
    next_request_id,
    parse_request_id,
    request_id_of,
    sync_counters,
)


def test_request_ids_are_sequential_within_year(conn):
    first, year, seq = next_request_id(conn, 2026)
    second, _, _ = next_request_id(conn, 2026)
    assert first == "ELT-2026-000001"
    assert second == "ELT-2026-000002"
    assert (year, seq) == (2026, 1)


def test_counter_is_per_year(conn):
    next_request_id(conn, 2026)
    next_request_id(conn, 2026)
    other, _, _ = next_request_id(conn, 2027)
    assert other == "ELT-2027-000001"


def test_position_ids_are_scoped_to_request(conn):
    request_id, _, _ = next_request_id(conn, 2026)
    first, _ = next_position_id(conn, request_id)
    second, _ = next_position_id(conn, request_id)
    assert first == "ELT-2026-000001-01"
    assert second == "ELT-2026-000001-02"


def test_parsing_round_trip():
    request_id = format_request_id(2026, 123)
    assert request_id == "ELT-2026-000123"
    assert parse_request_id(request_id) == (2026, 123)
    assert request_id_of(format_position_id(request_id, 4)) == request_id
    assert parse_request_id("что-то не наше") is None
    assert request_id_of("ELT-2026-000123") is None


def test_sync_counters_after_bulk_load(conn):
    """После миграции строки вставлены с готовыми id — счётчики нужно догнать."""
    conn.execute(
        "INSERT INTO requests (request_id, year, seq, source, source_ref, content_hash, "
        "first_seen_at, last_seen_at) VALUES ('ELT-2026-000500', 2026, 500, 'ametist', "
        "'row:1', 'hash', '2026-01-01', '2026-01-01')"
    )
    sync_counters(conn)
    next_id, _, _ = next_request_id(conn, 2026)
    assert next_id == "ELT-2026-000501"
