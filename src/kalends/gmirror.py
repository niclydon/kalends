"""Google Calendar mirror: a generic publish adapter.

Kalends is a projection over existing schedulers. This module mirrors
a curated subset of that projection one-way to a Google Calendar, so
people who live in Google Calendar can see selected fleet work
without Kalends becoming a Google Calendar client in its core, and
without any Google credential ever entering this package.

What mirrors is deliberately narrow (see ``docs/MIRROR.md``):

* bands owned by a configured owner whose interval overlaps the
  sync window, and
* one-off instances owned by a configured owner whose planned start
  lies in the sync window.

Recurring instances never mirror. Windows and actuals are not inputs
to this module at all: availability is not an event, and an actual is
history, not a plan.

The mirror is one-way and reconciling. Every event it creates
carries a private extended property, ``kalends_key``, holding the
:func:`stable_key` of the Kalends row it came from. Reconciliation
matches on that key alone, so the mirror owns exactly the events
carrying its key and never touches anything else on the calendar.
Edits made in Google Calendar never flow back; the next reconcile
overwrites a mirrored event whose summary or times have drifted.

This module is pure Python (standard library plus
``python-dateutil``, already a Kalends dependency). It defines the
:class:`GoogleClient` protocol a deployment must supply; the core
ships no Google SDK, no credential handling, and no transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse

__all__ = [
    "GoogleClient",
    "MirrorItem",
    "ReconcileAction",
    "ReconcileResult",
    "eligible_items",
    "event_body",
    "reconcile",
    "stable_key",
]

#: Fallback duration for a band or one-off with no recorded end.
DEFAULT_DURATION = timedelta(minutes=30)

_MIRROR_NOTE = (
    "Mirrored one-way from Kalends. "
    "Edits made here do not flow back to Kalends."
)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _get(row, name: str):
    """Read ``name`` from a model object or a plain dict row."""
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


# ---------------------------------------------------------------------------
# Stable keys
# ---------------------------------------------------------------------------

_KIND_PREFIXES = {
    "band": "band",
    "one-off": "oneoff",
    "oneoff": "oneoff",
    "one_off": "oneoff",
}


def stable_key(kind: str, native_id: str) -> str:
    """Deterministic key for a Kalends row.

    ``stable_key("band", "b-1")`` returns ``"kalends:band:b-1"`` and
    ``stable_key("one-off", "i-1")`` returns
    ``"kalends:oneoff:i-1"``. The key is stored in a Google event's
    ``extendedProperties.private.kalends_key`` and is the only join
    between a Kalends row and its mirrored event, so it must never
    depend on titles, times, or anything else that can change.
    """
    normalised = str(kind).strip().lower()
    try:
        prefix = _KIND_PREFIXES[normalised]
    except KeyError:
        raise ValueError(
            f"stable_key: unknown kind {kind!r} "
            f"(expected one of {sorted(_KIND_PREFIXES)})"
        ) from None
    return f"kalends:{prefix}:{native_id}"


# ---------------------------------------------------------------------------
# Desired set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MirrorItem:
    """A normalised desired event: one Kalends row to mirror."""

    key: str
    owner_id: Optional[str]
    title: str
    start: datetime
    end: datetime
    description: str = ""
    source_kind: str = "band"  # "band" or "one-off"

    def __post_init__(self) -> None:
        # Frozen dataclass: normalise naive timestamps to UTC via
        # object.__setattr__ so instants are always comparable.
        if self.start is not None and self.start.tzinfo is None:
            object.__setattr__(
                self, "start", self.start.replace(tzinfo=timezone.utc)
            )
        if self.end is not None and self.end.tzinfo is None:
            object.__setattr__(
                self, "end", self.end.replace(tzinfo=timezone.utc)
            )


def eligible_items(
    bands: Iterable,
    instances: Iterable,
    owners: Iterable[str],
    window_start: datetime,
    window_end: datetime,
) -> list[MirrorItem]:
    """The curated subset of Kalends rows to mirror.

    ``owners`` is the configured set of eligible ``owner_id`` values.
    ``bands`` and ``instances`` are core ``Band`` / ``Instance``
    objects (or dicts with the same field names). Windows and
    actuals are not inputs at all.

    Rules, exactly:

    * A band is eligible when its owner is in ``owners`` and its
      interval overlaps ``[window_start, window_end]``. The band
      interval is ``started_at`` to ``ended_at`` if ended, else
      ``lease_expires_at`` if set, else ``started_at + 30 minutes``.
      Overlap means ``start < window_end and end > window_start``,
      so a band ending exactly at ``window_start`` or starting
      exactly at ``window_end`` does not overlap.
    * An instance is eligible when its owner is in ``owners``, its
      ``shape`` is ``"one-off"``, and its ``planned_start`` lies in
      ``[window_start, window_end)``. Its end is ``planned_end`` if
      set, else ``planned_start + 30 minutes``.
    * Recurring instances (``shape == "instance"``) are never
      eligible, for any owner.
    * A row whose computed end is at or before its start is an empty
      interval (a hold released before its future start is the real
      case) and is never eligible: there is no block of time to show.

    The result is sorted by start, then key.
    """
    if isinstance(owners, str):
        owner_set = {owners}
    else:
        owner_set = set(owners or [])
    ws = _aware(window_start)
    we = _aware(window_end)
    assert ws is not None and we is not None

    items: list[MirrorItem] = []

    for band in bands or []:
        owner_id = _get(band, "owner_id")
        if owner_id not in owner_set:
            continue
        started = _aware(_get(band, "started_at"))
        if started is None:
            continue
        end = (
            _aware(_get(band, "ended_at"))
            or _aware(_get(band, "lease_expires_at"))
            or (started + DEFAULT_DURATION)
        )
        if end <= started:
            # Empty interval: e.g. a hold released before its future
            # start. There is no block of time to mirror.
            continue
        if not (started < we and end > ws):
            continue
        band_id = _get(band, "band_id") or _get(band, "source_native_id") or ""
        key = stable_key("band", str(band_id))
        title = _get(band, "title") or ""
        items.append(
            MirrorItem(
                key=key,
                owner_id=owner_id,
                title=title,
                start=started,
                end=end,
                description=f"Band: {title}",
                source_kind="band",
            )
        )

    for inst in instances or []:
        owner_id = _get(inst, "owner_id")
        if owner_id not in owner_set:
            continue
        if _get(inst, "shape") != "one-off":
            continue
        planned_start = _aware(_get(inst, "planned_start"))
        if planned_start is None:
            continue
        if not (ws <= planned_start < we):
            continue
        end = _aware(_get(inst, "planned_end")) or (
            planned_start + DEFAULT_DURATION
        )
        if end <= planned_start:
            continue
        instance_id = (
            _get(inst, "instance_id") or _get(inst, "source_native_id") or ""
        )
        key = stable_key("one-off", str(instance_id))
        title = _get(inst, "title") or ""
        items.append(
            MirrorItem(
                key=key,
                owner_id=owner_id,
                title=title,
                start=planned_start,
                end=end,
                description=f"One-off: {title}",
                source_kind="one-off",
            )
        )

    items.sort(key=lambda item: (item.start, item.key))
    return items


# ---------------------------------------------------------------------------
# Google event bodies
# ---------------------------------------------------------------------------


def event_body(item: MirrorItem, timezone_name: str = "UTC") -> dict:
    """Build a Google Calendar API event resource for ``item``.

    ``start`` / ``end`` are RFC 3339 ``dateTime`` values with an
    offset, converted from the item's instants into ``timezone_name``,
    plus a ``timeZone`` field naming that zone: the shape the Google
    Calendar API expects for a timed event. The body carries the
    item's stable key, owner, and kind in
    ``extendedProperties.private`` and never carries attendees.
    """
    tz_name = timezone_name or "UTC"
    tz = ZoneInfo(tz_name)
    start_local = _aware(item.start).astimezone(tz)  # type: ignore[union-attr]
    end_local = _aware(item.end).astimezone(tz)  # type: ignore[union-attr]
    if item.description:
        description = f"{item.description}\n\n{_MIRROR_NOTE}"
    else:
        description = _MIRROR_NOTE
    return {
        "summary": item.title,
        "description": description,
        "start": {"dateTime": start_local.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_local.isoformat(), "timeZone": tz_name},
        "extendedProperties": {
            "private": {
                "kalends_key": item.key,
                "kalends_owner": item.owner_id or "",
                "kalends_kind": item.source_kind,
            }
        },
    }


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GoogleClient(Protocol):
    """The Google Calendar transport a deployment supplies.

    Implementations wrap the Google Calendar API with the adopter's
    own credentials, outside this package. ``time_min`` / ``time_max``
    are RFC 3339 timestamps bounding the listing, as the API takes
    them. Event dicts are API event resources; each carries at least
    ``id``, ``summary``, ``start``, ``end``, and (for mirrored
    events) ``extendedProperties``.
    """

    def list_events(
        self, calendar_id: str, time_min: str, time_max: str
    ) -> list[dict]:
        ...

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        ...

    def patch_event(
        self, calendar_id: str, event_id: str, body: dict
    ) -> dict:
        ...

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        ...


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass
class ReconcileAction:
    """One reconciliation decision."""

    action: str  # "create", "update", "delete", or "unchanged"
    key: str
    event_id: Optional[str] = None
    title: Optional[str] = None


@dataclass
class ReconcileResult:
    """Counts and per-item actions from one reconcile pass."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    actions: list[ReconcileAction] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "actions": [
                {
                    "action": a.action,
                    "key": a.key,
                    "event_id": a.event_id,
                    "title": a.title,
                }
                for a in self.actions
            ],
        }


def _existing_key(event: dict) -> Optional[str]:
    if not isinstance(event, dict):
        return None
    props = event.get("extendedProperties") or {}
    private = props.get("private") or {}
    key = private.get("kalends_key")
    return str(key) if key else None


def _parse_event_time(value) -> Optional[datetime]:
    """Parse a Google event ``start`` / ``end`` value to an instant.

    Accepts the API shape (``{"dateTime": ..., "timeZone": ...}``),
    an all-day ``{"date": ...}`` (read as midnight UTC), a bare
    RFC 3339 string, or a datetime.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, dict):
        raw = value.get("dateTime") or value.get("date")
        if raw is None:
            return None
        parsed = isoparse(str(raw))
        return _aware(parsed)
    parsed = isoparse(str(value))
    return _aware(parsed)


def reconcile(
    desired: list[MirrorItem],
    existing: list[dict],
    client: Optional[GoogleClient],
    calendar_id: str,
    dry_run: bool = False,
    timezone_name: str = "UTC",
) -> ReconcileResult:
    """Reconcile ``desired`` against the calendar's ``existing`` events.

    Matching is by ``extendedProperties.private.kalends_key`` only:

    * a desired item with no matching existing event is inserted;
    * a matched event whose summary, start, or end differs from the
      desired item (instants compared, so the same moment written
      in another offset is not a difference) is patched;
    * an existing event carrying a ``kalends_key`` with no desired
      counterpart is deleted;
    * existing events without a ``kalends_key`` are ignored: the
      mirror does not own them.

    With ``dry_run=True`` the same decisions and counts are returned
    and the client receives zero write calls (``client`` may be
    ``None`` in that case). Without ``dry_run``, ``client`` is
    required.
    """
    if not dry_run and client is None:
        raise ValueError("reconcile: a client is required unless dry_run")

    result = ReconcileResult()

    existing_by_key: dict[str, dict] = {}
    duplicate_events: list[dict] = []
    for event in existing or []:
        key = _existing_key(event)
        if key is None:
            continue
        if key in existing_by_key:
            duplicate_events.append(event)
        else:
            existing_by_key[key] = event

    desired_keys: set[str] = set()

    for item in desired or []:
        desired_keys.add(item.key)
        match = existing_by_key.get(item.key)
        if match is None:
            result.created += 1
            result.actions.append(
                ReconcileAction(
                    action="create", key=item.key, title=item.title
                )
            )
            if not dry_run:
                assert client is not None
                client.insert_event(
                    calendar_id, event_body(item, timezone_name)
                )
            continue
        existing_start = _parse_event_time(match.get("start"))
        existing_end = _parse_event_time(match.get("end"))
        differs = (
            match.get("summary") != item.title
            or existing_start != _aware(item.start)
            or existing_end != _aware(item.end)
        )
        if differs:
            result.updated += 1
            result.actions.append(
                ReconcileAction(
                    action="update",
                    key=item.key,
                    event_id=match.get("id"),
                    title=item.title,
                )
            )
            if not dry_run:
                assert client is not None
                client.patch_event(
                    calendar_id,
                    match.get("id"),
                    event_body(item, timezone_name),
                )
        else:
            result.unchanged += 1
            result.actions.append(
                ReconcileAction(
                    action="unchanged",
                    key=item.key,
                    event_id=match.get("id"),
                    title=item.title,
                )
            )

    stale = [ev for key, ev in existing_by_key.items() if key not in desired_keys]
    for event in stale + duplicate_events:
        key = _existing_key(event) or ""
        result.deleted += 1
        result.actions.append(
            ReconcileAction(
                action="delete", key=key, event_id=event.get("id"),
                title=event.get("summary"),
            )
        )
        if not dry_run:
            assert client is not None
            client.delete_event(calendar_id, event.get("id"))

    return result
