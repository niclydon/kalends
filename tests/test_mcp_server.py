"""Tests for kalends-mcp: the service layer over the in-memory
store, the holds lifecycle against a frozen clock, the MCP tool
registry, and (when KALENDS_TEST_DSN is set) the live Postgres store.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from kalends.mcp_server import KalendsService, TOOL_NAMES
from kalends.store import InMemoryStore, KalendsError

T0 = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start: datetime = T0):
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(clock):
    return KalendsService(InMemoryStore(), now_fn=lambda: clock.now)


SOURCE = {"source_id": "cron-host-a", "adapter": "cron", "plane": "host-a"}


def a_series(series_id="cron-host-a:nightly", **over):
    row = {
        "series_id": series_id,
        "source_id": "cron-host-a",
        "source_native_id": series_id.split(":", 1)[-1],
        "title": "Nightly survey",
        "owner_kind": "worker",
        "owner_id": "worker-3",
        "plane": "host-a",
        "cadence_dialect": "cron",
        "cadence_expr": "7 3 * * *",
        "rrule": "FREQ=DAILY;BYHOUR=3;BYMINUTE=7",
        "resource_claims": {"slot_group": "survey"},
        "anchor_at": "2026-10-29T03:07:00+00:00",
    }
    row.update(over)
    return row


def an_instance(instance_id="inst-1", when="2026-10-30T03:07:00+00:00", **over):
    row = {
        "instance_id": instance_id,
        "series_id": "cron-host-a:nightly",
        "source_id": "cron-host-a",
        "source_native_id": f"nightly@{when}",
        "title": "Nightly survey",
        "owner_kind": "worker",
        "owner_id": "worker-3",
        "plane": "host-a",
        "planned_start": when,
        "planned_end": "2026-10-30T03:37:00+00:00",
        "status": "scheduled",
        "resource_claims": {"slot_group": "survey"},
    }
    row.update(over)
    return row


def a_band(band_id="band-1", **over):
    row = {
        "band_id": band_id,
        "source_id": "service-status",
        "source_native_id": "harbour-api",
        "title": "Harbour API",
        "owner_kind": "system",
        "plane": "host-b",
        "started_at": "2026-10-29T09:00:00+00:00",
        "status": "active",
    }
    row.update(over)
    return row


def a_window(window_id="win-1", **over):
    row = {
        "window_id": window_id,
        "owner_kind": "agent",
        "owner_id": "blue",
        "plane": "host-a",
        "kind": "quiet",
        "starts_at": "2026-10-30T22:00:00+00:00",
        "ends_at": "2026-10-31T06:00:00+00:00",
    }
    row.update(over)
    return row


def an_actual(**over):
    row = {
        "source_id": "cron-host-a",
        "source_native_id": "run-1",
        "series_id": "cron-host-a:nightly",
        "fired_at": "2026-10-30T03:07:11+00:00",
        "would_fire_at": "2026-10-30T03:07:00+00:00",
        "status": "completed",
        "kind": "firing",
    }
    row.update(over)
    return row


@pytest.fixture
def seeded(service):
    service.upsert_source(SOURCE)
    service.upsert_series(a_series())
    return service


# ---------------------------------------------------------------------------
# Round trips per entity
# ---------------------------------------------------------------------------

def test_source_round_trip_and_mark_synced(service):
    stored = service.upsert_source(SOURCE)
    assert stored["source_id"] == "cron-host-a"
    assert stored["enabled"] is True
    assert service.list_sources()["count"] == 1
    marked = service.mark_source_synced(
        "cron-host-a", synced_at="2026-10-30T01:00:00+00:00", status="ok")
    assert marked["last_synced_at"] == "2026-10-30T01:00:00+00:00"
    assert marked["notes"] == "ok"
    with pytest.raises(KalendsError):
        service.mark_source_synced("nope")


def test_series_upsert_get_list_update(seeded):
    got = seeded.get_series("cron-host-a:nightly")
    assert got["title"] == "Nightly survey"
    assert got["resource_claims"] == {"slot_group": "survey"}
    assert got["anchor_at"] == "2026-10-29T03:07:00+00:00"
    # upsert by primary key updates in place
    seeded.upsert_series(a_series(title="Nightly survey v2"))
    assert seeded.list_series()["count"] == 1
    assert seeded.get_series("cron-host-a:nightly")["title"] == "Nightly survey v2"
    # filters
    assert seeded.list_series(owner_id="worker-3")["count"] == 1
    assert seeded.list_series(owner_id="worker-9")["count"] == 0
    assert seeded.list_series(plane="host-b")["count"] == 0
    assert seeded.list_series(enabled=True)["count"] == 1
    assert seeded.list_series(enabled=False)["count"] == 0
    with pytest.raises(KalendsError):
        seeded.get_series("missing")


def test_instance_round_trip_and_filters(seeded):
    seeded.upsert_instance(an_instance())
    seeded.upsert_instance(an_instance(
        "inst-2", when="2026-11-02T03:07:00+00:00", status="completed"))
    assert seeded.list_instances()["count"] == 2
    window = seeded.list_instances(**{"from_ts": "2026-10-30T00:00:00+00:00",
                                      "to_ts": "2026-10-31T00:00:00+00:00"})
    assert [i["instance_id"] for i in window["instances"]] == ["inst-1"]
    assert seeded.list_instances(status="completed")["count"] == 1
    assert seeded.list_instances(series_id="cron-host-a:nightly")["count"] == 2
    assert seeded.list_instances(source_id="other")["count"] == 0


def test_band_round_trip_and_active_at(service):
    service.upsert_band(a_band())
    assert service.list_bands()["count"] == 1
    assert service.list_bands(active_at="2026-10-30T00:00:00+00:00")["count"] == 1
    assert service.list_bands(active_at="2026-10-28T00:00:00+00:00")["count"] == 0
    assert service.list_bands(plane="host-a")["count"] == 0


def test_window_round_trip(service):
    stored = service.upsert_window(a_window())
    assert stored["kind"] == "quiet"
    rows = service.list_windows(owner_id="blue")
    assert rows["count"] == 1
    assert service.list_windows(**{"from_ts": "2026-11-01T00:00:00+00:00"})["count"] == 0


def test_actual_insert_assigns_id_and_upsert_updates(service):
    stored = service.upsert_actual(an_actual())
    assert stored["actual_id"] == 1
    assert stored["kind"] == "firing"
    updated = service.upsert_actual(an_actual(actual_id=1, status="failed"))
    assert updated["actual_id"] == 1
    assert updated["status"] == "failed"
    assert service.list_actuals()["count"] == 1
    point = service.upsert_actual(an_actual(
        source_native_id="cycle-9", kind="point", is_empty=True,
        fired_at="2026-10-30T04:00:00+00:00"))
    assert point["actual_id"] == 2
    assert service.list_actuals(kind="point")["count"] == 1
    assert service.list_actuals(kind="firing")["count"] == 1


def test_deletes_report_bool(seeded):
    seeded.upsert_instance(an_instance())
    seeded.upsert_band(a_band())
    seeded.upsert_window(a_window())
    actual = seeded.upsert_actual(an_actual())
    assert seeded.delete_instance("inst-1") == {"deleted": True}
    assert seeded.delete_instance("inst-1") == {"deleted": False}
    assert seeded.delete_band("band-1") == {"deleted": True}
    assert seeded.delete_window("win-1") == {"deleted": True}
    assert seeded.delete_actual(actual["actual_id"]) == {"deleted": True}
    assert seeded.delete_series("cron-host-a:nightly") == {"deleted": True}
    assert seeded.delete_series("cron-host-a:nightly") == {"deleted": False}


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------

def test_batch_upserts_return_counts(seeded):
    assert seeded.batch_upsert_series(
        [a_series("cron-host-a:a"), a_series("cron-host-a:b")]) == {"upserted": 2}
    assert seeded.batch_upsert_instances(
        [an_instance("i-1"), an_instance("i-2")]) == {"upserted": 2}
    assert seeded.batch_upsert_bands([a_band("b-1"), a_band("b-2")]) == {"upserted": 2}
    assert seeded.batch_upsert_actuals(
        [an_actual(), an_actual(source_native_id="run-2")]) == {"upserted": 2}
    assert seeded.list_series()["count"] == 3
    assert seeded.list_actuals()["count"] == 2


def test_batch_replace_instances_respects_scope(seeded):
    seeded.upsert_instance(an_instance("inside", when="2026-10-30T03:07:00+00:00"))
    seeded.upsert_instance(an_instance("after", when="2026-11-05T03:07:00+00:00"))
    seeded.upsert_instance(an_instance(
        "other-source", when="2026-10-30T03:07:00+00:00", source_id="patrol-calendar"))
    result = seeded.batch_replace_instances(
        "cron-host-a", "2026-10-30T00:00:00+00:00", "2026-10-31T00:00:00+00:00",
        [an_instance("fresh", when="2026-10-30T09:00:00+00:00")])
    assert result == {"deleted": 1, "inserted": 1}
    ids = {i["instance_id"] for i in seeded.list_instances(limit=100)["instances"]}
    assert ids == {"after", "other-source", "fresh"}


def test_batch_replace_instances_rejects_scope_mismatch(seeded):
    with pytest.raises(KalendsError):
        seeded.batch_replace_instances(
            "cron-host-a", "2026-10-30T00:00:00+00:00", "2026-10-31T00:00:00+00:00",
            [an_instance("x", source_id="patrol-calendar")])


def test_batch_replace_actuals_respects_scope(seeded):
    seeded.upsert_actual(an_actual(fired_at="2026-10-30T03:07:11+00:00"))
    seeded.upsert_actual(an_actual(source_native_id="old",
                                   fired_at="2026-10-20T03:07:11+00:00"))
    seeded.upsert_actual(an_actual(source_native_id="other", source_id="fire-log",
                                   fired_at="2026-10-30T03:07:11+00:00"))
    result = seeded.batch_replace_actuals(
        "cron-host-a", "2026-10-30T00:00:00+00:00", "2026-10-31T00:00:00+00:00",
        [an_actual(source_native_id="fresh", fired_at="2026-10-30T05:00:00+00:00")])
    assert result == {"deleted": 1, "inserted": 1}
    natives = {a["source_native_id"] for a in seeded.list_actuals(limit=100)["actuals"]}
    assert natives == {"old", "other", "fresh"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda s: s.upsert_series({"series_id": "x"}),                       # missing fields
    lambda s: s.upsert_series(a_series(owner_kind="robot")),             # bad enum
    lambda s: s.upsert_series(a_series(bogus_field=1)),                  # unknown field
    lambda s: s.upsert_instance(an_instance(planned_start="not-a-date")),  # bad timestamp
    lambda s: s.upsert_band(a_band(status="running")),                   # bad enum
    lambda s: s.upsert_window(a_window(kind="loud")),                    # bad enum
    lambda s: s.upsert_actual({"source_id": "x", "source_native_id": "y"}),  # no fired_at
    lambda s: s.list_series(limit=0),                                    # bad limit
    lambda s: s.list_instances(offset=-1),                               # bad offset
    lambda s: s.batch_upsert_series({"not": "a list"}),                  # not a list
    lambda s: s.delete_actual("abc"),                                    # bad id type
])
def test_validation_errors(service, call):
    service.upsert_source(SOURCE)
    with pytest.raises(KalendsError):
        call(service)


# ---------------------------------------------------------------------------
# Holds lifecycle
# ---------------------------------------------------------------------------

def declare(service, **over):
    args = {"title": "Chart the harbour survey", "owner_id": "worker-3",
            "owner_kind": "worker", "plane": "host-a", "lease_seconds": 3600}
    args.update(over)
    return service.declare_hold(**args)


def test_hold_declare_heartbeat_release(service, clock):
    band = declare(service, resource_claims={"slot_group": "survey"},
                   metadata={"ticket": "ex-1"})
    assert band["source_id"] == "holds"
    assert band["band_id"].startswith("holds:hold-")
    assert band["lease_expires_at"] == "2026-10-30T01:00:00+00:00"
    assert band["status"] == "active"
    assert band["extensions"]["metadata"] == {"ticket": "ex-1"}
    # declaring auto-registers the holds source (the bands FK target)
    assert [s["source_id"] for s in service.list_sources()["sources"]] == ["holds"]

    clock.advance(600)
    beaten = service.heartbeat_hold(band["band_id"])
    # no explicit lease: the original 3600s span is reused from now
    assert beaten["last_heartbeat_at"] == "2026-10-30T00:10:00+00:00"
    assert beaten["lease_expires_at"] == "2026-10-30T01:10:00+00:00"

    clock.advance(60)
    beaten = service.heartbeat_hold(band["band_id"], lease_seconds=120)
    assert beaten["lease_expires_at"] == "2026-10-30T00:13:00+00:00"

    closed = service.release_hold(band["band_id"], end_reason="completed")
    assert closed["status"] == "done"
    assert closed["end_reason"] == "completed"
    assert closed["ended_at"] == "2026-10-30T00:11:00+00:00"
    # releasing again reports the closure that stands; heartbeating errors
    assert service.release_hold(band["band_id"])["end_reason"] == "completed"
    with pytest.raises(KalendsError):
        service.heartbeat_hold(band["band_id"])


def test_hold_release_defaults_and_reason_validation(service):
    band = declare(service)
    with pytest.raises(KalendsError):
        service.release_hold(band["band_id"], end_reason="expired")
    closed = service.release_hold(band["band_id"])
    assert closed["end_reason"] == "released"
    with pytest.raises(KalendsError):
        service.release_hold("holds:hold-unknown")
    with pytest.raises(KalendsError):
        service.heartbeat_hold("holds:hold-unknown")


def test_hold_expiry_materialises_on_read(service, clock):
    band = declare(service, lease_seconds=60)
    assert band["lease_expires_at"] == "2026-10-30T00:01:00+00:00"
    clock.advance(61)
    holds = service.list_holds()["holds"]
    assert len(holds) == 1
    expired = holds[0]
    assert expired["status"] == "done"
    assert expired["end_reason"] == "expired"
    assert expired["ended_at"] == "2026-10-30T00:01:00+00:00"  # at the lease, not at read time
    # persisted: the store row itself is closed, and later reads agree
    assert service.store.get_band(band["band_id"]).status == "done"
    assert service.list_holds(include_expired=False)["count"] == 0
    with pytest.raises(KalendsError):
        service.heartbeat_hold(band["band_id"])


def test_hold_list_filters_by_owner(service):
    declare(service, owner_id="worker-3")
    declare(service, owner_id="blue", owner_kind="agent")
    assert service.list_holds()["count"] == 2
    assert service.list_holds(owner_id="blue")["count"] == 1


def test_hold_declare_validation(service):
    with pytest.raises(KalendsError):
        declare(service, lease_seconds=0)
    with pytest.raises(KalendsError):
        declare(service, owner_kind="robot")
    with pytest.raises(KalendsError):
        declare(service, title="")


# ---------------------------------------------------------------------------
# MCP binding
# ---------------------------------------------------------------------------

def test_tool_registry_matches_contract():
    pytest.importorskip("mcp")
    import anyio
    import json

    from kalends.mcp_server import build_server

    server = build_server(KalendsService(InMemoryStore(), now_fn=lambda: T0))

    async def run():
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert names == set(TOOL_NAMES)
        assert len(names) == 30
        by_name = {t.name: t for t in tools}
        # the range argument is spelled "from" in the schema
        props = by_name["kalends_list_instances"].input_schema["properties"]
        assert "from" in props and "from_" not in props
        result = await server.call_tool("kalends_list_sources", {})
        assert json.loads(result.content[0].text) == {"sources": [], "count": 0}
        result = await server.call_tool("kalends_declare_hold", {
            "title": "Survey build", "owner_id": "worker-3",
            "plane": "host-a", "lease_seconds": 60})
        band = json.loads(result.content[0].text)
        assert band["band_id"].startswith("holds:hold-")
        assert band["lease_expires_at"] == "2026-10-30T00:01:00+00:00"
        result = await server.call_tool(
            "kalends_list_instances", {"from": "2026-10-30T00:00:00+00:00"})
        assert json.loads(result.content[0].text)["count"] == 0

    anyio.run(run)


def test_origin_guard_middleware():
    pytest.importorskip("starlette")
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from kalends.mcp_server import OriginGuardMiddleware

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def mcp(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/health", health), Route("/mcp", mcp)])
    client = TestClient(OriginGuardMiddleware(app, "s3cret"),
                        raise_server_exceptions=False)
    assert client.get("/health").status_code == 200          # health is open
    assert client.get("/mcp").status_code == 403              # no header
    assert client.get("/mcp", headers={"x-origin-secret": "wrong"}).status_code == 403
    assert client.get("/mcp", headers={"x-origin-secret": "s3cret"}).status_code == 200
    open_client = TestClient(OriginGuardMiddleware(app, None),
                             raise_server_exceptions=False)
    assert open_client.get("/mcp").status_code == 200         # unset secret: no guard


# ---------------------------------------------------------------------------
# Live Postgres store (only when a scratch DSN is provided)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("KALENDS_TEST_DSN"),
                    reason="KALENDS_TEST_DSN not set")
def test_postgres_store_live():
    from pathlib import Path

    from kalends.store import PostgresStore

    dsn = os.environ["KALENDS_TEST_DSN"]
    store = PostgresStore(dsn)
    schema = (Path(__file__).resolve().parents[1] / "schema" / "001_kalends.sql").read_text()
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(schema)
    svc = KalendsService(store, now_fn=lambda: T0)
    svc.upsert_source(SOURCE)
    svc.upsert_series(a_series())
    svc.upsert_instance(an_instance())
    try:
        assert svc.get_series("cron-host-a:nightly")["title"] == "Nightly survey"
        assert svc.list_instances()["count"] >= 1
        result = svc.batch_replace_instances(
            "cron-host-a", "2026-10-30T00:00:00+00:00", "2026-10-31T00:00:00+00:00",
            [an_instance("pg-fresh", when="2026-10-30T09:00:00+00:00")])
        assert result == {"deleted": 1, "inserted": 1}
        band = svc.declare_hold(title="Live hold", owner_id="worker-3",
                                plane="host-a", lease_seconds=60)
        assert band["lease_expires_at"] == "2026-10-30T00:01:00+00:00"
        assert svc.release_hold(band["band_id"])["status"] == "done"
    finally:
        svc.delete_instance("pg-fresh")
        svc.delete_series("cron-host-a:nightly")
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM calendar_bands WHERE source_id = 'holds'")
            cur.execute("DELETE FROM calendar_sources WHERE source_id IN ('cron-host-a', 'holds')")
