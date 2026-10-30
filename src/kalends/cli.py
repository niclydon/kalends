"""Command line interface.

Subcommands:

* ``kalends demo``: run the example fleet's adapters and projector and
  print the table view. Works out of the box from a source checkout.
* ``kalends table --dump FILE``: read a JSON calendar dump (the shape
  :meth:`kalends.projector.Projection.to_dict` writes: sources, series,
  instances, bands) and render the table view, with filters. With no
  ``--from``/``--to`` it shows the default display window: the
  previous 24 hours through the next 48 hours, stated in the header.
* ``kalends hold create|heartbeat|release|list``: the self-declared
  holds lane over a JSON store file (see docs/HOLDS.md).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dateutil.parser import isoparse

from .adapters import (
    CompletionPointsAdapter,
    CronAdapter,
    FireLogAdapter,
    PostgresJobsAdapter,
    ServiceStatusAdapter,
    SystemdAdapter,
    WorkClaimsAdapter,
    YamlCalendarAdapter,
)
from .adapters.base import AdapterResult
from .holds import HoldError, HoldStore
from .projector import Projection, project
from .tableview import TableFilters, render_table

#: The table view's default display window when no range is given:
#: the previous 24 hours through the next 48 hours (UTC). A fleet's
#: materialised horizon is 21 days; rendering all of it is an audit
#: dump, not a view. The window is stated in the output header.
DEFAULT_WINDOW_PAST = timedelta(hours=24)
DEFAULT_WINDOW_FUTURE = timedelta(hours=48)


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


def _demo_holds(now: datetime) -> list:
    """The holds walkthrough: one live hold and one whose lease has
    already lapsed, created in a throwaway store so the demo stays
    read-only against the repository."""
    store = HoldStore(Path(tempfile.mkdtemp(prefix="kalends-demo-")) / "holds.json")
    store.create_hold(
        "Hold: survey slot for the harbour audit",
        owner_kind="agent",
        owner_id="crimson",
        plane="platform",
        starts_at=now - timedelta(minutes=20),
        lease_seconds=3600,
        resource_claims={"slot_group": "survey"},
        now=now,
        hold_id="hold-demo-live",
    )
    # Created as of three hours ago with a one-hour lease and never
    # heartbeated: materialising it at the demo's now closes it as
    # expired, which is the walkthrough's point.
    store.create_hold(
        "Hold: chart table for the reef survey",
        owner_kind="agent",
        owner_id="azure",
        plane="platform",
        starts_at=now - timedelta(hours=3),
        lease_seconds=3600,
        now=now - timedelta(hours=3),
        hold_id="hold-demo-lapsed",
    )
    return store.bands(now=now)


def run_demo(examples: Path, now: Optional[datetime] = None) -> Projection:
    now = now or datetime.now(timezone.utc)
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
        FireLogAdapter.from_files(
            examples / "firelog-config.yaml",
            examples / "firelog.json",
        ).collect(),
        CompletionPointsAdapter.from_files(
            examples / "completions-config.yaml",
            examples / "completions.json",
        ).collect(),
        ServiceStatusAdapter.from_files(
            examples / "services-config.yaml",
            examples / "services.json",
        ).collect(),
        WorkClaimsAdapter.from_files(
            examples / "claims-config.yaml",
            examples / "claims.json",
        ).collect(),
    ]
    parts = _merge(results)
    parts["bands"] += _demo_holds(now)
    return project(
        parts["series"],
        provided_instances=parts["instances"],
        bands=parts["bands"],
        actuals=parts["actuals"],
        sources=parts["sources"],
        now=now,
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


def _default_window(filters: TableFilters, now: datetime) -> TableFilters:
    """Apply the documented default display window in place."""
    filters.start_from = now - DEFAULT_WINDOW_PAST
    filters.start_to = now + DEFAULT_WINDOW_FUTURE
    return filters


def _hold_band_line(band) -> str:
    state = band.status
    if band.end_reason:
        state = f"{band.status} ({band.end_reason})"
    lease = band.lease_expires_at.isoformat() if band.lease_expires_at else "-"
    return f"{band.band_id}  {state}  lease {lease}  {band.title}"


def _run_hold_command(args) -> int:
    store = HoldStore(args.store)
    now = datetime.now(timezone.utc)
    try:
        if args.hold_command == "create":
            claims = {}
            for pair in args.claim or []:
                key, _, value = pair.partition("=")
                claims[key] = value
            band = store.create_hold(
                args.title,
                owner_kind=args.owner_kind,
                owner_id=args.owner_id,
                plane=args.plane or "",
                starts_at=_parse_time(args.starts_at),
                ends_at=_parse_time(args.ends_at),
                lease_seconds=args.lease_seconds,
                resource_claims=claims,
                now=now,
                hold_id=args.id,
            )
            print(_hold_band_line(band))
        elif args.hold_command == "heartbeat":
            band = store.heartbeat_hold(
                args.id, now=now, lease_seconds=args.lease_seconds
            )
            print(_hold_band_line(band))
        elif args.hold_command == "release":
            band = store.release_hold(
                args.id, now=now, end_reason=args.end_reason
            )
            print(_hold_band_line(band))
        elif args.hold_command == "list":
            bands = store.list_holds(
                now=now, include_closed=not args.active_only
            )
            for band in bands:
                print(_hold_band_line(band))
            if not bands:
                print("(no holds)")
    except HoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


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

    hold_p = sub.add_parser("hold", help="self-declared holds over a JSON store")
    hold_p.add_argument(
        "--store",
        default=os.environ.get("KALENDS_HOLDS_FILE", "kalends-holds.json"),
        help="holds store file (default: $KALENDS_HOLDS_FILE or ./kalends-holds.json)",
    )
    hold_sub = hold_p.add_subparsers(dest="hold_command", required=True)
    create_p = hold_sub.add_parser("create", help="create a hold")
    create_p.add_argument("--title", required=True)
    create_p.add_argument("--owner-kind", dest="owner_kind", required=True)
    create_p.add_argument("--owner-id", dest="owner_id")
    create_p.add_argument("--plane")
    create_p.add_argument("--starts-at", dest="starts_at")
    create_p.add_argument("--ends-at", dest="ends_at")
    create_p.add_argument("--lease-seconds", dest="lease_seconds", type=int)
    create_p.add_argument("--claim", action="append", help="resource claim key=value")
    create_p.add_argument("--id", help="hold id (default: generated)")
    beat_p = hold_sub.add_parser("heartbeat", help="renew a hold's lease")
    beat_p.add_argument("id")
    beat_p.add_argument("--lease-seconds", dest="lease_seconds", type=int)
    rel_p = hold_sub.add_parser("release", help="close a hold")
    rel_p.add_argument("id")
    rel_p.add_argument(
        "--end-reason", dest="end_reason", default="released",
        choices=["released", "completed", "abandoned", "failed", "expired"],
    )
    list_p = hold_sub.add_parser("list", help="list holds")
    list_p.add_argument(
        "--active-only", action="store_true", help="hide closed holds"
    )

    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.command == "hold":
        return _run_hold_command(args)

    if args.command == "demo":
        projection = run_demo(_find_examples(args.examples), now=now)
        if args.dump_out:
            Path(args.dump_out).write_text(
                json.dumps(projection.to_dict(), indent=2) + "\n"
            )
        print(
            f"Kalends demo: {len(projection.series)} series, "
            f"{len(projection.instances)} instances in horizon, "
            f"{len(projection.bands)} bands, "
            f"{len(projection.sources)} sources"
        )
        filters = _filters_from_args(args)
        if not filters.any_time_filter():
            # A full 21-day horizon at fleet cadences is thousands of
            # rows. The demo shows a two-day window unless asked for a
            # range; the projection itself is unchanged.
            filters.start_from = now - timedelta(hours=12)
            filters.start_to = now + timedelta(hours=36)
            print(
                "Showing a two-day window around now; pass --from/--to "
                "for another range, or dump the projection and use "
                "`kalends table` for the full horizon."
            )
        print()
        sys.stdout.write(render_table(projection, filters, now=now))
        holds = [b for b in projection.bands if b.source_id == "holds"]
        if holds:
            print()
            print("Holds walkthrough (self-declared, design 6.2):")
            for band in sorted(holds, key=lambda b: b.band_id):
                print("  " + _hold_band_line(band))
        return 0

    if args.command == "table":
        data = json.loads(Path(args.dump).read_text())
        projection = Projection.from_dict(data)
        filters = _filters_from_args(args)
        defaulted = not filters.any_time_filter()
        if defaulted:
            _default_window(filters, now)
        sys.stdout.write(render_table(projection, filters, now=now))
        if defaulted:
            print(
                "Window: default display window, previous 24h through "
                "next 48h (UTC); pass --from/--to for another range."
            )
        return 0

    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
