"""The projector: definitions in, materialised instances out.

Storage-agnostic. Adapters hand it series definitions plus whatever
occurrences the sources themselves know about (a worker queue's next
runs, a timer's next elapse). The projector materialises instances over
a rolling horizon: the last 7 days and the next 14 days by default.

Projection rules, in order:

1. Only enabled series in lifecycle ``active`` project instances.
   Retired, deprecated, and dormant definitions stay visible as
   definitions and paint nothing.
2. A series with an RRULE is expanded. A source-provided instance that
   lands on an expanded occurrence (within five minutes) supersedes the
   expanded one: the source's own next-run field beats arithmetic.
3. A series without an RRULE gets exactly the instances its source
   provided, and no others. Self-chaining jobs therefore get exactly
   one instance, taken from the queue's next_run_at. Later occurrences
   are never fabricated: a chain's future runs are not clock times, and
   the calendar does not invent clock times.
4. Source-provided instances with no series (one-offs, ad-hoc runs)
   pass through untouched.
5. Actuals join to planned instances of the same series near their
   would-fire time, filling actual times and completion status. An
   actual that matches nothing becomes a standalone completed or failed
   instance, because work that happened is a fact even when nobody
   planned it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .expand import expand_rrule
from .model import Actual, Band, Instance, Series, Source

SUPERSEDE_TOLERANCE = timedelta(minutes=5)
ACTUAL_MATCH_TOLERANCE = timedelta(minutes=15)


@dataclass
class Projection:
    sources: list[Source] = field(default_factory=list)
    series: list[Series] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sources": [s.to_dict() for s in self.sources],
            "series": [s.to_dict() for s in self.series],
            "instances": [i.to_dict() for i in self.instances],
            "bands": [b.to_dict() for b in self.bands],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Projection":
        return cls(
            sources=[Source.from_dict(d) for d in data.get("sources", [])],
            series=[Series.from_dict(d) for d in data.get("series", [])],
            instances=[Instance.from_dict(d) for d in data.get("instances", [])],
            bands=[Band.from_dict(d) for d in data.get("bands", [])],
        )


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _in_horizon(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    return dt is not None and start <= dt <= end


def project(
    series_list: Iterable[Series],
    provided_instances: Iterable[Instance] = (),
    bands: Iterable[Band] = (),
    actuals: Iterable[Actual] = (),
    sources: Iterable[Source] = (),
    now: Optional[datetime] = None,
    past_days: int = 7,
    future_days: int = 14,
) -> Projection:
    """Materialise instances for the horizon around ``now``."""
    now = _aware(now) or datetime.now(timezone.utc)
    horizon_start = now - timedelta(days=past_days)
    horizon_end = now + timedelta(days=future_days)

    all_series = list(series_list)
    by_id = {s.series_id: s for s in all_series}
    provided = list(provided_instances)
    provided_by_series: dict[str, list[Instance]] = {}
    standalone: list[Instance] = []
    for inst in provided:
        if inst.series_id and inst.series_id in by_id:
            provided_by_series.setdefault(inst.series_id, []).append(inst)
        else:
            standalone.append(inst)

    instances: list[Instance] = []
    seen_ids: set[str] = set()

    def emit(inst: Instance) -> None:
        if inst.instance_id in seen_ids:
            return
        if not _in_horizon(_aware(inst.planned_start), horizon_start, horizon_end):
            return
        seen_ids.add(inst.instance_id)
        instances.append(inst)

    for series in all_series:
        if not series.enabled or series.lifecycle_state != "active":
            continue
        own = provided_by_series.get(series.series_id, [])
        if series.rrule:
            anchor = _aware(series.source_updated_at) or horizon_start
            starts = expand_rrule(
                series.rrule, anchor, horizon_start, horizon_end, series.timezone
            )
            used: set[int] = set()
            for start in starts:
                match = None
                for idx, cand in enumerate(own):
                    if idx in used or cand.planned_start is None:
                        continue
                    if abs(_aware(cand.planned_start) - start) <= SUPERSEDE_TOLERANCE:
                        match = idx
                        break
                if match is not None:
                    used.add(match)
                    emit(own[match])
                else:
                    end = (
                        start + timedelta(seconds=series.expected_duration_sec)
                        if series.expected_duration_sec
                        else None
                    )
                    emit(
                        Instance(
                            instance_id=f"{series.series_id}@{start.isoformat()}",
                            series_id=series.series_id,
                            source_id=series.source_id,
                            source_native_id=series.source_native_id,
                            title=series.title,
                            owner_kind=series.owner_kind,
                            owner_id=series.owner_id,
                            plane=series.plane,
                            shape="instance",
                            planned_start=start,
                            planned_end=end,
                            status="scheduled",
                            resource_claims=dict(series.resource_claims),
                            source_updated_at=series.source_updated_at,
                            synced_at=series.synced_at,
                        )
                    )
            # Provided instances that matched no occurrence still stand:
            # the source says this run exists, so the calendar shows it.
            for idx, cand in enumerate(own):
                if idx not in used:
                    emit(cand)
        else:
            # No RRULE: the source's own occurrences are all there is.
            if series.cadence_dialect == "self-chain":
                # A chain has exactly one next run by construction. If a
                # queue anomalously offers more, take the earliest and
                # let the extra rows be reconciled at the source.
                own = sorted(
                    own,
                    key=lambda c: _aware(c.planned_start) or horizon_end,
                )[:1]
            for cand in own:
                emit(cand)

    for inst in standalone:
        emit(inst)

    _apply_actuals(instances, list(actuals), horizon_start, horizon_end)

    instances.sort(key=lambda i: (_aware(i.planned_start) or horizon_start, i.title))
    return Projection(
        sources=list(sources),
        series=all_series,
        instances=instances,
        bands=list(bands),
    )


def _apply_actuals(
    instances: list[Instance],
    actuals: list[Actual],
    horizon_start: datetime,
    horizon_end: datetime,
) -> None:
    by_series: dict[str, list[Instance]] = {}
    for inst in instances:
        if inst.series_id:
            by_series.setdefault(inst.series_id, []).append(inst)

    for actual in actuals:
        target_time = _aware(actual.would_fire_at) or _aware(actual.fired_at)
        match: Optional[Instance] = None
        if actual.series_id:
            for cand in by_series.get(actual.series_id, []):
                if cand.planned_start is None or cand.actual_start is not None:
                    continue
                if abs(_aware(cand.planned_start) - target_time) <= ACTUAL_MATCH_TOLERANCE:
                    match = cand
                    break
        if match is not None:
            match.actual_start = _aware(actual.fired_at)
            match.actual_end = _aware(actual.fired_at)
            match.status = actual.status
        elif _in_horizon(_aware(actual.fired_at), horizon_start, horizon_end):
            instances.append(
                Instance(
                    instance_id=f"actual:{actual.source_id}:{actual.source_native_id}",
                    series_id=actual.series_id,
                    source_id=actual.source_id,
                    source_native_id=actual.source_native_id,
                    title=actual.series_id or actual.source_native_id,
                    shape="instance",
                    planned_start=_aware(actual.would_fire_at),
                    actual_start=_aware(actual.fired_at),
                    actual_end=_aware(actual.fired_at),
                    status=actual.status,
                )
            )
