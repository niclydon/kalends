"""YAML cadence calendar adapter.

Some fleets keep a hand-written commissioning calendar: a YAML file of
named recurring duties, each with an owner, a cadence word, a state,
and sometimes a slot group (at most one member of the group may run per
tick). That file is already config-as-data; this adapter projects it.

Expected shape: a list of rows, or a mapping holding a list of rows
under a key (``rows``, ``items``, ``entries``, ``calendar``, or a key
you name). Row fields, all renamable through ``field_map``:

* ``name`` (required), ``owner``, ``owner_kind``
* ``cadence``: a word (weekly, monthly, quarterly, ...), an interval
  string (``every 6h``), or a cron expression
* ``time_of_day`` (``"06:30"``) and ``weekday`` (``Friday``): together
  they give a cadence word a phase. The adapter turns them into the
  series' anchor: the most recent moment, at or before the sync time,
  matching that weekday and time in the row's timezone. A weekly word
  alone says *how often*; the anchor says *when*, without inventing a
  fuller RRULE than the source stated.
* ``anchor_at``: an explicit anchor timestamp, when the file carries
  one (a last or next occurrence); it beats the computed anchor
* ``mechanism``: how the row is dispatched (free text; ``parked`` here
  also parks the series)
* ``state``: active / parked / pending-build / disabled / retired / ...
* ``slot_group``: becomes a resource claim, copied onto every instance
  the projector makes from this series
* ``event_triggers``: noted on the series; triggers are not schedules
  and project no instances of their own
* ``plane``, ``timezone``: optional per-row overrides

State mapping: parked, paused, and pending states become lifecycle
``dormant`` (the series exists, projects nothing, and can wake);
disabled becomes ``enabled=False``; retired and deprecated pass
through; anything else is ``active``.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from ..cadence import _YAML_WORDS, cron_to_rrule, interval_to_rrule, yaml_cadence_to_rrule
from ..model import Series
from .base import AdapterResult, SourceAdapter, parse_timestamp

_DEFAULT_FIELDS = {
    "name": "name",
    "owner": "owner",
    "owner_kind": "owner_kind",
    "cadence": "cadence",
    "mechanism": "mechanism",
    "state": "state",
    "slot_group": "slot_group",
    "event_triggers": "event_triggers",
    "plane": "plane",
    "timezone": "timezone",
    "time_of_day": "time_of_day",
    "weekday": "weekday",
    "anchor_at": "anchor_at",
}

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _parse_time_of_day(value: Any) -> Optional[time]:
    if value is None:
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _build_anchor(
    tz_name: Optional[str],
    time_of_day: Any,
    weekday: Any,
    now: datetime,
) -> Optional[datetime]:
    """The most recent moment at or before ``now`` matching the row's
    weekday and time of day, in the row's timezone. None when the row
    declares neither: a bare cadence word has no honest anchor."""
    tod = _parse_time_of_day(time_of_day)
    target_weekday: Optional[int] = None
    if weekday is not None:
        target_weekday = _WEEKDAYS.get(str(weekday).strip().lower())
        if target_weekday is None:
            return None
    if tod is None and target_weekday is None:
        return None
    try:
        zone = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    local_now = now.astimezone(zone)
    tod = tod or time(0, 0)
    for back in range(0, 8):
        day = local_now.date() - timedelta(days=back)
        if target_weekday is not None and day.weekday() != target_weekday:
            continue
        candidate = datetime.combine(day, tod, tzinfo=zone)
        if candidate <= local_now:
            return candidate.astimezone(timezone.utc)
    return None
_ROW_KEYS = ("rows", "items", "entries", "calendar", "schedule")

_DORMANT_STATES = {"parked", "paused", "pending", "pending-build", "on-hold", "held"}
_DISABLED_STATES = {"disabled", "off", "suspended"}


def _find_rows(doc: Any, rows_key: Optional[str]) -> list[dict]:
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if isinstance(doc, dict):
        keys = (rows_key,) if rows_key else _ROW_KEYS
        for key in keys:
            value = doc.get(key) if key else None
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        for value in doc.values():
            if isinstance(value, list) and value and all(
                isinstance(r, dict) for r in value
            ):
                return list(value)
    return []


def _state_to_lifecycle(state: Optional[str], mechanism: Optional[str]):
    text = (state or "").strip().lower()
    mech = (mechanism or "").strip().lower()
    if text in _DORMANT_STATES or mech == "parked":
        return "dormant", True
    if text in _DISABLED_STATES:
        return "dormant", False
    if text == "retired":
        return "retired", False
    if text == "deprecated":
        return "deprecated", False
    return "active", True


def _dialect_for(cadence_text: Optional[str]) -> tuple[str, Optional[str]]:
    if cadence_text is None:
        return "none", None
    text = str(cadence_text).strip()
    if not text or text.lower() == "none":
        return "none", None
    if text.lower() in _YAML_WORDS:
        return "rrule", _YAML_WORDS[text.lower()]
    hit = interval_to_rrule(text)
    if hit:
        return "interval", hit
    hit = cron_to_rrule(text)
    if hit:
        return "cron", hit
    return "none", yaml_cadence_to_rrule(text)


class YamlCalendarAdapter(SourceAdapter):
    kind = "yaml-calendar"
    shapes = ("series",)

    def __init__(
        self,
        doc: Any,
        *,
        source_id: str = "yaml-calendar",
        plane: str = "host-a",
        default_owner_kind: str = "agent",
        field_map: Optional[dict[str, str]] = None,
        rows_key: Optional[str] = None,
        sync_interval_sec: Optional[int] = 86400,
        source_mtime: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ):
        super().__init__(source_id, plane, sync_interval_sec=sync_interval_sec)
        self.doc = doc
        self.default_owner_kind = default_owner_kind
        self.fields = {**_DEFAULT_FIELDS, **(field_map or {})}
        self.rows_key = rows_key
        self.source_mtime = source_mtime
        self.now = now

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "YamlCalendarAdapter":
        p = Path(path)
        doc = yaml.safe_load(p.read_text())
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        return cls(doc, source_mtime=mtime, **kwargs)

    def _get(self, row: dict, core_name: str):
        return row.get(self.fields[core_name])

    def collect(self) -> AdapterResult:
        now = self.now or datetime.now(timezone.utc)
        rows = _find_rows(self.doc, self.rows_key)
        series_out: list[Series] = []

        for row in rows:
            name = self._get(row, "name")
            if not name:
                continue
            name = str(name)
            cadence_raw = self._get(row, "cadence")
            dialect, rrule = _dialect_for(
                None if cadence_raw is None else str(cadence_raw)
            )
            lifecycle, enabled = _state_to_lifecycle(
                self._get(row, "state"), self._get(row, "mechanism")
            )
            claims: dict = {}
            slot_group = self._get(row, "slot_group")
            if slot_group:
                claims["slot_group"] = str(slot_group)
            triggers = self._get(row, "event_triggers") or []
            if isinstance(triggers, str):
                triggers = [triggers]
            anchor = parse_timestamp(self._get(row, "anchor_at")) or _build_anchor(
                self._get(row, "timezone") or "UTC",
                self._get(row, "time_of_day"),
                self._get(row, "weekday"),
                now,
            )
            series_out.append(
                Series(
                    series_id=f"{self.source_id}:{name}",
                    source_id=self.source_id,
                    source_native_id=name,
                    title=name,
                    owner_kind=self._get(row, "owner_kind") or self.default_owner_kind,
                    owner_id=self._get(row, "owner"),
                    plane=self._get(row, "plane") or self.plane,
                    cadence_dialect=dialect,
                    cadence_expr=None if cadence_raw is None else str(cadence_raw),
                    rrule=rrule,
                    timezone=self._get(row, "timezone") or "UTC",
                    enabled=enabled,
                    lifecycle_state=lifecycle,
                    synced_at=now,
                    anchor_at=anchor,
                    resource_claims=claims,
                    event_triggers=[str(t) for t in triggers],
                )
            )

        source = self.make_source(last_source_at=self.source_mtime, synced_at=now)
        return AdapterResult(
            source=source,
            series=series_out,
            stats={"rows": len(rows), "series": len(series_out)},
        )
