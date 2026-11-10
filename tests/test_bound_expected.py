"""Tests for the bound_expected marker (public parts).

Schema: calendar_instances.bound_expected (001 fresh installs, 003
for existing ones). The projector marks instances of a series whose
resource_claims carry cadence_semantics == 'bound'; other series
stay unmarked. Stores round-trip the field, batch upserts included,
and kalends_list_instances filters on it following the 0.5.0
owner/shape filter pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from kalends.mcp_server import KalendsService
from kalends.model import Instance, Series
from kalends.projector import project
from kalends.store import InMemoryStore

NOW = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
T0 = NOW

SOURCE = {"source_id": "cron-host-a", "adapter": "cron", "plane": "host-a"}


def daily_series(series_id="test:daily", **kw) -> Series:
    base = dict(
        series_id=series_id, source_id="test",
        source_native_id=series_id.split(":", 1)[-1], title="Daily job",
        owner_kind="system", plane="host-a", cadence_dialect="cron",
        cadence_expr="0 3 * * *",
        rrule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0",
    )
    base.update(kw)
    return Series(**base)


# ---------------------------------------------------------------------------
# Projector marking
# ---------------------------------------------------------------------------

def test_projector_marks_bound_series_instances():
    series = daily_series(
        resource_claims={"cadence_semantics": "bound",
                         "slot_group": "survey"})
    out = project([series], now=NOW)
    assert len(out.instances) == 21
    assert all(i.bound_expected is True for i in out.instances)


def test_projector_leaves_metronome_series_unmarked():
    series = daily_series(resource_claims={"cadence_semantics": "metronome"})
    out = project([series], now=NOW)
    assert len(out.instances) == 21
    assert all(i.bound_expected is False for i in out.instances)


def test_projector_leaves_unmarked_series_unmarked():
    out = project([daily_series()], now=NOW)
    assert out.instances
    assert all(i.bound_expected is False for i in out.instances)


def test_projector_marks_provided_instances_of_bound_series():
    chain = Series(
        series_id="test:chain", source_id="test", source_native_id="chain",
        title="Chain job", owner_kind="worker", plane="platform",
        cadence_dialect="self-chain", cadence_expr="idle_delay_seconds=900",
        rrule=None, resource_claims={"cadence_semantics": "bound"},
    )
    queued = Instance(
        instance_id="test:queue:1", series_id="test:chain", source_id="test",
        source_native_id="1", title="Chain job",
        planned_start=datetime(2026, 10, 30, 6, 15, tzinfo=timezone.utc),
        status="scheduled",
    )
    out = project([chain], provided_instances=[queued], now=NOW)
    assert [i.instance_id for i in out.instances] == ["test:queue:1"]
    assert out.instances[0].bound_expected is True


def test_projector_mixed_fleet_marks_only_bound():
    bound = daily_series("test:bound",
                         resource_claims={"cadence_semantics": "bound"})
    plain = daily_series("test:plain")
    out = project([bound, plain], now=NOW)
    by_series = {}
    for inst in out.instances:
        by_series.setdefault(inst.series_id, set()).add(inst.bound_expected)
    assert by_series == {"test:bound": {True}, "test:plain": {False}}


# ---------------------------------------------------------------------------
# Model + schema
# ---------------------------------------------------------------------------

def test_instance_model_round_trip():
    inst = Instance(instance_id="i-1", source_id="s", source_native_id="1",
                    title="T", bound_expected=True)
    assert Instance.from_dict(inst.to_dict()).bound_expected is True
    plain = Instance(instance_id="i-2", source_id="s", source_native_id="2",
                     title="T")
    assert plain.bound_expected is False
    assert plain.to_dict()["bound_expected"] is False


def test_schema_carries_bound_expected():
    root = Path(__file__).resolve().parents[1]
    sql001 = (root / "schema" / "001_kalends.sql").read_text()
    assert "bound_expected" in sql001
    sql003 = (root / "schema" / "003_bound_expected.sql").read_text()
    assert "calendar_instances" in sql003
    assert "ADD COLUMN IF NOT EXISTS bound_expected" in sql003


# ---------------------------------------------------------------------------
# Store round-trip + filter
# ---------------------------------------------------------------------------

def _inst(instance_id, bound):
    return Instance(
        instance_id=instance_id, source_id="cron-host-a",
        source_native_id=instance_id, title="Nightly survey",
        owner_kind="worker", owner_id="worker-3", plane="host-a",
        planned_start=datetime(2026, 10, 30, 3, 7, tzinfo=timezone.utc),
        status="scheduled", bound_expected=bound,
    )


def test_store_round_trip_and_bound_expected_filter():
    store = InMemoryStore()
    store.upsert_instance(_inst("i-bound", True))
    store.upsert_instance(_inst("i-plain", False))
    assert store.list_instances()[0].bound_expected in (True, False)
    assert [i.instance_id for i in store.list_instances(bound_expected=True)] == ["i-bound"]
    assert [i.instance_id for i in store.list_instances(bound_expected=False)] == ["i-plain"]
    assert len(store.list_instances()) == 2
    # upsert flips the stored value in place
    store.upsert_instance(_inst("i-plain", True))
    assert [i.instance_id for i in store.list_instances(bound_expected=True)] == [
        "i-bound", "i-plain"]


def test_store_batch_upsert_round_trips_bound_expected():
    store = InMemoryStore()
    assert store.batch_upsert_instances(
        [_inst("b-1", True), _inst("b-2", False)]) == 2
    assert [i.instance_id for i in store.list_instances(bound_expected=True)] == ["b-1"]


# ---------------------------------------------------------------------------
# Service / MCP surface
# ---------------------------------------------------------------------------

@pytest.fixture
def service():
    svc = KalendsService(InMemoryStore(), now_fn=lambda: T0)
    svc.upsert_source(SOURCE)
    return svc


def an_instance(instance_id="inst-1", **over):
    row = {
        "instance_id": instance_id, "series_id": None,
        "source_id": "cron-host-a", "source_native_id": instance_id,
        "title": "Nightly survey", "owner_kind": "worker",
        "owner_id": "worker-3", "plane": "host-a",
        "planned_start": "2026-10-30T03:07:00+00:00", "status": "scheduled",
    }
    row.update(over)
    return row


def test_service_upsert_and_filter_bound_expected(service):
    service.upsert_instance(an_instance("inst-1", bound_expected=True))
    service.upsert_instance(an_instance("inst-2"))
    assert service.list_instances()["count"] == 2
    marked = service.list_instances(bound_expected=True)
    assert [i["instance_id"] for i in marked["instances"]] == ["inst-1"]
    assert marked["instances"][0]["bound_expected"] is True
    unmarked = service.list_instances(bound_expected=False)
    assert [i["instance_id"] for i in unmarked["instances"]] == ["inst-2"]
    assert unmarked["instances"][0]["bound_expected"] is False
    # composes with the 0.5.0 filters
    assert service.list_instances(
        bound_expected=True, owner_id="worker-9")["count"] == 0
    assert service.list_instances(
        bound_expected=True, owner_id="worker-3")["count"] == 1


def test_service_batch_upsert_round_trips_bound_expected(service):
    result = service.batch_upsert_instances([
        an_instance("bb-1", bound_expected=True),
        an_instance("bb-2", bound_expected=False),
    ])
    assert result == {"upserted": 2}
    assert [i["instance_id"] for i in
            service.list_instances(bound_expected=True)["instances"]] == ["bb-1"]


def test_tool_schema_exposes_bound_expected_filter():
    pytest.importorskip("mcp")
    import anyio
    import json

    from kalends.mcp_server import build_server

    server = build_server(KalendsService(InMemoryStore(), now_fn=lambda: T0))

    async def run():
        tools = await server.list_tools()
        by_name = {t.name: t for t in tools}
        props = by_name["kalends_list_instances"].input_schema["properties"]
        assert "bound_expected" in props
        result = await server.call_tool(
            "kalends_list_instances", {"bound_expected": True})
        assert json.loads(result.content[0].text) == {
            "instances": [], "count": 0}

    anyio.run(run)
