"""Tests for the owner/shape filters on the list tools.

``kalends_list_instances`` filters by ``owner_id``, ``owner_kind``
and ``shape``; ``kalends_list_bands`` filters by ``owner_kind``.
Filtering happens in the store, an unknown filter value returns an
empty result (never an error), and unfiltered calls are unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalends.mcp_server import KalendsService
from kalends.store import InMemoryStore

T0 = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)

SOURCE = {"source_id": "cron-host-a", "adapter": "cron", "plane": "host-a"}


@pytest.fixture
def service():
    return KalendsService(InMemoryStore(), now_fn=lambda: T0)


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
        "source_native_id": band_id,
        "title": "Harbour API",
        "owner_kind": "system",
        "plane": "host-b",
        "started_at": "2026-10-29T09:00:00+00:00",
        "status": "active",
    }
    row.update(over)
    return row


@pytest.fixture
def seeded_instances(service):
    service.upsert_source(SOURCE)
    service.upsert_instance(an_instance(
        "inst-a1", when="2026-10-30T03:07:00+00:00",
        owner_id="worker-3", owner_kind="worker", shape="instance"))
    service.upsert_instance(an_instance(
        "inst-a2", when="2026-10-31T03:07:00+00:00",
        owner_id="worker-3", owner_kind="worker", shape="one-off"))
    service.upsert_instance(an_instance(
        "inst-b1", when="2026-11-01T03:07:00+00:00",
        owner_id="worker-9", owner_kind="worker", shape="instance"))
    service.upsert_instance(an_instance(
        "inst-c1", when="2026-11-02T03:07:00+00:00",
        owner_id="blue", owner_kind="agent", shape="one-off"))
    service.upsert_instance(an_instance(
        "inst-c2", when="2026-11-03T03:07:00+00:00",
        owner_id="blue", owner_kind="agent", shape="instance"))
    return service


@pytest.fixture
def seeded_bands(service):
    service.upsert_band(a_band("band-1", owner_kind="system"))
    service.upsert_band(a_band("band-2", owner_kind="worker", owner_id="worker-3"))
    service.upsert_band(a_band("band-3", owner_kind="agent", owner_id="blue"))
    service.upsert_band(a_band("band-4", owner_kind="worker", owner_id="worker-9"))
    return service


def _instance_ids(result):
    return [i["instance_id"] for i in result["instances"]]


def _band_ids(result):
    return [b["band_id"] for b in result["bands"]]


# ---------------------------------------------------------------------------
# Instances: filtered results are exactly the matching subset
# ---------------------------------------------------------------------------

def test_list_instances_owner_id_filter_matches_unfiltered_subset(seeded_instances):
    unfiltered = seeded_instances.list_instances(limit=100)["instances"]
    for owner_id in ("worker-3", "worker-9", "blue"):
        expected = [i["instance_id"] for i in unfiltered if i["owner_id"] == owner_id]
        assert _instance_ids(seeded_instances.list_instances(owner_id=owner_id)) == expected
        assert expected  # the fixture actually exercises this owner


def test_list_instances_owner_kind_filter_matches_unfiltered_subset(seeded_instances):
    unfiltered = seeded_instances.list_instances(limit=100)["instances"]
    for owner_kind in ("worker", "agent"):
        expected = [i["instance_id"] for i in unfiltered if i["owner_kind"] == owner_kind]
        assert _instance_ids(seeded_instances.list_instances(owner_kind=owner_kind)) == expected
        assert expected


def test_list_instances_shape_filter_matches_unfiltered_subset(seeded_instances):
    unfiltered = seeded_instances.list_instances(limit=100)["instances"]
    for shape in ("instance", "one-off"):
        expected = [i["instance_id"] for i in unfiltered if i["shape"] == shape]
        assert _instance_ids(seeded_instances.list_instances(shape=shape)) == expected
        assert expected


def test_list_instances_combined_filters_match_unfiltered_subset(seeded_instances):
    unfiltered = seeded_instances.list_instances(limit=100)["instances"]
    expected = [i["instance_id"] for i in unfiltered
                if i["owner_id"] == "worker-3" and i["owner_kind"] == "worker"
                and i["shape"] == "one-off"]
    result = seeded_instances.list_instances(
        owner_id="worker-3", owner_kind="worker", shape="one-off")
    assert _instance_ids(result) == expected == ["inst-a2"]
    # a combination nothing satisfies is empty, not an error
    assert seeded_instances.list_instances(
        owner_id="worker-3", owner_kind="agent")["count"] == 0


def test_list_instances_filters_apply_in_store_directly(seeded_instances):
    store = seeded_instances.store
    assert [i.instance_id for i in store.list_instances(owner_id="blue")] == ["inst-c1", "inst-c2"]
    assert [i.instance_id for i in store.list_instances(owner_kind="agent", shape="instance")] == ["inst-c2"]
    assert store.list_instances(shape="bogus") == []


# ---------------------------------------------------------------------------
# Instances: pagination under a filter
# ---------------------------------------------------------------------------

def test_list_instances_pagination_under_filter(seeded_instances):
    full = _instance_ids(seeded_instances.list_instances(owner_kind="worker", limit=100))
    assert full == ["inst-a1", "inst-a2", "inst-b1"]
    pages = []
    offset = 0
    while True:
        page = seeded_instances.list_instances(owner_kind="worker", limit=2, offset=offset)
        pages.extend(_instance_ids(page))
        if page["count"] < 2:
            break
        offset += 2
    assert pages == full
    assert len(pages) == len(set(pages))  # no duplicates; equality above rules out gaps


# ---------------------------------------------------------------------------
# Unknown filter values: empty, not an error
# ---------------------------------------------------------------------------

def test_list_instances_unknown_filter_values_return_empty(seeded_instances):
    assert seeded_instances.list_instances(owner_id="bogus") == {"instances": [], "count": 0}
    assert seeded_instances.list_instances(owner_kind="bogus") == {"instances": [], "count": 0}
    assert seeded_instances.list_instances(shape="bogus") == {"instances": [], "count": 0}


def test_list_bands_unknown_owner_kind_returns_empty(seeded_bands):
    assert seeded_bands.list_bands(owner_kind="bogus") == {"bands": [], "count": 0}


# ---------------------------------------------------------------------------
# Bands: owner_kind filter
# ---------------------------------------------------------------------------

def test_list_bands_owner_kind_filter_matches_unfiltered_subset(seeded_bands):
    unfiltered = seeded_bands.list_bands(limit=100)["bands"]
    for owner_kind in ("system", "worker", "agent"):
        expected = [b["band_id"] for b in unfiltered if b["owner_kind"] == owner_kind]
        assert _band_ids(seeded_bands.list_bands(owner_kind=owner_kind)) == expected
        assert expected


def test_list_bands_owner_kind_combines_with_other_filters(seeded_bands):
    assert _band_ids(seeded_bands.list_bands(owner_kind="worker", owner_id="worker-3")) == ["band-2"]
    assert seeded_bands.list_bands(owner_kind="agent", owner_id="worker-3")["count"] == 0


def test_list_bands_filters_apply_in_store_directly(seeded_bands):
    store = seeded_bands.store
    assert [b.band_id for b in store.list_bands(owner_kind="worker")] == ["band-2", "band-4"]
    assert store.list_bands(owner_kind="bogus") == []


def test_list_bands_pagination_under_owner_kind_filter(seeded_bands):
    full = _band_ids(seeded_bands.list_bands(owner_kind="worker", limit=100))
    assert full == ["band-2", "band-4"]
    pages = []
    for offset in (0, 1, 2):
        pages.extend(_band_ids(seeded_bands.list_bands(owner_kind="worker", limit=1, offset=offset)))
    assert pages == full
    assert len(pages) == len(set(pages))


# ---------------------------------------------------------------------------
# Unfiltered behaviour and tool schema
# ---------------------------------------------------------------------------

def test_unfiltered_lists_unchanged(seeded_instances, seeded_bands):
    # the same service serves both fixtures' rows: 5 instances, 4 bands
    assert seeded_instances.list_instances()["count"] == 5
    assert seeded_instances.list_bands()["count"] == 4


def test_tool_registry_exposes_new_filter_params():
    pytest.importorskip("mcp")
    import anyio
    import json

    from kalends.mcp_server import build_server

    server = build_server(KalendsService(InMemoryStore(), now_fn=lambda: T0))

    async def run():
        tools = await server.list_tools()
        by_name = {t.name: t for t in tools}
        instance_props = by_name["kalends_list_instances"].input_schema["properties"]
        for name in ("owner_id", "owner_kind", "shape"):
            assert name in instance_props
        band_props = by_name["kalends_list_bands"].input_schema["properties"]
        assert "owner_kind" in band_props
        result = await server.call_tool(
            "kalends_list_instances", {"owner_id": "bogus"})
        assert json.loads(result.content[0].text) == {"instances": [], "count": 0}
        result = await server.call_tool(
            "kalends_list_bands", {"owner_kind": "bogus"})
        assert json.loads(result.content[0].text) == {"bands": [], "count": 0}

    anyio.run(run)
