"""Tests for kalends_delete_source.

Deleting a source is refused while it still has dependent rows
(series, instances, bands, windows, actuals) unless force=true is
passed; with force the dependents go with the source. An unknown
source errors cleanly. Exercised at the service layer (over the
in-memory store), directly against the store, and through the MCP
tool registry.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from kalends.mcp_server import KalendsService
from kalends.store import InMemoryStore, KalendsError

T0 = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)

SOURCE = {"source_id": "src-a", "adapter": "cron", "plane": "host-a"}
OTHER = {"source_id": "src-b", "adapter": "cron", "plane": "host-b"}


@pytest.fixture
def service():
    return KalendsService(InMemoryStore(), now_fn=lambda: T0)


def seed_dependents(service, source_id="src-a"):
    prefix = source_id
    service.upsert_series({
        "series_id": f"{prefix}:nightly", "source_id": source_id,
        "source_native_id": "nightly", "title": "Nightly",
        "owner_kind": "worker", "owner_id": "worker-3", "plane": "host-a",
    })
    service.upsert_instance({
        "instance_id": f"{prefix}:inst-1", "series_id": f"{prefix}:nightly",
        "source_id": source_id, "source_native_id": "nightly@1",
        "title": "Nightly", "owner_kind": "worker", "owner_id": "worker-3",
        "plane": "host-a", "planned_start": "2026-10-30T03:07:00+00:00",
        "status": "scheduled",
    })
    service.upsert_band({
        "band_id": f"{prefix}:band-1", "source_id": source_id,
        "source_native_id": "svc", "title": "Service band",
        "owner_kind": "system", "plane": "host-a",
        "started_at": "2026-10-29T09:00:00+00:00", "status": "active",
    })
    service.upsert_window({
        "window_id": f"{prefix}:win-1", "owner_kind": "agent",
        "owner_id": "blue", "plane": "host-a", "kind": "quiet",
        "source_id": source_id, "source_native_id": "win",
        "starts_at": "2026-10-30T22:00:00+00:00",
        "ends_at": "2026-10-31T06:00:00+00:00",
    })
    return service.upsert_actual({
        "source_id": source_id, "source_native_id": "run-1",
        "series_id": f"{prefix}:nightly", "instance_id": f"{prefix}:inst-1",
        "fired_at": "2026-10-30T03:07:11+00:00", "status": "completed",
        "kind": "firing",
    })


def test_delete_source_unknown_errors(service):
    with pytest.raises(KalendsError, match="unknown source"):
        service.delete_source("nope")
    with pytest.raises(KalendsError, match="unknown source"):
        service.delete_source("nope", force=True)
    with pytest.raises(KalendsError):
        service.delete_source("")


def test_delete_source_zero_dependents(service):
    service.upsert_source(SOURCE)
    result = service.delete_source("src-a")
    assert result == {
        "deleted": True, "source_id": "src-a", "force": False,
        "deleted_dependents": {"series": 0, "instances": 0, "bands": 0,
                               "windows": 0, "actuals": 0},
    }
    assert service.list_sources() == {"sources": [], "count": 0}
    # deleting it again is the unknown-source error, not a silent false
    with pytest.raises(KalendsError, match="unknown source"):
        service.delete_source("src-a")


def test_delete_source_refused_with_dependents_no_force(service):
    service.upsert_source(SOURCE)
    seed_dependents(service)
    with pytest.raises(KalendsError, match="dependent rows"):
        service.delete_source("src-a")
    # refusal is total: source and every dependent row still present
    assert service.list_sources()["count"] == 1
    assert service.list_series()["count"] == 1
    assert service.list_instances()["count"] == 1
    assert service.list_bands()["count"] == 1
    assert service.list_windows()["count"] == 1
    assert service.list_actuals()["count"] == 1


def test_delete_source_force_deletes_dependents(service):
    service.upsert_source(SOURCE)
    service.upsert_source(OTHER)
    seed_dependents(service, "src-a")
    seed_dependents(service, "src-b")
    result = service.delete_source("src-a", force=True)
    assert result["deleted"] is True
    assert result["force"] is True
    assert result["deleted_dependents"] == {
        "series": 1, "instances": 1, "bands": 1, "windows": 1, "actuals": 1}
    # the source and all of its dependents are gone
    assert [s["source_id"] for s in service.list_sources()["sources"]] == ["src-b"]
    assert service.list_series(source_id="src-a")["count"] == 0
    assert service.list_instances(source_id="src-a")["count"] == 0
    assert service.list_bands(source_id="src-a")["count"] == 0
    assert service.list_windows()["count"] == 1  # src-b's window remains
    assert service.list_actuals(source_id="src-a")["count"] == 0
    # the other source is untouched (transaction integrity / scoping)
    assert service.list_series(source_id="src-b")["count"] == 1
    assert service.list_instances(source_id="src-b")["count"] == 1
    assert service.list_bands(source_id="src-b")["count"] == 1
    assert service.list_actuals(source_id="src-b")["count"] == 1
    with pytest.raises(KalendsError):
        service.get_series("src-a:nightly")


def test_delete_source_force_cascades_fk_dependents(service):
    # An actual filed by another source against this source's instance
    # still FK-references the instance, so force must remove it too or
    # the delete cannot land.
    service.upsert_source(SOURCE)
    service.upsert_source(OTHER)
    seed_dependents(service, "src-a")
    service.upsert_actual({
        "source_id": "src-b", "source_native_id": "cross-run",
        "series_id": "src-a:nightly", "instance_id": "src-a:inst-1",
        "fired_at": "2026-10-30T04:00:00+00:00", "status": "completed",
        "kind": "firing",
    })
    result = service.delete_source("src-a", force=True)
    assert result["deleted_dependents"]["actuals"] == 2
    assert service.list_actuals()["count"] == 0
    assert [s["source_id"] for s in service.list_sources()["sources"]] == ["src-b"]


def test_delete_source_store_level_directly(service):
    store = service.store
    service.upsert_source(SOURCE)
    assert store.delete_source("src-a")["deleted"] is True
    with pytest.raises(KalendsError, match="unknown source"):
        store.delete_source("src-a")


def test_delete_source_tool_registered_and_callable():
    pytest.importorskip("mcp")
    import anyio
    import json

    from kalends.mcp_server import build_server

    server = build_server(KalendsService(InMemoryStore(), now_fn=lambda: T0))

    async def run():
        tools = await server.list_tools()
        by_name = {t.name: t for t in tools}
        assert "kalends_delete_source" in by_name
        props = by_name["kalends_delete_source"].input_schema["properties"]
        assert "source_id" in props and "force" in props
        result = await server.call_tool(
            "kalends_upsert_source", {"source": SOURCE})
        assert json.loads(result.content[0].text)["source_id"] == "src-a"
        result = await server.call_tool(
            "kalends_delete_source", {"source_id": "src-a"})
        payload = json.loads(result.content[0].text)
        assert payload["deleted"] is True
        result = await server.call_tool("kalends_list_sources", {})
        assert json.loads(result.content[0].text)["count"] == 0

    anyio.run(run)


@pytest.mark.skipif(not os.environ.get("KALENDS_TEST_DSN"),
                    reason="KALENDS_TEST_DSN not set")
def test_delete_source_postgres_live():
    from pathlib import Path

    from kalends.store import PostgresStore

    dsn = os.environ["KALENDS_TEST_DSN"]
    store = PostgresStore(dsn)
    schema = (Path(__file__).resolve().parents[1]
              / "schema" / "001_kalends.sql").read_text()
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(schema)
    svc = KalendsService(store, now_fn=lambda: T0)
    svc.upsert_source({"source_id": "pg-del", "adapter": "cron",
                       "plane": "host-a"})
    try:
        seed_dependents(svc, "pg-del")
        with pytest.raises(KalendsError, match="dependent rows"):
            svc.delete_source("pg-del")
        assert svc.list_sources()["count"] >= 1
        result = svc.delete_source("pg-del", force=True)
        assert result["deleted_dependents"] == {
            "series": 1, "instances": 1, "bands": 1, "windows": 1,
            "actuals": 1}
        assert svc.list_series(source_id="pg-del")["count"] == 0
        assert svc.list_instances(source_id="pg-del")["count"] == 0
        assert svc.list_bands(source_id="pg-del")["count"] == 0
        assert svc.list_actuals(source_id="pg-del")["count"] == 0
    finally:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM calendar_actuals WHERE source_id = 'pg-del'")
            cur.execute("DELETE FROM calendar_instances WHERE source_id = 'pg-del'")
            cur.execute("DELETE FROM calendar_bands WHERE source_id = 'pg-del'")
            cur.execute("DELETE FROM calendar_windows WHERE source_id = 'pg-del'")
            cur.execute("DELETE FROM calendar_series WHERE source_id = 'pg-del'")
            cur.execute("DELETE FROM calendar_sources WHERE source_id = 'pg-del'")
