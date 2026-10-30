"""Phase 2: actuals adapters and joins, bands adapters, the holds
API, series anchors, series resource claims, and the table view's
default display window."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kalends.adapters import (
    CompletionPointsAdapter,
    FireLogAdapter,
    ServiceStatusAdapter,
    WorkClaimsAdapter,
    YamlCalendarAdapter,
)
from kalends.cli import _default_window, main
from kalends.holds import HoldError, HoldStore
from kalends.model import Instance, Series
from kalends.projector import Projection, project
from kalends.tableview import TableFilters, render_table

NOW = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def daily_series(**kw) -> Series:
    base = dict(
        series_id="test:daily",
        source_id="test",
        source_native_id="daily",
        title="Daily job",
        owner_kind="system",
        plane="host-a",
        cadence_dialect="cron",
        cadence_expr="0 3 * * *",
        rrule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0",
    )
    base.update(kw)
    return Series(**base)


# --- fire log adapter + join ---------------------------------------------------

FIRELOG_CONFIG = {
    "source_id": "fire-log",
    "plane": "platform",
    "series_source_id": "test",
    "columns": {
        "job": "job_type",
        "run_id": "run_id",
        "fired_at": "fired_at",
        "would_fire_at": "would_fire_at",
        "observed_only": "observed_only",
        "outcome": "outcome",
    },
}


def test_firelog_adapter_mapping():
    rows = [
        {"job_type": "daily", "run_id": "r1", "fired_at": "2026-10-29T03:04:00Z",
         "would_fire_at": "2026-10-29T03:00:00Z", "observed_only": True,
         "outcome": "completed"},
        {"job_type": "daily", "run_id": "r2", "fired_at": "2026-10-28T03:00:00Z",
         "would_fire_at": "2026-10-28T03:00:00Z", "observed_only": False,
         "outcome": "error"},
        {"job_type": "daily", "run_id": "r3", "fired_at": None,
         "would_fire_at": None, "observed_only": False, "outcome": "completed"},
    ]
    result = FireLogAdapter(FIRELOG_CONFIG, rows=rows).collect()
    assert len(result.actuals) == 2  # the null firing is skipped
    first = result.actuals[0]
    assert first.series_id == "test:daily"
    assert first.kind == "firing"
    assert first.observed_only is True
    assert result.actuals[1].status == "failed"  # "error" reads as failed
    assert result.stats["observed"] == 1
    assert result.source.last_source_at == utc("2026-10-29T03:04:00Z")


def test_firelog_join_sets_actuals_and_variance():
    rows = [
        {"job_type": "daily", "run_id": "r1", "fired_at": "2026-10-29T03:04:00Z",
         "would_fire_at": "2026-10-29T03:00:00Z", "observed_only": False,
         "outcome": "completed"},
    ]
    actuals = FireLogAdapter(FIRELOG_CONFIG, rows=rows).collect().actuals
    out = project([daily_series()], actuals=actuals, now=NOW)
    hit = [i for i in out.instances if i.planned_start == utc("2026-10-29T03:00:00Z")]
    assert len(hit) == 1
    assert hit[0].status == "completed"
    assert hit[0].actual_start == utc("2026-10-29T03:04:00Z")
    assert hit[0].extensions["variance_sec"] == 240
    assert hit[0].extensions["would_fire_at"] == "2026-10-29T03:00:00+00:00"


def test_firelog_join_observed_only_stays_observed():
    rows = [
        {"job_type": "daily", "run_id": "r9", "fired_at": "2026-10-29T03:00:30Z",
         "would_fire_at": "2026-10-29T03:00:00Z", "observed_only": "true",
         "outcome": "completed"},
    ]
    actuals = FireLogAdapter(FIRELOG_CONFIG, rows=rows).collect().actuals
    out = project([daily_series()], actuals=actuals, now=NOW)
    hit = [i for i in out.instances if i.planned_start == utc("2026-10-29T03:00:00Z")][0]
    assert hit.extensions.get("observed") is True
    assert hit.extensions["variance_sec"] == 30


def test_planned_instance_that_never_fired_stays_scheduled():
    out = project([daily_series()], now=NOW)
    past = [i for i in out.instances if i.planned_start < NOW]
    assert past  # the horizon includes past occurrences
    assert all(i.status == "scheduled" for i in past)
    assert all(i.actual_start is None for i in past)


# --- completion points -----------------------------------------------------------

COMPLETIONS_CONFIG = {
    "source_id": "completions",
    "plane": "platform",
    "series_source_id": "test",
    "series_names": {"agent-a": "daily"},
    "columns": {
        "agent": "agent_id",
        "completed_at": "completed_at",
        "run_id": "run_id",
        "is_empty": "is_empty",
    },
}


def test_completion_point_matches_without_inventing_duration():
    rows = [
        {"agent_id": "agent-a", "completed_at": "2026-10-29T03:05:00Z",
         "run_id": "c1", "is_empty": True},
    ]
    actuals = CompletionPointsAdapter(COMPLETIONS_CONFIG, rows=rows).collect().actuals
    assert actuals[0].kind == "point" and actuals[0].is_empty is True
    out = project([daily_series()], actuals=actuals, now=NOW)
    hit = [i for i in out.instances if i.planned_start == utc("2026-10-29T03:00:00Z")][0]
    assert hit.status == "completed"
    assert hit.actual_start == utc("2026-10-29T03:05:00Z")
    assert hit.actual_end is None  # a cycle has no recorded duration
    assert hit.extensions.get("point") is True
    assert hit.extensions.get("empty") is True


def test_unmatched_completion_point_stands_alone():
    rows = [
        {"agent_id": "ghost", "completed_at": "2026-10-29T10:00:00Z",
         "run_id": "c9", "is_empty": False},
    ]
    actuals = CompletionPointsAdapter(COMPLETIONS_CONFIG, rows=rows).collect().actuals
    out = project([], actuals=actuals, now=NOW)
    assert len(out.instances) == 1
    point = out.instances[0]
    assert point.title == "ghost"  # the adapter's label, not a series id
    assert point.planned_start is None
    assert point.actual_start == utc("2026-10-29T10:00:00Z")
    assert point.actual_end is None


# --- service bands -----------------------------------------------------------------

SERVICES_CONFIG = {
    "source_id": "service-status",
    "plane": "platform",
    "owner_kind": "system",
    "columns": {
        "service": "service",
        "status": "current_status",
        "status_since": "status_since",
        "last_checked_at": "last_checked_at",
        "plane": "host",
    },
    "status_map": {"degraded": "blocked", "down": "blocked"},
}


def test_service_status_bands():
    rows = [
        {"service": "relay-api", "current_status": "degraded",
         "status_since": "2026-10-29T22:10:00Z",
         "last_checked_at": "2026-10-30T00:00:00Z", "host": "host-b"},
        {"service": "harbour-db", "current_status": "ok",
         "status_since": "2026-10-20T01:15:00Z",
         "last_checked_at": "2026-10-30T00:00:00Z", "host": "host-a"},
    ]
    result = ServiceStatusAdapter(SERVICES_CONFIG, rows=rows).collect()
    assert len(result.bands) == 2
    relay = result.bands[0]
    assert relay.started_at == utc("2026-10-29T22:10:00Z")
    assert relay.last_heartbeat_at == utc("2026-10-30T00:00:00Z")
    assert relay.status == "blocked"  # mapped
    assert relay.extensions["status_value"] == "degraded"  # source word kept
    assert relay.plane == "host-b"  # per-row plane column
    assert relay.ended_at is None and relay.end_reason is None
    assert result.stats["statuses"] == {"degraded": 1, "ok": 1}


# --- claim bands ---------------------------------------------------------------------

CLAIMS_CONFIG = {
    "source_id": "work-claims",
    "plane": "host-a",
    "owner_kind": "worker",
    "columns": {
        "id": "claim_id",
        "item": "item_ref",
        "status": "claim_status",
        "runner": "runner_id",
        "runner_kind": "runner_kind",
        "claimed_at": "claimed_at",
        "heartbeat_at": "heartbeat_at",
        "lease_expires_at": "lease_expires_at",
        "released_at": "released_at",
        "outcome": "outcome",
    },
    "item_titles": {"WL-1": "Chart room refit"},
}


def _claim(**kw):
    base = {
        "claim_id": "cl-1", "item_ref": "WL-1", "claim_status": "claimed",
        "runner_id": "worker-3", "runner_kind": "coder",
        "claimed_at": "2026-10-30T04:50:00Z",
        "heartbeat_at": "2026-10-30T05:55:00Z",
        "lease_expires_at": "2026-10-30T06:20:00Z",
        "released_at": None, "outcome": None,
    }
    base.update(kw)
    return base


def test_claim_band_active_with_resolved_title():
    result = WorkClaimsAdapter(
        CLAIMS_CONFIG, rows=[_claim()], now=utc("2026-10-30T06:00:00Z")
    ).collect()
    band = result.bands[0]
    assert band.title == "Chart room refit"  # item_titles resolved it
    assert band.status == "active" and band.end_reason is None
    assert band.owner_id == "worker-3"
    assert band.resource_claims == {"runner_kind": "coder"}
    assert band.lease_expires_at == utc("2026-10-30T06:20:00Z")


def test_claim_band_completed_end_reason():
    row = _claim(
        claim_id="cl-2", claim_status="completed",
        released_at="2026-10-30T03:20:00Z", outcome="shipped",
    )
    band = WorkClaimsAdapter(
        CLAIMS_CONFIG, rows=[row], now=utc("2026-10-30T06:00:00Z")
    ).collect().bands[0]
    assert band.status == "done"
    assert band.end_reason == "completed"
    assert band.ended_at == utc("2026-10-30T03:20:00Z")
    assert band.extensions["outcome"] == "shipped"


def test_claim_band_lease_expired_without_release():
    row = _claim(
        claim_id="cl-3", lease_expires_at="2026-10-30T04:30:00Z",
        heartbeat_at="2026-10-30T04:28:00Z",
    )
    result = WorkClaimsAdapter(
        CLAIMS_CONFIG, rows=[row], now=utc("2026-10-30T06:00:00Z")
    ).collect()
    band = result.bands[0]
    assert band.status == "done"
    assert band.end_reason == "expired"
    assert band.ended_at == utc("2026-10-30T04:30:00Z")  # the lease time
    assert result.stats["end_reasons"] == {"expired": 1}


def test_claim_released_at_closes_a_blocked_claim():
    row = _claim(
        claim_id="cl-4", claim_status="blocked",
        released_at="2026-10-30T05:00:00Z",
        lease_expires_at="2026-10-30T09:00:00Z",
    )
    band = WorkClaimsAdapter(
        CLAIMS_CONFIG, rows=[row], now=utc("2026-10-30T06:00:00Z")
    ).collect().bands[0]
    assert band.status == "done"
    assert band.end_reason == "released"
    assert band.ended_at == utc("2026-10-30T05:00:00Z")


def test_claim_title_falls_back_to_ref():
    row = _claim(item_ref="WL-unknown")
    band = WorkClaimsAdapter(
        CLAIMS_CONFIG, rows=[row], now=utc("2026-10-30T06:00:00Z")
    ).collect().bands[0]
    assert band.title == "WL-unknown"


# --- anchors ---------------------------------------------------------------------------

def test_anchor_phase_locks_interval_expansion():
    anchored = Series(
        series_id="test:six", source_id="test", source_native_id="six",
        title="Six-hourly", cadence_dialect="interval",
        cadence_expr="every 6h", rrule="FREQ=HOURLY;INTERVAL=6",
        anchor_at=utc("2026-10-29T06:00:00Z"),
        source_updated_at=utc("2026-10-29T06:45:00Z"),
        plane="host-a",
    )
    out = project([anchored], now=NOW)
    assert out.instances
    assert all(i.planned_start.minute == 0 for i in out.instances)
    assert "phase_approximate" not in anchored.extensions


def test_unanchored_interval_expansion_is_marked_approximate():
    unanchored = Series(
        series_id="test:six", source_id="test", source_native_id="six",
        title="Six-hourly", cadence_dialect="interval",
        cadence_expr="every 6h", rrule="FREQ=HOURLY;INTERVAL=6",
        source_updated_at=utc("2026-10-29T06:45:00Z"),
        plane="host-a",
    )
    out = project([unanchored], now=NOW)
    assert out.instances
    assert all(i.planned_start.minute == 45 for i in out.instances)
    assert unanchored.extensions.get("phase_approximate") is True


def test_anchor_does_not_truncate_the_past_horizon():
    series = Series(
        series_id="test:hourly", source_id="test", source_native_id="hourly",
        title="Hourly", cadence_dialect="interval",
        cadence_expr="every 1h", rrule="FREQ=HOURLY;INTERVAL=1",
        anchor_at=utc("2026-10-30T11:00:00Z"),
        plane="host-a",
    )
    out = project([series], now=utc("2026-10-30T12:00:00Z"))
    starts = [i.planned_start for i in out.instances]
    assert min(starts) <= utc("2026-10-24T00:00:00Z")  # stepped back, phase kept
    assert all(s.minute == 0 for s in starts)


def test_yaml_weekday_and_time_build_anchor():
    doc = {"rows": [
        {"name": "audit", "owner": "crimson", "cadence": "weekly",
         "weekday": "Friday", "time_of_day": "06:00", "state": "active"},
        {"name": "digest", "owner": "sable", "cadence": "daily",
         "time_of_day": "05:30", "state": "active"},
    ]}
    result = YamlCalendarAdapter(
        doc, source_id="yaml", plane="platform",
        now=utc("2026-10-30T12:00:00Z"),  # a Friday
    ).collect()
    by_name = {s.source_native_id: s for s in result.series}
    assert by_name["audit"].anchor_at == utc("2026-10-30T06:00:00Z")
    # 05:30 today is past at noon, so today's is the anchor.
    assert by_name["digest"].anchor_at == utc("2026-10-30T05:30:00Z")


def test_yaml_anchor_rolls_back_before_todays_time():
    doc = {"rows": [
        {"name": "digest", "owner": "sable", "cadence": "daily",
         "time_of_day": "05:30", "state": "active"},
    ]}
    result = YamlCalendarAdapter(
        doc, source_id="yaml", plane="platform",
        now=utc("2026-10-30T05:00:00Z"),  # before 05:30
    ).collect()
    assert result.series[0].anchor_at == utc("2026-10-29T05:30:00Z")


# --- series resource claims ----------------------------------------------------------------

def test_series_resource_claims_reach_instances():
    series = daily_series(resource_claims={"slot_group": "survey"})
    out = project([series], now=NOW)
    assert out.instances
    assert all(
        i.resource_claims == {"slot_group": "survey"} for i in out.instances
    )


# --- holds -----------------------------------------------------------------------------------

T0 = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> HoldStore:
    return HoldStore(tmp_path / "holds.json")


def test_hold_create_defaults_and_list(tmp_path):
    store = _store(tmp_path)
    band = store.create_hold(
        "Build the chart pack", owner_kind="worker", owner_id="worker-3",
        plane="host-a", now=T0, hold_id="h1",
        resource_claims={"slot_group": "survey"},
    )
    assert band.status == "active"
    assert band.started_at == T0
    assert band.lease_expires_at == T0 + timedelta(hours=1)  # default lease
    assert band.resource_claims == {"slot_group": "survey"}
    listed = store.list_holds(now=T0 + timedelta(minutes=10))
    assert [b.band_id for b in listed] == ["holds:h1"]


def test_hold_fixed_end_is_the_lease(tmp_path):
    store = _store(tmp_path)
    band = store.create_hold(
        "Fixed block", owner_kind="agent", owner_id="crimson",
        starts_at=T0, ends_at=T0 + timedelta(hours=2), now=T0, hold_id="h2",
    )
    assert band.lease_expires_at == T0 + timedelta(hours=2)


def test_hold_heartbeat_renews_lease(tmp_path):
    store = _store(tmp_path)
    store.create_hold("Work", owner_kind="agent", now=T0,
                      lease_seconds=600, hold_id="h3")
    beat_at = T0 + timedelta(minutes=5)
    band = store.heartbeat_hold("h3", now=beat_at)
    assert band.last_heartbeat_at == beat_at
    # No explicit span: the original 600s lease length is reused.
    assert band.lease_expires_at == beat_at + timedelta(seconds=600)
    band = store.heartbeat_hold("h3", now=beat_at, lease_seconds=3600)
    assert band.lease_expires_at == beat_at + timedelta(seconds=3600)


def test_hold_release_closes_with_reason(tmp_path):
    store = _store(tmp_path)
    store.create_hold("Work", owner_kind="agent", now=T0, hold_id="h4")
    band = store.release_hold("h4", now=T0 + timedelta(minutes=30),
                              end_reason="completed")
    assert band.status == "done"
    assert band.end_reason == "completed"
    assert band.ended_at == T0 + timedelta(minutes=30)


def test_hold_expires_without_heartbeat(tmp_path):
    store = _store(tmp_path)
    store.create_hold("Short work", owner_kind="agent", now=T0,
                      lease_seconds=600, hold_id="h5")
    later = T0 + timedelta(seconds=601)
    listed = store.list_holds(now=later)
    assert listed[0].status == "done"
    assert listed[0].end_reason == "expired"
    assert listed[0].ended_at == T0 + timedelta(seconds=600)
    # The closure is persisted: a fresh store over the same file agrees.
    assert _store(tmp_path).list_holds(now=later)[0].end_reason == "expired"
    # And an expired hold cannot be heartbeated back to life.
    with pytest.raises(HoldError):
        store.heartbeat_hold("h5", now=later)


def test_hold_bands_feed_the_projector(tmp_path):
    store = _store(tmp_path)
    store.create_hold("Work", owner_kind="agent", owner_id="crimson",
                      plane="host-a", now=T0, hold_id="h6")
    out = project([], bands=store.bands(now=T0), now=T0)
    assert len(out.bands) == 1
    assert out.bands[0].source_id == "holds"


def test_hold_cli_round_trip(tmp_path, capsys):
    path = str(tmp_path / "cli-holds.json")
    assert main(["hold", "--store", path, "create", "--title", "CLI work",
                 "--owner-kind", "agent", "--owner-id", "azure",
                 "--id", "cli-1"]) == 0
    assert main(["hold", "--store", path, "list"]) == 0
    out = capsys.readouterr().out
    assert "CLI work" in out and "active" in out
    assert main(["hold", "--store", path, "release", "cli-1",
                 "--end-reason", "completed"]) == 0
    assert main(["hold", "--store", path, "list", "--active-only"]) == 0
    assert "(no holds)" in capsys.readouterr().out


# --- table view: window + notes ---------------------------------------------------------------

def test_table_default_window_bounds():
    filters = TableFilters()
    _default_window(filters, NOW)
    assert filters.start_from == NOW - timedelta(hours=24)
    assert filters.start_to == NOW + timedelta(hours=48)


def test_table_window_is_stated_in_header():
    proj = Projection(instances=[
        Instance(instance_id="s:1", source_id="s", source_native_id="1",
                 title="Inside", planned_start=NOW + timedelta(hours=1)),
    ])
    filters = _default_window(TableFilters(), NOW)
    text = render_table(proj, filters, now=NOW)
    assert text.splitlines()[0].startswith("Window (UTC):")
    assert "Inside" in text


def test_table_marks_missed_and_shows_variance():
    missed = Instance(
        instance_id="s:1", source_id="s", source_native_id="1",
        title="Missed job", planned_start=NOW - timedelta(hours=2),
        status="scheduled",
    )
    fired = Instance(
        instance_id="s:2", source_id="s", source_native_id="2",
        title="Fired job", planned_start=NOW - timedelta(hours=3),
        actual_start=NOW - timedelta(hours=3) + timedelta(minutes=4),
        status="completed", extensions={"variance_sec": 240},
    )
    text = render_table(Projection(instances=[missed, fired]), now=NOW)
    missed_line = [ln for ln in text.splitlines() if "Missed job" in ln][0]
    fired_line = [ln for ln in text.splitlines() if "Fired job" in ln][0]
    assert "MISSED" in missed_line
    assert "variance +4m" in fired_line


# --- example fleet fixtures ----------------------------------------------------------------------

def test_example_fleet_phase2_fixtures(examples_dir):
    fire = FireLogAdapter.from_files(
        examples_dir / "firelog-config.yaml", examples_dir / "firelog.json"
    ).collect()
    assert len(fire.actuals) == 5
    assert fire.stats["observed"] == 1

    points = CompletionPointsAdapter.from_files(
        examples_dir / "completions-config.yaml",
        examples_dir / "completions.json",
    ).collect()
    assert len(points.actuals) == 3
    assert points.stats["empty"] == 1
    sable = [a for a in points.actuals if a.extensions["label"] == "sable"][0]
    assert sable.series_id == "patrol-calendar:beacon-health-digest"

    services = ServiceStatusAdapter.from_files(
        examples_dir / "services-config.yaml", examples_dir / "services.json"
    ).collect()
    assert len(services.bands) == 2
    degraded = [b for b in services.bands if b.title == "tide-relay-api"][0]
    assert degraded.status == "blocked"
    assert degraded.extensions["status_value"] == "degraded"

    claims = WorkClaimsAdapter.from_files(
        examples_dir / "claims-config.yaml", examples_dir / "claims.json",
        now=utc("2026-10-30T06:00:00Z"),
    ).collect()
    assert len(claims.bands) == 3
    by_title = {b.title: b for b in claims.bands}
    assert by_title["Chart room refit"].status == "active"
    assert by_title["Harbour light wiring"].end_reason == "completed"
    assert by_title["Reef marker survey"].end_reason == "expired"


# --- schema ----------------------------------------------------------------------------------------

def test_schema_002_and_001_carry_anchor_and_claims():
    root = Path(__file__).resolve().parents[1]
    sql002 = (root / "schema" / "002_anchors_and_claims.sql").read_text()
    assert "anchor_at" in sql002 and "resource_claims" in sql002
    assert "is_empty" in sql002
    sql001 = (root / "schema" / "001_kalends.sql").read_text()
    assert "anchor_at" in sql001 and "resource_claims" in sql001
