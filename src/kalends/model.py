"""Core data model for Kalends.

The dataclasses in this module mirror the Postgres schema in
``schema/001_kalends.sql`` (design doc section 5). Two design rules shape
everything here:

* Definitions and instances are separate objects. A :class:`Series` is a
  definition; an :class:`Instance` is one materialised occurrence of it.
* Every row carries provenance and freshness: which source produced it,
  the source-native identifier, when the source last changed the row, and
  when the calendar last synced it. Stale data is shown as stale, never
  presented as current.

Timestamps are timezone-aware datetimes throughout. ``to_dict`` renders
them as ISO 8601 strings so a whole projection can be dumped to JSON and
read back losslessly with ``from_dict``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Optional

from dateutil.parser import isoparse

OWNER_KINDS = ("agent", "worker", "system", "human-facing")
CADENCE_DIALECTS = (
    "cron",
    "systemd-calendar",
    "systemd-relative",
    "interval",
    "rrule",
    "self-chain",
    "none",
)
LIFECYCLE_STATES = ("active", "retired", "deprecated", "dormant")
INSTANCE_STATUSES = (
    "scheduled",
    "dispatched",
    "in-progress",
    "completed",
    "failed",
    "timed-out",
    "skipped",
    "forecast",
)
BAND_STATUSES = ("active", "blocked", "idle", "done")
BAND_END_REASONS = (
    "completed",
    "released",
    "expired",
    "abandoned",
    "failed",
    "observed-stopped",
)
WINDOW_KINDS = ("available", "maintenance", "quiet", "hold")
OVERLAP_POLICIES = ("skip", "buffer", "cancel-other", "allow-all")


def _ts_out(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _ts_in(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    return isoparse(str(value))


_TIMESTAMP_FIELDS = {
    "last_synced_at",
    "last_source_at",
    "source_updated_at",
    "synced_at",
    "planned_start",
    "planned_end",
    "actual_start",
    "actual_end",
    "started_at",
    "last_heartbeat_at",
    "lease_expires_at",
    "ended_at",
    "starts_at",
    "ends_at",
    "fired_at",
    "would_fire_at",
}


def _roundtrip(cls, data: dict):
    """Build a dataclass of ``cls`` from a plain dict, parsing timestamps."""
    known = {f.name for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key not in known:
            continue
        if key in _TIMESTAMP_FIELDS:
            value = _ts_in(value)
        kwargs[key] = value
    return cls(**kwargs)


@dataclass
class Source:
    """An adapter registry row: one upstream system the calendar reads."""

    source_id: str
    adapter: str
    plane: str
    enabled: bool = True
    sync_interval_sec: Optional[int] = None
    last_synced_at: Optional[datetime] = None
    last_source_at: Optional[datetime] = None
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "adapter": self.adapter,
            "plane": self.plane,
            "enabled": self.enabled,
            "sync_interval_sec": self.sync_interval_sec,
            "last_synced_at": _ts_out(self.last_synced_at),
            "last_source_at": _ts_out(self.last_source_at),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Source":
        return _roundtrip(cls, data)


@dataclass
class Series:
    """A recurring definition. Never rendered directly; the projector
    expands it into instances over the horizon."""

    series_id: str
    source_id: str
    source_native_id: str
    title: str
    owner_kind: str = "system"
    owner_id: Optional[str] = None
    plane: str = ""
    cadence_dialect: str = "none"
    cadence_expr: Optional[str] = None
    rrule: Optional[str] = None
    timezone: str = "UTC"
    expected_duration_sec: Optional[int] = None
    overlap_policy: str = "allow-all"
    enabled: bool = True
    lifecycle_state: str = "active"
    source_updated_at: Optional[datetime] = None
    synced_at: Optional[datetime] = None
    # Adapter extension metadata: default resource claims the projector
    # copies onto every instance of this series, and event triggers a
    # source recorded for the series. Carried in JSON dumps; not columns
    # in the v1 SQL schema (claims live on instances there).
    resource_claims: dict = field(default_factory=dict)
    event_triggers: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "series_id": self.series_id,
            "source_id": self.source_id,
            "source_native_id": self.source_native_id,
            "title": self.title,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "plane": self.plane,
            "cadence_dialect": self.cadence_dialect,
            "cadence_expr": self.cadence_expr,
            "rrule": self.rrule,
            "timezone": self.timezone,
            "expected_duration_sec": self.expected_duration_sec,
            "overlap_policy": self.overlap_policy,
            "enabled": self.enabled,
            "lifecycle_state": self.lifecycle_state,
            "source_updated_at": _ts_out(self.source_updated_at),
            "synced_at": _ts_out(self.synced_at),
            "resource_claims": dict(self.resource_claims),
            "event_triggers": list(self.event_triggers),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Series":
        return _roundtrip(cls, data)


@dataclass
class Instance:
    """One occurrence of a series, or a standalone planned run."""

    instance_id: str
    source_id: str
    source_native_id: str
    title: str
    series_id: Optional[str] = None
    owner_kind: str = "system"
    owner_id: Optional[str] = None
    plane: str = ""
    shape: str = "instance"  # "instance" or "one-off"
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    actual_start: Optional[datetime] = None
    actual_end: Optional[datetime] = None
    status: str = "scheduled"
    resource_claims: dict = field(default_factory=dict)
    source_updated_at: Optional[datetime] = None
    synced_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "series_id": self.series_id,
            "source_id": self.source_id,
            "source_native_id": self.source_native_id,
            "title": self.title,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "plane": self.plane,
            "shape": self.shape,
            "planned_start": _ts_out(self.planned_start),
            "planned_end": _ts_out(self.planned_end),
            "actual_start": _ts_out(self.actual_start),
            "actual_end": _ts_out(self.actual_end),
            "status": self.status,
            "resource_claims": dict(self.resource_claims),
            "source_updated_at": _ts_out(self.source_updated_at),
            "synced_at": _ts_out(self.synced_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Instance":
        return _roundtrip(cls, data)


@dataclass
class Band:
    """Long-running work: a start and a heartbeat, no scheduled end.

    A band ends when its heartbeat stops, its lease expires, or a poller
    stops seeing the thing (``end_reason`` records which)."""

    band_id: str
    source_id: str
    source_native_id: str
    title: str
    owner_kind: str = "system"
    owner_id: Optional[str] = None
    plane: str = ""
    started_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    end_reason: Optional[str] = None
    status: str = "active"
    resource_claims: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "band_id": self.band_id,
            "source_id": self.source_id,
            "source_native_id": self.source_native_id,
            "title": self.title,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "plane": self.plane,
            "started_at": _ts_out(self.started_at),
            "last_heartbeat_at": _ts_out(self.last_heartbeat_at),
            "lease_expires_at": _ts_out(self.lease_expires_at),
            "ended_at": _ts_out(self.ended_at),
            "end_reason": self.end_reason,
            "status": self.status,
            "resource_claims": dict(self.resource_claims),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Band":
        return _roundtrip(cls, data)


@dataclass
class Window:
    """An availability window, or its inverse (maintenance, quiet hours,
    a self-declared hold), modelled after RFC 7953 VAVAILABILITY."""

    window_id: str
    owner_kind: str
    owner_id: Optional[str] = None
    plane: str = ""
    kind: str = "available"
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    rrule: Optional[str] = None
    priority: int = 0
    source_id: Optional[str] = None
    source_native_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "plane": self.plane,
            "kind": self.kind,
            "starts_at": _ts_out(self.starts_at),
            "ends_at": _ts_out(self.ends_at),
            "rrule": self.rrule,
            "priority": self.priority,
            "source_id": self.source_id,
            "source_native_id": self.source_native_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Window":
        return _roundtrip(cls, data)


@dataclass
class Actual:
    """One observed firing, from a source whose fire log is separate.

    Actuals are joined to planned instances at projection time; they are
    never copied wholesale into the calendar (a busy fire log is millions
    of rows)."""

    source_id: str
    source_native_id: str  # the source-native run id
    fired_at: datetime
    series_id: Optional[str] = None
    would_fire_at: Optional[datetime] = None
    observed_only: bool = False
    status: str = "completed"  # "completed" or "failed"

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "source_native_id": self.source_native_id,
            "series_id": self.series_id,
            "fired_at": _ts_out(self.fired_at),
            "would_fire_at": _ts_out(self.would_fire_at),
            "observed_only": self.observed_only,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Actual":
        return _roundtrip(cls, data)
