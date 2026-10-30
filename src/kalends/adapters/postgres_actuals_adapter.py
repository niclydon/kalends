"""Generic Postgres actuals adapters: fire logs and completion points.

Phase 1 projected the plan. Phase 2 joins what actually happened, from
the two shapes of actuals record a fleet typically already keeps:

* A **fire log**: one row per firing, with the time it fired and the
  time it was due (would-fire). ``FireLogAdapter`` maps such a table
  into ``Actual`` objects the projector joins to planned instances, so
  planned-versus-actual variance is visible and a planned instance
  that never fired stays visibly un-fired. Rows flagged observed-only
  stay observed: the scheduler saw a firing it did not cause, and the
  calendar never presents it as executed by the fleet.
* A **completion log**: one row per finished agent cycle, with a
  completion time and often an is-empty flag. ``CompletionPointsAdapter``
  maps it into point actuals. A cycle completion is a moment, not an
  interval; no duration is recorded and none is invented.

Both adapters are config-driven exactly like ``PostgresJobsAdapter``:
the config names the source's columns, so any schema shaped like
fire-log or completions works without code changes. Input rows come
from a psql JSON dump file (a bare array, or an object with a ``rows``
list) or are passed in directly.

Fire-log config (YAML or JSON)::

    source_id: fire-log
    plane: platform
    series_source_id: job-registry   # whose series the job keys name
    columns:
      job: job_type
      run_id: run_id
      fired_at: fired_at
      would_fire_at: would_fire_at
      observed_only: observed_only
      outcome: outcome
    outcomes:              # optional: source word -> core status
      error: failed

Completion config::

    source_id: agent-completions
    plane: platform
    series_source_id: patrol-calendar
    series_names:          # optional: agent key -> series native id,
      sable: beacon-health-digest   # when they differ
    columns:
      agent: agent_id
      completed_at: completed_at
      run_id: run_id
      is_empty: is_empty

A fire log can be enormous (the reference deployment's holds ten
million rows). The adapter consumes a bounded extract the caller
pulled; it never asks anyone to copy the whole log into the calendar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..model import Actual
from .base import AdapterResult, SourceAdapter, parse_timestamp
from .postgres_jobs_adapter import _load_config, _load_rows

_TRUTHY = (True, 1, "1", "true", "t", "yes", "y")
_FAILED_WORDS = ("failed", "failure", "error", "errored", "timed-out", "timeout")


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return value in _TRUTHY


class _ActualsBase(SourceAdapter):
    """Shared config plumbing for the actuals adapters."""

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
            cfg.get("source_id", "actuals"),
            cfg.get("plane", "platform"),
            sync_interval_sec=cfg.get("sync_interval_sec", 3600),
        )
        self.cols = cfg.get("columns", {})
        self.series_source_id = cfg.get("series_source_id", self.source_id)
        self.rows = rows or []
        self.now = now

    @classmethod
    def from_files(cls, config: Any, rows_path: Optional[str | Path] = None, **kw):
        return cls(config, rows=_load_rows(rows_path) if rows_path else [], **kw)

    def _col(self, row: dict, core_name: str):
        key = self.cols.get(core_name)
        return row.get(key) if key else None

    def _series_id(self, native: str) -> str:
        return f"{self.series_source_id}:{native}"


class FireLogAdapter(_ActualsBase):
    kind = "postgres-fire-log"
    shapes = ("instance",)

    def collect(self) -> AdapterResult:
        now = self.now or datetime.now(timezone.utc)
        outcomes = self.config.get("outcomes", {}) or {}
        actuals: list[Actual] = []
        last_source_at: Optional[datetime] = None
        stats: dict[str, Any] = {"rows": len(self.rows), "observed": 0}

        for row in self.rows:
            job = self._col(row, "job")
            fired = parse_timestamp(self._col(row, "fired_at"))
            if job is None or fired is None:
                continue
            job = str(job)
            run_id = self._col(row, "run_id")
            native = str(run_id) if run_id is not None else f"{job}@{fired.isoformat()}"
            outcome_raw = self._col(row, "outcome")
            if outcome_raw is not None and str(outcome_raw) in outcomes:
                status = outcomes[str(outcome_raw)]
            elif outcome_raw is not None and str(outcome_raw).strip().lower() in _FAILED_WORDS:
                status = "failed"
            else:
                status = "completed"
            observed = _truthy(self._col(row, "observed_only"))
            if observed:
                stats["observed"] += 1
            actuals.append(
                Actual(
                    source_id=self.source_id,
                    source_native_id=native,
                    series_id=self._series_id(job),
                    fired_at=fired,
                    would_fire_at=parse_timestamp(
                        self._col(row, "would_fire_at")
                    ),
                    observed_only=observed,
                    status=status,
                    kind="firing",
                    extensions={"label": job},
                )
            )
            if last_source_at is None or fired > last_source_at:
                last_source_at = fired

        stats["actuals"] = len(actuals)
        source = self.make_source(last_source_at=last_source_at, synced_at=now)
        return AdapterResult(source=source, actuals=actuals, stats=stats)


class CompletionPointsAdapter(_ActualsBase):
    kind = "postgres-completions"
    shapes = ("instance",)

    def collect(self) -> AdapterResult:
        now = self.now or datetime.now(timezone.utc)
        series_names = self.config.get("series_names", {}) or {}
        actuals: list[Actual] = []
        last_source_at: Optional[datetime] = None
        stats: dict[str, Any] = {"rows": len(self.rows), "empty": 0}

        for row in self.rows:
            agent = self._col(row, "agent")
            completed = parse_timestamp(self._col(row, "completed_at"))
            if agent is None or completed is None:
                continue
            agent = str(agent)
            run_id = self._col(row, "run_id")
            native = (
                str(run_id)
                if run_id is not None
                else f"{agent}@{completed.isoformat()}"
            )
            is_empty = _truthy(self._col(row, "is_empty"))
            if is_empty:
                stats["empty"] += 1
            native_series = str(series_names.get(agent, agent))
            actuals.append(
                Actual(
                    source_id=self.source_id,
                    source_native_id=native,
                    series_id=self._series_id(native_series),
                    fired_at=completed,
                    would_fire_at=None,
                    observed_only=False,
                    status="completed",
                    kind="point",
                    is_empty=is_empty,
                    extensions={"label": agent},
                )
            )
            if last_source_at is None or completed > last_source_at:
                last_source_at = completed

        stats["actuals"] = len(actuals)
        source = self.make_source(last_source_at=last_source_at, synced_at=now)
        return AdapterResult(source=source, actuals=actuals, stats=stats)
