import json
from datetime import datetime, timedelta, timezone

from kalends.agentpage import (
    agent_data_from_store,
    list_owners,
    render_agent_page,
    render_owners_index,
)
from kalends.bandview import TimelineData
from kalends.cli import main as cli_main
from kalends.model import Band, Instance, Series, Source, Window
from kalends.projector import Projection
from kalends.store import InMemoryStore, normalize_actual

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _source() -> Source:
    return Source(
        source_id="fresh-src", adapter="cron", plane="host-a",
        sync_interval_sec=3600,
        last_synced_at=NOW - timedelta(minutes=5),
        last_source_at=NOW - timedelta(minutes=5),
    )


def _series(owner_id="crimson") -> Series:
    return Series(
        series_id="fresh-src:cycle", source_id="fresh-src",
        source_native_id="cycle", title="Harbour cycle",
        owner_kind="agent", owner_id=owner_id, plane="platform",
        cadence_dialect="interval", cadence_expr="every 6h",
        rrule="FREQ=HOURLY;INTERVAL=6", lifecycle_state="active",
    )


def _point(series_id, native, fired, empty):
    return {
        "actual_id": None, "source_id": "fresh-src",
        "source_native_id": native, "series_id": series_id,
        "instance_id": None, "fired_at": fired, "would_fire_at": None,
        "observed_only": False, "status": "completed",
        "kind": "point", "is_empty": empty,
    }


def _data() -> TimelineData:
    series = _series()
    points = [
        _point(series.series_id, "p-1", NOW - timedelta(days=1), True),
        _point(series.series_id, "p-2", NOW - timedelta(days=2), False),
        _point(series.series_id, "p-3", NOW - timedelta(days=3), False),
        _point(series.series_id, "p-4", NOW - timedelta(days=4), False),
        # Outside the 7-day lookback: must not move the share.
        _point(series.series_id, "p-0", NOW - timedelta(days=30), True),
    ]
    next_inst = Instance(
        instance_id="i-next", source_id="fresh-src", source_native_id="n",
        title="Harbour cycle", series_id=series.series_id,
        owner_kind="agent", owner_id="crimson", plane="platform",
        planned_start=NOW + timedelta(hours=3), status="scheduled",
    )
    band = Band(
        band_id="b-1", source_id="fresh-src", source_native_id="svc",
        title="Survey session", owner_kind="agent", owner_id="crimson",
        plane="platform", started_at=NOW - timedelta(hours=2),
        status="active",
    )
    window = Window(
        window_id="w-1", owner_kind="agent", owner_id="crimson",
        plane="platform", kind="available",
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=5),
    )
    return TimelineData(
        sources=[_source()], series=[series], instances=[next_inst],
        bands=[band], actuals=points, windows=[window],
    )


def test_agent_page_sections_and_empty_share_math():
    text = render_agent_page(_data(), "agent", "crimson", now=NOW)
    assert "Agent page: agent/crimson" in text
    assert "Harbour cycle" in text
    assert "[active] cadence interval every 6h" in text
    assert "Survey session" in text  # active band
    assert "available" in text  # window intersecting the window
    assert "EMPTY" in text  # the empty cycle is flagged, not hidden
    # 1 empty of 4 points inside the lookback; the 30-day-old empty
    # point is outside it and excluded.
    assert (
        "Empty cycles: 1 of 4 completion points empty (25.0%) "
        "in the last 7 days"
    ) in text


def test_agent_page_next_instances_soonest_first_and_capped():
    data = _data()
    for n in range(4):
        data.instances.append(
            Instance(
                instance_id=f"i-{n}", source_id="fresh-src",
                source_native_id=str(n), title=f"Run {n}",
                series_id="fresh-src:cycle", owner_kind="agent",
                owner_id="crimson", plane="platform",
                planned_start=NOW + timedelta(hours=10 - n),
                status="scheduled",
            )
        )
    text = render_agent_page(data, "agent", "crimson", now=NOW, next_n=2)
    section = text.split("Next instances", 1)[1].split("Active bands", 1)[0]
    assert "(2):" in section
    assert "Harbour cycle" in section  # soonest (+3h) first


def test_agent_page_no_points_reports_none_not_zero_percent():
    data = _data()
    data.actuals = []
    text = render_agent_page(data, "agent", "crimson", now=NOW)
    assert "Empty cycles: no completion points in the last 7 days" in text


def test_agent_page_for_example_fleet_empty_agent(examples_dir):
    """The fictional owner with is_empty points in the example fleet
    is crimson (completions.json); its page counts that cycle."""
    completions = json.loads((examples_dir / "completions.json").read_text())
    empty = [r for r in completions if r["is_empty"]]
    assert [r["agent_id"] for r in empty] == ["crimson"]

    from kalends.cli import run_demo

    projection = run_demo(examples_dir, now=NOW)
    text = render_agent_page(projection, "agent", "crimson", now=NOW)
    assert (
        "Empty cycles: 1 of 1 completion points empty (100.0%) "
        "in the last 7 days"
    ) in text


def test_agent_page_sable_has_no_empty_cycles(examples_dir):
    from kalends.cli import run_demo

    projection = run_demo(examples_dir, now=NOW)
    text = render_agent_page(
        projection, "human-facing", "sable", now=NOW
    )
    assert "Empty cycles: 0 of 1 completion points empty (0.0%)" in text


def test_owners_index_counts():
    data = _data()
    owners = list_owners(data)
    crimson = [o for o in owners if o["owner_id"] == "crimson"][0]
    assert crimson == {
        "owner_kind": "agent", "owner_id": "crimson",
        "series": 1, "instances": 1, "bands": 1,
    }
    text = render_owners_index(data, now=NOW)
    assert "agent/crimson  series 1  instances 1  bands 1" in text


def test_agent_page_from_store_windowed():
    store = InMemoryStore()
    data = _data()
    store.upsert_source(_source())
    for s in data.series:
        store.upsert_series(s)
    for i in data.instances:
        store.upsert_instance(i)
    for b in data.bands:
        store.upsert_band(b)
    for w in data.windows:
        store.upsert_window(w)
    for row in data.actuals:
        store.upsert_actual(normalize_actual(row))
    fetched = agent_data_from_store(store, "agent", "crimson", NOW)
    text = render_agent_page(fetched, "agent", "crimson", now=NOW)
    assert "Empty cycles: 1 of 4 completion points empty (25.0%)" in text
    assert "Survey session" in text
    assert "Harbour cycle" in text


def test_cli_band_hour_agent_and_agents(tmp_path, capsys):
    data = _data()
    projection = Projection(
        sources=data.sources, series=data.series,
        instances=data.instances, bands=data.bands,
    )
    dump = tmp_path / "dump.json"
    payload = projection.to_dict()
    payload["actuals"] = [
        {**row, "fired_at": row["fired_at"].isoformat()}
        for row in data.actuals
    ]
    payload["windows"] = [w.to_dict() for w in data.windows]
    dump.write_text(json.dumps(payload))

    assert cli_main(["agents", "--dump", str(dump)]) == 0
    assert "agent/crimson" in capsys.readouterr().out

    assert cli_main(
        ["band", "--dump", str(dump),
         "--from", "2026-10-29T12:00:00Z", "--to", "2026-11-01T12:00:00Z"]
    ) == 0
    out = capsys.readouterr().out
    assert "Band view" in out and "Survey session" in out

    assert cli_main(
        ["hour", "--dump", str(dump), "--at", "2026-10-30T15:30:00Z"]
    ) == 0
    out = capsys.readouterr().out
    assert "Hour drill-down" in out and "Harbour cycle" in out

    assert cli_main(
        ["agent", "--dump", str(dump),
         "--owner-kind", "agent", "--owner", "crimson"]
    ) == 0
    out = capsys.readouterr().out
    assert "Agent page: agent/crimson" in out
    # CLI now is the real clock, far past the fixture points, so the
    # lookback must be widened to see them.
    assert cli_main(
        ["agent", "--dump", str(dump), "--owner-kind", "agent",
         "--owner", "crimson", "--lookback-days", "4000"]
    ) == 0
    out = capsys.readouterr().out
    assert "Empty cycles: 2 of 5 completion points empty (40.0%)" in out
