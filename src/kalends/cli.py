"""Command line interface.

Subcommands:

* ``kalends demo``: run the example fleet's adapters and projector and
  print the table view. Works out of the box from a source checkout.
* ``kalends table --dump FILE``: read a JSON calendar dump (the shape
  :meth:`kalends.projector.Projection.to_dict` writes: sources, series,
  instances, bands) and render the table view, with filters.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dateutil.parser import isoparse

from .adapters import (
    CronAdapter,
    PostgresJobsAdapter,
    SystemdAdapter,
    YamlCalendarAdapter,
)
from .adapters.base import AdapterResult
from .projector import Projection, project
from .tableview import TableFilters, render_table


def _find_examples(explicit: Optional[str]) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("KALENDS_EXAMPLES_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.cwd() / "examples" / "example-fleet")
    candidates.append(
        Path(__file__).resolve().parents[2] / "examples" / "example-fleet"
    )
    for cand in candidates:
        if (cand / "systemd-timers.json").exists():
            return cand
    raise SystemExit(
        "example fleet not found; pass --examples DIR or set KALENDS_EXAMPLES_DIR"
    )


def _merge(results: list[AdapterResult]) -> dict:
    merged = {"sources": [], "series": [], "instances": [], "bands": [], "actuals": []}
    for res in results:
        merged["sources"].append(res.source)
        merged["series"] += res.series
        merged["instances"] += res.instances
        merged["bands"] += res.bands
        merged["actuals"] += res.actuals
    return merged


def run_demo(examples: Path) -> Projection:
    results = [
        SystemdAdapter.from_files(
            examples / "systemd-timers.json",
            examples / "systemd-units.json",
            source_id="systemd",
            plane="host-a",
        ).collect(),
        CronAdapter.from_file(
            examples / "crontab",
            source_id="crontab",
            plane="host-a",
            owner_id="ops",
        ).collect(),
        YamlCalendarAdapter.from_file(
            examples / "patrols.yaml",
            source_id="patrol-calendar",
            plane="platform",
            default_owner_kind="agent",
        ).collect(),
        PostgresJobsAdapter.from_files(
            examples / "jobs-config.yaml",
            examples / "jobs.json",
            examples / "queue.json",
        ).collect(),
    ]
    parts = _merge(results)
    return project(
        parts["series"],
        provided_instances=parts["instances"],
        bands=parts["bands"],
        actuals=parts["actuals"],
        sources=parts["sources"],
    )


def _parse_time(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    dt = isoparse(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _filters_from_args(args) -> TableFilters:
    return TableFilters(
        plane=args.plane,
        owner=args.owner,
        owner_kind=args.owner_kind,
        source=args.source,
        shape=args.shape,
        start_from=_parse_time(args.from_),
        start_to=_parse_time(args.to),
        include_series=not getattr(args, "no_series", False),
    )


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plane")
    parser.add_argument("--owner")
    parser.add_argument("--owner-kind", dest="owner_kind")
    parser.add_argument("--source")
    parser.add_argument("--shape", choices=["series", "instance", "one-off", "band"])
    parser.add_argument("--from", dest="from_")
    parser.add_argument("--to")
    parser.add_argument(
        "--no-series", action="store_true", help="hide definition rows"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kalends",
        description="One calendar for every agent, job, and long-running "
        "task in a multi-agent fleet.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo_p = sub.add_parser("demo", help="run the example fleet and print its table")
    demo_p.add_argument("--examples", help="path to the example fleet directory")
    demo_p.add_argument(
        "--dump-out", help="also write the projection as a JSON calendar dump"
    )
    _add_filter_args(demo_p)

    table_p = sub.add_parser("table", help="render a JSON calendar dump as a table")
    table_p.add_argument("--dump", required=True, help="JSON dump to read")
    _add_filter_args(table_p)

    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.command == "demo":
        projection = run_demo(_find_examples(args.examples))
        if args.dump_out:
            Path(args.dump_out).write_text(
                json.dumps(projection.to_dict(), indent=2) + "\n"
            )
        print(
            f"Kalends demo: {len(projection.series)} series, "
            f"{len(projection.instances)} instances in horizon, "
            f"{len(projection.bands)} active bands, "
            f"{len(projection.sources)} sources"
        )
        filters = _filters_from_args(args)
        if not filters.any_time_filter():
            # A full 21-day horizon at fleet cadences is thousands of
            # rows. The demo shows a two-day window unless asked for a
            # range; the projection itself is unchanged.
            from datetime import timedelta

            filters.start_from = now - timedelta(hours=12)
            filters.start_to = now + timedelta(hours=36)
            print(
                "Showing a two-day window around now; pass --from/--to "
                "for another range, or dump the projection and use "
                "`kalends table` for the full horizon."
            )
        print()
        sys.stdout.write(render_table(projection, filters, now=now))
        return 0

    if args.command == "table":
        data = json.loads(Path(args.dump).read_text())
        projection = Projection.from_dict(data)
        sys.stdout.write(render_table(projection, _filters_from_args(args), now=now))
        return 0

    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
