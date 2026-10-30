"""Crontab adapter.

Parses crontab text: standard 5-field lines, the @-macros (@daily,
@weekly, @hourly, @monthly, @yearly, @reboot), comments, blank lines,
and environment assignments (``NAME=value``), which are skipped.
Anything after the five schedule fields is the command and becomes the
entry's title.

``@reboot`` and cron constructs RRULE cannot express exactly (a
day-of-month and a day-of-week both restricted) still produce series,
with ``rrule=None`` and the original expression kept: the definition is
real even when no honest expansion exists.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..cadence import cron_macro_to_rrule, cron_to_rrule
from ..model import Series
from .base import AdapterResult, SourceAdapter

_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


class CronAdapter(SourceAdapter):
    kind = "cron"
    shapes = ("series",)

    def __init__(
        self,
        text: str,
        *,
        source_id: str = "crontab",
        plane: str = "host-a",
        owner_kind: str = "system",
        owner_id: Optional[str] = None,
        sync_interval_sec: Optional[int] = 3600,
        source_mtime: Optional[datetime] = None,
    ):
        super().__init__(source_id, plane, sync_interval_sec=sync_interval_sec)
        self.text = text
        self.owner_kind = owner_kind
        self.owner_id = owner_id
        self.source_mtime = source_mtime

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "CronAdapter":
        p = Path(path)
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        return cls(p.read_text(), source_mtime=mtime, **kwargs)

    def collect(self) -> AdapterResult:
        now = datetime.now(timezone.utc)
        series_out: list[Series] = []
        skipped = 0

        for lineno, raw_line in enumerate(self.text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if _ENV_LINE.match(line):
                continue
            native_id = f"line-{lineno}"
            if line.startswith("@"):
                parts = line.split(None, 1)
                if len(parts) != 2:
                    skipped += 1
                    continue
                expr, command = parts
                rrule = cron_macro_to_rrule(expr)
            else:
                parts = line.split(None, 5)
                if len(parts) != 6:
                    skipped += 1
                    continue
                expr = " ".join(parts[:5])
                command = parts[5]
                rrule = cron_to_rrule(expr)
            series_out.append(
                Series(
                    series_id=f"{self.source_id}:{native_id}",
                    source_id=self.source_id,
                    source_native_id=native_id,
                    title=command.strip(),
                    owner_kind=self.owner_kind,
                    owner_id=self.owner_id,
                    plane=self.plane,
                    cadence_dialect="cron",
                    cadence_expr=expr,
                    rrule=rrule,
                    synced_at=now,
                )
            )

        source = self.make_source(last_source_at=self.source_mtime, synced_at=now)
        return AdapterResult(
            source=source,
            series=series_out,
            stats={"entries": len(series_out), "skipped_lines": skipped},
        )
