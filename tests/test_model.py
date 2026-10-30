from datetime import datetime, timezone

from kalends.model import Band, Instance, Series, Source, Window
from kalends.projector import Projection


def test_series_roundtrip():
    series = Series(
        series_id="src:job",
        source_id="src",
        source_native_id="job",
        title="Job",
        owner_kind="worker",
        owner_id="crimson",
        plane="host-a",
        cadence_dialect="interval",
        cadence_expr="every 6h",
        rrule="FREQ=HOURLY;INTERVAL=6",
        source_updated_at=datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc),
        resource_claims={"slot_group": "survey"},
        event_triggers=["weather-alert"],
    )
    back = Series.from_dict(series.to_dict())
    assert back == series


def test_instance_roundtrip_keeps_times():
    inst = Instance(
        instance_id="src:job@1",
        series_id="src:job",
        source_id="src",
        source_native_id="job",
        title="Job",
        planned_start=datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc),
        actual_start=datetime(2026, 10, 31, 3, 1, tzinfo=timezone.utc),
        status="completed",
    )
    back = Instance.from_dict(inst.to_dict())
    assert back == inst
    assert back.planned_start.tzinfo is not None


def test_projection_dump_roundtrip():
    proj = Projection(
        sources=[
            Source(
                source_id="s",
                adapter="cron",
                plane="host-a",
                last_synced_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
            )
        ],
        bands=[
            Band(
                band_id="s:b1",
                source_id="s",
                source_native_id="b1",
                title="Running thing",
                started_at=datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc),
            )
        ],
        series=[
            Series(
                series_id="s:x",
                source_id="s",
                source_native_id="x",
                title="X",
                plane="host-a",
            )
        ],
    )
    back = Projection.from_dict(proj.to_dict())
    assert back.sources[0].source_id == "s"
    assert back.bands[0].started_at == proj.bands[0].started_at
    assert back.series[0].title == "X"


def test_window_roundtrip():
    win = Window(
        window_id="w1",
        owner_kind="agent",
        owner_id="azure",
        plane="host-b",
        kind="quiet",
        priority=5,
    )
    assert Window.from_dict(win.to_dict()) == win
