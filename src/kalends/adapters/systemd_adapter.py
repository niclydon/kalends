"""systemd timer adapter.

Reads timer data in the shape of ``systemctl list-timers --all
--output=json``: a list of objects with NEXT, LEFT, LAST, PASSED, UNIT
and ACTIVATES fields (key case is ignored). A companion units mapping
supplies each timer's ``OnCalendar`` expression where the deployment
has one::

    {"backup.timer": {"OnCalendar": "*-*-* 03:00:00",
                      "Description": "Nightly volume backup"}}

Timers with an OnCalendar expression become systemd-calendar series.
Timers without one are relative timers (OnBoot / OnUnitActive style):
they become systemd-relative series with no RRULE, and their NEXT field
is carried as the single provided instance, which is all the source
honestly knows. LAST becomes an actual: a point, not a history.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..cadence import classify_systemd_timer, systemd_calendar_to_rrule
from ..model import Actual, Instance, Series
from .base import AdapterResult, SourceAdapter, parse_timestamp


def _lower_keys(row: dict) -> dict:
    return {str(k).lower(): v for k, v in row.items()}


class SystemdAdapter(SourceAdapter):
    kind = "systemd"
    shapes = ("series", "instance")

    def __init__(
        self,
        timers: list[dict],
        units: Optional[dict[str, dict]] = None,
        *,
        source_id: str = "systemd",
        plane: str = "host-a",
        owner_map: Optional[dict[str, tuple[str, str]]] = None,
        sync_interval_sec: Optional[int] = 3600,
    ):
        super().__init__(source_id, plane, sync_interval_sec=sync_interval_sec)
        if isinstance(timers, dict):
            timers = timers.get("timers", [])
        self.timers = timers
        self.units = {k: _lower_keys(v) for k, v in (units or {}).items()}
        self.owner_map = owner_map or {}

    @classmethod
    def from_files(
        cls,
        timers_path: str | Path,
        units_path: Optional[str | Path] = None,
        **kwargs,
    ) -> "SystemdAdapter":
        timers = json.loads(Path(timers_path).read_text())
        units = (
            json.loads(Path(units_path).read_text()) if units_path else None
        )
        return cls(timers, units, **kwargs)

    def _unit_info(self, *names: Optional[str]) -> dict:
        for name in names:
            if name and name in self.units:
                return self.units[name]
        return {}

    def collect(self) -> AdapterResult:
        now = datetime.now(timezone.utc)
        series_out: list[Series] = []
        instances: list[Instance] = []
        actuals: list[Actual] = []
        last_seen: Optional[datetime] = None

        for raw in self.timers:
            row = _lower_keys(raw)
            unit = row.get("unit")
            if not unit:
                continue
            activates = row.get("activates")
            plane = row.get("plane") or row.get("host") or self.plane
            info = self._unit_info(unit, activates)
            on_calendar = info.get("oncalendar")
            description = info.get("description")

            title = (
                description
                or (activates.removesuffix(".service") if activates else None)
                or unit.removesuffix(".timer")
            )
            owner_kind, owner_id = self.owner_map.get(
                unit, ("system", None)
            )
            series_id = f"{self.source_id}:{plane}:{unit}"

            if on_calendar:
                dialect = "systemd-calendar"
                cadence_expr = on_calendar
                rrule = systemd_calendar_to_rrule(on_calendar)
            else:
                raw_text = " ".join(
                    f"{label}={info[key]}"
                    for label, key in (
                        ("OnUnitActiveSec", "onunitactivesec"),
                        ("OnBootSec", "onbootsec"),
                        ("OnStartupSec", "onstartupsec"),
                    )
                    if info.get(key)
                )
                dialect = classify_systemd_timer(None, raw_text)
                cadence_expr = raw_text or None
                rrule = None

            series = Series(
                series_id=series_id,
                source_id=self.source_id,
                source_native_id=unit,
                title=title,
                owner_kind=owner_kind,
                owner_id=owner_id,
                plane=plane,
                cadence_dialect=dialect,
                cadence_expr=cadence_expr,
                rrule=rrule,
                synced_at=now,
            )
            series_out.append(series)

            next_at = parse_timestamp(row.get("next"))
            if next_at is not None:
                instances.append(
                    Instance(
                        instance_id=f"{series_id}@next:{next_at.isoformat()}",
                        series_id=series_id,
                        source_id=self.source_id,
                        source_native_id=unit,
                        title=title,
                        owner_kind=owner_kind,
                        owner_id=owner_id,
                        plane=plane,
                        shape="instance",
                        planned_start=next_at,
                        status="scheduled",
                        synced_at=now,
                    )
                )
            last_at = parse_timestamp(row.get("last"))
            if last_at is not None:
                actuals.append(
                    Actual(
                        source_id=self.source_id,
                        source_native_id=unit,
                        series_id=series_id,
                        fired_at=last_at,
                        status="completed",
                    )
                )
                if last_seen is None or last_at > last_seen:
                    last_seen = last_at

        source = self.make_source(last_source_at=last_seen, synced_at=now)
        return AdapterResult(
            source=source,
            series=series_out,
            instances=instances,
            actuals=actuals,
            stats={"timers": len(series_out)},
        )
