"""The projector: definitions in, materialised instances out.

Storage-agnostic. Adapters hand it series definitions plus whatever
occurrences the sources themselves know about (a worker queue's next
runs, a timer's next elapse). The projector materialises instances over
a rolling horizon: the last 7 days and the next 14 days by default.

Projection rules, in order:

1. Only enabled series in lifecycle ``active`` project instances.
   Retired, deprecated, and dormant definitions stay visible as
   definitions and paint nothing.
2. A series with an RRULE is expanded. Expansion phase-locks to the
   series' anchor (``anchor_at``: a source-reported occurrence) when
   one exists, and falls back to the source's last-update time when it
   does not. A series whose expansion phase rests on that fallback,
   and whose rule does not itself pin the phase (BYHOUR/BYDAY style),
   is marked ``phase_approximate`` in its extensions: cadence-true,
   but the minute-of-hour is an artefact of the anchor, and views can
   say so. A source-provided instance that lands on an expanded
   occurrence (within five minutes) supersedes the expanded one: the
   source's own next-run field beats arithmetic.
3. A series without an RRULE gets exactly the instances its source
   provided, and no others. Self-chaining jobs therefore get exactly
   one instance, taken from the queue's next_run_at. Later occurrences
   are never fabricated: a chain's future runs are not clock times, and
   the calendar does not invent clock times.
4. Source-provided instances with no series (one-offs, ad-hoc runs)
   pass through untouched.
5. Actuals join to planned instances of the same series near their
   would-fire time, filling actual times and completion status, and
   recording the variance (fired minus would-fire, or minus planned)
   in the instance's extensions. An ``observed_only`` actual marks the
   instance observed: the fleet saw the firing, it did not cause it,
   and the calendar never presents it as executed-by-us. An actual
   that matches nothing becomes a standalone completed or failed
   instance, because work that happened is a fact even when nobody
   planned it.
6. Completion points (``Actual.kind == "point"``) are moments, not
   intervals. Matched to a planned instance they set its actual start
   and completion status but no actual end: a cycle has no recorded
   duration and the calendar does not invent one. Unmatched, they
   become point instances with an actual time and no planned time.
   ``is_empty`` rides along into the instance's extensions either way.
7. A planned instance in the past that no actual matched stays
   ``scheduled`` with null actual fields. That is the missed firing,
   visible as itself; views mark it, the projector does not rewrite
   its status into a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .expand import expand_rrule
from .model import Actual, Band, Instance, Series, Source

SUPERSEDE_TOLERANCE = timedelta(minutes=5)
ACTUAL_MATCH_TOLERANCE = timedelta(minutes=15)
# A completion point belongs to the nearest planned cycle of its series
# within half a day. Cycle cadences are hours-scale; anything farther
# away is a different cycle.
POINT_MATCH_TOLERANCE = timedelta(hours=12)


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


def _rrule_parts(rrule_text: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    text = rrule_text.upper().replace("RRULE:", "")
    for chunk in text.split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            parts[key.strip()] = value.strip()
    return parts


# Parts that pin occurrences to calendar positions a whole-period
# step back could shift; series carrying them keep their anchor as-is.
_STEP_BACK_BLOCKERS = ("BYMONTHDAY", "BYSETPOS", "BYWEEKNO", "BYYEARDAY")
_FREQ_STEPS = {
    "SECONDLY": timedelta(seconds=1),
    "MINUTELY": timedelta(minutes=1),
    "HOURLY": timedelta(hours=1),
    "DAILY": timedelta(days=1),
    "WEEKLY": timedelta(weeks=1),
}


def _effective_anchor(
    series: Series, anchor: datetime, horizon_start: datetime
) -> datetime:
    """Step an anchor back by whole periods to cover the horizon.

    An RRULE generates nothing before its dtstart, so an anchor at a
    recent occurrence (a last firing this morning) would silently
    truncate the past week of a series that has run for years.
    Stepping back by whole periods preserves the phase exactly for
    interval-style rules, which is the only case where the step is
    provably phase-preserving; anything with calendar-pinned parts
    keeps its anchor untouched.
    """
    if anchor <= horizon_start or not series.rrule:
        return anchor
    parts = _rrule_parts(series.rrule)
    if any(key in parts for key in _STEP_BACK_BLOCKERS):
        return anchor
    freq = parts.get("FREQ", "")
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except ValueError:
        return anchor
    if interval < 1:
        return anchor
    step = _FREQ_STEPS.get(freq)
    if step is not None:
        span = anchor - horizon_start
        periods = span // (step * interval) + 1
        return anchor - periods * step * interval
    if freq in ("MONTHLY", "YEARLY"):
        from dateutil.relativedelta import relativedelta

        step = relativedelta(months=interval if freq == "MONTHLY" else 0,
                             years=interval if freq == "YEARLY" else 0)
        candidate = anchor
        for _ in range(1200):
            if candidate <= horizon_start:
                return candidate
            candidate = candidate - step
        return anchor
    return anchor


def _phase_approximate(series: Series) -> bool:
    """True when an RRULE series' expansion phase rests on the
    fallback anchor (the source's last-update time) rather than on a
    reported occurrence or on phase pinned inside the rule itself."""
    if not series.rrule or _aware(series.anchor_at) is not None:
        return False
    parts = _rrule_parts(series.rrule)
    freq = parts.get("FREQ", "")
    time_pinned = "BYHOUR" in parts
    if freq == "DAILY":
        return not time_pinned
    if freq == "WEEKLY":
        return not (time_pinned and "BYDAY" in parts)
    if freq in ("MONTHLY", "YEARLY"):
        return not (time_pinned and ("BYMONTHDAY" in parts or "BYDAY" in parts))
    # Interval-style frequencies (HOURLY, MINUTELY, ...): the phase is
    # the anchor's phase unless the rule pins it outright.
    return not (time_pinned and "BYMINUTE" in parts)


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
            if _phase_approximate(series):
                series.extensions["phase_approximate"] = True
            anchor = _effective_anchor(
                series,
                _aware(series.anchor_at)
                or _aware(series.source_updated_at)
                or horizon_start,
                horizon_start,
            )
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

    _apply_actuals(instances, list(actuals), by_id, horizon_start, horizon_end)

    # Bound marking: instances of a series whose cadence semantics
    # are bound (resource_claims.cadence_semantics == "bound") are
    # owed by the cadence; every other series' instances stay
    # unmarked. Applied last so expanded, provided, and
    # actual-derived instances are all covered.
    for inst in instances:
        series = by_id.get(inst.series_id or "")
        if series is not None and (series.resource_claims or {}).get(
                "cadence_semantics") == "bound":
            inst.bound_expected = True

    instances.sort(
        key=lambda i: (
            _aware(i.planned_start) or _aware(i.actual_start) or horizon_start,
            i.title,
        )
    )
    return Projection(
        sources=list(sources),
        series=all_series,
        instances=instances,
        bands=list(bands),
    )


def _annotate_from_actual(inst: Instance, actual: Actual) -> None:
    fired = _aware(actual.fired_at)
    baseline = _aware(actual.would_fire_at) or _aware(inst.planned_start)
    inst.extensions["fired_at"] = fired.isoformat() if fired else None
    if actual.would_fire_at is not None:
        inst.extensions["would_fire_at"] = _aware(actual.would_fire_at).isoformat()
    if fired is not None and baseline is not None:
        inst.extensions["variance_sec"] = int((fired - baseline).total_seconds())
    if actual.observed_only:
        inst.extensions["observed"] = True
    if actual.kind == "point":
        inst.extensions["point"] = True
    if actual.is_empty:
        inst.extensions["empty"] = True


def _apply_actuals(
    instances: list[Instance],
    actuals: list[Actual],
    series_by_id: dict[str, Series],
    horizon_start: datetime,
    horizon_end: datetime,
) -> None:
    by_series: dict[str, list[Instance]] = {}
    for inst in instances:
        if inst.series_id:
            by_series.setdefault(inst.series_id, []).append(inst)

    for actual in actuals:
        fired = _aware(actual.fired_at)
        is_point = actual.kind == "point"
        target_time = _aware(actual.would_fire_at) or fired
        tolerance = POINT_MATCH_TOLERANCE if is_point else ACTUAL_MATCH_TOLERANCE
        match: Optional[Instance] = None
        if actual.series_id and target_time is not None:
            candidates = [
                cand
                for cand in by_series.get(actual.series_id, [])
                if cand.planned_start is not None and cand.actual_start is None
            ]
            candidates.sort(
                key=lambda c: abs(_aware(c.planned_start) - target_time)
            )
            if candidates and (
                abs(_aware(candidates[0].planned_start) - target_time) <= tolerance
            ):
                match = candidates[0]
        if match is not None:
            match.actual_start = fired
            # A firing is a point observation of a run: its end is the
            # firing time unless a duration source says otherwise. A
            # completion point has no end at all, and none is invented.
            match.actual_end = None if is_point else fired
            match.status = actual.status
            _annotate_from_actual(match, actual)
        elif _in_horizon(fired, horizon_start, horizon_end):
            series = series_by_id.get(actual.series_id or "")
            label = actual.extensions.get("label")
            standalone = Instance(
                instance_id=f"actual:{actual.source_id}:{actual.source_native_id}",
                series_id=actual.series_id if series is not None else None,
                source_id=actual.source_id,
                source_native_id=actual.source_native_id,
                title=(
                    series.title
                    if series is not None
                    else (str(label) if label else (actual.series_id or actual.source_native_id))
                ),
                owner_kind=series.owner_kind if series is not None else "system",
                owner_id=series.owner_id if series is not None else None,
                plane=series.plane if series is not None else "",
                shape="instance",
                planned_start=_aware(actual.would_fire_at),
                actual_start=fired,
                actual_end=None if is_point else fired,
                status=actual.status,
            )
            _annotate_from_actual(standalone, actual)
            instances.append(standalone)
            if standalone.series_id:
                by_series.setdefault(standalone.series_id, []).append(standalone)
