"""Agent pages: design view 5, one page per owner.

For one owner (owner kind and id, always taken from the data, never
hardcoded): its series with lifecycle and cadence, its next instances,
its active bands, the windows intersecting the display window, and its
recent actuals with empty cycles flagged. The page closes with the
owner's empty-cycle share over the lookback: empty completion points
divided by all completion points, as a percentage. A fleet where an
agent quietly does nothing is only visible if the nothing is counted;
this page counts it.

Rendering follows the table view's conventions: pure-Python aligned
monospace text and a freshness footer with stale sources marked. The
page accepts a Projection (demo/CLI path) or windowed store data via
:func:`agent_data_from_store`, which queries a KalendsStore per series
and per owner with explicit time bounds, never an unbounded fleet
listing. An owners index (:func:`render_owners_index`) makes pages
discoverable from the data itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .bandview import TimelineData, _actual_row
from .model import Window
from .projector import Projection
from .tableview import _fmt_time, _source_line

DEFAULT_WINDOW_PAST = timedelta(hours=24)
DEFAULT_WINDOW_FUTURE = timedelta(hours=48)
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_NEXT_N = 5


@dataclass
class AgentPage:
    owner_kind: str
    owner_id: Optional[str]
    series: list = field(default_factory=list)
    next_instances: list = field(default_factory=list)
    active_bands: list = field(default_factory=list)
    windows: list = field(default_factory=list)
    recent_actuals: list[dict] = field(default_factory=list)
    empty_points: int = 0
    total_points: int = 0
    lookback_days: int = DEFAULT_LOOKBACK_DAYS

    @property
    def empty_share_pct(self) -> Optional[float]:
        if self.total_points == 0:
            return None
        return 100.0 * self.empty_points / self.total_points

    def empty_line(self) -> str:
        if self.total_points == 0:
            return (
                f"Empty cycles: no completion points in the last "
                f"{self.lookback_days} days"
            )
        return (
            f"Empty cycles: {self.empty_points} of {self.total_points} "
            f"completion points empty ({self.empty_share_pct:.1f}%) "
            f"in the last {self.lookback_days} days"
        )


def _owner_matches(obj, owner_kind: str, owner_id: Optional[str]) -> bool:
    return obj.owner_kind == owner_kind and obj.owner_id == owner_id


def _points_from_instances(instances, lo: datetime, hi: datetime) -> list[dict]:
    """Completion points joined onto instances by the projector ride
    in extensions (point/empty) with the moment at actual_start (or
    the fired_at extension). Derive point rows from them."""
    rows: list[dict] = []
    for inst in instances:
        ext = inst.extensions or {}
        if not ext.get("point"):
            continue
        fired = inst.actual_start
        if fired is None and ext.get("fired_at"):
            from dateutil.parser import isoparse

            fired = isoparse(str(ext["fired_at"]))
        if fired is None or not (lo <= fired < hi):
            continue
        rows.append(
            {
                "actual_id": None,
                "source_id": inst.source_id,
                "source_native_id": inst.source_native_id,
                "series_id": inst.series_id,
                "instance_id": inst.instance_id,
                "fired_at": fired,
                "would_fire_at": inst.planned_start,
                "observed_only": bool(ext.get("observed")),
                "status": inst.status,
                "kind": "point",
                "is_empty": bool(ext.get("empty")),
                "title": inst.title,
            }
        )
    return rows


def collect_agent_page(
    data: TimelineData,
    owner_kind: str,
    owner_id: Optional[str],
    now: datetime,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    next_n: int = DEFAULT_NEXT_N,
) -> AgentPage:
    lo = window_start or (now - DEFAULT_WINDOW_PAST)
    hi = window_end or (now + DEFAULT_WINDOW_FUTURE)
    lookback_start = now - timedelta(days=lookback_days)

    series = sorted(
        (s for s in data.series if _owner_matches(s, owner_kind, owner_id)),
        key=lambda s: s.title,
    )
    upcoming = [
        i for i in data.instances
        if _owner_matches(i, owner_kind, owner_id)
        and (i.planned_start or i.actual_start) is not None
        and (i.planned_start or i.actual_start) >= now
        and (i.planned_start or i.actual_start) < hi
    ]
    upcoming.sort(key=lambda i: (i.planned_start or i.actual_start, i.title))
    active_bands = sorted(
        (
            b for b in data.bands
            if _owner_matches(b, owner_kind, owner_id)
            and (b.started_at is None or b.started_at < hi)
            and (b.ended_at is None or b.ended_at > lo)
        ),
        key=lambda b: b.started_at or lo,
    )
    windows = sorted(
        (
            w for w in data.windows
            if _owner_matches(w, owner_kind, owner_id)
            and w.starts_at is not None
            and w.starts_at < hi
            and (w.ends_at is None or w.ends_at > lo)
        ),
        key=lambda w: w.starts_at,
    )

    # Explicit actual rows win when any of them belong to this owner;
    # otherwise points joined onto instances are the source, so a
    # page never double-counts a cycle and never misses joined ones.
    points = [
        r for r in data.actuals
        if r.get("kind") == "point"
        and r.get("fired_at") is not None
        and lookback_start <= r["fired_at"] <= now
        and _actual_belongs_to(data, r, owner_kind, owner_id)
    ]
    recent = [
        r for r in data.actuals
        if r.get("fired_at") is not None
        and lookback_start <= r["fired_at"] <= now
        and _actual_belongs_to(data, r, owner_kind, owner_id)
    ]
    if not points and not recent:
        derived = _points_from_instances(data.instances, lookback_start,
                                         now + timedelta(seconds=1))
        derived = [r for r in derived
                   if _owner_matches_instance(data, r, owner_kind, owner_id)]
        points = derived
        recent = list(derived)
        # Firings joined onto instances also belong on the page.
        for inst in data.instances:
            if not _owner_matches(inst, owner_kind, owner_id):
                continue
            if inst.actual_start is None:
                continue
            if not (lookback_start <= inst.actual_start <= now):
                continue
            if (inst.extensions or {}).get("point"):
                continue
            recent.append(
                {
                    "fired_at": inst.actual_start,
                    "kind": "firing",
                    "status": inst.status,
                    "is_empty": False,
                    "title": inst.title,
                    "series_id": inst.series_id,
                }
            )
    recent.sort(key=lambda r: r["fired_at"], reverse=True)
    return AgentPage(
        owner_kind=owner_kind,
        owner_id=owner_id,
        series=series,
        next_instances=upcoming[:next_n],
        active_bands=active_bands,
        windows=windows,
        recent_actuals=recent,
        empty_points=sum(1 for r in points if r.get("is_empty")),
        total_points=len(points),
        lookback_days=lookback_days,
    )


def _owner_matches_instance(data, row, owner_kind, owner_id) -> bool:
    inst = next(
        (i for i in data.instances if i.instance_id == row.get("instance_id")),
        None,
    )
    if inst is not None:
        if _owner_matches(inst, owner_kind, owner_id):
            return True
        # A standalone completion point carries no series and no
        # owner: the projector titles it with the completing agent's
        # label (see CompletionPointsAdapter), and that label is the
        # only attribution the data keeps. Use it, for points only.
        ext = inst.extensions or {}
        return bool(
            ext.get("point") and inst.series_id is None
            and owner_id is not None and inst.title == owner_id
        )
    return True


def _actual_belongs_to(data, row, owner_kind, owner_id) -> bool:
    if row.get("instance_id"):
        return _owner_matches_instance(data, row, owner_kind, owner_id)
    if row.get("series_id"):
        series = next(
            (s for s in data.series if s.series_id == row["series_id"]), None
        )
        if series is not None:
            return _owner_matches(series, owner_kind, owner_id)
    return False


def render_agent_page(
    data,
    owner_kind: str,
    owner_id: Optional[str],
    now: Optional[datetime] = None,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    next_n: int = DEFAULT_NEXT_N,
) -> str:
    if isinstance(data, Projection):
        data = TimelineData.from_projection(data)
    now = now or datetime.now(timezone.utc)
    page = collect_agent_page(
        data, owner_kind, owner_id, now,
        window_start=window_start, window_end=window_end,
        lookback_days=lookback_days, next_n=next_n,
    )
    owner = f"{owner_kind}/{owner_id}" if owner_id else owner_kind
    out: list[str] = [f"Agent page: {owner}"]
    out.append("")
    out.append(f"Series ({len(page.series)}):")
    if not page.series:
        out.append("  (none)")
    for s in page.series:
        cadence = s.cadence_expr or s.rrule or s.cadence_dialect
        out.append(
            f"  {s.title}  [{s.lifecycle_state}] cadence {s.cadence_dialect} "
            f"{cadence}  plane {s.plane}  source {s.source_id}"
        )
    out.append("")
    out.append(f"Next instances ({len(page.next_instances)}):")
    if not page.next_instances:
        out.append("  (none in window)")
    for inst in page.next_instances:
        out.append(
            f"  {_fmt_time(inst.planned_start or inst.actual_start)}  "
            f"{inst.title}  [{inst.status}] plane {inst.plane}"
        )
    out.append("")
    out.append(f"Active bands ({len(page.active_bands)}):")
    if not page.active_bands:
        out.append("  (none)")
    for band in page.active_bands:
        out.append(
            f"  {band.title}  [{band.status}] since {_fmt_time(band.started_at)} "
            f"plane {band.plane}"
        )
    out.append("")
    out.append(f"Windows intersecting the window ({len(page.windows)}):")
    if not page.windows:
        out.append("  (none)")
    for w in page.windows:
        out.append(
            f"  {w.kind}  {_fmt_time(w.starts_at)} .. {_fmt_time(w.ends_at)} "
            f"plane {w.plane}"
        )
    out.append("")
    out.append(f"Recent actuals (last {lookback_days} days, {len(page.recent_actuals)}):")
    if not page.recent_actuals:
        out.append("  (none)")
    series_titles = {s.series_id: s.title for s in data.series}
    for row in page.recent_actuals:
        title = row.get("title") or series_titles.get(row.get("series_id") or "", row.get("series_id") or "actual")
        empty = " EMPTY" if row.get("is_empty") else ""
        out.append(
            f"  {_fmt_time(row['fired_at'])}  {title}  kind "
            f"{row.get('kind') or 'firing'} status {row.get('status') or 'completed'}{empty}"
        )
    out.append("")
    out.append(page.empty_line())
    out.append("")
    out.append(f"Sources (freshness at {_fmt_time(now)} UTC):")
    for source in sorted(data.sources, key=lambda s: s.source_id):
        out.append("  " + _source_line(source, now))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Owners index
# ---------------------------------------------------------------------------

def list_owners(data) -> list[dict]:
    if isinstance(data, Projection):
        data = TimelineData.from_projection(data)
    owners: dict[tuple[str, Optional[str]], dict] = {}

    def slot(kind, owner_id):
        return owners.setdefault(
            (kind, owner_id),
            {"owner_kind": kind, "owner_id": owner_id,
             "series": 0, "instances": 0, "bands": 0},
        )

    for s in data.series:
        slot(s.owner_kind, s.owner_id)["series"] += 1
    for i in data.instances:
        slot(i.owner_kind, i.owner_id)["instances"] += 1
    for b in data.bands:
        slot(b.owner_kind, b.owner_id)["bands"] += 1
    return [owners[k] for k in sorted(owners, key=lambda k: (k[0], k[1] or ""))]


def render_owners_index(data, now: Optional[datetime] = None) -> str:
    if isinstance(data, Projection):
        data = TimelineData.from_projection(data)
    now = now or datetime.now(timezone.utc)
    owners = list_owners(data)
    out = ["Owners index (kind/id  series  instances  bands):"]
    if not owners:
        out.append("(no owners in data)")
    for row in owners:
        owner = (
            f"{row['owner_kind']}/{row['owner_id']}"
            if row["owner_id"] else row["owner_kind"]
        )
        out.append(
            f"  {owner}  series {row['series']}  instances "
            f"{row['instances']}  bands {row['bands']}"
        )
    out.append("")
    out.append(f"Sources (freshness at {_fmt_time(now)} UTC):")
    for source in sorted(data.sources, key=lambda s: s.source_id):
        out.append("  " + _source_line(source, now))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Store path (windowed and owner-scoped; never an unbounded fleet list)
# ---------------------------------------------------------------------------

def agent_data_from_store(store, owner_kind: str, owner_id: Optional[str],
                          now: datetime,
                          window_start: Optional[datetime] = None,
                          window_end: Optional[datetime] = None,
                          lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> TimelineData:
    """Build one owner's TimelineData from a KalendsStore.

    Series are owner-scoped; instances and actuals are fetched per
    series with explicit from/to bounds covering both the display
    window and the actuals lookback; bands and windows are fetched
    owner-scoped. Nothing here lists the fleet unbounded.
    """
    lo = window_start or (now - DEFAULT_WINDOW_PAST)
    hi = window_end or (now + DEFAULT_WINDOW_FUTURE)
    lookback_start = now - timedelta(days=lookback_days)
    series = store.list_series(owner_id=owner_id, owner_kind=owner_kind)
    instances: list = []
    actuals: list[dict] = []
    fetch_from = min(lo, lookback_start)
    for s in series:
        instances += store.list_instances(
            series_id=s.series_id, from_ts=lo, to_ts=hi
        )
        actuals += store.list_actuals(
            series_id=s.series_id, from_ts=fetch_from,
            to_ts=now + timedelta(seconds=1),
        )
    bands = store.list_bands(owner_id=owner_id)
    bands = [b for b in bands if b.owner_kind == owner_kind]
    windows = store.list_windows(owner_id=owner_id, from_ts=lo, to_ts=hi)
    windows = [w for w in windows if w.owner_kind == owner_kind]
    return TimelineData(
        sources=store.list_sources(),
        series=series,
        instances=instances,
        bands=bands,
        actuals=[_actual_row(a) for a in actuals],
        windows=windows,
    )
