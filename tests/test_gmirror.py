"""Tests for the Google Calendar mirror (kalends.gmirror)."""

from datetime import datetime, timedelta, timezone

import pytest

from kalends.cli import main as cli_main
from kalends.gmirror import (
    MirrorItem,
    eligible_items,
    event_body,
    reconcile,
    stable_key,
)
from kalends.model import Band, Instance

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
WS = NOW - timedelta(days=1)
WE = NOW + timedelta(days=7)


def _band(band_id="b-1", owner_id="crimson", **kw):
    defaults = dict(
        band_id=band_id,
        source_id="src",
        source_native_id=band_id,
        title=f"Band {band_id}",
        owner_kind="agent",
        owner_id=owner_id,
        plane="host-a",
        started_at=NOW - timedelta(hours=2),
        status="active",
    )
    defaults.update(kw)
    return Band(**defaults)


def _instance(instance_id="i-1", owner_id="crimson", **kw):
    defaults = dict(
        instance_id=instance_id,
        source_id="src",
        source_native_id=instance_id,
        title=f"Instance {instance_id}",
        owner_kind="agent",
        owner_id=owner_id,
        plane="host-a",
        shape="one-off",
        planned_start=NOW + timedelta(hours=3),
        planned_end=NOW + timedelta(hours=4),
        status="scheduled",
    )
    defaults.update(kw)
    return Instance(**defaults)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_recurring_instance_excluded_even_for_eligible_owner():
    recurring = _instance("i-rec", shape="instance")
    items = eligible_items([], [recurring], {"crimson"}, WS, WE)
    assert items == []


def test_one_off_for_other_owner_excluded():
    other = _instance("i-other", owner_id="azure")
    items = eligible_items([], [other], {"crimson"}, WS, WE)
    assert items == []


def test_one_off_eligible_and_band_eligible():
    items = eligible_items(
        [_band("b-1")], [_instance("i-1")], {"crimson"}, WS, WE
    )
    keys = {item.key for item in items}
    assert keys == {"kalends:band:b-1", "kalends:oneoff:i-1"}
    by_key = {item.key: item for item in items}
    assert by_key["kalends:band:b-1"].source_kind == "band"
    assert by_key["kalends:oneoff:i-1"].source_kind == "one-off"
    assert by_key["kalends:oneoff:i-1"].owner_id == "crimson"


def test_band_for_other_owner_excluded():
    items = eligible_items([_band(owner_id="azure")], [], {"crimson"}, WS, WE)
    assert items == []


def test_band_overlap_edges():
    # Ends exactly at window start: no overlap.
    ends_at_ws = _band(
        "b-edge-1",
        started_at=WS - timedelta(hours=2),
        ended_at=WS,
    )
    # Starts exactly at window end: no overlap.
    starts_at_we = _band("b-edge-2", started_at=WE, ended_at=WE + timedelta(hours=1))
    # Straddles window start: overlaps.
    straddles = _band(
        "b-edge-3",
        started_at=WS - timedelta(hours=1),
        ended_at=WS + timedelta(hours=1),
    )
    # Entirely before the window: no overlap.
    before = _band(
        "b-edge-4",
        started_at=WS - timedelta(days=3),
        ended_at=WS - timedelta(days=2),
    )
    items = eligible_items(
        [ends_at_ws, starts_at_we, straddles, before], [], {"crimson"}, WS, WE
    )
    assert [item.key for item in items] == ["kalends:band:b-edge-3"]


def test_band_with_no_end_uses_lease_then_fallback():
    lease = NOW + timedelta(hours=5)
    with_lease = _band("b-lease", ended_at=None, lease_expires_at=lease)
    no_end = _band(
        "b-open", ended_at=None, lease_expires_at=None,
        started_at=NOW,
    )
    items = eligible_items([with_lease, no_end], [], {"crimson"}, WS, WE)
    by_key = {item.key: item for item in items}
    assert by_key["kalends:band:b-lease"].end == lease
    assert by_key["kalends:band:b-open"].end == NOW + timedelta(minutes=30)


def test_band_ended_at_takes_precedence_over_lease():
    ended = NOW + timedelta(hours=1)
    band = _band(
        "b-ended", ended_at=ended,
        lease_expires_at=NOW + timedelta(hours=9),
    )
    items = eligible_items([band], [], {"crimson"}, WS, WE)
    assert items[0].end == ended


def test_one_off_window_is_half_open():
    at_start = _instance("i-start", planned_start=WS, planned_end=WS + timedelta(hours=1))
    at_end = _instance("i-end", planned_start=WE, planned_end=WE + timedelta(hours=1))
    just_before = _instance(
        "i-before", planned_start=WS - timedelta(minutes=1),
        planned_end=WS + timedelta(hours=1),
    )
    items = eligible_items([], [at_start, at_end, just_before], {"crimson"}, WS, WE)
    assert [item.key for item in items] == ["kalends:oneoff:i-start"]


def test_one_off_without_planned_end_gets_30_minutes():
    inst = _instance("i-noend", planned_end=None)
    items = eligible_items([], [inst], {"crimson"}, WS, WE)
    assert items[0].end == items[0].start + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Stable keys
# ---------------------------------------------------------------------------


def test_stable_key_is_deterministic_and_kind_distinct():
    assert stable_key("band", "42") == stable_key("band", "42")
    assert stable_key("band", "42") == "kalends:band:42"
    assert stable_key("one-off", "42") == "kalends:oneoff:42"
    assert stable_key("band", "42") != stable_key("one-off", "42")
    with pytest.raises(ValueError):
        stable_key("window", "42")


# ---------------------------------------------------------------------------
# Event bodies
# ---------------------------------------------------------------------------


def _item(**kw):
    defaults = dict(
        key="kalends:band:b-1",
        owner_id="crimson",
        title="Survey service",
        start=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
        end=datetime(2026, 11, 2, 13, 0, tzinfo=UTC),
        description="Band: Survey service",
        source_kind="band",
    )
    defaults.update(kw)
    return MirrorItem(**defaults)


def test_event_body_carries_private_key_and_no_attendees():
    body = event_body(_item(), "UTC")
    assert body["summary"] == "Survey service"
    private = body["extendedProperties"]["private"]
    assert private["kalends_key"] == "kalends:band:b-1"
    assert private["kalends_owner"] == "crimson"
    assert private["kalends_kind"] == "band"
    assert "attendees" not in body
    assert "do not flow back" in body["description"]


def test_event_body_converts_to_named_timezone_with_offset():
    body = event_body(_item(), "America/New_York")
    # 12:00 UTC in early November is 07:00 EST (UTC-5).
    assert body["start"] == {
        "dateTime": "2026-11-02T07:00:00-05:00",
        "timeZone": "America/New_York",
    }
    assert body["end"]["dateTime"] == "2026-11-02T08:00:00-05:00"
    assert body["end"]["timeZone"] == "America/New_York"


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


class FakeClient:
    """Records every call; serves no data of its own."""

    def __init__(self):
        self.calls = []
        self.inserted = []
        self.patched = []
        self.deleted = []

    def list_events(self, calendar_id, time_min, time_max):
        self.calls.append(("list", calendar_id, time_min, time_max))
        return []

    def insert_event(self, calendar_id, body):
        self.calls.append(("insert", calendar_id, body))
        self.inserted.append(body)
        return {"id": "new-1", **body}

    def patch_event(self, calendar_id, event_id, body):
        self.calls.append(("patch", calendar_id, event_id, body))
        self.patched.append((event_id, body))
        return {"id": event_id, **body}

    def delete_event(self, calendar_id, event_id):
        self.calls.append(("delete", calendar_id, event_id))
        self.deleted.append(event_id)

    @property
    def write_calls(self):
        return [c for c in self.calls if c[0] in ("insert", "patch", "delete")]


def _existing(item, event_id="evt-1", **overrides):
    body = event_body(item, "UTC")
    event = {"id": event_id, **body}
    event.update(overrides)
    return event


def test_reconcile_creates_missing():
    client = FakeClient()
    result = reconcile([_item()], [], client, "cal-1")
    assert result.created == 1
    assert result.updated == 0 and result.deleted == 0 and result.unchanged == 0
    assert len(client.inserted) == 1
    assert client.inserted[0]["extendedProperties"]["private"]["kalends_key"] == "kalends:band:b-1"
    assert result.actions[0].action == "create"


def test_reconcile_unchanged_when_matching():
    item = _item()
    client = FakeClient()
    result = reconcile([item], [_existing(item)], client, "cal-1")
    assert result.unchanged == 1
    assert client.write_calls == []


def test_reconcile_same_instant_other_offset_is_unchanged():
    item = _item()
    existing = _existing(item)
    # Same instant as 12:00 UTC, written at +02:00.
    existing["start"] = {"dateTime": "2026-11-02T14:00:00+02:00", "timeZone": "UTC"}
    existing["end"] = {"dateTime": "2026-11-02T15:00:00+02:00", "timeZone": "UTC"}
    client = FakeClient()
    result = reconcile([item], [existing], client, "cal-1")
    assert result.unchanged == 1
    assert client.write_calls == []


def test_reconcile_updates_when_summary_or_times_differ():
    item = _item()
    by_title = _existing(item, summary="Old title")
    by_time = _existing(
        item, event_id="evt-2",
        start={"dateTime": "2026-11-02T11:00:00+00:00", "timeZone": "UTC"},
    )
    client = FakeClient()
    result = reconcile([item], [by_title], client, "cal-1")
    assert result.updated == 1 and len(client.patched) == 1
    client2 = FakeClient()
    result2 = reconcile([item], [by_time], client2, "cal-1")
    assert result2.updated == 1 and len(client2.patched) == 1


def test_reconcile_deletes_orphans_and_ignores_unkeyed():
    orphan_item = _item(key="kalends:band:b-gone", title="Gone service")
    orphan = _existing(orphan_item, event_id="evt-orphan")
    unkeyed = {
        "id": "evt-personal",
        "summary": "Lunch with a friend",
        "start": {"dateTime": "2026-11-02T12:00:00+00:00"},
        "end": {"dateTime": "2026-11-02T13:00:00+00:00"},
    }
    client = FakeClient()
    result = reconcile([], [orphan, unkeyed], client, "cal-1")
    assert result.deleted == 1
    assert client.deleted == ["evt-orphan"]


def test_reconcile_dry_run_makes_zero_write_calls():
    item = _item()
    stale = _existing(_item(key="kalends:band:b-old", title="Old"), event_id="evt-old")
    drifted = _existing(item, event_id="evt-drift", summary="Drifted")
    client = FakeClient()
    result = reconcile(
        [item, _item(key="kalends:oneoff:i-9", title="New one-off",
                      source_kind="one-off")],
        [stale, drifted],
        client,
        "cal-1",
        dry_run=True,
    )
    assert result.created == 1
    assert result.updated == 1
    assert result.deleted == 1
    assert client.write_calls == []
    assert client.calls == []


def test_reconcile_dry_run_allows_no_client():
    result = reconcile([_item()], [], None, "cal-1", dry_run=True)
    assert result.created == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_gmirror_dry_run_prints_desired_set(capsys):
    rc = cli_main(["gmirror", "--dry-run", "--owners", "crimson"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out.lower()
    assert "desired" in out.lower()


def test_cli_gmirror_without_client_exits_nonzero(capsys, monkeypatch):
    monkeypatch.delenv("KALENDS_GMIRROR_CLIENT_FACTORY", raising=False)
    rc = cli_main(["gmirror", "--owners", "crimson"])
    captured = capsys.readouterr()
    assert rc != 0
    text = captured.out + captured.err
    assert "MIRROR" in text or "client" in text.lower()
