from datetime import datetime, timezone

from kalends.model import Actual, Instance, Series
from kalends.projector import project

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


def test_recurring_expansion_count_frozen_now():
    out = project([daily_series()], now=NOW)
    # Horizon Oct 23 .. Nov 13; 03:00 occurrences Oct 23 .. Nov 12.
    assert len(out.instances) == 21
    assert all(i.status == "scheduled" for i in out.instances)


def test_self_chain_exactly_one_instance_from_queue():
    chain = Series(
        series_id="test:chain",
        source_id="test",
        source_native_id="chain",
        title="Chain job",
        owner_kind="worker",
        plane="platform",
        cadence_dialect="self-chain",
        cadence_expr="idle_delay_seconds=900",
        rrule=None,
    )
    queued = Instance(
        instance_id="test:queue:1",
        series_id="test:chain",
        source_id="test",
        source_native_id="1",
        title="Chain job",
        planned_start=utc("2026-10-30T06:15:00Z"),
        status="scheduled",
    )
    later = Instance(
        instance_id="test:queue:2",
        series_id="test:chain",
        source_id="test",
        source_native_id="2",
        title="Chain job",
        planned_start=utc("2026-10-30T09:00:00Z"),
        status="scheduled",
    )
    out = project([chain], provided_instances=[later, queued], now=NOW)
    assert len(out.instances) == 1
    assert out.instances[0].instance_id == "test:queue:1"
    assert out.instances[0].planned_start == utc("2026-10-30T06:15:00Z")


def test_self_chain_without_queue_row_projects_nothing():
    chain = Series(
        series_id="test:chain",
        source_id="test",
        source_native_id="chain",
        title="Chain job",
        cadence_dialect="self-chain",
        rrule=None,
    )
    out = project([chain], now=NOW)
    assert out.instances == []


def test_one_off_passes_through_and_horizon_filters():
    inside = Instance(
        instance_id="test:once:1",
        source_id="test",
        source_native_id="once-1",
        title="Certificate renewal",
        shape="one-off",
        planned_start=utc("2026-11-05T12:00:00Z"),
        status="scheduled",
    )
    outside = Instance(
        instance_id="test:once:2",
        source_id="test",
        source_native_id="once-2",
        title="Far future thing",
        shape="one-off",
        planned_start=utc("2027-01-01T00:00:00Z"),
        status="scheduled",
    )
    out = project([], provided_instances=[inside, outside], now=NOW)
    assert [i.instance_id for i in out.instances] == ["test:once:1"]


def test_retired_and_dormant_series_project_nothing():
    retired = daily_series(series_id="test:retired", lifecycle_state="retired")
    dormant = daily_series(series_id="test:dormant", lifecycle_state="dormant")
    disabled = daily_series(series_id="test:disabled", enabled=False)
    out = project([retired, dormant, disabled], now=NOW)
    assert out.instances == []
    assert len(out.series) == 3  # definitions stay visible


def test_provided_instance_supersedes_expanded_occurrence():
    queued = Instance(
        instance_id="test:queue:99",
        series_id="test:daily",
        source_id="test",
        source_native_id="99",
        title="Daily job",
        planned_start=utc("2026-10-31T03:00:00Z"),
        status="scheduled",
    )
    out = project([daily_series()], provided_instances=[queued], now=NOW)
    assert len(out.instances) == 21  # replaced, not duplicated
    ids = [i.instance_id for i in out.instances]
    assert "test:queue:99" in ids


def test_actual_joins_planned_instance():
    actual = Actual(
        source_id="test",
        source_native_id="run-1",
        series_id="test:daily",
        fired_at=utc("2026-10-29T03:01:00Z"),
        would_fire_at=utc("2026-10-29T03:00:00Z"),
        status="completed",
    )
    out = project([daily_series()], actuals=[actual], now=NOW)
    hit = [i for i in out.instances if i.planned_start == utc("2026-10-29T03:00:00Z")]
    assert len(hit) == 1
    assert hit[0].status == "completed"
    assert hit[0].actual_start == utc("2026-10-29T03:01:00Z")


def test_unmatched_actual_becomes_standalone_instance():
    actual = Actual(
        source_id="test",
        source_native_id="run-x",
        series_id="test:unknown",
        fired_at=utc("2026-10-29T10:00:00Z"),
        status="failed",
    )
    out = project([], actuals=[actual], now=NOW)
    assert len(out.instances) == 1
    assert out.instances[0].status == "failed"
    assert out.instances[0].actual_start == utc("2026-10-29T10:00:00Z")
