"""Generic Postgres job-registry + worker-queue adapter.

Many agent platforms keep the same two tables under different names: a
registry of job definitions (name, schedule kind, cadence, lifecycle)
and a queue of job instances (next run time, status, worker, lease).
This adapter is driven entirely by a config that names the columns, so
it works against any such pair of tables without code changes.

Config (YAML or JSON)::

    source_id: job-registry
    plane: platform            # logical plane for rows with no literal host
    owner_kind: worker
    registry:
      columns:
        name: job_name
        schedule_kind: kind
        cadence_hours: cadence_hours
        idle_delay_seconds: idle_delay_seconds
        host_pattern: host
        lifecycle: lifecycle
        executor: executor
        owner: owner
        updated_at: updated_at
        anchor_at: last_run_at
      kinds:
        recurring: recurring     # the source's word for interval jobs
        self_chain: self-chain   # the source's word for self-chaining jobs
        none: none               # the source's word for "no schedule"
    queue:
      columns:
        id: id
        name: job_name
        next_run_at: next_run_at
        status: status
        worker: worker_id
        claimed_at: claimed_at
        heartbeat_at: heartbeat_at
        lease_expires_at: lease_expires_at
        completed_at: completed_at
        failed_at: failed_at
      statuses:
        pending: pending
        processing: processing
        completed: completed
        failed: failed

Mapping rules:

* Only recurring and self-chain kinds become series. A "none" kind is
  a capability, not a schedule: projecting it would invent an empty
  series. Its queue rows still appear, as standalone instances.
* Recurring kinds take their RRULE from the cadence-hours column.
  Self-chain kinds get no RRULE, ever; their next instance comes from
  the queue.
* A host pattern is a constraint, not a placement: blank patterns and
  patterns containing wildcards land on the configured logical plane.
  Only a literal host value becomes the plane.
* Queue rows: pending becomes the job's next instance; processing
  becomes a band held by the worker, with heartbeat and lease;
  completed and failed become actuals.
* Input rows may come from live queries or from a JSON dump file (a
  bare array, or an object with a ``rows`` list), e.g. produced by
  ``psql -t -A -c "SELECT json_agg(t) FROM (SELECT ... ) t"``. The
  dump path keeps the adapter testable without a live database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from ..cadence import interval_hours_to_rrule
from ..model import Actual, Band, Instance, Series
from .base import AdapterResult, SourceAdapter, parse_timestamp

_WILDCARDS = ("%", "*", "?")


def _load_rows(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("rows", "registry", "queue"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _load_config(config: Any) -> dict:
    if isinstance(config, dict):
        return config
    text = Path(config).read_text()
    if str(config).endswith(".json"):
        return json.loads(text)
    return yaml.safe_load(text)


class PostgresJobsAdapter(SourceAdapter):
    kind = "postgres-job-registry"
    shapes = ("series", "instance", "band")

    def __init__(
        self,
        config: Any,
        registry_rows: Optional[list[dict]] = None,
        queue_rows: Optional[list[dict]] = None,
    ):
        cfg = _load_config(config)
        self.config = cfg
        super().__init__(
            cfg.get("source_id", "job-registry"),
            cfg.get("plane", "platform"),
            sync_interval_sec=cfg.get("sync_interval_sec", 3600),
        )
        self.owner_kind = cfg.get("owner_kind", "worker")
        self.registry_rows = registry_rows or []
        self.queue_rows = queue_rows or []
        reg = cfg.get("registry", {})
        self.reg_cols = reg.get("columns", {})
        self.kinds = reg.get("kinds", {})
        queue = cfg.get("queue", {})
        self.queue_cols = queue.get("columns", {})
        self.statuses = queue.get("statuses", {})

    @classmethod
    def from_files(
        cls,
        config: Any,
        registry_path: Optional[str | Path] = None,
        queue_path: Optional[str | Path] = None,
    ) -> "PostgresJobsAdapter":
        return cls(
            config,
            registry_rows=_load_rows(registry_path) if registry_path else [],
            queue_rows=_load_rows(queue_path) if queue_path else [],
        )

    def _col(self, row: dict, cols: dict, core_name: str):
        key = cols.get(core_name)
        return row.get(key) if key else None

    def _plane_for(self, host_pattern: Any) -> str:
        if host_pattern is None:
            return self.plane
        text = str(host_pattern).strip()
        if not text or any(w in text for w in _WILDCARDS):
            return self.plane
        return text

    def collect(self) -> AdapterResult:
        now = datetime.now(timezone.utc)
        series_out: list[Series] = []
        series_by_name: dict[str, Series] = {}
        row_info: dict[str, dict[str, Any]] = {}
        stats: dict[str, Any] = {"registry_rows": len(self.registry_rows),
                                  "kinds": {}, "queue_statuses": {}}
        last_source_at: Optional[datetime] = None

        for row in self.registry_rows:
            name = self._col(row, self.reg_cols, "name")
            if not name:
                continue
            name = str(name)
            kind_value = self._col(row, self.reg_cols, "schedule_kind")
            kind_text = "" if kind_value is None else str(kind_value)
            stats["kinds"][kind_text] = stats["kinds"].get(kind_text, 0) + 1

            plane = self._plane_for(self._col(row, self.reg_cols, "host_pattern"))
            owner = self._col(row, self.reg_cols, "owner") or self._col(
                row, self.reg_cols, "executor"
            )
            updated = parse_timestamp(self._col(row, self.reg_cols, "updated_at"))
            if updated and (last_source_at is None or updated > last_source_at):
                last_source_at = updated
            row_info[name] = {"plane": plane, "owner": owner}

            lifecycle_raw = self._col(row, self.reg_cols, "lifecycle")
            lifecycle_text = (
                "active" if lifecycle_raw is None else str(lifecycle_raw).strip().lower()
            )
            if lifecycle_text in ("retired", "deprecated", "dormant"):
                lifecycle, enabled = lifecycle_text, False
            elif lifecycle_text == "active":
                lifecycle, enabled = "active", True
            else:
                lifecycle, enabled = "active", lifecycle_text == "active"

            anchor = parse_timestamp(self._col(row, self.reg_cols, "anchor_at"))
            if kind_text == self.kinds.get("recurring"):
                hours = self._col(row, self.reg_cols, "cadence_hours")
                series = Series(
                    series_id=f"{self.source_id}:{name}",
                    source_id=self.source_id,
                    source_native_id=name,
                    title=name,
                    owner_kind=self.owner_kind,
                    owner_id=owner,
                    plane=plane,
                    cadence_dialect="interval",
                    cadence_expr=None if hours is None else f"every {hours}h",
                    rrule=interval_hours_to_rrule(hours),
                    enabled=enabled,
                    lifecycle_state=lifecycle,
                    source_updated_at=updated,
                    synced_at=now,
                    anchor_at=anchor,
                )
            elif kind_text == self.kinds.get("self_chain"):
                idle = self._col(row, self.reg_cols, "idle_delay_seconds")
                series = Series(
                    series_id=f"{self.source_id}:{name}",
                    source_id=self.source_id,
                    source_native_id=name,
                    title=name,
                    owner_kind=self.owner_kind,
                    owner_id=owner,
                    plane=plane,
                    cadence_dialect="self-chain",
                    cadence_expr=(
                        None if idle is None else f"idle_delay_seconds={idle}"
                    ),
                    rrule=None,
                    enabled=enabled,
                    lifecycle_state=lifecycle,
                    source_updated_at=updated,
                    synced_at=now,
                    anchor_at=anchor,
                )
            else:
                continue  # "none" and unrecognised kinds are not series
            series_out.append(series)
            series_by_name[name] = series

        instances: list[Instance] = []
        bands: list[Band] = []
        actuals: list[Actual] = []

        for row in self.queue_rows:
            name = self._col(row, self.queue_cols, "name")
            if not name:
                continue
            name = str(name)
            status_raw = self._col(row, self.queue_cols, "status")
            status_text = "" if status_raw is None else str(status_raw)
            stats["queue_statuses"][status_text] = (
                stats["queue_statuses"].get(status_text, 0) + 1
            )
            series = series_by_name.get(name)
            info = row_info.get(name, {})
            plane = series.plane if series else info.get("plane", self.plane)
            owner = series.owner_id if series else info.get("owner")
            row_id = self._col(row, self.queue_cols, "id")
            native = str(row_id) if row_id is not None else name

            if status_text == self.statuses.get("pending"):
                next_at = parse_timestamp(
                    self._col(row, self.queue_cols, "next_run_at")
                )
                if next_at is None:
                    continue
                instances.append(
                    Instance(
                        instance_id=f"{self.source_id}:queue:{native}",
                        series_id=series.series_id if series else None,
                        source_id=self.source_id,
                        source_native_id=native,
                        title=name,
                        owner_kind=self.owner_kind,
                        owner_id=owner,
                        plane=plane,
                        shape="instance",
                        planned_start=next_at,
                        status="scheduled",
                        synced_at=now,
                    )
                )
            elif status_text == self.statuses.get("processing"):
                started = parse_timestamp(
                    self._col(row, self.queue_cols, "claimed_at")
                ) or parse_timestamp(self._col(row, self.queue_cols, "heartbeat_at"))
                if started is None:
                    continue
                worker = self._col(row, self.queue_cols, "worker")
                bands.append(
                    Band(
                        band_id=f"{self.source_id}:queue:{native}",
                        source_id=self.source_id,
                        source_native_id=native,
                        title=name,
                        owner_kind=self.owner_kind,
                        owner_id=str(worker) if worker is not None else owner,
                        plane=plane,
                        started_at=started,
                        last_heartbeat_at=parse_timestamp(
                            self._col(row, self.queue_cols, "heartbeat_at")
                        ),
                        lease_expires_at=parse_timestamp(
                            self._col(row, self.queue_cols, "lease_expires_at")
                        ),
                        status="active",
                        resource_claims=(
                            {"worker": str(worker)} if worker is not None else {}
                        ),
                    )
                )
            elif status_text in (
                self.statuses.get("completed"),
                self.statuses.get("failed"),
            ):
                done_at = parse_timestamp(
                    self._col(row, self.queue_cols, "completed_at")
                ) or parse_timestamp(self._col(row, self.queue_cols, "failed_at"))
                if done_at is None:
                    continue
                actuals.append(
                    Actual(
                        source_id=self.source_id,
                        source_native_id=native,
                        series_id=series.series_id if series else None,
                        fired_at=done_at,
                        status=(
                            "failed"
                            if status_text == self.statuses.get("failed")
                            else "completed"
                        ),
                    )
                )

        source = self.make_source(last_source_at=last_source_at, synced_at=now)
        stats["series"] = len(series_out)
        stats["instances"] = len(instances)
        stats["bands"] = len(bands)
        return AdapterResult(
            source=source,
            series=series_out,
            instances=instances,
            bands=bands,
            actuals=actuals,
            stats=stats,
        )
