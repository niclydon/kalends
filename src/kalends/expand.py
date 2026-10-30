"""RRULE expansion over a horizon.

Thin wrapper over ``dateutil.rrule.rrulestr``. Occurrences are generated
in the series' own timezone (so a 09:07 local cadence stays 09:07 local
across daylight-saving changes) and returned as UTC-aware datetimes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr


def _zone(name: Optional[str]) -> ZoneInfo:
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def expand_rrule(
    rrule_text: str,
    dtstart: datetime,
    horizon_start: datetime,
    horizon_end: datetime,
    tz_name: Optional[str] = "UTC",
) -> list[datetime]:
    """All occurrences of ``rrule_text`` inside [horizon_start, horizon_end].

    ``dtstart`` anchors the rule (its phase, for interval rules) and is
    interpreted in the series timezone. Returns UTC-aware datetimes,
    sorted ascending. An unparseable rule yields an empty list: a broken
    cadence shows as no instances, never as invented ones.
    """
    zone = _zone(tz_name)
    if dtstart.tzinfo is None:
        dtstart = dtstart.replace(tzinfo=timezone.utc)
    local_start = dtstart.astimezone(zone)
    try:
        rule = rrulestr(rrule_text, dtstart=local_start)
        found = rule.between(
            horizon_start.astimezone(zone),
            horizon_end.astimezone(zone),
            inc=True,
        )
    except (ValueError, TypeError):
        return []
    return sorted(dt.astimezone(timezone.utc) for dt in found)
