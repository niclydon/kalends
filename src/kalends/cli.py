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
* ``kalends band``: the band view (resource timeline), rows by owner
  or by plane, day or week scale, over a dump (``--dump``) or the demo
  fleet, with the table view's default window when no range is given.
* ``kalends hour --at ISO [--day]``: the hour drill-down over the
  same dump/demo input.
* ``kalends agent --owner-kind K --owner ID``: one owner's agent
  page over the same input; ``kalends agents`` lists the owners
  present in the data with counts.
* ``kalends gmirror``: mirror the curated subset (see
  docs/MIRROR.md) to a Google Calendar through a deployment-supplied
  client; ``--dry-run`` prints the desired set (and, with a client,
  the planned actions) and needs no client.
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
from .agentpage import render_agent_page, render_owners_index
from .bandview import (
    BandFilters,
    HourFilters,
    TimelineData,
    render_band_view,
    render_hour,
)
from .holds import HoldError, HoldStore
from .gmirror import eligible_items, reconcile
from .model import Actual, Window
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


def _load_timeline_data(args, now: datetime) -> TimelineData:
    """The dump/demo input every view shares: a JSON dump when
    ``--dump`` is given (with optional ``actuals`` and ``windows``
    arrays alongside the projection shape), otherwise the example
    fleet demo projection."""
    dump = getattr(args, "dump", None)
    if dump:
        data = json.loads(Path(dump).read_text())
        projection = Projection.from_dict(data)
        actuals = [Actual.from_dict(d) for d in data.get("actuals", [])]
        windows = [Window.from_dict(d) for d in data.get("windows", [])]
        return TimelineData.from_projection(
            projection, actuals=actuals, windows=windows
        )
    projection = run_demo(_find_examples(getattr(args, "examples", None)), now=now)
    return TimelineData.from_projection(projection)


def _add_view_input_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dump", help="JSON dump to read (default: demo fleet)")
    parser.add_argument("--examples", help="path to the example fleet directory")
    parser.add_argument("--plane")
    parser.add_argument("--owner")
    parser.add_argument("--owner-kind", dest="owner_kind")
    parser.add_argument("--source")
    parser.add_argument("--from", dest="from_")
    parser.add_argument("--to")
    parser.add_argument(
        "--no-actuals", action="store_true",
        help="planned only: hide actual marks (planned vs actual toggle)",
    )


def _hold_band_line(band) -> str:
    state = band.status
    if band.end_reason:
        state = f"{band.status} ({band.end_reason})"
    lease = band.lease_expires_at.isoformat() if band.lease_expires_at else "-"
    return f"{band.band_id}  {state}  lease {lease}  {band.title}"



def _parse_owners(raw_values, data) -> set:
    """Resolve --owners: repeated and/or comma-separated values.

    With no --owners at all, every owner present in the input is
    eligible, so a bare dry-run over a small fleet shows its set.
    """
    if not raw_values:
        owners = set()
        for row in list(data.bands) + list(data.instances):
            if row.owner_id:
                owners.add(row.owner_id)
        return owners
    owners = set()
    for raw in raw_values:
        for part in str(raw).split(","):
            part = part.strip()
            if part:
                owners.add(part)
    return owners


def _gmirror_client_from_env():
    """The deployment-supplied Google client factory hook.

    Reads ``KALENDS_GMIRROR_CLIENT_FACTORY`` (``some.module:factory``
    or ``some.module.factory``), a zero-argument callable in the
    deployment's own code returning a ``GoogleClient``. Returns
    ``None`` when the variable is unset: the public core ships no
    client and holds no Google credentials (see docs/MIRROR.md).
    """
    import importlib

    spec = os.environ.get("KALENDS_GMIRROR_CLIENT_FACTORY")
    if not spec:
        return None
    if ":" in spec:
        module_name, _, attr_path = spec.partition(":")
    else:
        module_name, _, attr_path = spec.rpartition(".")
    if not module_name or not attr_path:
        raise ValueError(
            "KALENDS_GMIRROR_CLIENT_FACTORY must name a factory "
            "as some.module:factory"
        )
    obj = importlib.import_module(module_name)
    for attr in attr_path.split("."):
        obj = getattr(obj, attr)
    if callable(obj):
        return obj()
    return obj


def _run_gmirror_command(args, now: datetime) -> int:
    data = _load_timeline_data(args, now)
    owners = _parse_owners(getattr(args, "owners", None), data)
    window_start = _parse_time(args.from_) or (
        now - timedelta(days=args.window_days_back)
    )
    window_end = _parse_time(args.to) or (
        now + timedelta(days=args.window_days_ahead)
    )
    desired = eligible_items(
        data.bands, data.instances, owners, window_start, window_end
    )
    timezone_name = args.timezone_name or "UTC"
    calendar_id = args.calendar_id or "primary"
    try:
        client = _gmirror_client_from_env()
    except Exception as exc:  # factory misconfigured in deployment code
        print(f"error: could not load the Google client factory: {exc}",
              file=sys.stderr)
        return 2

    header = (
        f"gmirror: window {window_start.isoformat()} .. "
        f"{window_end.isoformat()}, owners "
        f"{', '.join(sorted(owners)) or '(none)'}, "
        f"timezone {timezone_name}, calendar {calendar_id}"
    )

    if args.dry_run:
        print(header)
        print("dry-run: no writes will be made")
        for item in desired:
            print(
                f"  {item.key}  {item.start.isoformat()} .. "
                f"{item.end.isoformat()}  {item.title}  "
                f"owner {item.owner_id}"
            )
        print(f"desired: {len(desired)} item(s)")
        if client is not None:
            existing = client.list_events(
                calendar_id, window_start.isoformat(), window_end.isoformat()
            )
            result = reconcile(
                desired, existing, client, calendar_id,
                dry_run=True, timezone_name=timezone_name,
            )
            for action in result.actions:
                print(f"  would {action.action}: {action.key}")
            print(
                f"planned: created {result.created}, "
                f"updated {result.updated}, "
                f"deleted {result.deleted}, "
                f"unchanged {result.unchanged}"
            )
        else:
            print(
                "dry-run: no client configured; showing the desired "
                "set only, no existing calendar was read."
            )
        return 0

    if client is None:
        print(
            "error: no Google Calendar client is configured for this "
            "deployment. The public core never holds Google "
            "credentials: a deployment supplies a client implementing "
            "kalends.gmirror.GoogleClient (see docs/MIRROR.md) and "
            "points KALENDS_GMIRROR_CLIENT_FACTORY at its factory. "
            "Re-run with --dry-run to preview the desired set "
            "without a client.",
            file=sys.stderr,
        )
        return 2

    existing = client.list_events(
        calendar_id, window_start.isoformat(), window_end.isoformat()
    )
    result = reconcile(
        desired, existing, client, calendar_id,
        dry_run=False, timezone_name=timezone_name,
    )
    print(header)
    print(
        f"mirrored: created {result.created}, "
        f"updated {result.updated}, "
        f"deleted {result.deleted}, "
        f"unchanged {result.unchanged}"
    )
    return 0


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

    band_p = sub.add_parser("band", help="render the band (resource timeline) view")
    _add_view_input_args(band_p)
    band_p.add_argument(
        "--group-by", dest="group_by", choices=["owner", "plane"],
        default="owner", help="rows keyed by owner (default) or by plane",
    )
    band_p.add_argument(
        "--scale", choices=["day", "week"], default="day",
        help="time-axis granularity (default: day)",
    )

    hour_p = sub.add_parser("hour", help="drill down into one hour (or a day)")
    _add_view_input_args(hour_p)
    hour_p.add_argument("--at", required=True, help="ISO timestamp in the hour")
    hour_p.add_argument(
        "--day", action="store_true",
        help="day mode: the 24 hours starting at --at's hour",
    )

    agent_p = sub.add_parser("agent", help="render one owner's agent page")
    _add_view_input_args(agent_p)
    agent_p.add_argument("--lookback-days", dest="lookback_days", type=int, default=7)
    agent_p.add_argument("--next", dest="next_n", type=int, default=5,
                         help="number of next instances to show")

    agents_p = sub.add_parser("agents", help="list owners present in the data")
    agents_p.add_argument("--dump", help="JSON dump to read (default: demo fleet)")
    agents_p.add_argument("--examples", help="path to the example fleet directory")

    gmirror_p = sub.add_parser(
        "gmirror",
        help="mirror the curated subset to a Google Calendar "
        "(see docs/MIRROR.md)",
    )
    gmirror_p.add_argument("--dump", help="JSON dump to read (default: demo fleet)")
    gmirror_p.add_argument("--examples", help="path to the example fleet directory")
    gmirror_p.add_argument(
        "--owners", action="append", default=None,
        help="eligible owner id (repeatable, comma-separated; "
        "default: every owner in the input)",
    )
    gmirror_p.add_argument(
        "--timezone", dest="timezone_name", default="UTC",
        help="timezone event times are written in (default: UTC)",
    )
    gmirror_p.add_argument(
        "--calendar-id", dest="calendar_id", default="primary",
        help="target calendar id (default: primary)",
    )
    gmirror_p.add_argument(
        "--window-days-back", dest="window_days_back", type=int, default=1,
        help="sync window start, days before now (default: 1)",
    )
    gmirror_p.add_argument(
        "--window-days-ahead", dest="window_days_ahead", type=int, default=14,
        help="sync window end, days after now (default: 14)",
    )
    gmirror_p.add_argument("--from", dest="from_", help="window start ISO time (overrides --window-days-back)")
    gmirror_p.add_argument("--to", help="window end ISO time (overrides --window-days-ahead)")
    gmirror_p.add_argument(
        "--dry-run", action="store_true",
        help="print the desired set and planned actions; make no writes",
    )

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

    if args.command == "gmirror":
        return _run_gmirror_command(args, now)

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

    if args.command in ("band", "hour", "agent", "agents"):
        data = _load_timeline_data(args, now)
        if args.command == "agents":
            sys.stdout.write(render_owners_index(data, now=now))
            return 0
        if args.command == "band":
            filters = BandFilters(
                window_start=_parse_time(args.from_),
                window_end=_parse_time(args.to),
                group_by=args.group_by,
                scale=args.scale,
                plane=args.plane,
                owner=args.owner,
                owner_kind=args.owner_kind,
                source=args.source,
                show_actual=not args.no_actuals,
            )
            defaulted = filters.window_start is None and filters.window_end is None
            sys.stdout.write(render_band_view(data, filters, now=now))
            if defaulted:
                print(
                    "Window: default display window, previous 24h through "
                    "next 48h (UTC); pass --from/--to for another range."
                )
            return 0
        if args.command == "hour":
            at = _parse_time(args.at)
            assert at is not None
            filters = HourFilters(
                plane=args.plane,
                owner=args.owner,
                owner_kind=args.owner_kind,
                source=args.source,
                show_actual=not args.no_actuals,
            )
            sys.stdout.write(
                render_hour(data, at, day=args.day, filters=filters, now=now)
            )
            return 0
        # agent page: --owner/--owner-kind come from the shared view
        # args; the owner is taken from the data, never hardcoded.
        if not args.owner_kind or not args.owner:
            print(
                "error: kalends agent needs --owner-kind and --owner "
                "(see `kalends agents` for the owners in the data)",
                file=sys.stderr,
            )
            return 2
        sys.stdout.write(
            render_agent_page(
                data,
                args.owner_kind,
                args.owner,
                now=now,
                window_start=_parse_time(args.from_),
                window_end=_parse_time(args.to),
                lookback_days=args.lookback_days,
                next_n=args.next_n,
            )
        )
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
