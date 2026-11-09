"""kalends-mcp: the Kalends MCP server.

Every Kalends CRUD operation goes through these tools. Adapters and
projection lanes write with the batch tools; people and agents read
with the list tools; work no scheduler started goes on the calendar
through the hold tools. There is no other write path: if a lane
writes the ``calendar_*`` tables directly, it is doing it wrong.

The code is split so the tool logic is testable without a transport
or a database:

* :class:`KalendsService` holds all tool logic (validation, holds
  lease semantics, batch shapes) over any
  :class:`kalends.store.KalendsStore`.
* :func:`build_server` binds a service to an MCP server object
  (Python MCP SDK, ``mcp>=2.3,<3``) as thin one-line tools.
* :func:`main` is the ``kalends-mcp`` console entry point: it builds
  a :class:`kalends.store.PostgresStore` from ``KALENDS_DSN`` (or
  ``DATABASE_URL``) and serves stdio by default, or streamable HTTP
  when ``TRANSPORT=http``.

HTTP mode binds ``BIND_HOST`` / ``PORT`` (defaults ``127.0.0.1`` and
``7899``). When ``ORIGIN_SHARED_SECRET`` is set, every HTTP request
except ``GET /health`` must carry it as the ``x-origin-secret``
header; when it is unset in HTTP mode the server still runs (an
adopter may front it differently) but logs a warning.

Hold tools implement the lease contract from ``docs/HOLDS.md`` over
``calendar_bands`` rows with ``source_id`` ``holds``: a hold is alive
while its lease is in the future; heartbeat renews it; an expired
lease reads as closed with ``end_reason`` ``expired`` and
``ended_at`` at the lease expiry, persisted on first observation;
heartbeating a closed hold is an error, never a resurrection.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .holds import DEFAULT_LEASE_SECONDS, HOLDS_SOURCE_ID
from .model import (
    BAND_END_REASONS,
    BAND_STATUSES,
    CADENCE_DIALECTS,
    INSTANCE_STATUSES,
    LIFECYCLE_STATES,
    OWNER_KINDS,
    WINDOW_KINDS,
    Band,
    Instance,
    Series,
    Source,
    Window,
)
from .store import KalendsError, KalendsStore, PostgresStore, actual_to_dict, normalize_actual, parse_ts

log = logging.getLogger("kalends.mcp_server")

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 7899
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000

_RELEASE_REASONS = ("released", "completed", "abandoned", "failed")

# Allowed input keys per entity: the model dataclass fields. Anything
# else is a caller bug and is rejected rather than silently dropped.
_SOURCE_KEYS = {"source_id", "adapter", "plane", "enabled", "sync_interval_sec",
                "last_synced_at", "last_source_at", "notes"}
_SERIES_KEYS = {"series_id", "source_id", "source_native_id", "title", "owner_kind",
                "owner_id", "plane", "cadence_dialect", "cadence_expr", "rrule",
                "timezone", "expected_duration_sec", "overlap_policy", "enabled",
                "lifecycle_state", "source_updated_at", "synced_at", "anchor_at",
                "resource_claims", "event_triggers", "extensions"}
_INSTANCE_KEYS = {"instance_id", "series_id", "source_id", "source_native_id", "title",
                  "owner_kind", "owner_id", "plane", "shape", "planned_start",
                  "planned_end", "actual_start", "actual_end", "status",
                  "resource_claims", "source_updated_at", "synced_at", "extensions"}
_BAND_KEYS = {"band_id", "source_id", "source_native_id", "title", "owner_kind",
              "owner_id", "plane", "started_at", "last_heartbeat_at",
              "lease_expires_at", "ended_at", "end_reason", "status",
              "resource_claims", "extensions"}
_WINDOW_KEYS = {"window_id", "owner_kind", "owner_id", "plane", "kind", "starts_at",
                "ends_at", "rrule", "priority", "source_id", "source_native_id"}
_ACTUAL_KEYS = {"actual_id", "source_id", "source_native_id", "series_id",
                "instance_id", "fired_at", "would_fire_at", "observed_only",
                "status", "kind", "is_empty", "extensions"}


def _check_keys(data: Any, allowed: set, entity: str) -> dict:
    if not isinstance(data, dict):
        raise KalendsError(f"{entity}: expected an object, got {type(data).__name__}")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise KalendsError(f"{entity}: unknown field(s): {', '.join(unknown)}")
    return data


def _require(data: dict, entity: str, *fields: str) -> None:
    for field in fields:
        if data.get(field) is None or data.get(field) == "":
            raise KalendsError(f"{entity}: {field} is required")


def _enum(data: dict, entity: str, field: str, values: tuple) -> None:
    value = data.get(field)
    if value is not None and value not in values:
        raise KalendsError(
            f"{entity}: {field} must be one of {list(values)}, got {value!r}")


def _claims(data: dict, entity: str) -> None:
    claims = data.get("resource_claims")
    if claims is not None and not isinstance(claims, dict):
        raise KalendsError(f"{entity}: resource_claims must be an object")


def _aware_all(obj: Any) -> Any:
    """Normalise every datetime field on a dataclass to aware UTC."""
    for name, value in vars(obj).items():
        if isinstance(value, datetime) and value.tzinfo is None:
            setattr(obj, name, value.replace(tzinfo=timezone.utc))
    return obj


def _entity(data: Any, cls, entity: str, allowed: set, required: tuple):
    _check_keys(data, allowed, entity)
    _require(data, entity, *required)
    _claims(data, entity)
    try:
        obj = cls.from_dict(data)
    except KalendsError:
        raise
    except Exception as exc:  # dateutil / dataclass conversion errors
        raise KalendsError(f"{entity}: {exc}") from exc
    return _aware_all(obj)


def _page_args(limit: Optional[int], offset: Optional[int]) -> tuple[int, int]:
    if limit is None:
        limit = DEFAULT_LIST_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise KalendsError("limit must be a positive integer")
    if offset is None:
        offset = 0
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise KalendsError("offset must be a non-negative integer")
    return min(limit, MAX_LIST_LIMIT), offset


class KalendsService:
    """All kalends-mcp tool logic, over a store.

    ``now_fn`` is injectable so hold lease behaviour is testable
    against a frozen clock.
    """

    def __init__(self, store: KalendsStore,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.store = store
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self._now_fn()
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    # -- reads -----------------------------------------------------------

    def list_sources(self) -> dict:
        rows = self.store.list_sources()
        return {"sources": [r.to_dict() for r in rows], "count": len(rows)}

    def list_series(self, source_id=None, owner_id=None, owner_kind=None,
                    plane=None, enabled=None, limit=None, offset=None) -> dict:
        if owner_kind is not None and owner_kind not in OWNER_KINDS:
            raise KalendsError(f"owner_kind must be one of {list(OWNER_KINDS)}")
        limit, offset = _page_args(limit, offset)
        rows = self.store.list_series(source_id=source_id, owner_id=owner_id,
                                      owner_kind=owner_kind, plane=plane,
                                      enabled=enabled, limit=limit, offset=offset)
        return {"series": [r.to_dict() for r in rows], "count": len(rows)}

    def get_series(self, series_id: str) -> dict:
        if not series_id:
            raise KalendsError("series_id is required")
        row = self.store.get_series(series_id)
        if row is None:
            raise KalendsError(f"unknown series: {series_id}")
        return row.to_dict()

    def list_instances(self, series_id=None, source_id=None, from_ts=None,
                       to_ts=None, status=None, owner_id=None, owner_kind=None,
                       shape=None, limit=None, offset=None) -> dict:
        if status is not None and status not in INSTANCE_STATUSES:
            raise KalendsError(f"status must be one of {list(INSTANCE_STATUSES)}")
        limit, offset = _page_args(limit, offset)
        rows = self.store.list_instances(
            series_id=series_id, source_id=source_id,
            from_ts=parse_ts(from_ts, "from"), to_ts=parse_ts(to_ts, "to"),
            status=status, owner_id=owner_id, owner_kind=owner_kind,
            shape=shape, limit=limit, offset=offset)
        return {"instances": [r.to_dict() for r in rows], "count": len(rows)}

    def list_bands(self, owner_id=None, owner_kind=None, plane=None, status=None, source_id=None,
                   active_at=None, limit=None, offset=None) -> dict:
        if status is not None and status not in BAND_STATUSES:
            raise KalendsError(f"status must be one of {list(BAND_STATUSES)}")
        limit, offset = _page_args(limit, offset)
        rows = self.store.list_bands(
            owner_id=owner_id, owner_kind=owner_kind, plane=plane, status=status, source_id=source_id,
            active_at=parse_ts(active_at, "active_at"), limit=limit, offset=offset)
        return {"bands": [r.to_dict() for r in rows], "count": len(rows)}

    def list_windows(self, owner_id=None, from_ts=None, to_ts=None,
                     limit=None) -> dict:
        limit, _offset = _page_args(limit, 0)
        rows = self.store.list_windows(
            owner_id=owner_id, from_ts=parse_ts(from_ts, "from"),
            to_ts=parse_ts(to_ts, "to"), limit=limit)
        return {"windows": [r.to_dict() for r in rows], "count": len(rows)}

    def list_actuals(self, series_id=None, source_id=None, from_ts=None,
                     to_ts=None, kind=None, limit=None, offset=None) -> dict:
        if kind is not None and kind not in ("firing", "point"):
            raise KalendsError("kind must be 'firing' or 'point'")
        limit, offset = _page_args(limit, offset)
        rows = self.store.list_actuals(
            series_id=series_id, source_id=source_id,
            from_ts=parse_ts(from_ts, "from"), to_ts=parse_ts(to_ts, "to"),
            kind=kind, limit=limit, offset=offset)
        return {"actuals": [actual_to_dict(r) for r in rows], "count": len(rows)}

    # -- single writes -----------------------------------------------------

    def upsert_source(self, source: dict) -> dict:
        obj = _entity(source, Source, "source", _SOURCE_KEYS,
                      ("source_id", "adapter", "plane"))
        return self.store.upsert_source(obj).to_dict()

    def upsert_series(self, series: dict) -> dict:
        _check_keys(series, _SERIES_KEYS, "series")
        _enum(series, "series", "owner_kind", OWNER_KINDS)
        _enum(series, "series", "cadence_dialect", CADENCE_DIALECTS)
        _enum(series, "series", "lifecycle_state", LIFECYCLE_STATES)
        obj = _entity(series, Series, "series", _SERIES_KEYS,
                      ("series_id", "source_id", "source_native_id", "title"))
        return self.store.upsert_series(obj).to_dict()

    def upsert_instance(self, instance: dict) -> dict:
        _check_keys(instance, _INSTANCE_KEYS, "instance")
        _enum(instance, "instance", "owner_kind", OWNER_KINDS)
        _enum(instance, "instance", "status", INSTANCE_STATUSES)
        if instance.get("shape") is not None and instance["shape"] not in ("instance", "one-off"):
            raise KalendsError("instance: shape must be 'instance' or 'one-off'")
        obj = _entity(instance, Instance, "instance", _INSTANCE_KEYS,
                      ("instance_id", "source_id", "source_native_id", "title"))
        return self.store.upsert_instance(obj).to_dict()

    def upsert_band(self, band: dict) -> dict:
        _check_keys(band, _BAND_KEYS, "band")
        _enum(band, "band", "owner_kind", OWNER_KINDS)
        _enum(band, "band", "status", BAND_STATUSES)
        _enum(band, "band", "end_reason", BAND_END_REASONS)
        obj = _entity(band, Band, "band", _BAND_KEYS,
                      ("band_id", "source_id", "source_native_id", "title"))
        return self.store.upsert_band(obj).to_dict()

    def upsert_window(self, window: dict) -> dict:
        _check_keys(window, _WINDOW_KEYS, "window")
        _enum(window, "window", "owner_kind", OWNER_KINDS)
        _enum(window, "window", "kind", WINDOW_KINDS)
        obj = _entity(window, Window, "window", _WINDOW_KEYS,
                      ("window_id", "owner_kind"))
        return self.store.upsert_window(obj).to_dict()

    def upsert_actual(self, actual: dict) -> dict:
        _check_keys(actual, _ACTUAL_KEYS, "actual")
        _enum(actual, "actual", "kind", ("firing", "point"))
        row = normalize_actual({k: v for k, v in actual.items() if k != "extensions"})
        return actual_to_dict(self.store.upsert_actual(row))

    # -- deletes -------------------------------------------------------------

    def delete_series(self, series_id: str) -> dict:
        if not series_id:
            raise KalendsError("series_id is required")
        return {"deleted": self.store.delete_series(series_id)}

    def delete_instance(self, instance_id: str) -> dict:
        if not instance_id:
            raise KalendsError("instance_id is required")
        return {"deleted": self.store.delete_instance(instance_id)}

    def delete_band(self, band_id: str) -> dict:
        if not band_id:
            raise KalendsError("band_id is required")
        return {"deleted": self.store.delete_band(band_id)}

    def delete_window(self, window_id: str) -> dict:
        if not window_id:
            raise KalendsError("window_id is required")
        return {"deleted": self.store.delete_window(window_id)}

    def delete_actual(self, actual_id: int) -> dict:
        try:
            actual_id = int(actual_id)
        except (TypeError, ValueError) as exc:
            raise KalendsError("actual_id must be an integer") from exc
        return {"deleted": self.store.delete_actual(actual_id)}

    # -- batch writes ----------------------------------------------------------

    @staticmethod
    def _batch_list(value: Any, entity: str) -> list:
        if not isinstance(value, list):
            raise KalendsError(f"{entity}: expected a list of objects")
        return value

    def batch_upsert_series(self, series: list) -> dict:
        rows = []
        for item in self._batch_list(series, "series"):
            _check_keys(item, _SERIES_KEYS, "series")
            _enum(item, "series", "owner_kind", OWNER_KINDS)
            _enum(item, "series", "cadence_dialect", CADENCE_DIALECTS)
            _enum(item, "series", "lifecycle_state", LIFECYCLE_STATES)
            rows.append(_entity(item, Series, "series", _SERIES_KEYS,
                                ("series_id", "source_id", "source_native_id", "title")))
        return {"upserted": self.store.batch_upsert_series(rows)}

    def batch_upsert_instances(self, instances: list) -> dict:
        rows = []
        for item in self._batch_list(instances, "instances"):
            _check_keys(item, _INSTANCE_KEYS, "instance")
            _enum(item, "instance", "owner_kind", OWNER_KINDS)
            _enum(item, "instance", "status", INSTANCE_STATUSES)
            rows.append(_entity(item, Instance, "instance", _INSTANCE_KEYS,
                                ("instance_id", "source_id", "source_native_id", "title")))
        return {"upserted": self.store.batch_upsert_instances(rows)}

    def batch_replace_instances(self, source_id: str, horizon_start: Any,
                                horizon_end: Any, instances: list) -> dict:
        if not source_id:
            raise KalendsError("source_id is required")
        start = parse_ts(horizon_start, "horizon_start")
        end = parse_ts(horizon_end, "horizon_end")
        if start is None or end is None:
            raise KalendsError("horizon_start and horizon_end are required")
        if end < start:
            raise KalendsError("horizon_end must not be before horizon_start")
        rows = []
        for item in self._batch_list(instances, "instances"):
            _check_keys(item, _INSTANCE_KEYS, "instance")
            _enum(item, "instance", "owner_kind", OWNER_KINDS)
            _enum(item, "instance", "status", INSTANCE_STATUSES)
            obj = _entity(item, Instance, "instance", _INSTANCE_KEYS,
                          ("instance_id", "source_id", "source_native_id", "title"))
            if obj.source_id != source_id:
                raise KalendsError(
                    f"instance {obj.instance_id}: source_id {obj.source_id!r} "
                    f"does not match replace scope {source_id!r}")
            rows.append(obj)
        deleted, inserted = self.store.batch_replace_instances(source_id, start, end, rows)
        return {"deleted": deleted, "inserted": inserted}

    def batch_upsert_bands(self, bands: list) -> dict:
        rows = []
        for item in self._batch_list(bands, "bands"):
            _check_keys(item, _BAND_KEYS, "band")
            _enum(item, "band", "owner_kind", OWNER_KINDS)
            _enum(item, "band", "status", BAND_STATUSES)
            _enum(item, "band", "end_reason", BAND_END_REASONS)
            rows.append(_entity(item, Band, "band", _BAND_KEYS,
                                ("band_id", "source_id", "source_native_id", "title")))
        return {"upserted": self.store.batch_upsert_bands(rows)}

    def batch_upsert_actuals(self, actuals: list) -> dict:
        rows = []
        for item in self._batch_list(actuals, "actuals"):
            _check_keys(item, _ACTUAL_KEYS, "actual")
            _enum(item, "actual", "kind", ("firing", "point"))
            rows.append(normalize_actual(
                {k: v for k, v in item.items() if k != "extensions"}))
        return {"upserted": self.store.batch_upsert_actuals(rows)}

    def batch_replace_actuals(self, source_id: str, window_start: Any,
                              window_end: Any, actuals: list) -> dict:
        if not source_id:
            raise KalendsError("source_id is required")
        start = parse_ts(window_start, "window_start")
        end = parse_ts(window_end, "window_end")
        if start is None or end is None:
            raise KalendsError("window_start and window_end are required")
        if end < start:
            raise KalendsError("window_end must not be before window_start")
        rows = []
        for item in self._batch_list(actuals, "actuals"):
            _check_keys(item, _ACTUAL_KEYS, "actual")
            _enum(item, "actual", "kind", ("firing", "point"))
            row = normalize_actual({k: v for k, v in item.items() if k != "extensions"})
            if row["source_id"] != source_id:
                raise KalendsError(
                    f"actual {row['source_native_id']!r}: source_id "
                    f"{row['source_id']!r} does not match replace scope {source_id!r}")
            rows.append(row)
        deleted, inserted = self.store.batch_replace_actuals(source_id, start, end, rows)
        return {"deleted": deleted, "inserted": inserted}

    def mark_source_synced(self, source_id: str, synced_at: Any = None,
                           status: Optional[str] = None,
                           note: Optional[str] = None) -> dict:
        if not source_id:
            raise KalendsError("source_id is required")
        when = parse_ts(synced_at, "synced_at") or self._now()
        return self.store.mark_source_synced(source_id, when, status, note).to_dict()

    # -- holds ---------------------------------------------------------------

    def _materialise_hold(self, band: Band, now: datetime) -> Band:
        """Persist the expired closure the first time it is observed."""
        if band.ended_at is None and band.status != "done" \
                and band.lease_expires_at is not None and band.lease_expires_at <= now:
            band.status = "done"
            band.end_reason = "expired"
            band.ended_at = band.lease_expires_at
            self.store.upsert_band(band)
        return band

    def _holds(self, now: datetime) -> list[Band]:
        rows = self.store.list_bands(source_id=HOLDS_SOURCE_ID, limit=MAX_LIST_LIMIT, offset=0)
        return [self._materialise_hold(b, now) for b in rows]

    def _find_hold(self, band_id: str, now: datetime) -> Band:
        band = self.store.get_band(band_id)
        if band is None or band.source_id != HOLDS_SOURCE_ID:
            raise KalendsError(f"unknown hold: {band_id}")
        return self._materialise_hold(band, now)

    def _ensure_holds_source(self, plane: str) -> None:
        if all(s.source_id != HOLDS_SOURCE_ID for s in self.store.list_sources()):
            self.store.upsert_source(Source(
                source_id=HOLDS_SOURCE_ID, adapter="holds-api",
                plane=plane or "platform",
                notes="Self-declared holds (kalends_declare_hold)."))

    def declare_hold(self, title: str, owner_id: str, owner_kind: Optional[str] = None,
                     plane: Optional[str] = None, starts_at: Any = None,
                     lease_seconds: Optional[int] = None,
                     resource_claims: Optional[dict] = None,
                     metadata: Optional[dict] = None) -> dict:
        if not title:
            raise KalendsError("title is required")
        if not owner_id:
            raise KalendsError("owner_id is required")
        if not plane:
            raise KalendsError("plane is required")
        owner_kind = owner_kind or "system"
        if owner_kind not in OWNER_KINDS:
            raise KalendsError(f"owner_kind must be one of {list(OWNER_KINDS)}")
        if lease_seconds is not None:
            if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) \
                    or lease_seconds <= 0:
                raise KalendsError("lease_seconds must be a positive integer")
        if resource_claims is not None and not isinstance(resource_claims, dict):
            raise KalendsError("resource_claims must be an object")
        if metadata is not None and not isinstance(metadata, dict):
            raise KalendsError("metadata must be an object")
        now = self._now()
        start = parse_ts(starts_at, "starts_at") or now
        lease_expires = max(start, now) + timedelta(
            seconds=lease_seconds or DEFAULT_LEASE_SECONDS)
        native = f"hold-{secrets.token_hex(4)}"
        extensions = {"band_kind": "hold"}
        if metadata:
            extensions["metadata"] = dict(metadata)
        band = Band(
            band_id=f"{HOLDS_SOURCE_ID}:{native}",
            source_id=HOLDS_SOURCE_ID,
            source_native_id=native,
            title=title,
            owner_kind=owner_kind,
            owner_id=owner_id,
            plane=plane,
            started_at=start,
            last_heartbeat_at=now,
            lease_expires_at=lease_expires,
            status="active",
            resource_claims=dict(resource_claims or {}),
            extensions=extensions,
        )
        self._ensure_holds_source(plane)
        return self.store.upsert_band(band).to_dict()

    def heartbeat_hold(self, band_id: str, lease_seconds: Optional[int] = None) -> dict:
        if not band_id:
            raise KalendsError("band_id is required")
        if lease_seconds is not None:
            if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) \
                    or lease_seconds <= 0:
                raise KalendsError("lease_seconds must be a positive integer")
        now = self._now()
        band = self._find_hold(band_id, now)
        if band.ended_at is not None or band.status == "done":
            raise KalendsError(
                f"hold {band_id} is closed ({band.end_reason or band.status}); "
                "create a new hold instead of heartbeating a closed one")
        if lease_seconds is not None:
            span = timedelta(seconds=lease_seconds)
        elif band.lease_expires_at is not None and band.last_heartbeat_at is not None:
            span = band.lease_expires_at - band.last_heartbeat_at
        else:
            span = timedelta(seconds=DEFAULT_LEASE_SECONDS)
        band.last_heartbeat_at = now
        band.lease_expires_at = now + span
        return self.store.upsert_band(band).to_dict()

    def release_hold(self, band_id: str, end_reason: Optional[str] = None) -> dict:
        if not band_id:
            raise KalendsError("band_id is required")
        end_reason = end_reason or "released"
        if end_reason not in _RELEASE_REASONS:
            raise KalendsError(
                f"end_reason must be one of {list(_RELEASE_REASONS)}, got {end_reason!r}")
        now = self._now()
        band = self._find_hold(band_id, now)
        if band.ended_at is not None or band.status == "done":
            return band.to_dict()  # the closure that stands (incl. expiry)
        band.status = "done"
        band.end_reason = end_reason
        band.ended_at = now
        return self.store.upsert_band(band).to_dict()

    def list_holds(self, owner_id: Optional[str] = None,
                   include_expired: bool = True) -> dict:
        now = self._now()
        rows = self._holds(now)
        if owner_id is not None:
            rows = [b for b in rows if b.owner_id == owner_id]
        if not include_expired:
            rows = [b for b in rows if b.ended_at is None and b.status != "done"]
        rows.sort(key=lambda b: b.started_at or now)
        return {"holds": [b.to_dict() for b in rows], "count": len(rows)}



# ---------------------------------------------------------------------------
# MCP binding
# ---------------------------------------------------------------------------

TOOL_NAMES = (
    # reads
    "kalends_list_sources", "kalends_list_series", "kalends_get_series",
    "kalends_list_instances", "kalends_list_bands", "kalends_list_windows",
    "kalends_list_actuals", "kalends_list_holds",
    # single writes
    "kalends_upsert_source", "kalends_upsert_series", "kalends_upsert_instance",
    "kalends_upsert_band", "kalends_upsert_window", "kalends_upsert_actual",
    # deletes
    "kalends_delete_series", "kalends_delete_instance", "kalends_delete_band",
    "kalends_delete_window", "kalends_delete_actual",
    # batch
    "kalends_batch_upsert_series", "kalends_batch_upsert_instances",
    "kalends_batch_replace_instances", "kalends_batch_upsert_bands",
    "kalends_batch_upsert_actuals", "kalends_batch_replace_actuals",
    "kalends_mark_source_synced",
    # holds
    "kalends_declare_hold", "kalends_heartbeat_hold", "kalends_release_hold",
)


def _register_time_range_tool(server, name, description, extra_params, handler):
    """Register a list tool whose contract spells the range start
    ``from`` — a Python keyword, so the tool function takes
    ``**kwargs`` under a hand-built signature whose ``from_ts``
    parameter carries the alias ``from`` in the generated schema."""
    import inspect

    from pydantic import Field

    params = list(extra_params) + [
        ("from_ts", "from", Optional[str], None),
        ("to_ts", "to", Optional[str], None),
    ]

    def fn(**kwargs):
        mapped = {}
        for field_name, alias, _annotation, _default in params:
            key = alias or field_name
            if key in kwargs:
                mapped[field_name] = kwargs[key]
        return handler(**mapped)

    fn.__name__ = name
    fn.__doc__ = description
    signature_params = []
    for field_name, alias, annotation, default in params:
        if alias:
            default = Field(default=default, alias=alias)
        signature_params.append(inspect.Parameter(
            field_name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=default, annotation=annotation))
    fn.__signature__ = inspect.Signature(signature_params)
    server.add_tool(fn, name=name, description=description)


def build_server(service: KalendsService):
    """Bind a :class:`KalendsService` to an MCP server (SDK v2:
    ``MCPServer``, formerly FastMCP). Returns the server object."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover - extra not installed
        raise KalendsError(
            "kalends-mcp needs the Python MCP SDK: install the mcp "
            "extra (pip install 'kalends[mcp]')"
        ) from exc

    server = MCPServer(
        "kalends-mcp",
        instructions=(
            "Kalends: one calendar for every agent, job, and "
            "long-running task in a fleet. All calendar CRUD goes "
            "through these tools; use the batch tools for "
            "high-volume projection lanes."
        ),
    )

    @server.custom_route("/health", methods=["GET"])
    async def health(request):  # noqa: ANN001 - starlette request
        from starlette.responses import JSONResponse
        return JSONResponse({"status": "ok", "service": "kalends-mcp"})

    # -- reads -----------------------------------------------------------

    @server.tool()
    def kalends_list_sources() -> dict:
        """List calendar sources (the adapter registry)."""
        return service.list_sources()

    @server.tool()
    def kalends_list_series(source_id: Optional[str] = None,
                            owner_id: Optional[str] = None,
                            owner_kind: Optional[str] = None,
                            plane: Optional[str] = None,
                            enabled: Optional[bool] = None,
                            limit: Optional[int] = None,
                            offset: Optional[int] = None) -> dict:
        """List recurring series definitions, with optional filters."""
        return service.list_series(source_id=source_id, owner_id=owner_id,
                                   owner_kind=owner_kind, plane=plane,
                                   enabled=enabled, limit=limit, offset=offset)

    @server.tool()
    def kalends_get_series(series_id: str) -> dict:
        """Fetch one series by id. Errors when the series is unknown."""
        return service.get_series(series_id)

    _register_time_range_tool(
        server, "kalends_list_instances",
        "List materialised instances. from/to bound planned_start as [from, to).",
        [("series_id", None, Optional[str], None),
         ("source_id", None, Optional[str], None),
         ("status", None, Optional[str], None),
         ("owner_id", None, Optional[str], None),
         ("owner_kind", None, Optional[str], None),
         ("shape", None, Optional[str], None),
         ("limit", None, Optional[int], None),
         ("offset", None, Optional[int], None)],
        service.list_instances)

    @server.tool()
    def kalends_list_bands(owner_id: Optional[str] = None,
                           owner_kind: Optional[str] = None,
                           plane: Optional[str] = None,
                           status: Optional[str] = None,
                           source_id: Optional[str] = None,
                           active_at: Optional[str] = None,
                           limit: Optional[int] = None,
                           offset: Optional[int] = None) -> dict:
        """List long-running bands, with optional filters."""
        return service.list_bands(owner_id=owner_id, owner_kind=owner_kind, plane=plane, status=status,
                                  source_id=source_id, active_at=active_at,
                                  limit=limit, offset=offset)

    _register_time_range_tool(
        server, "kalends_list_windows",
        "List availability windows. from/to bound starts_at as [from, to).",
        [("owner_id", None, Optional[str], None),
         ("limit", None, Optional[int], None)],
        service.list_windows)

    _register_time_range_tool(
        server, "kalends_list_actuals",
        "List observed actuals (firings and points). from/to bound fired_at as [from, to).",
        [("series_id", None, Optional[str], None),
         ("source_id", None, Optional[str], None),
         ("kind", None, Optional[str], None),
         ("limit", None, Optional[int], None),
         ("offset", None, Optional[int], None)],
        service.list_actuals)

    @server.tool()
    def kalends_list_holds(owner_id: Optional[str] = None,
                           include_expired: bool = True) -> dict:
        """List self-declared holds, with lease expiry materialised as of now."""
        return service.list_holds(owner_id=owner_id, include_expired=include_expired)

    # -- single writes ------------------------------------------------------

    @server.tool()
    def kalends_upsert_source(source: dict) -> dict:
        """Upsert one calendar source by source_id; returns the stored row."""
        return service.upsert_source(source)

    @server.tool()
    def kalends_upsert_series(series: dict) -> dict:
        """Upsert one series by series_id; returns the stored row."""
        return service.upsert_series(series)

    @server.tool()
    def kalends_upsert_instance(instance: dict) -> dict:
        """Upsert one instance by instance_id; returns the stored row."""
        return service.upsert_instance(instance)

    @server.tool()
    def kalends_upsert_band(band: dict) -> dict:
        """Upsert one band by band_id; returns the stored row."""
        return service.upsert_band(band)

    @server.tool()
    def kalends_upsert_window(window: dict) -> dict:
        """Upsert one window by window_id; returns the stored row."""
        return service.upsert_window(window)

    @server.tool()
    def kalends_upsert_actual(actual: dict) -> dict:
        """Upsert one actual by actual_id (inserts when no actual_id is given); returns the stored row."""
        return service.upsert_actual(actual)

    # -- deletes --------------------------------------------------------------

    @server.tool()
    def kalends_delete_series(series_id: str) -> dict:
        """Delete one series. Returns {deleted: bool}."""
        return service.delete_series(series_id)

    @server.tool()
    def kalends_delete_instance(instance_id: str) -> dict:
        """Delete one instance. Returns {deleted: bool}."""
        return service.delete_instance(instance_id)

    @server.tool()
    def kalends_delete_band(band_id: str) -> dict:
        """Delete one band. Returns {deleted: bool}."""
        return service.delete_band(band_id)

    @server.tool()
    def kalends_delete_window(window_id: str) -> dict:
        """Delete one window. Returns {deleted: bool}."""
        return service.delete_window(window_id)

    @server.tool()
    def kalends_delete_actual(actual_id: int) -> dict:
        """Delete one actual. Returns {deleted: bool}."""
        return service.delete_actual(actual_id)

    # -- batch ------------------------------------------------------------------

    @server.tool()
    def kalends_batch_upsert_series(series: list) -> dict:
        """Upsert many series in one transaction. Returns {upserted: n}."""
        return service.batch_upsert_series(series)

    @server.tool()
    def kalends_batch_upsert_instances(instances: list) -> dict:
        """Upsert many instances in one transaction. Returns {upserted: n}."""
        return service.batch_upsert_instances(instances)

    @server.tool()
    def kalends_batch_replace_instances(source_id: str, horizon_start: str,
                                        horizon_end: str, instances: list) -> dict:
        """Scoped replace: in one transaction, delete this source's instances with planned_start within [horizon_start, horizon_end], then insert the given instances. Returns {deleted, inserted}."""
        return service.batch_replace_instances(source_id, horizon_start,
                                               horizon_end, instances)

    @server.tool()
    def kalends_batch_upsert_bands(bands: list) -> dict:
        """Upsert many bands in one transaction. Returns {upserted: n}."""
        return service.batch_upsert_bands(bands)

    @server.tool()
    def kalends_batch_upsert_actuals(actuals: list) -> dict:
        """Upsert many actuals in one transaction. Returns {upserted: n}."""
        return service.batch_upsert_actuals(actuals)

    @server.tool()
    def kalends_batch_replace_actuals(source_id: str, window_start: str,
                                      window_end: str, actuals: list) -> dict:
        """Scoped replace: in one transaction, delete this source's actuals with fired_at within [window_start, window_end], then insert the given actuals. Returns {deleted, inserted}."""
        return service.batch_replace_actuals(source_id, window_start,
                                             window_end, actuals)

    @server.tool()
    def kalends_mark_source_synced(source_id: str,
                                   synced_at: Optional[str] = None,
                                   status: Optional[str] = None,
                                   note: Optional[str] = None) -> dict:
        """Stamp a source's freshness (last_synced_at; status/note are recorded in the source's notes, the schema's only text field for them). Returns the stored source."""
        return service.mark_source_synced(source_id, synced_at=synced_at,
                                          status=status, note=note)

    # -- holds --------------------------------------------------------------------

    @server.tool()
    def kalends_declare_hold(title: str, owner_id: str, plane: str,
                             owner_kind: Optional[str] = None,
                             starts_at: Optional[str] = None,
                             lease_seconds: Optional[int] = None,
                             resource_claims: Optional[dict] = None,
                             metadata: Optional[dict] = None) -> dict:
        """Put work no scheduler started on the calendar as a lease-held band. Returns the band, including its id and lease_expires_at."""
        if not plane:
            raise KalendsError("plane is required")
        return service.declare_hold(title=title, owner_id=owner_id,
                                    owner_kind=owner_kind, plane=plane,
                                    starts_at=starts_at,
                                    lease_seconds=lease_seconds,
                                    resource_claims=resource_claims,
                                    metadata=metadata)

    @server.tool()
    def kalends_heartbeat_hold(band_id: str,
                               lease_seconds: Optional[int] = None) -> dict:
        """Renew a live hold's lease to now + lease_seconds (default: reuse the hold's original lease length). Errors on a closed or unknown hold."""
        return service.heartbeat_hold(band_id, lease_seconds=lease_seconds)

    @server.tool()
    def kalends_release_hold(band_id: str,
                             end_reason: Optional[str] = None) -> dict:
        """Close a hold early. end_reason: released (default), completed, abandoned, or failed."""
        return service.release_hold(band_id, end_reason=end_reason)

    return server


# ---------------------------------------------------------------------------
# HTTP origin guard + entry point
# ---------------------------------------------------------------------------

class OriginGuardMiddleware:
    """ASGI middleware for the house origin-guard pattern: when a
    shared secret is configured, every request except
    ``GET /health`` must carry it as the ``x-origin-secret`` header.
    """

    def __init__(self, app, secret: Optional[str]):
        self.app = app
        self.secret = secret or ""

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and self.secret:
            path = scope.get("path", "")
            method = scope.get("method", "")
            if not (method == "GET" and path == "/health"):
                headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in scope.get("headers", [])
                }
                if headers.get("x-origin-secret") != self.secret:
                    from starlette.responses import JSONResponse
                    response = JSONResponse(
                        {"error": "forbidden: missing or wrong x-origin-secret"},
                        status_code=403)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def run_http(server, host: str, port: int, secret: Optional[str]) -> None:
    import uvicorn

    if not secret:
        log.warning(
            "kalends-mcp http mode: ORIGIN_SHARED_SECRET is not set; "
            "the origin guard is off. Front the server with your own "
            "auth or set the secret.")
    app = server.streamable_http_app(host=host)
    uvicorn.run(OriginGuardMiddleware(app, secret),
                host=host, port=port, log_level="info")


def service_from_env() -> KalendsService:
    dsn = os.environ.get("KALENDS_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "kalends-mcp: set KALENDS_DSN (or DATABASE_URL) to the "
            "Postgres database holding the calendar_* tables; apply "
            "schema/001_kalends.sql first.")
    return KalendsService(PostgresStore(dsn))


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    server = build_server(service_from_env())
    transport = os.environ.get("TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http"):
        host = os.environ.get("BIND_HOST", DEFAULT_HTTP_HOST)
        try:
            port = int(os.environ.get("PORT", str(DEFAULT_HTTP_PORT)))
        except ValueError as exc:
            raise SystemExit(f"kalends-mcp: PORT is not an integer: {exc}") from exc
        run_http(server, host, port, os.environ.get("ORIGIN_SHARED_SECRET"))
    elif transport == "stdio":
        server.run(transport="stdio")
    else:
        raise SystemExit(
            f"kalends-mcp: unknown TRANSPORT {transport!r} (use stdio or http)")


if __name__ == "__main__":
    main()
