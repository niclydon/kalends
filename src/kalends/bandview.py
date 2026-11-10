"""The band view and the hour drill-down: design views 2 and 3.

The band view is a resource timeline. Each row is one owner (or, with
``group_by="plane"``, one plane); bands are drawn as spans and
instances as marks on the same time axis, so one owner's service band,
session band, and job instances read as a single timeline. The hour
drill-down lists everything intersecting one hour (or, in day mode,
the 24 hours starting at a given hour), grouped by owner and plane,
planned and actual side by side.

Rendering follows the table view's conventions exactly: pure-Python
aligned monospace text, no dependencies, and a freshness footer, one
line per source with stale sources marked. That is a deliberate
choice, not a placeholder for a web UI: the design (section 9) bounds
every view by its window, not by fleet size, because views query
materialised, windowed data. A text axis of fixed width therefore
scales to any fleet: the window gets busier, never wider, and the
render cost is proportional to the rows inside the window.

Both views accept a :class:`~kalends.projector.Projection` (the demo
and CLI path) or data pulled windowed from a
:class:`~kalends.store.KalendsStore` via :func:`data_from_store`, so a
deployment can feed them without materialising the whole fleet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .model import Actual, Band, Instance, Series, Source, Window
from .projector import Projection
from .tableview import _fmt_time, _source_line

AXIS_WIDTH = 72
DEFAULT_WINDOW_PAST = timedelta(hours=24)
DEFAULT_WINDOW_FUTURE = timedelta(hours=48)


@dataclass
class TimelineData:
    """Everything a timeline-style view needs, from either source."""

    sources: list[Source] = field(default_factory=list)
    series: list[Series] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)
    actuals: list[dict] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)

    @classmethod
    def from_projection(
        cls,
        projection: Projection,
        actuals: Optional[list] = None,
        windows: Optional[list[Window]] = None,
    ) -> "TimelineData":
        return cls(
            sources=list(projection.sources),
            series=list(projection.series),
            instances=list(projection.instances),
            bands=list(projection.bands),
            actuals=[_actual_row(a) for a in (actuals or [])],
            windows=list(windows or []),
        )


def _actual_row(actual) -> dict:
    """Normalise an Actual object or store dict to a plain dict with
    aware datetimes under the actuals column names."""
    if isinstance(actual, Actual):
        return {
            "actual_id": None,
            "source_id": actual.source_id,
            "source_native_id": actual.source_native_id,
            "series_id": actual.series_id,
            "instance_id": None,
            "fired_at": actual.fired_at,
            "would_fire_at": actual.would_fire_at,
            "observed_only": actual.observed_only,
            "status": actual.status,
            "kind": actual.kind,
            "is_empty": actual.is_empty,
        }
    return dict(actual)


@dataclass
class BandFilters:
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    group_by: str = "owner"  # "owner" or "plane"
    scale: str = "day"  # "day" or "week": the time-axis granularity
    plane: Optional[str] = None
    owner: Optional[str] = None
    owner_kind: Optional[str] = None
    source: Optional[str] = None
    show_actual: bool = True

    def resolved_window(self, now: datetime) -> tuple[datetime, datetime]:
        start = self.window_start or (now - DEFAULT_WINDOW_PAST)
        end = self.window_end or (now + DEFAULT_WINDOW_FUTURE)
        return start, end


def _owner_cell(kind: str, owner_id: Optional[str]) -> str:
    return f"{kind}/{owner_id}" if owner_id else kind


def _matches(kind, owner_id, plane, source_id, f: BandFilters) -> bool:
    if f.plane and plane != f.plane:
        return False
    if f.owner and owner_id != f.owner:
        return False
    if f.owner_kind and kind != f.owner_kind:
        return False
    if f.source and source_id != f.source:
        return False
    return True


def _instance_span(inst: Instance) -> tuple[Optional[datetime], Optional[datetime]]:
    start = inst.planned_start or inst.actual_start
    end = inst.planned_end or inst.actual_end or start
    return start, end


def _overlaps(start, end, lo: datetime, hi: datetime) -> bool:
    if start is None:
        return False
    end = end or start
    return start < hi and end >= lo


def _pos(moment: datetime, lo: datetime, hi: datetime) -> int:
    """Axis column for a moment, clipped to the axis at both edges."""
    if moment <= lo:
        return 0
    if moment >= hi:
        return AXIS_WIDTH - 1
    frac = (moment - lo).total_seconds() / (hi - lo).total_seconds()
    return min(AXIS_WIDTH - 1, int(frac * AXIS_WIDTH))


def _axis_ticks(lo: datetime, hi: datetime, scale: str) -> str:
    """Tick labels under the axis. Day scale ticks every six hours,
    week scale every day: the scale is the axis granularity."""
    step = timedelta(days=1) if scale == "week" else timedelta(hours=6)
    fmt = "%m-%d" if scale == "week" else "%H:%M"
    chars = [" "] * AXIS_WIDTH
    tick = lo
    # Align the first tick to the step grid in UTC.
    if scale == "week":
        tick = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        tick = lo.replace(minute=0, second=0, microsecond=0)
        tick = tick.replace(hour=(tick.hour // 6) * 6)
    if tick < lo:
        tick += step
    while tick < hi:
        label = tick.strftime(fmt)
        col = _pos(tick, lo, hi)
        if col + len(label) <= AXIS_WIDTH:
            for i, ch in enumerate(label):
                chars[col + i] = ch
        tick += step
    return "".join(chars).rstrip()


def _band_timeline(band: Band, lo: datetime, hi: datetime) -> str:
    chars = ["."] * AXIS_WIDTH
    start = band.started_at or lo
    end = band.ended_at or hi
    first = _pos(max(start, lo), lo, hi)
    last = _pos(min(end, hi), lo, hi)
    for col in range(first, last + 1):
        chars[col] = "="
    if band.started_at is not None and band.started_at < lo:
        chars[0] = "<"
    if band.ended_at is None or band.ended_at > hi:
        chars[AXIS_WIDTH - 1] = ">"
    return "".join(chars)


def _instance_timeline(inst: Instance, lo: datetime, hi: datetime,
                       show_actual: bool) -> str:
    chars = ["."] * AXIS_WIDTH
    start, end = _instance_span(inst)
    if start is None:
        return "".join(chars)
    first = _pos(max(start, lo), lo, hi)
    last = _pos(min(end or start, hi), lo, hi)
    if last > first:
        for col in range(first, last + 1):
            chars[col] = "-"
    mark = "*"
    if inst.status == "failed":
        mark = "X"
    elif inst.actual_start is not None:
        mark = "o"
    chars[first] = mark
    if show_actual and inst.actual_start is not None:
        acol = _pos(inst.actual_start, lo, hi)
        if start <= inst.actual_start < hi or inst.planned_start is None:
            chars[acol] = "A"
    return "".join(chars)


def collect_band_rows(
    data: TimelineData, filters: BandFilters, now: datetime
) -> dict[str, list[dict]]:
    """Group the window's bands and instances by row key."""
    lo, hi = filters.resolved_window(now)
    rows: dict[str, list[dict]] = {}

    def key_for(kind, owner_id, plane) -> str:
        if filters.group_by == "plane":
            return plane or "(no plane)"
        return _owner_cell(kind, owner_id)

    for band in data.bands:
        if not _matches(band.owner_kind, band.owner_id, band.plane,
                        band.source_id, filters):
            continue
        if not _overlaps(band.started_at or lo, band.ended_at or hi, lo, hi):
            continue
        rows.setdefault(key_for(band.owner_kind, band.owner_id, band.plane), []).append(
            {
                "sort": band.started_at or lo,
                "timeline": _band_timeline(band, lo, hi),
                "label": (
                    f"band  {band.title} [{band.status}] "
                    f"{_fmt_time(band.started_at)} .. "
                    f"{_fmt_time(band.ended_at) if band.ended_at else 'open'}"
                ),
                "owner": _owner_cell(band.owner_kind, band.owner_id),
                "plane": band.plane,
            }
        )
    for inst in data.instances:
        if not _matches(inst.owner_kind, inst.owner_id, inst.plane,
                        inst.source_id, filters):
            continue
        start, end = _instance_span(inst)
        if not _overlaps(start, end, lo, hi):
            continue
        note = ""
        if filters.show_actual and inst.actual_start is not None:
            note = f" actual {_fmt_time(inst.actual_start)} [{inst.status}]"
        elif inst.status != "scheduled":
            note = f" [{inst.status}]"
        if (inst.extensions or {}).get("empty"):
            note += " empty cycle"
        rows.setdefault(key_for(inst.owner_kind, inst.owner_id, inst.plane), []).append(
            {
                "sort": start or lo,
                "timeline": _instance_timeline(inst, lo, hi, filters.show_actual),
                "label": f"inst  {inst.title} planned {_fmt_time(start)}{note}",
                "owner": _owner_cell(inst.owner_kind, inst.owner_id),
                "plane": inst.plane,
            }
        )
    for items in rows.values():
        items.sort(key=lambda r: (r["sort"], r["label"]))
    return rows


def render_band_view(
    data,
    filters: Optional[BandFilters] = None,
    now: Optional[datetime] = None,
) -> str:
    if isinstance(data, Projection):
        data = TimelineData.from_projection(data)
    filters = filters or BandFilters()
    if filters.group_by not in ("owner", "plane"):
        raise ValueError(f"group_by must be 'owner' or 'plane', got {filters.group_by!r}")
    if filters.scale not in ("day", "week"):
        raise ValueError(f"scale must be 'day' or 'week', got {filters.scale!r}")
    now = now or datetime.now(timezone.utc)
    lo, hi = filters.resolved_window(now)
    rows = collect_band_rows(data, filters, now)

    out: list[str] = []
    out.append(
        f"Band view (UTC): {_fmt_time(lo)} .. {_fmt_time(hi)}  "
        f"grouped by {filters.group_by}, {filters.scale} scale"
    )
    out.append(
        "Legend: = band span (< continues from before, > still open)  "
        "- instance span  * planned instance  o instance with an actual  "
        "X failed instance  A actual start (planned vs actual)"
    )
    indent = "  "
    out.append(indent + "|" + "-" * AXIS_WIDTH + "|")
    out.append(indent + " " + _axis_ticks(lo, hi, filters.scale))
    if not rows:
        out.append("(no rows match)")
    for key in sorted(rows):
        out.append(key)
        for item in rows[key]:
            extra = ""
            if filters.group_by == "plane":
                extra = f"  owner {item['owner']}"
            elif item["plane"]:
                extra = f"  plane {item['plane']}"
            out.append(f"{indent}{item['timeline']}  {item['label']}{extra}")
    out.append("")
    out.append(f"Sources (freshness at {_fmt_time(now)} UTC):")
    for source in sorted(data.sources, key=lambda s: s.source_id):
        out.append("  " + _source_line(source, now))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Hour drill-down
# ---------------------------------------------------------------------------

@dataclass
class HourFilters:
    plane: Optional[str] = None
    owner: Optional[str] = None
    owner_kind: Optional[str] = None
    source: Optional[str] = None
    show_actual: bool = True


def hour_window(at: datetime, day: bool = False) -> tuple[datetime, datetime]:
    """The hour containing ``at`` (truncated), or in day mode the 24
    hourly rows' worth: the day window starting at that hour."""
    start = at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return start, start + (timedelta(days=1) if day else timedelta(hours=1))


def collect_hour(data: TimelineData, lo: datetime, hi: datetime,
                 filters: HourFilters) -> dict[tuple[str, str], list[dict]]:
    """Everything intersecting [lo, hi), grouped by (owner, plane)."""
    bf = BandFilters(plane=filters.plane, owner=filters.owner,
                     owner_kind=filters.owner_kind, source=filters.source,
                     show_actual=filters.show_actual)
    groups: dict[tuple[str, str], list[dict]] = {}

    def add(kind, owner_id, plane, sort, line):
        groups.setdefault((_owner_cell(kind, owner_id), plane or ""), []).append(
            {"sort": sort, "line": line}
        )

    seen_actual_instances: set[str] = set()
    for inst in data.instances:
        if not _matches(inst.owner_kind, inst.owner_id, inst.plane,
                        inst.source_id, bf):
            continue
        start, end = _instance_span(inst)
        actual_in = (
            inst.actual_start is not None and lo <= inst.actual_start < hi
        )
        if not (_overlaps(start, end, lo, hi) or actual_in):
            continue
        parts = [f"instance  {inst.title}"]
        parts.append(
            f"planned {_fmt_time(inst.planned_start)} .. {_fmt_time(inst.planned_end)}"
        )
        if filters.show_actual:
            if inst.actual_start is not None:
                parts.append(
                    f"actual {_fmt_time(inst.actual_start)} status {inst.status}"
                )
                if inst.instance_id:
                    seen_actual_instances.add(inst.instance_id)
            else:
                parts.append(f"status {inst.status}")
        if (inst.extensions or {}).get("empty"):
            parts.append("empty cycle")
        add(inst.owner_kind, inst.owner_id, inst.plane, start or lo,
            "  ".join(parts))
    for band in data.bands:
        if not _matches(band.owner_kind, band.owner_id, band.plane,
                        band.source_id, bf):
            continue
        if not _overlaps(band.started_at or lo, band.ended_at or hi, lo, hi):
            continue
        add(
            band.owner_kind, band.owner_id, band.plane, band.started_at or lo,
            f"band      {band.title}  {_fmt_time(band.started_at)} .. "
            f"{_fmt_time(band.ended_at) if band.ended_at else 'open'} "
            f"[{band.status}]",
        )
    for row in data.actuals:
        fired = row.get("fired_at")
        if fired is None or not (lo <= fired < hi):
            continue
        if row.get("instance_id") and row["instance_id"] in seen_actual_instances:
            continue
        if filters.source and row.get("source_id") != filters.source:
            continue
        series = next(
            (s for s in data.series if s.series_id == row.get("series_id")), None
        )
        kind = series.owner_kind if series else "system"
        owner_id = series.owner_id if series else None
        plane = series.plane if series else ""
        if not _matches(kind, owner_id, plane, row.get("source_id") or "", bf):
            continue
        title = series.title if series else (row.get("series_id") or "actual")
        empty = " empty cycle" if row.get("is_empty") else ""
        add(
            kind, owner_id, plane, fired,
            f"actual    {title}  fired {_fmt_time(fired)} "
            f"kind {row.get('kind') or 'firing'} status "
            f"{row.get('status') or 'completed'}{empty}",
        )
    for items in groups.values():
        items.sort(key=lambda r: (r["sort"], r["line"]))
    return groups


def render_hour(
    data,
    at: datetime,
    day: bool = False,
    filters: Optional[HourFilters] = None,
    now: Optional[datetime] = None,
) -> str:
    if isinstance(data, Projection):
        data = TimelineData.from_projection(data)
    filters = filters or HourFilters()
    now = now or datetime.now(timezone.utc)
    lo, hi = hour_window(at, day=day)
    groups = collect_hour(data, lo, hi, filters)
    span = "day" if day else "hour"
    out = [f"Hour drill-down (UTC): {span} starting {_fmt_time(lo)} .. {_fmt_time(hi)}"]
    if not groups:
        out.append("(nothing intersects)")
    for (owner, plane) in sorted(groups):
        out.append(f"{owner}  plane {plane}" if plane else owner)
        for item in groups[(owner, plane)]:
            out.append("  " + item["line"])
    out.append("")
    out.append(f"Sources (freshness at {_fmt_time(now)} UTC):")
    for source in sorted(data.sources, key=lambda s: s.source_id):
        out.append("  " + _source_line(source, now))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Store path (windowed; never an unbounded list call)
# ---------------------------------------------------------------------------

def data_from_store(store, lo: datetime, hi: datetime, *,
                    owner: Optional[str] = None,
                    owner_kind: Optional[str] = None,
                    plane: Optional[str] = None) -> TimelineData:
    """Build TimelineData from a KalendsStore with bounded queries.

    Instances and actuals are fetched with explicit from/to bounds.
    Bands are fetched with the store's overlap predicate over the
    same window (a band overlaps iff it starts before the window
    ends and its effective end — end, else lease expiry, else
    infinity — is after the window starts), so a band that starts
    and ends strictly inside the window is returned too. (The store
    path used to probe ``active_at`` at the window's start, middle,
    and end, which missed exactly those bands.)
    Series are scoped by the owner/plane filters when given.
    """
    instances = store.list_instances(from_ts=lo, to_ts=hi)
    if owner or owner_kind or plane:
        instances = [
            i for i in instances
            if (not owner or i.owner_id == owner)
            and (not owner_kind or i.owner_kind == owner_kind)
            and (not plane or i.plane == plane)
        ]
    bands = [
        b for b in store.list_bands(owner_id=owner, plane=plane,
                                    overlap_start=lo, overlap_end=hi)
        if (not owner_kind or b.owner_kind == owner_kind)
    ]
    actuals = store.list_actuals(from_ts=lo, to_ts=hi)
    series = store.list_series(owner_id=owner, owner_kind=owner_kind,
                               plane=plane)
    return TimelineData(
        sources=store.list_sources(),
        series=series,
        instances=instances,
        bands=bands,
        actuals=[_actual_row(a) for a in actuals],
    )
