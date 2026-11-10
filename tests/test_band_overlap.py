"""Regression tests for the store-path band overlap gap (Phase 3).

The band view's store path used to probe bands with ``active_at``
at the window's start, middle, and end, so a band that starts AND
ends strictly inside the query window — without covering any probe
point — was missed. The store now answers a real overlap question:
a band overlaps [window_start, window_end) iff
``started_at < window_end`` and ``effective_end > window_start``,
where effective_end is ``ended_at`` if present, else
``lease_expires_at`` if present, else infinity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalends.bandview import BandFilters, data_from_store, render_band_view
from kalends.model import Band, Source
from kalends.store import InMemoryStore

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
LO = NOW - timedelta(hours=24)
HI = NOW + timedelta(hours=48)


def _band(band_id, **over):
    row = {
        "band_id": band_id, "source_id": "fresh-src",
        "source_native_id": band_id, "title": band_id,
        "owner_kind": "agent", "owner_id": "crimson", "plane": "platform",
        "started_at": NOW - timedelta(hours=1), "status": "active",
    }
    row.update(over)
    return Band(**row)


def _store_with(*bands):
    store = InMemoryStore()
    store.upsert_source(Source(source_id="fresh-src", adapter="cron",
                               plane="platform"))
    for band in bands:
        store.upsert_band(band)
    return store


# ---------------------------------------------------------------------------
# The regression: a band strictly inside the window, covering no probe point
# ---------------------------------------------------------------------------

def test_data_from_store_returns_band_strictly_inside_window():
    # Window is [NOW-24h, NOW+48h); the old probes were at NOW-24h,
    # NOW+12h and NOW+48h. This band runs NOW+2h .. NOW+4h: strictly
    # inside the window, active at none of the probe points.
    inside = _band(
        "Inside job",
        started_at=NOW + timedelta(hours=2),
        ended_at=NOW + timedelta(hours=4), status="done",
    )
    store = _store_with(inside)
    fetched = data_from_store(store, LO, HI)
    assert [b.band_id for b in fetched.bands] == ["Inside job"]
    text = render_band_view(
        fetched, BandFilters(window_start=LO, window_end=HI), now=NOW)
    assert "Inside job" in text


def test_store_overlap_returns_band_strictly_inside_window():
    inside = _band(
        "b-inside",
        started_at=NOW + timedelta(hours=2),
        ended_at=NOW + timedelta(hours=4), status="done",
    )
    outside = _band(
        "b-outside",
        started_at=NOW - timedelta(days=5),
        ended_at=NOW - timedelta(days=4), status="done",
    )
    store = _store_with(inside, outside)
    rows = store.list_bands(overlap_start=LO, overlap_end=HI)
    assert [b.band_id for b in rows] == ["b-inside"]


# ---------------------------------------------------------------------------
# Effective end: ended_at, else lease_expires_at, else infinity
# ---------------------------------------------------------------------------

def test_store_overlap_uses_lease_as_effective_end():
    # No ended_at: the lease is the end. A lease expiring inside the
    # window still overlaps it; a lease that expired before the
    # window started does not, even though the band never "ended".
    live_lease = _band(
        "b-lease-live", started_at=NOW - timedelta(days=2),
        lease_expires_at=NOW + timedelta(hours=1),
    )
    dead_lease = _band(
        "b-lease-dead", started_at=NOW - timedelta(days=5),
        lease_expires_at=NOW - timedelta(days=2),
    )
    store = _store_with(live_lease, dead_lease)
    rows = store.list_bands(overlap_start=LO, overlap_end=HI)
    assert [b.band_id for b in rows] == ["b-lease-live"]


def test_store_overlap_ended_at_wins_over_lease():
    # ended_at present: it, not the (later) lease, is the end.
    ended = _band(
        "b-ended", started_at=NOW - timedelta(days=5),
        ended_at=NOW - timedelta(days=2),
        lease_expires_at=NOW + timedelta(days=2), status="done",
    )
    store = _store_with(ended)
    assert store.list_bands(overlap_start=LO, overlap_end=HI) == []


def test_store_overlap_open_ended_band_overlaps():
    open_band = _band("b-open", started_at=NOW - timedelta(days=2))
    store = _store_with(open_band)
    rows = store.list_bands(overlap_start=LO, overlap_end=HI)
    assert [b.band_id for b in rows] == ["b-open"]


def test_store_overlap_boundaries_are_strict():
    ends_at_start = _band(
        "b-ends-at-start", started_at=LO - timedelta(hours=2),
        ended_at=LO, status="done",
    )
    starts_at_end = _band(
        "b-starts-at-end", started_at=HI,
        ended_at=HI + timedelta(hours=2), status="done",
    )
    touches_inside = _band(
        "b-touching", started_at=HI - timedelta(hours=1),
        ended_at=HI, status="done",
    )
    store = _store_with(ends_at_start, starts_at_end, touches_inside)
    rows = store.list_bands(overlap_start=LO, overlap_end=HI)
    assert [b.band_id for b in rows] == ["b-touching"]


def test_store_overlap_composes_with_other_filters():
    inside = _band(
        "b-inside", owner_id="azure",
        started_at=NOW + timedelta(hours=2),
        ended_at=NOW + timedelta(hours=4), status="done",
    )
    store = _store_with(inside)
    assert store.list_bands(overlap_start=LO, overlap_end=HI,
                            owner_id="crimson") == []
    assert [b.band_id for b in store.list_bands(
        overlap_start=LO, overlap_end=HI, owner_id="azure")] == ["b-inside"]
