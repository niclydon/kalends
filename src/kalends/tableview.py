"""The table view: design view 1, the audit surface.

One aligned text table holding series, instances, and bands together,
filterable by plane, owner kind, owner, source, shape, and time range.
A freshness footer follows, one line per source, with stale sources
marked in plain sight: a source whose last sync is older than its sync
interval (default 24h) is STALE, because old data presented as current
is how fleets lie to themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .projector import Projection

DEFAULT_STALE_AFTER_SEC = 24 * 3600


@dataclass
class TableFilters:
    plane: Optional[str] = None
    owner: Optional[str] = None
    owner_kind: Optional[str] = None
    source: Optional[str] = None
    shape: Optional[str] = None
    start_from: Optional[datetime] = None
    start_to: Optional[datetime] = None
    # Series are definitions, not occurrences: a time-range filter asks
    # what happens in the window, and definitions stay as context unless
    # the caller turns them off.
    include_series: bool = True

    def any_time_filter(self) -> bool:
        return self.start_from is not None or self.start_to is not None


def _fmt_time(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _fmt_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _matches(owner_kind, owner_id, plane, source_id, shape, filters) -> bool:
    if filters.plane and plane != filters.plane:
        return False
    if filters.owner and owner_id != filters.owner:
        return False
    if filters.owner_kind and owner_kind != filters.owner_kind:
        return False
    if filters.source and source_id != filters.source:
        return False
    if filters.shape and shape != filters.shape:
        return False
    return True


def _in_range(start: Optional[datetime], filters: TableFilters) -> bool:
    if start is None:
        return not filters.any_time_filter()
    if filters.start_from and start < filters.start_from:
        return False
    if filters.start_to and start > filters.start_to:
        return False
    return True


def _fmt_variance(seconds: int) -> str:
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(int(seconds))
    if seconds < 90:
        return f"{sign}{seconds}s"
    if seconds < 5400:
        return f"{sign}{seconds // 60}m"
    return f"{sign}{seconds / 3600:.1f}h"


def _instance_note(inst, now: Optional[datetime]) -> str:
    """The actuals story of an instance, in one short cell.

    A past instance still ``scheduled`` with no actual is MISSED: the
    plan said it would fire and nothing says it did. The status stays
    ``scheduled`` in the model; the mark lives here, in the view.
    """
    ext = inst.extensions or {}
    parts: list[str] = []
    if (
        inst.status == "scheduled"
        and inst.actual_start is None
        and inst.planned_start is not None
        and now is not None
        and inst.planned_start < now
    ):
        parts.append("MISSED")
    if inst.actual_start is not None:
        parts.append(f"actual {_fmt_time(inst.actual_start)}")
    if ext.get("variance_sec") is not None:
        parts.append(f"variance {_fmt_variance(ext['variance_sec'])}")
    if ext.get("observed"):
        parts.append("observed")
    if ext.get("point"):
        parts.append("point")
    if ext.get("empty"):
        parts.append("empty cycle")
    return " · ".join(parts) if parts else "-"


def _band_note(band) -> str:
    ext = band.extensions or {}
    if ext.get("status_value"):
        return str(ext["status_value"])
    if band.end_reason:
        return f"ended: {band.end_reason}"
    if band.lease_expires_at:
        return f"lease {_fmt_time(band.lease_expires_at)}"
    return "-"


def collect_rows(
    projection: Projection,
    filters: TableFilters,
    now: Optional[datetime] = None,
) -> list[dict]:
    rows: list[dict] = []
    for inst in projection.instances:
        if not _matches(
            inst.owner_kind, inst.owner_id, inst.plane, inst.source_id,
            inst.shape, filters,
        ):
            continue
        # A point actual has no planned start; its moment is its
        # actual start, and that is where it belongs in the window.
        start = inst.planned_start or inst.actual_start
        if not _in_range(start, filters):
            continue
        rows.append(
            {
                "start": start,
                "end": _fmt_time(inst.planned_end) if inst.planned_end else "-",
                "shape": inst.shape,
                "status": inst.status,
                "owner": _owner_cell(inst.owner_kind, inst.owner_id),
                "plane": inst.plane,
                "title": inst.title,
                "source": inst.source_id,
                "note": _instance_note(inst, now),
            }
        )
    for band in projection.bands:
        if not _matches(
            band.owner_kind, band.owner_id, band.plane, band.source_id,
            "band", filters,
        ):
            continue
        if not _in_range(band.started_at, filters):
            continue
        rows.append(
            {
                "start": band.started_at,
                "end": _fmt_time(band.ended_at) if band.ended_at else "open",
                "shape": "band",
                "status": band.status,
                "owner": _owner_cell(band.owner_kind, band.owner_id),
                "plane": band.plane,
                "title": band.title,
                "source": band.source_id,
                "note": _band_note(band),
            }
        )
    if filters.include_series and (not filters.shape or filters.shape == "series"):
        for series in projection.series:
            if not _matches(
                series.owner_kind, series.owner_id, series.plane,
                series.source_id, "series", filters,
            ):
                continue
            rows.append(
                {
                    "start": None,
                    "end": series.cadence_expr or "-",
                    "shape": "series",
                    "status": series.lifecycle_state,
                    "owner": _owner_cell(series.owner_kind, series.owner_id),
                    "plane": series.plane,
                    "title": series.title,
                    "source": series.source_id,
                    "note": (
                        "phase approximate"
                        if (series.extensions or {}).get("phase_approximate")
                        else "-"
                    ),
                }
            )
    far = datetime.max.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: (r["start"] is None, r["start"] or far, r["title"]))
    return rows


def _owner_cell(kind: str, owner_id: Optional[str]) -> str:
    return f"{kind}/{owner_id}" if owner_id else kind


def render_table(
    projection: Projection,
    filters: Optional[TableFilters] = None,
    now: Optional[datetime] = None,
) -> str:
    filters = filters or TableFilters()
    now = now or datetime.now(timezone.utc)
    rows = collect_rows(projection, filters, now=now)

    headers = ["START (UTC)", "END / CADENCE", "SHAPE", "STATUS", "OWNER", "PLANE", "TITLE", "SOURCE", "NOTE"]
    body = [
        [
            _fmt_time(r["start"]),
            r["end"],
            r["shape"],
            r["status"],
            r["owner"],
            r["plane"],
            r["title"],
            r["source"],
            r["note"],
        ]
        for r in rows
    ]
    widths = [
        max([len(headers[i])] + [len(row[i]) for row in body]) for i in range(len(headers))
    ]

    def line(cells: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    out: list[str] = []
    if filters.any_time_filter():
        lo = _fmt_time(filters.start_from) if filters.start_from else "earliest"
        hi = _fmt_time(filters.start_to) if filters.start_to else "latest"
        out.append(f"Window (UTC): {lo} .. {hi} — instances and bands by start time")
    out += [line(headers), line(["-" * w for w in widths])]
    out += [line(row) for row in body]
    if not body:
        out.append("(no rows match)")
    out.append("")
    out.append(f"Sources (freshness at {_fmt_time(now)} UTC):")
    for source in sorted(projection.sources, key=lambda s: s.source_id):
        out.append("  " + _source_line(source, now))
    return "\n".join(out) + "\n"


def _source_line(source, now: datetime) -> str:
    if source.last_synced_at is None:
        age_text = "never synced"
        stale = True
    else:
        age = (now - source.last_synced_at).total_seconds()
        age_text = f"synced {_fmt_time(source.last_synced_at)} (age {_fmt_age(age)})"
        threshold = source.sync_interval_sec or DEFAULT_STALE_AFTER_SEC
        stale = age > threshold
    data_text = (
        f"source data as of {_fmt_time(source.last_source_at)}"
        if source.last_source_at
        else "source data time unknown"
    )
    line = (
        f"{source.source_id} [{source.adapter}] plane {source.plane}: "
        f"{age_text}; {data_text}"
    )
    if stale:
        line += "  ** STALE **"
    return line
