"""Storage layer for the Kalends MCP server.

The MCP tools in :mod:`kalends.mcp_server` talk to a *store*: an
object implementing the :class:`KalendsStore` protocol below. Two
implementations ship here:

* :class:`PostgresStore` — the production store, over the six
  ``calendar_*`` tables defined by ``schema/001_kalends.sql`` (with
  the ``002`` additions). One connection per call; batch operations
  run in a single transaction and use ``executemany``, so a
  high-volume projection pass is a handful of round trips, not one
  per row.
* :class:`InMemoryStore` — a dict-backed store with identical
  semantics, for tests and for embedding the tool logic without a
  database. It is the fake the unit tests run against.

Entities travel as the core dataclasses from :mod:`kalends.model`,
except actuals. The model ``Actual`` carries no row id (it is a
projection-time join object), but the ``calendar_actuals`` table has
an identity primary key (``actual_id``) and an ``instance_id``
column, so the store layer represents an actual as a plain dict with
exactly the table's columns. :func:`normalize_actual` builds that
dict from an ``Actual``, a dump dict, or a tool argument.

Upsert semantics are upsert-by-primary-key throughout: a row whose
primary key already exists is updated in place; otherwise it is
inserted. ``calendar_actuals`` has no natural unique key, so an
actual without an ``actual_id`` always inserts (and is assigned
one), and an actual with one updates that row.

The Postgres column lists below are the schema. Fields the model
carries only for JSON dumps (``extensions`` everywhere,
``event_triggers`` on series) are not columns and are not persisted
by :class:`PostgresStore`; :class:`InMemoryStore` keeps the objects
it is given whole, annotations included.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from dateutil.parser import isoparse

from .model import Actual, Band, Instance, Series, Source, Window

SOURCE_COLUMNS = (
    "source_id", "adapter", "plane", "enabled", "sync_interval_sec",
    "last_synced_at", "last_source_at", "notes",
)
SERIES_COLUMNS = (
    "series_id", "source_id", "source_native_id", "title", "owner_kind",
    "owner_id", "plane", "cadence_dialect", "cadence_expr", "rrule",
    "timezone", "expected_duration_sec", "overlap_policy", "enabled",
    "lifecycle_state", "source_updated_at", "synced_at", "anchor_at",
    "resource_claims",
)
INSTANCE_COLUMNS = (
    "instance_id", "series_id", "source_id", "source_native_id",
    "title", "owner_kind", "owner_id", "plane", "shape",
    "planned_start", "planned_end", "actual_start", "actual_end",
    "status", "bound_expected", "resource_claims", "source_updated_at",
    "synced_at",
)
BAND_COLUMNS = (
    "band_id", "source_id", "source_native_id", "title", "owner_kind",
    "owner_id", "plane", "started_at", "last_heartbeat_at",
    "lease_expires_at", "ended_at", "end_reason", "status",
    "resource_claims",
)
WINDOW_COLUMNS = (
    "window_id", "owner_kind", "owner_id", "plane", "kind",
    "starts_at", "ends_at", "rrule", "priority", "source_id",
    "source_native_id",
)
ACTUAL_COLUMNS = (
    "actual_id", "source_id", "source_native_id", "series_id",
    "instance_id", "fired_at", "would_fire_at", "observed_only",
    "status", "kind", "is_empty",
)

_JSONB_COLUMNS = {"resource_claims"}
_TIMESTAMP_COLUMNS = {
    "last_synced_at", "last_source_at", "source_updated_at", "synced_at",
    "anchor_at", "planned_start", "planned_end", "actual_start",
    "actual_end", "started_at", "last_heartbeat_at", "lease_expires_at",
    "ended_at", "starts_at", "ends_at", "fired_at", "would_fire_at",
}


class KalendsError(ValueError):
    """A store or tool operation could not be carried out.

    Subclasses ``ValueError`` so argument validation and state errors
    (unknown id, closed hold) surface the same way to MCP clients.
    """


def parse_ts(value: Any, field: str = "timestamp") -> Optional[datetime]:
    """Parse an ISO 8601 string (or pass a datetime through) to an
    aware datetime. Naive values are read as UTC."""
    if value is None or isinstance(value, datetime):
        result = value
    else:
        try:
            result = isoparse(str(value))
        except (ValueError, OverflowError) as exc:
            raise KalendsError(f"{field}: not an ISO 8601 timestamp: {value!r}") from exc
    if result is not None and result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def normalize_actual(data: Any) -> dict:
    """Normalise an actual (model object or dict) to the store dict:
    exactly the ``calendar_actuals`` columns, timestamps parsed."""
    if isinstance(data, Actual):
        row = data.to_dict()
    elif isinstance(data, dict):
        row = dict(data)
    else:
        raise KalendsError(f"actual: expected an object, got {type(data).__name__}")
    out: dict[str, Any] = {}
    for col in ACTUAL_COLUMNS:
        value = row.get(col)
        if col in _TIMESTAMP_COLUMNS:
            value = parse_ts(value, f"actual.{col}")
        out[col] = value
    if out["source_id"] is None or out["source_native_id"] is None:
        raise KalendsError("actual: source_id and source_native_id are required")
    if out["fired_at"] is None:
        raise KalendsError("actual: fired_at is required")
    if out["actual_id"] is not None:
        try:
            out["actual_id"] = int(out["actual_id"])
        except (TypeError, ValueError) as exc:
            raise KalendsError("actual: actual_id must be an integer") from exc
    out["observed_only"] = bool(out["observed_only"] or False)
    out["is_empty"] = bool(out["is_empty"] or False)
    out["status"] = out["status"] or "completed"
    out["kind"] = out["kind"] or "firing"
    return out


def actual_to_dict(row: dict) -> dict:
    """Render a store actual dict as a tool result (ISO timestamps)."""
    out = dict(row)
    for col in ("fired_at", "would_fire_at"):
        value = out.get(col)
        out[col] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _page(rows: list, limit: Optional[int], offset: Optional[int]) -> list:
    start = offset or 0
    if limit is None:
        return rows[start:]
    return rows[start:start + limit]


class KalendsStore(Protocol):
    """The store protocol every backend implements.

    List methods take the tool contract's filters; ``limit`` /
    ``offset`` page the result (``None`` limit means the caller's
    default, applied by the service layer). Batch methods are
    single-transaction in every backend.
    """

    # sources
    def list_sources(self) -> list[Source]: ...
    def upsert_source(self, source: Source) -> Source: ...
    def mark_source_synced(self, source_id: str, synced_at: datetime,
                           status: Optional[str], note: Optional[str]) -> Source: ...
    def delete_source(self, source_id: str, force: bool = False) -> dict: ...

    # series
    def list_series(self, source_id=None, owner_id=None, owner_kind=None,
                    plane=None, enabled=None, limit=None, offset=None) -> list[Series]: ...
    def get_series(self, series_id: str) -> Optional[Series]: ...
    def upsert_series(self, series: Series) -> Series: ...
    def batch_upsert_series(self, rows: list[Series]) -> int: ...
    def delete_series(self, series_id: str) -> bool: ...

    # instances
    def list_instances(self, series_id=None, source_id=None, from_ts=None,
                       to_ts=None, status=None, owner_id=None, owner_kind=None,
                       shape=None, bound_expected=None,
                       limit=None, offset=None) -> list[Instance]: ...
    def upsert_instance(self, instance: Instance) -> Instance: ...
    def batch_upsert_instances(self, rows: list[Instance]) -> int: ...
    def batch_replace_instances(self, source_id: str, horizon_start: datetime,
                                horizon_end: datetime, rows: list[Instance]) -> tuple[int, int]: ...
    def delete_instance(self, instance_id: str) -> bool: ...

    # bands
    def list_bands(self, owner_id=None, owner_kind=None, plane=None, status=None, source_id=None,
                   active_at=None, overlap_start=None, overlap_end=None,
                   limit=None, offset=None) -> list[Band]: ...
    def get_band(self, band_id: str) -> Optional[Band]: ...
    def upsert_band(self, band: Band) -> Band: ...
    def batch_upsert_bands(self, rows: list[Band]) -> int: ...
    def delete_band(self, band_id: str) -> bool: ...

    # windows
    def list_windows(self, owner_id=None, from_ts=None, to_ts=None,
                     limit=None) -> list[Window]: ...
    def upsert_window(self, window: Window) -> Window: ...
    def delete_window(self, window_id: str) -> bool: ...

    # actuals (store dicts, see normalize_actual)
    def list_actuals(self, series_id=None, source_id=None, from_ts=None,
                     to_ts=None, kind=None, limit=None, offset=None) -> list[dict]: ...
    def upsert_actual(self, row: dict) -> dict: ...
    def batch_upsert_actuals(self, rows: list[dict]) -> int: ...
    def batch_replace_actuals(self, source_id: str, window_start: datetime,
                              window_end: datetime, rows: list[dict]) -> tuple[int, int]: ...
    def delete_actual(self, actual_id: int) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

class InMemoryStore:
    """Dict-backed :class:`KalendsStore`. Mirrors the Postgres
    store's semantics (upsert by primary key, scoped replace ranges
    inclusive at both ends, freshness stamping) without a database."""

    def __init__(self) -> None:
        self.sources: dict[str, Source] = {}
        self.series: dict[str, Series] = {}
        self.instances: dict[str, Instance] = {}
        self.bands: dict[str, Band] = {}
        self.windows: dict[str, Window] = {}
        self.actuals: dict[int, dict] = {}
        self._next_actual_id = 1

    # -- sources ---------------------------------------------------------

    def list_sources(self) -> list[Source]:
        return [self.sources[k] for k in sorted(self.sources)]

    def upsert_source(self, source: Source) -> Source:
        self.sources[source.source_id] = source
        return source

    def mark_source_synced(self, source_id, synced_at, status, note):
        source = self.sources.get(source_id)
        if source is None:
            raise KalendsError(f"unknown source: {source_id}")
        source.last_synced_at = synced_at
        text = _sync_note(status, note)
        if text is not None:
            source.notes = text
        return source

    def delete_source(self, source_id, force=False):
        if source_id not in self.sources:
            raise KalendsError(f"unknown source: {source_id}")
        series_ids = {sid for sid, s in self.series.items()
                      if s.source_id == source_id}
        instance_ids = {iid for iid, i in self.instances.items()
                        if i.source_id == source_id or i.series_id in series_ids}
        dependents = {
            "series": len(series_ids),
            "instances": len(instance_ids),
            "bands": sum(1 for b in self.bands.values()
                         if b.source_id == source_id),
            "windows": sum(1 for w in self.windows.values()
                           if w.source_id == source_id),
            "actuals": sum(1 for a in self.actuals.values()
                           if a["source_id"] == source_id
                           or a["series_id"] in series_ids
                           or a["instance_id"] in instance_ids),
        }
        if any(dependents.values()) and not force:
            raise KalendsError(
                f"source {source_id} has dependent rows "
                f"(series={dependents['series']}, "
                f"instances={dependents['instances']}, "
                f"bands={dependents['bands']}, "
                f"windows={dependents['windows']}, "
                f"actuals={dependents['actuals']}); pass force=true to "
                "delete them with the source")
        deleted = {k: (v if force else 0) for k, v in dependents.items()}
        if force:
            for aid in [aid for aid, a in self.actuals.items()
                        if a["source_id"] == source_id
                        or a["series_id"] in series_ids
                        or a["instance_id"] in instance_ids]:
                del self.actuals[aid]
            for iid in instance_ids:
                del self.instances[iid]
            for bid in [bid for bid, b in self.bands.items()
                        if b.source_id == source_id]:
                del self.bands[bid]
            for wid in [wid for wid, w in self.windows.items()
                        if w.source_id == source_id]:
                del self.windows[wid]
            for sid in series_ids:
                del self.series[sid]
        del self.sources[source_id]
        return {"deleted": True, "source_id": source_id, "force": bool(force),
                "deleted_dependents": deleted}

    # -- series ----------------------------------------------------------

    def list_series(self, source_id=None, owner_id=None, owner_kind=None,
                    plane=None, enabled=None, limit=None, offset=None):
        rows = [s for s in self.series.values()
                if (source_id is None or s.source_id == source_id)
                and (owner_id is None or s.owner_id == owner_id)
                and (owner_kind is None or s.owner_kind == owner_kind)
                and (plane is None or s.plane == plane)
                and (enabled is None or s.enabled == enabled)]
        rows.sort(key=lambda s: s.series_id)
        return _page(rows, limit, offset)

    def get_series(self, series_id):
        return self.series.get(series_id)

    def upsert_series(self, series):
        self.series[series.series_id] = series
        return series

    def batch_upsert_series(self, rows):
        for row in rows:
            self.upsert_series(row)
        return len(rows)

    def delete_series(self, series_id):
        return self.series.pop(series_id, None) is not None

    # -- instances ---------------------------------------------------------

    def list_instances(self, series_id=None, source_id=None, from_ts=None,
                       to_ts=None, status=None, owner_id=None, owner_kind=None,
                       shape=None, bound_expected=None, limit=None, offset=None):
        rows = [i for i in self.instances.values()
                if (series_id is None or i.series_id == series_id)
                and (source_id is None or i.source_id == source_id)
                and (status is None or i.status == status)
                and (owner_id is None or i.owner_id == owner_id)
                and (owner_kind is None or i.owner_kind == owner_kind)
                and (shape is None or i.shape == shape)
                and (bound_expected is None or i.bound_expected == bound_expected)
                and (from_ts is None or (i.planned_start is not None and i.planned_start >= from_ts))
                and (to_ts is None or (i.planned_start is not None and i.planned_start < to_ts))]
        rows.sort(key=lambda i: (i.planned_start or datetime.min.replace(tzinfo=timezone.utc), i.instance_id))
        return _page(rows, limit, offset)

    def upsert_instance(self, instance):
        self.instances[instance.instance_id] = instance
        return instance

    def batch_upsert_instances(self, rows):
        for row in rows:
            self.upsert_instance(row)
        return len(rows)

    def batch_replace_instances(self, source_id, horizon_start, horizon_end, rows):
        doomed = [iid for iid, i in self.instances.items()
                  if i.source_id == source_id and i.planned_start is not None
                  and horizon_start <= i.planned_start <= horizon_end]
        for iid in doomed:
            del self.instances[iid]
        for row in rows:
            self.upsert_instance(row)
        return len(doomed), len(rows)

    def delete_instance(self, instance_id):
        return self.instances.pop(instance_id, None) is not None

    # -- bands -------------------------------------------------------------

    def list_bands(self, owner_id=None, owner_kind=None, plane=None, status=None, source_id=None,
                   active_at=None, overlap_start=None, overlap_end=None,
                   limit=None, offset=None):
        rows = [b for b in self.bands.values()
                if (owner_id is None or b.owner_id == owner_id)
                and (owner_kind is None or b.owner_kind == owner_kind)
                and (plane is None or b.plane == plane)
                and (status is None or b.status == status)
                and (source_id is None or b.source_id == source_id)
                and (active_at is None or _active_at(b, active_at))
                and _overlaps_window(b, overlap_start, overlap_end)]
        rows.sort(key=lambda b: b.band_id)
        return _page(rows, limit, offset)

    def get_band(self, band_id):
        band = self.bands.get(band_id)
        if band is None:  # holds are also addressed by source-native id
            for candidate in self.bands.values():
                if candidate.source_native_id == band_id:
                    return candidate
        return band

    def upsert_band(self, band):
        self.bands[band.band_id] = band
        return band

    def batch_upsert_bands(self, rows):
        for row in rows:
            self.upsert_band(row)
        return len(rows)

    def delete_band(self, band_id):
        return self.bands.pop(band_id, None) is not None

    # -- windows -----------------------------------------------------------

    def list_windows(self, owner_id=None, from_ts=None, to_ts=None, limit=None):
        rows = [w for w in self.windows.values()
                if (owner_id is None or w.owner_id == owner_id)
                and (from_ts is None or (w.starts_at is not None and w.starts_at >= from_ts))
                and (to_ts is None or (w.starts_at is not None and w.starts_at < to_ts))]
        rows.sort(key=lambda w: w.window_id)
        return _page(rows, limit, None)

    def upsert_window(self, window):
        self.windows[window.window_id] = window
        return window

    def delete_window(self, window_id):
        return self.windows.pop(window_id, None) is not None

    # -- actuals -----------------------------------------------------------

    def list_actuals(self, series_id=None, source_id=None, from_ts=None,
                     to_ts=None, kind=None, limit=None, offset=None):
        rows = [a for a in self.actuals.values()
                if (series_id is None or a["series_id"] == series_id)
                and (source_id is None or a["source_id"] == source_id)
                and (kind is None or a["kind"] == kind)
                and (from_ts is None or a["fired_at"] >= from_ts)
                and (to_ts is None or a["fired_at"] < to_ts)]
        rows.sort(key=lambda a: (a["fired_at"], a["actual_id"] or 0))
        return _page(rows, limit, offset)

    def upsert_actual(self, row):
        row = dict(row)
        actual_id = row.get("actual_id")
        if actual_id is not None and actual_id in self.actuals:
            self.actuals[actual_id] = row
            return row
        if actual_id is None:
            actual_id = self._next_actual_id
            row["actual_id"] = actual_id
        self._next_actual_id = max(self._next_actual_id, actual_id + 1)
        self.actuals[actual_id] = row
        return row

    def batch_upsert_actuals(self, rows):
        for row in rows:
            self.upsert_actual(row)
        return len(rows)

    def batch_replace_actuals(self, source_id, window_start, window_end, rows):
        doomed = [aid for aid, a in self.actuals.items()
                  if a["source_id"] == source_id
                  and window_start <= a["fired_at"] <= window_end]
        for aid in doomed:
            del self.actuals[aid]
        for row in rows:
            self.upsert_actual(row)
        return len(doomed), len(rows)

    def delete_actual(self, actual_id):
        return self.actuals.pop(actual_id, None) is not None


def _active_at(band: Band, moment: datetime) -> bool:
    if band.started_at is not None and band.started_at > moment:
        return False
    if band.ended_at is not None and band.ended_at <= moment:
        return False
    return True


def _band_effective_end(band: Band):
    """When a band stops occupying time: its recorded end if it has
    one, else its lease expiry, else never (infinity)."""
    if band.ended_at is not None:
        return band.ended_at
    return band.lease_expires_at


def _overlaps_window(band: Band, start, end) -> bool:
    """True iff the band overlaps the window [start, end): the band
    starts before the window ends and its effective end is after the
    window starts. Either bound may be None (unbounded)."""
    if end is not None and band.started_at is not None \
            and not band.started_at < end:
        return False
    effective_end = _band_effective_end(band)
    if start is not None and effective_end is not None \
            and not effective_end > start:
        return False
    return True


def _sync_note(status: Optional[str], note: Optional[str]) -> Optional[str]:
    """calendar_sources has no status column; a sync status is
    recorded in ``notes`` alongside (or as) the caller's note."""
    if status and note:
        return f"{status}: {note}"
    return note if note is not None else status



# ---------------------------------------------------------------------------
# Postgres store
# ---------------------------------------------------------------------------

_TABLES = {
    "source": ("calendar_sources", SOURCE_COLUMNS, "source_id", Source),
    "series": ("calendar_series", SERIES_COLUMNS, "series_id", Series),
    "instance": ("calendar_instances", INSTANCE_COLUMNS, "instance_id", Instance),
    "band": ("calendar_bands", BAND_COLUMNS, "band_id", Band),
    "window": ("calendar_windows", WINDOW_COLUMNS, "window_id", Window),
}


def _obj_params(obj: Any, columns: tuple) -> dict:
    params = {}
    for col in columns:
        value = getattr(obj, col)
        if col in _JSONB_COLUMNS:
            value = json.dumps(value or {})
        params[col] = value
    return params


def _upsert_sql(table: str, columns: tuple, pk: str) -> str:
    cols = ", ".join(columns)
    placeholders = ", ".join(f"%({c})s" for c in columns)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != pk)
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT ({pk}) DO UPDATE SET {updates} RETURNING *"
    )


def _row_obj(cls, row: dict):
    kwargs = {}
    for key, value in row.items():
        if key in _JSONB_COLUMNS and isinstance(value, str):
            value = json.loads(value)
        kwargs[key] = value
    return cls(**{k: v for k, v in kwargs.items()
                  if k in {f.name for f in cls.__dataclass_fields__.values()}})


class PostgresStore:
    """The production store: the six ``calendar_*`` tables over
    psycopg 3. Construct with a libpq DSN / connection string.

    Every method is one connection and one transaction. Batch
    methods use a single ``executemany`` inside that transaction, so
    a replace or upsert pass either lands whole or not at all.
    """

    def __init__(self, dsn: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - extra not installed
            raise KalendsError(
                "PostgresStore needs psycopg: install the mcp extra "
                "(pip install 'kalends[mcp]')"
            ) from exc
        self._dsn = dsn
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._bound_expected_ok: Optional[bool] = None

    def _connect(self):
        return self._psycopg.connect(self._dsn, row_factory=self._dict_row)

    def _bound_expected_supported(self) -> bool:
        """Whether calendar_instances already carries the
        bound_expected column (migration 003). Probed once and
        cached: a pre-migration database must keep working with this
        code deployed, so writes omit the column and reads default
        it to false until the migration lands (a restart after the
        migration re-probes). A failed probe reads as unsupported:
        degraded, never broken."""
        if self._bound_expected_ok is None:
            try:
                with self._connect() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'calendar_instances' "
                        "AND column_name = 'bound_expected'")
                    self._bound_expected_ok = cur.fetchone() is not None
            except Exception:
                self._bound_expected_ok = False
        return self._bound_expected_ok

    def _instance_columns(self) -> tuple:
        if self._bound_expected_supported():
            return INSTANCE_COLUMNS
        return tuple(c for c in INSTANCE_COLUMNS if c != "bound_expected")

    # -- generic helpers ---------------------------------------------------

    def _upsert(self, kind: str, obj: Any):
        table, columns, pk, cls = _TABLES[kind]
        if kind == "instance":
            columns = self._instance_columns()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_upsert_sql(table, columns, pk), _obj_params(obj, columns))
            return _row_obj(cls, cur.fetchone())

    def _batch_upsert(self, kind: str, rows: list) -> int:
        if not rows:
            return 0
        table, columns, pk, _cls = _TABLES[kind]
        if kind == "instance":
            columns = self._instance_columns()
        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany(
                _upsert_sql(table, columns, pk),
                [_obj_params(row, columns) for row in rows],
            )
        return len(rows)

    def _delete(self, kind: str, pk_value: Any) -> bool:
        table, _columns, pk, _cls = _TABLES[kind]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE {pk} = %s", (pk_value,))
            return cur.rowcount > 0

    def _list(self, kind: str, clauses: list, params: list,
              order: str, limit, offset) -> list:
        table, _columns, _pk, cls = _TABLES[kind]
        sql = f"SELECT * FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order} LIMIT %s OFFSET %s"
        params = params + [limit, offset or 0]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_obj(cls, row) for row in cur.fetchall()]

    # -- sources -----------------------------------------------------------

    def list_sources(self):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM calendar_sources ORDER BY source_id")
            return [_row_obj(Source, row) for row in cur.fetchall()]

    def upsert_source(self, source):
        return self._upsert("source", source)

    def mark_source_synced(self, source_id, synced_at, status, note):
        text = _sync_note(status, note)
        with self._connect() as conn, conn.cursor() as cur:
            if text is not None:
                cur.execute(
                    "UPDATE calendar_sources SET last_synced_at = %s, notes = %s "
                    "WHERE source_id = %s RETURNING *",
                    (synced_at, text, source_id),
                )
            else:
                cur.execute(
                    "UPDATE calendar_sources SET last_synced_at = %s "
                    "WHERE source_id = %s RETURNING *",
                    (synced_at, source_id),
                )
            row = cur.fetchone()
        if row is None:
            raise KalendsError(f"unknown source: {source_id}")
        return _row_obj(Source, row)

    def delete_source(self, source_id, force=False):
        series_of = ("SELECT series_id FROM calendar_series "
                     "WHERE source_id = %s")
        instances_of = ("SELECT instance_id FROM calendar_instances "
                        "WHERE source_id = %s OR series_id IN (" + series_of + ")")
        count_sql = {
            "series": ("SELECT count(*) AS n FROM calendar_series "
                       "WHERE source_id = %s", (source_id,)),
            "instances": ("SELECT count(*) AS n FROM calendar_instances "
                          "WHERE source_id = %s OR series_id IN ("
                          + series_of + ")", (source_id, source_id)),
            "bands": ("SELECT count(*) AS n FROM calendar_bands "
                      "WHERE source_id = %s", (source_id,)),
            "windows": ("SELECT count(*) AS n FROM calendar_windows "
                        "WHERE source_id = %s", (source_id,)),
            "actuals": ("SELECT count(*) AS n FROM calendar_actuals "
                        "WHERE source_id = %s OR series_id IN (" + series_of
                        + ") OR instance_id IN (" + instances_of + ")",
                        (source_id, source_id, source_id, source_id)),
        }
        delete_sql = [
            ("actuals",
             "DELETE FROM calendar_actuals WHERE source_id = %s "
             "OR series_id IN (" + series_of + ") OR instance_id IN ("
             + instances_of + ")",
             (source_id, source_id, source_id, source_id)),
            ("instances",
             "DELETE FROM calendar_instances WHERE source_id = %s "
             "OR series_id IN (" + series_of + ")", (source_id, source_id)),
            ("bands", "DELETE FROM calendar_bands WHERE source_id = %s",
             (source_id,)),
            ("windows", "DELETE FROM calendar_windows WHERE source_id = %s",
             (source_id,)),
            ("series", "DELETE FROM calendar_series WHERE source_id = %s",
             (source_id,)),
        ]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 AS ok FROM calendar_sources "
                "WHERE source_id = %s FOR UPDATE", (source_id,))
            if cur.fetchone() is None:
                raise KalendsError(f"unknown source: {source_id}")
            dependents = {}
            for key, (sql, params) in count_sql.items():
                cur.execute(sql, params)
                dependents[key] = cur.fetchone()["n"]
            if any(dependents.values()) and not force:
                raise KalendsError(
                    f"source {source_id} has dependent rows "
                    f"(series={dependents['series']}, "
                    f"instances={dependents['instances']}, "
                    f"bands={dependents['bands']}, "
                    f"windows={dependents['windows']}, "
                    f"actuals={dependents['actuals']}); pass force=true to "
                    "delete them with the source")
            deleted = {k: 0 for k in dependents}
            if force:
                for key, sql, params in delete_sql:
                    cur.execute(sql, params)
                    deleted[key] = cur.rowcount
            cur.execute("DELETE FROM calendar_sources WHERE source_id = %s",
                        (source_id,))
        return {"deleted": True, "source_id": source_id, "force": bool(force),
                "deleted_dependents": deleted}

    # -- series --------------------------------------------------------------

    def list_series(self, source_id=None, owner_id=None, owner_kind=None,
                    plane=None, enabled=None, limit=None, offset=None):
        clauses, params = [], []
        for col, value in (("source_id", source_id), ("owner_id", owner_id),
                           ("owner_kind", owner_kind), ("plane", plane),
                           ("enabled", enabled)):
            if value is not None:
                clauses.append(f"{col} = %s")
                params.append(value)
        return self._list("series", clauses, params, "series_id", limit, offset)

    def get_series(self, series_id):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM calendar_series WHERE series_id = %s",
                        (series_id,))
            row = cur.fetchone()
        return _row_obj(Series, row) if row else None

    def upsert_series(self, series):
        return self._upsert("series", series)

    def batch_upsert_series(self, rows):
        return self._batch_upsert("series", rows)

    def delete_series(self, series_id):
        return self._delete("series", series_id)

    # -- instances -------------------------------------------------------------

    def list_instances(self, series_id=None, source_id=None, from_ts=None,
                       to_ts=None, status=None, owner_id=None, owner_kind=None,
                       shape=None, bound_expected=None, limit=None, offset=None):
        clauses, params = [], []
        for col, value in (("series_id", series_id), ("source_id", source_id),
                           ("status", status), ("owner_id", owner_id),
                           ("owner_kind", owner_kind), ("shape", shape)):
            if value is not None:
                clauses.append(f"{col} = %s")
                params.append(value)
        if bound_expected is not None:
            if self._bound_expected_supported():
                clauses.append("bound_expected = %s")
                params.append(bound_expected)
            elif bound_expected:
                # Pre-migration every row reads as bound_expected
                # false, so a true filter matches nothing.
                clauses.append("FALSE")
        if from_ts is not None:
            clauses.append("planned_start >= %s")
            params.append(from_ts)
        if to_ts is not None:
            clauses.append("planned_start < %s")
            params.append(to_ts)
        return self._list("instance", clauses, params,
                          "planned_start NULLS LAST, instance_id", limit, offset)

    def upsert_instance(self, instance):
        return self._upsert("instance", instance)

    def batch_upsert_instances(self, rows):
        return self._batch_upsert("instance", rows)

    def batch_replace_instances(self, source_id, horizon_start, horizon_end, rows):
        table, _all_columns, pk, _cls = _TABLES["instance"]
        columns = self._instance_columns()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM calendar_instances WHERE source_id = %s "
                "AND planned_start >= %s AND planned_start <= %s",
                (source_id, horizon_start, horizon_end),
            )
            deleted = cur.rowcount
            if rows:
                cur.executemany(
                    _upsert_sql(table, columns, pk),
                    [_obj_params(row, columns) for row in rows],
                )
        return deleted, len(rows)

    def delete_instance(self, instance_id):
        return self._delete("instance", instance_id)

    # -- bands ---------------------------------------------------------------

    def list_bands(self, owner_id=None, owner_kind=None, plane=None, status=None, source_id=None,
                   active_at=None, overlap_start=None, overlap_end=None,
                   limit=None, offset=None):
        clauses, params = [], []
        for col, value in (("owner_id", owner_id), ("owner_kind", owner_kind), ("plane", plane),
                           ("status", status), ("source_id", source_id)):
            if value is not None:
                clauses.append(f"{col} = %s")
                params.append(value)
        if active_at is not None:
            clauses.append("(started_at IS NULL OR started_at <= %s)")
            params.append(active_at)
            clauses.append("(ended_at IS NULL OR ended_at > %s)")
            params.append(active_at)
        if overlap_end is not None:
            clauses.append("(started_at IS NULL OR started_at < %s)")
            params.append(overlap_end)
        if overlap_start is not None:
            clauses.append(
                "(COALESCE(ended_at, lease_expires_at) IS NULL "
                "OR COALESCE(ended_at, lease_expires_at) > %s)")
            params.append(overlap_start)
        return self._list("band", clauses, params, "band_id", limit, offset)

    def get_band(self, band_id):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM calendar_bands WHERE band_id = %s "
                "OR source_native_id = %s ORDER BY band_id LIMIT 1",
                (band_id, band_id),
            )
            row = cur.fetchone()
        return _row_obj(Band, row) if row else None

    def upsert_band(self, band):
        return self._upsert("band", band)

    def batch_upsert_bands(self, rows):
        return self._batch_upsert("band", rows)

    def delete_band(self, band_id):
        return self._delete("band", band_id)

    # -- windows ---------------------------------------------------------------

    def list_windows(self, owner_id=None, from_ts=None, to_ts=None, limit=None):
        clauses, params = [], []
        if owner_id is not None:
            clauses.append("owner_id = %s")
            params.append(owner_id)
        if from_ts is not None:
            clauses.append("starts_at >= %s")
            params.append(from_ts)
        if to_ts is not None:
            clauses.append("starts_at < %s")
            params.append(to_ts)
        return self._list("window", clauses, params, "window_id", limit, 0)

    def upsert_window(self, window):
        return self._upsert("window", window)

    def delete_window(self, window_id):
        return self._delete("window", window_id)

    # -- actuals ---------------------------------------------------------------

    # actual_id is GENERATED ALWAYS AS IDENTITY (schema 001), so an
    # insert that names it needs OVERRIDING SYSTEM VALUE; without it
    # Postgres rejects every with-id upsert (the no-id insert below is
    # unaffected, which is why replace-style writes never noticed).
    _ACTUAL_UPSERT_WITH_ID = (
        "INSERT INTO calendar_actuals (" + ", ".join(ACTUAL_COLUMNS)
        + ") OVERRIDING SYSTEM VALUE VALUES ("
        + ", ".join(f"%({c})s" for c in ACTUAL_COLUMNS) + ") "
        "ON CONFLICT (actual_id) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in ACTUAL_COLUMNS if c != "actual_id")
        + " RETURNING *"
    )
    _ACTUAL_INSERT = (
        "INSERT INTO calendar_actuals ("
        + ", ".join(c for c in ACTUAL_COLUMNS if c != "actual_id") + ") VALUES ("
        + ", ".join(f"%({c})s" for c in ACTUAL_COLUMNS if c != "actual_id")
        + ") RETURNING *"
    )

    def list_actuals(self, series_id=None, source_id=None, from_ts=None,
                     to_ts=None, kind=None, limit=None, offset=None):
        clauses, params = [], []
        for col, value in (("series_id", series_id), ("source_id", source_id),
                           ("kind", kind)):
            if value is not None:
                clauses.append(f"{col} = %s")
                params.append(value)
        if from_ts is not None:
            clauses.append("fired_at >= %s")
            params.append(from_ts)
        if to_ts is not None:
            clauses.append("fired_at < %s")
            params.append(to_ts)
        sql = "SELECT * FROM calendar_actuals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY fired_at, actual_id LIMIT %s OFFSET %s"
        params = params + [limit, offset or 0]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def _actual_sql(self, row: dict) -> str:
        return self._ACTUAL_UPSERT_WITH_ID if row.get("actual_id") is not None \
            else self._ACTUAL_INSERT

    def upsert_actual(self, row):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(self._actual_sql(row), row)
            return dict(cur.fetchone())

    def batch_upsert_actuals(self, rows):
        if not rows:
            return 0
        with_id = [r for r in rows if r.get("actual_id") is not None]
        without_id = [r for r in rows if r.get("actual_id") is None]
        with self._connect() as conn, conn.cursor() as cur:
            if with_id:
                cur.executemany(self._ACTUAL_UPSERT_WITH_ID, with_id)
            if without_id:
                cur.executemany(self._ACTUAL_INSERT, without_id)
        return len(rows)

    def batch_replace_actuals(self, source_id, window_start, window_end, rows):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM calendar_actuals WHERE source_id = %s "
                "AND fired_at >= %s AND fired_at <= %s",
                (source_id, window_start, window_end),
            )
            deleted = cur.rowcount
            with_id = [r for r in rows if r.get("actual_id") is not None]
            without_id = [r for r in rows if r.get("actual_id") is None]
            if with_id:
                cur.executemany(self._ACTUAL_UPSERT_WITH_ID, with_id)
            if without_id:
                cur.executemany(self._ACTUAL_INSERT, without_id)
        return deleted, len(rows)

    def delete_actual(self, actual_id):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM calendar_actuals WHERE actual_id = %s",
                        (actual_id,))
            return cur.rowcount > 0
