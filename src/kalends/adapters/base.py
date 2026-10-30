"""The adapter contract.

An adapter reads one source system's own source of truth and reconciles
it into core objects. Every adapter:

* enumerates definitions (series) the source knows;
* enumerates recent and upcoming occurrences where the source knows
  them (a timer's next elapse, a queue's pending rows) as provided
  instances, plus bands and actuals where the source has them;
* reports freshness: when it synced, and how old the source's own data
  is, so a stale source shows as stale in every view;
* declares the temporal shapes it can express. An adapter that cannot
  express actuals says so, and the calendar shows the gap rather than
  implying coverage;
* keeps the original cadence expression on every series, always.

Adapters are read-only against their sources.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from dateutil.parser import isoparse, parse as loose_parse

from ..model import Actual, Band, Instance, Series, Source


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a timestamp from whatever shape a source uses.

    Accepts aware datetimes, ISO 8601 strings, looser date strings, and
    epoch numbers (seconds, milliseconds, or microseconds, guessed by
    magnitude). Returns an aware UTC datetime or None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        num = float(value)
        if num <= 0:
            return None
        if num > 1e14:
            num /= 1_000_000
        elif num > 1e11:
            num /= 1_000
        dt = datetime.fromtimestamp(num, tz=timezone.utc)
        return dt
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = isoparse(text)
        except ValueError:
            try:
                dt = loose_parse(text)
            except (ValueError, OverflowError):
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

ALL_SHAPES = ("series", "instance", "one-off", "band", "window")


@dataclass
class AdapterResult:
    """Everything one adapter pass produced."""

    source: Source
    series: list[Series] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)
    actuals: list[Actual] = field(default_factory=list)
    # Free-form tallies an adapter wants on the record (rows read, rows
    # skipped and why). Surfaced by tooling, not rendered as calendar data.
    stats: dict = field(default_factory=dict)


class SourceAdapter(abc.ABC):
    """Base class for all adapters."""

    #: Adapter kind name, recorded on the source row (e.g. "systemd").
    kind: str = "abstract"
    #: Temporal shapes this adapter can express.
    shapes: tuple[str, ...] = ("series",)

    def __init__(
        self,
        source_id: str,
        plane: str,
        *,
        sync_interval_sec: Optional[int] = None,
        notes: Optional[str] = None,
    ):
        self.source_id = source_id
        self.plane = plane
        self.sync_interval_sec = sync_interval_sec
        self.notes = notes

    def make_source(
        self,
        *,
        last_source_at: Optional[datetime] = None,
        synced_at: Optional[datetime] = None,
    ) -> Source:
        return Source(
            source_id=self.source_id,
            adapter=self.kind,
            plane=self.plane,
            enabled=True,
            sync_interval_sec=self.sync_interval_sec,
            last_synced_at=synced_at or datetime.now(timezone.utc),
            last_source_at=last_source_at,
            notes=self.notes,
        )

    @abc.abstractmethod
    def collect(self) -> AdapterResult:
        """Read the source and return its calendar objects."""
        raise NotImplementedError
