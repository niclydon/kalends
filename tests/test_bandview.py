import json
from datetime import datetime, timedelta, timezone

import pytest

from kalends.bandview import (
    BandFilters,
    HourFilters,
    TimelineData,
    data_from_store,
    render_band_view,
    render_hour,
)
from kalends.model import Band, Instance, Source
from kalends.projector import Projection
from kalends.store import InMemoryStore

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _data() -> TimelineData:
    source = Source(
        source_id="fresh-src", adapter="cron", plane="host-a",
        sync_interval_sec=3600,
        last_synced_at=NOW - timedelta(minutes=5),
        last_source_at=NOW - timedelta(minutes=5),
    )
    long_band = Band(
        band_id="b-long", source_id="fresh-src", source_native_id="long",
        title="Long service", owner_kind="agent", owner_id="crimson",
        plane="platform",
        started_at=NOW - timedelta(days=2),
        ended_at=NOW + timedelta(days=3), status="active",
    )
    azure_band = Band(
        band_id="b-azure", source_id="fresh-src", source_native_id="svc",
        title="Relay service", owner_kind="agent", owner_id="azure",
        plane="host-b", started_at=NOW - timedelta(hours=3),
        status="active",
    )
    ended_band = Band(
        band_id="b-done", source_id="fresh-src", source_native_id="old",
        title="Finished service", owner_kind="agent", owner_id="crimson",
        plane="platform", started_at=NOW - timedelta(days=5),
        ended_at=NOW - timedelta(days=4), status="done",
    )
    upcoming = Instance(
        instance_id="i-1", source_id="fresh-src", source_native_id="1",
        title="Harbour audit run", owner_kind="agent", owner_id="crimson",
        plane="platform", planned_start=NOW + timedelta(hours=2),
        planned_end=NOW + timedelta(hours=3), status="scheduled",
    )
    done = Instance(
        instance_id="i-2", source_id="fresh-src", source_native_id="2",
        title="Morning sweep", owner_kind="agent", owner_id="crimson",
        plane="platform", planned_start=NOW - timedelta(hours=1),
        actual_start=NOW - timedelta(minutes=55), status="completed",
    )
    later = Instance(
        instance_id="i-3", source_id="fresh-src", source_native_id="3",
        title="Tomorrow job", owner_kind="agent", owner_id="azure",
        plane="host-b", planned_start=NOW + timedelta(hours=25),
        status="scheduled",
    )
    return TimelineData(
        sources=[source],
        instances=[upcoming, done, later],
        bands=[long_band, azure_band, ended_band],
    )


def _row_section(text: str, key: str) -> str:
    lines = text.splitlines()
    start = lines.index(key)
    section = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        section.append(line)
    return "\n".join(section)


def test_band_view_groups_bands_and_instances_by_owner():
    text = render_band_view(_data(), now=NOW)
    section = _row_section(text, "agent/crimson")
    assert "Long service" in section
    assert "Harbour audit run" in section
    assert "Morning sweep" in section
    # The band that ended before the default window is not drawn.
    assert "Finished service" not in text
    azure = _row_section(text, "agent/azure")
    assert "Relay service" in azure and "Tomorrow job" in azure


def test_band_view_group_by_plane_toggle():
    text = render_band_view(
        _data(), BandFilters(group_by="plane"), now=NOW
    )
    platform = _row_section(text, "platform")
    assert "Long service" in platform and "Harbour audit run" in platform
    assert "owner agent/crimson" in platform
    host_b = _row_section(text, "host-b")
    assert "Relay service" in host_b and "Tomorrow job" in host_b
    assert "grouped by plane" in text


def test_band_view_default_window_is_minus_24h_plus_48h():
    text = render_band_view(_data(), now=NOW)
    assert "2026-10-29 12:00 .. 2026-11-01 12:00" in text


def test_band_view_explicit_window_and_week_scale():
    text = render_band_view(
        _data(),
        BandFilters(
            window_start=NOW, window_end=NOW + timedelta(days=7),
            scale="week",
        ),
        now=NOW,
    )
    assert "week scale" in text
    assert "10-31" in text  # day-granularity ticks on the week axis
    with pytest.raises(ValueError):
        render_band_view(_data(), BandFilters(scale="month"), now=NOW)


def test_band_span_is_clipped_at_both_window_edges():
    text = render_band_view(_data(), now=NOW)
    line = [ln for ln in text.splitlines() if "Long service" in ln][0]
    timeline = line[2:74]
    assert len(timeline) == 72
    assert timeline[0] == "<" and timeline[-1] == ">"
    assert set(timeline) == {"<", "=", ">"}


def test_band_view_actual_toggle():
    shown = render_band_view(_data(), BandFilters(show_actual=True), now=NOW)
    hidden = render_band_view(_data(), BandFilters(show_actual=False), now=NOW)
    sweep_shown = [ln for ln in shown.splitlines() if "Morning sweep" in ln][0]
    sweep_hidden = [ln for ln in hidden.splitlines() if "Morning sweep" in ln][0]
    assert "A" in sweep_shown[2:74]
    assert "actual" in sweep_shown
    assert "A" not in sweep_hidden[2:74]
    assert "actual" not in sweep_hidden


def test_band_view_freshness_footer_marks_stale():
    data = _data()
    data.sources[0].last_synced_at = NOW - timedelta(days=3)
    text = render_band_view(data, now=NOW)
    assert "STALE" in text


# ---------------------------------------------------------------------------
# Hour drill-down
# ---------------------------------------------------------------------------

def _hour_data() -> TimelineData:
    data = _data()
    point = {
        "actual_id": 1, "source_id": "fresh-src",
        "source_native_id": "p-1", "series_id": None,
        "instance_id": "i-2", "fired_at": NOW + timedelta(minutes=30),
        "would_fire_at": None, "observed_only": False,
        "status": "completed", "kind": "point", "is_empty": True,
    }
    data.actuals = [point]
    return data


def test_hour_drilldown_exact_contents():
    # The hour 12:00-13:00 on the fixture day.
    text = render_hour(_hour_data(), NOW + timedelta(minutes=20), now=NOW)
    assert "hour starting 2026-10-30 12:00 .. 2026-10-30 13:00" in text
    assert "Harbour audit run" not in text  # starts at 14:00
    assert "Long service" in text  # band running through the hour
    assert "Relay service" in text
    assert "Finished service" not in text
    assert "agent/crimson  plane platform" in text  # grouped owner+plane


def test_hour_drilldown_shows_planned_and_actual():
    at = NOW - timedelta(minutes=30)  # the 11:00 hour
    text = render_hour(_hour_data(), at, now=NOW)
    line = [ln for ln in text.splitlines() if "Morning sweep" in ln][0]
    assert "planned 2026-10-30 11:00" in line
    assert "actual 2026-10-30 11:05" in line
    assert "status completed" in line
    # Planned-only mode drops the actual story.
    planned = render_hour(
        _hour_data(), at, filters=HourFilters(show_actual=False), now=NOW
    )
    line = [ln for ln in planned.splitlines() if "Morning sweep" in ln][0]
    assert "actual" not in line


def test_hour_day_mode_covers_24_hours():
    text = render_hour(_hour_data(), NOW, day=True, now=NOW)
    assert "day starting 2026-10-30 12:00 .. 2026-10-31 12:00" in text
    assert "Harbour audit run" in text
    assert "Tomorrow job" not in text  # 25h after NOW is outside the day


# ---------------------------------------------------------------------------
# Store path
# ---------------------------------------------------------------------------

def test_band_view_from_store_is_windowed():
    store = InMemoryStore()
    data = _data()
    for source in data.sources:
        store.upsert_source(source)
    for inst in data.instances:
        store.upsert_instance(inst)
    store.upsert_instance(
        Instance(
            instance_id="i-far", source_id="fresh-src",
            source_native_id="far", title="Far future job",
            owner_kind="agent", owner_id="crimson", plane="platform",
            planned_start=NOW + timedelta(days=30), status="scheduled",
        )
    )
    for band in data.bands:
        store.upsert_band(band)
    lo, hi = NOW - timedelta(hours=24), NOW + timedelta(hours=48)
    fetched = data_from_store(store, lo, hi)
    titles = [i.title for i in fetched.instances]
    assert "Far future job" not in titles
    assert "Harbour audit run" in titles
    text = render_band_view(
        fetched, BandFilters(window_start=lo, window_end=hi), now=NOW
    )
    assert "Long service" in text
    assert "Far future job" not in text


def test_views_accept_a_projection_directly():
    data = _data()
    projection = Projection(
        sources=data.sources, instances=data.instances, bands=data.bands
    )
    text = render_band_view(projection, now=NOW)
    assert "Long service" in text


def test_example_fleet_fixtures_present_in_demo_projection(examples_dir):
    from kalends.cli import run_demo

    projection = run_demo(examples_dir, now=NOW)
    text = render_band_view(projection, now=NOW)
    # The example fleet's services are bands on the timeline.
    assert "harbour-db" in text or "tide-relay-api" in text
    completions = json.loads(
        (examples_dir / "completions.json").read_text()
    )
    empty_agents = [r["agent_id"] for r in completions if r["is_empty"]]
    assert empty_agents == ["crimson"]
