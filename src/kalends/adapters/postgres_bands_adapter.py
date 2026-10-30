"""Generic Postgres band adapters: service status and work claims.

Bands are long-running work with a start and a heartbeat but no
scheduled end. Two source shapes recur in fleets, and both are mapped
here by config alone, like the other Postgres adapters:

* A **service status table**: one row per service, carrying its
  current status, the moment that status began (status-since), and
  the last time it was checked. ``ServiceStatusAdapter`` makes one
  open band per service, starting at status-since, with the check
  time as its heartbeat. The source's own status word is preserved in
  the band's extensions; a configurable map may also translate it into
  a core band status (a degraded service reads as blocked, an ok one
  as active).
* A **work-claims table**: one row per claim on a piece of work, with
  claim, heartbeat, lease, and release times and an outcome.
  ``WorkClaimsAdapter`` makes one band per claim: open while held,
  closed when the claim resolves, with the end reason mapped from the
  claim status (completed, released, expired, abandoned, failed,
  observed-stopped). A claim whose lease has passed without a release
  closes as expired at the lease time: a lease is a promise, and the
  calendar holds the source to it.

Config for services (YAML or JSON)::

    source_id: service-status
    plane: platform
    owner_kind: system
    columns:
      service: service
      status: current_status
      status_since: status_since
      last_checked_at: last_checked_at
      plane: host            # optional per-row plane column
    status_map:              # optional: source word -> band status
      degraded: blocked

Config for claims::

    source_id: work-claims
    plane: host-a
    owner_kind: worker
    columns:
      id: claim_id
      item: item_ref
      item_title: item_title   # optional; else item_titles, else the ref
      status: claim_status
      runner: runner_id
      runner_kind: runner_kind
      lane: lane               # optional; recorded as a claim
      claimed_at: claimed_at
      heartbeat_at: heartbeat_at
      lease_expires_at: lease_expires_at
      released_at: released_at
      outcome: outcome
    item_titles:               # optional ref -> title map
      WL-1042: "Chart room refit"
    status_map:                # optional overrides, per source word:
      blocked: {status: blocked}
      finished: {status: done, end_reason: completed}
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..model import Band
from .base import AdapterResult, SourceAdapter, parse_timestamp
from .postgres_jobs_adapter import _load_config, _load_rows

# Source claim-status word -> (core band status, end reason or None).
_DEFAULT_CLAIM_MAP: dict[str, tuple[str, Optional[str]]] = {
    "claimed": ("active", None),
    "active": ("active", None),
    "processing": ("active", None),
    "in-progress": ("active", None),
    "held": ("active", None),
    "blocked": ("blocked", None),
    "completed": ("done", "completed"),
    "complete": ("done", "completed"),
    "done": ("done", "completed"),
    "released": ("done", "released"),
    "expired": ("done", "expired"),
    "abandoned": ("done", "abandoned"),
    "failed": ("done", "failed"),
    "observed-stopped": ("done", "observed-stopped"),
}


class _BandsBase(SourceAdapter):
    def __init__(
        self,
        config: Any,
        rows: Optional[list[dict]] = None,
        *,
        now: Optional[datetime] = None,
    ):
        cfg = _load_config(config)
        self.config = cfg
        super().__init__(
            cfg.get("source_id", "bands"),
            cfg.get("plane", "platform"),
            sync_interval_sec=cfg.get("sync_interval_sec", 3600),
        )
        self.cols = cfg.get("columns", {})
        self.owner_kind = cfg.get("owner_kind", "system")
        self.rows = rows or []
        self.now = now

    @classmethod
    def from_files(cls, config: Any, rows_path: Optional[str | Path] = None, **kw):
        return cls(config, rows=_load_rows(rows_path) if rows_path else [], **kw)

    def _col(self, row: dict, core_name: str):
        key = self.cols.get(core_name)
        return row.get(key) if key else None


class ServiceStatusAdapter(_BandsBase):
    kind = "postgres-service-status"
    shapes = ("band",)

    def collect(self) -> AdapterResult:
        now = self.now or datetime.now(timezone.utc)
        status_map = self.config.get("status_map", {}) or {}
        bands: list[Band] = []
        last_source_at: Optional[datetime] = None
        stats: dict[str, Any] = {"rows": len(self.rows), "statuses": {}}

        for row in self.rows:
            service = self._col(row, "service")
            since = parse_timestamp(self._col(row, "status_since"))
            if service is None or since is None:
                continue
            service = str(service)
            raw_status = self._col(row, "status")
            raw_text = "" if raw_status is None else str(raw_status)
            stats["statuses"][raw_text] = stats["statuses"].get(raw_text, 0) + 1
            checked = parse_timestamp(self._col(row, "last_checked_at"))
            plane = self._col(row, "plane") or self.plane
            bands.append(
                Band(
                    band_id=f"{self.source_id}:service:{service}",
                    source_id=self.source_id,
                    source_native_id=service,
                    title=service,
                    owner_kind=self.owner_kind,
                    owner_id=None,
                    plane=str(plane),
                    started_at=since,
                    last_heartbeat_at=checked,
                    status=status_map.get(raw_text, "active"),
                    extensions={"band_kind": "service", "status_value": raw_text},
                )
            )
            if checked and (last_source_at is None or checked > last_source_at):
                last_source_at = checked

        stats["bands"] = len(bands)
        source = self.make_source(last_source_at=last_source_at, synced_at=now)
        return AdapterResult(source=source, bands=bands, stats=stats)


class WorkClaimsAdapter(_BandsBase):
    kind = "postgres-work-claims"
    shapes = ("band",)

    def _status_for(self, raw_text: str) -> tuple[str, Optional[str]]:
        overrides = self.config.get("status_map", {}) or {}
        hit = overrides.get(raw_text)
        if isinstance(hit, dict):
            return hit.get("status", "active"), hit.get("end_reason")
        if isinstance(hit, str):
            return hit, None
        return _DEFAULT_CLAIM_MAP.get(raw_text.strip().lower(), ("active", None))

    def collect(self) -> AdapterResult:
        now = self.now or datetime.now(timezone.utc)
        item_titles = self.config.get("item_titles", {}) or {}
        bands: list[Band] = []
        last_source_at: Optional[datetime] = None
        stats: dict[str, Any] = {
            "rows": len(self.rows),
            "statuses": {},
            "end_reasons": {},
        }

        for row in self.rows:
            ref = self._col(row, "item")
            claimed = parse_timestamp(self._col(row, "claimed_at"))
            if ref is None or claimed is None:
                continue
            ref = str(ref)
            row_id = self._col(row, "id")
            native = str(row_id) if row_id is not None else ref
            raw_status = self._col(row, "status")
            raw_text = "" if raw_status is None else str(raw_status)
            stats["statuses"][raw_text] = stats["statuses"].get(raw_text, 0) + 1
            status, end_reason = self._status_for(raw_text)

            heartbeat = parse_timestamp(self._col(row, "heartbeat_at"))
            lease = parse_timestamp(self._col(row, "lease_expires_at"))
            released = parse_timestamp(self._col(row, "released_at"))
            ended_at: Optional[datetime] = None
            if end_reason is not None:
                ended_at = released
            elif released is not None:
                # A release timestamp is a closing event whatever the
                # status word says (a blocked claim that was let go is
                # closed, not eternally blocked).
                status, end_reason, ended_at = "done", "released", released
            elif lease is not None and lease < now:
                # Held on paper, dead in fact: the lease passed with no
                # release and no heartbeat can revive it now.
                status, end_reason, ended_at = "done", "expired", lease

            title = (
                self._col(row, "item_title")
                or item_titles.get(ref)
                or ref
            )
            runner = self._col(row, "runner")
            runner_kind = self._col(row, "runner_kind")
            claims: dict[str, Any] = {}
            if runner_kind is not None:
                claims["runner_kind"] = str(runner_kind)
            lane = self._col(row, "lane")
            if lane is not None:
                claims["lane"] = str(lane)
            outcome = self._col(row, "outcome")
            extensions: dict[str, Any] = {"band_kind": "claim"}
            if outcome is not None:
                extensions["outcome"] = str(outcome)
            if end_reason is not None:
                stats["end_reasons"][end_reason] = (
                    stats["end_reasons"].get(end_reason, 0) + 1
                )
            for stamp in (heartbeat, released, claimed):
                if stamp and (last_source_at is None or stamp > last_source_at):
                    last_source_at = stamp
            bands.append(
                Band(
                    band_id=f"{self.source_id}:claim:{native}",
                    source_id=self.source_id,
                    source_native_id=native,
                    title=str(title),
                    owner_kind=self.owner_kind,
                    owner_id=str(runner) if runner is not None else None,
                    plane=self.plane,
                    started_at=claimed,
                    last_heartbeat_at=heartbeat,
                    lease_expires_at=lease,
                    ended_at=ended_at,
                    end_reason=end_reason,
                    status=status,
                    resource_claims=claims,
                    extensions=extensions,
                )
            )

        stats["bands"] = len(bands)
        source = self.make_source(last_source_at=last_source_at, synced_at=now)
        return AdapterResult(source=source, bands=bands, stats=stats)
