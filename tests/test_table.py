from datetime import datetime, timedelta, timezone

from kalends.model import Instance, Series, Source
from kalends.projector import Projection
from kalends.tableview import TableFilters, render_table

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _projection() -> Projection:
    fresh = Source(
        source_id="fresh-src",
        adapter="cron",
        plane="host-a",
        sync_interval_sec=3600,
        last_synced_at=NOW - timedelta(minutes=5),
        last_source_at=NOW - timedelta(minutes=5),
    )
    stale = Source(
        source_id="stale-src",
        adapter="systemd",
        plane="host-b",
        sync_interval_sec=3600,
        last_synced_at=NOW - timedelta(days=3),
        last_source_at=NOW - timedelta(days=3),
    )
    inst_a = Instance(
        instance_id="fresh-src:1",
        source_id="fresh-src",
        source_native_id="1",
        title="Alpha job",
        owner_kind="agent",
        owner_id="crimson",
        plane="host-a",
        planned_start=NOW + timedelta(hours=1),
        status="scheduled",
    )
    inst_b = Instance(
        instance_id="stale-src:1",
        source_id="stale-src",
        source_native_id="1",
        title="Beta job",
        owner_kind="system",
        plane="host-b",
        planned_start=NOW + timedelta(hours=2),
        status="scheduled",
    )
    series = Series(
        series_id="fresh-src:daily",
        source_id="fresh-src",
        source_native_id="daily",
        title="Daily definition",
        owner_kind="agent",
        owner_id="crimson",
        plane="host-a",
        cadence_dialect="cron",
        cadence_expr="0 3 * * *",
        rrule="FREQ=DAILY",
    )
    return Projection(
        sources=[fresh, stale], series=[series], instances=[inst_a, inst_b]
    )


def test_stale_source_is_marked_in_freshness_footer():
    text = render_table(_projection(), now=NOW)
    footer = text.split("Sources (freshness", 1)[1]
    stale_line = [ln for ln in footer.splitlines() if "stale-src" in ln][0]
    fresh_line = [ln for ln in footer.splitlines() if "fresh-src" in ln][0]
    assert "STALE" in stale_line
    assert "STALE" not in fresh_line
    assert "3.0d" in stale_line  # the age is shown, not just a flag


def test_table_renders_rows_aligned():
    text = render_table(_projection(), now=NOW)
    assert "Alpha job" in text and "Beta job" in text
    assert "Daily definition" in text  # series rows are part of the view
    header = text.splitlines()[0]
    assert header.startswith("START (UTC)")


def test_plane_filter():
    text = render_table(
        _projection(), TableFilters(plane="host-a"), now=NOW
    )
    assert "Alpha job" in text
    assert "Beta job" not in text


def test_shape_filter_hides_series():
    text = render_table(
        _projection(), TableFilters(shape="instance"), now=NOW
    )
    assert "Alpha job" in text
    assert "Daily definition" not in text
