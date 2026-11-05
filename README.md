# Kalends

One calendar for every agent, job, and long-running task in a
multi-agent fleet.

A fleet of agents runs on schedules nobody can see in one place: cron
on one host, systemd timers on another, a Postgres job registry, a
worker queue, a hand-written YAML calendar of duties, heartbeat
cadences, long-running services and sessions. Each scheduler knows its
own slice. Kalends projects all of them into one calendar, so a human
or an agent can answer: what is happening at a given time, across the
whole fleet? What is this agent doing, has it done, and is it about to
do? What collides?

Kalends is a projection over existing schedulers, not a new scheduler.
It never executes work. Schedulers keep scheduling; Kalends makes
their combined behaviour visible, queryable, and presentable as time,
with provenance and freshness on every row, so stale data shows as
stale instead of posing as current.

## The name

The Kalends was the first day of the Roman month, publicly proclaimed:
which days held festivals, which held business, when debts fell due.
The word *calendar* derives from it. This system's job is the same
proclamation, for a fleet: what every agent, job, and long-running
task does, on which day.

## Concepts in one paragraph

Definitions and occurrences are separate objects. A **series** is a
recurring definition (a cron line, a timer, a registry row). An
**instance** is one materialised occurrence of it, expanded by the
projector over a rolling horizon (last 7 days, next 14 days). A
**band** is long-running work with a start and a heartbeat but no
scheduled end: a service, a session, a job a worker is holding under a
lease. A **window** is availability or its inverse. Every entry has an
owner (kind: agent, worker, system, or human-facing) and a host plane
(the machine or runtime it runs on, which may be a logical plane like
"platform"). Cadences are normalised to iCalendar RRULE where the
mapping is exact, and recorded honestly where it is not: a
self-chaining job, whose next run is decided when the previous run
finishes, gets exactly one instance, taken from the queue, and no
fabricated successors. The full model is in
[docs/DESIGN.md](docs/DESIGN.md); the adapter contract is in
[docs/ADAPTERS.md](docs/ADAPTERS.md); the adoption guide — survey
your own fleet, wire it, and drive everything through the MCP
server — is in [docs/ADOPTING.md](docs/ADOPTING.md). The Google
Calendar mirror — what subset publishes one-way, and how a
deployment supplies its client — is in
[docs/MIRROR.md](docs/MIRROR.md).

## Quickstart: the example fleet

The repository ships a fictional example fleet (agents named for
colours or ships, hosts named host-a and host-b): systemd timers, a
crontab, a YAML patrol calendar, a small job registry plus worker
queue, a fire log, an agent-cycle completion log, a service status
table, and a work-claims table. Run it end to end:

```bash
examples/example-fleet/run_demo.sh
```

or directly:

```bash
PYTHONPATH=src python3 -m kalends.cli demo
```

The demo runs all the adapters, projects the horizon, and prints the
table view: instances and bands in a two-day window, all series
definitions, and a freshness footer showing each source's sync age.
The window shows the Phase 2 story as well as the plan: an instance
with its actual firing time and variance, an observed-only firing
marked observed, past instances that never fired marked MISSED, an
empty agent cycle, service and claim bands, and a short holds
walkthrough (one live hold, one whose lease has expired). Flags
filter it: `--plane host-b`, `--owner-kind agent`, `--source
patrol-calendar`, `--from 2026-10-30 --to 2026-11-06`, `--no-series`.

To work with your own projection:

```bash
# write a projection to a JSON calendar dump
PYTHONPATH=src python3 -m kalends.cli demo --dump-out fleet.json

# render any dump later, with filters; with no --from/--to the table
# shows the default window (previous 24h through next 48h, UTC) and
# states it in the header
PYTHONPATH=src python3 -m kalends.cli table --dump fleet.json --plane host-a
```

Work no scheduler started puts itself on the calendar through holds
(docs/HOLDS.md):

```bash
PYTHONPATH=src python3 -m kalends.cli hold create \
    --title "Build the harbour chart pack" --owner-kind worker \
    --owner-id worker-3 --plane host-a --lease-seconds 3600
PYTHONPATH=src python3 -m kalends.cli hold list
```

## Using it on a real fleet

- Point `SystemdAdapter` at `systemctl list-timers --all
  --output=json` output (plus a units mapping for OnCalendar strings).
- Point `CronAdapter` at any crontab file.
- Point `YamlCalendarAdapter` at a YAML cadence calendar of named
  duties; field names are renamable through its field map.
- Point `PostgresJobsAdapter` at your job registry and queue tables:
  its config names your columns, so it works against any schema shaped
  like registry-plus-queue, live or from a psql JSON dump.
- Point `FireLogAdapter` at a bounded extract of your fire log and
  `CompletionPointsAdapter` at your agent-cycle completions, so
  planned instances gain actuals (with variance, observed marks, and
  missed firings left visible) and agent cycles appear as points.
- Point `ServiceStatusAdapter` at your service status table and
  `WorkClaimsAdapter` at your claims table to get bands: one per
  service since its status began, one per claim under its lease.
- Load projections into Postgres with `schema/001_kalends.sql`
  (installs created before Phase 2 apply
  `schema/002_anchors_and_claims.sql` instead of recreating anything;
  the `db` extra, `pip install kalends[db]`, provides psycopg for that
  path), or keep dumps as JSON.
- Serve the calendar through `kalends-mcp` (the `mcp` extra,
  `pip install kalends[mcp]`): an MCP server over the Postgres
  tables, and the one supported CRUD path — reads, single and batch
  upserts, scoped batch replaces, freshness stamps, and the holds
  operations, over stdio or streamable HTTP. Set `KALENDS_DSN`,
  run `kalends-mcp`, and have every lane write through its tools.
  See [docs/ADOPTING.md](docs/ADOPTING.md).

## Repository layout

- `src/kalends/` — the package: `model.py` (core dataclasses),
  `cadence.py` (dialect normalisers), `expand.py` (RRULE expansion),
  `projector.py` (definitions in, instances out), `holds.py` (the
  self-declared holds lane), `tableview.py`, `bandview.py`, and
  `agentpage.py` (the table view, the band view with the hour
  drill-down, and agent pages) and `cli.py` (the view and holds
  CLI), `adapters/` (the reference adapters and
  the base contract), `store.py` and `mcp_server.py` (the storage
  layer and the `kalends-mcp` server: all CRUD, batch included,
  through MCP tools).
- `schema/` — Postgres DDL for the six core tables (`001` for fresh
  installs, `002` brings a Phase 1 install forward).
- `docs/` — the design, the adapter contract, the holds API, and
  the adoption guide.
- `examples/example-fleet/` — the fictional fleet every example and
  test runs against.
- `tests/` — the pytest suite.

## Development

Python 3.11+. Runtime dependencies are only `python-dateutil` and
`PyYAML`; `psycopg` is an optional extra for database loading, and
the `mcp` extra (`pip install kalends[mcp]`) adds the MCP SDK,
psycopg, and uvicorn for the `kalends-mcp` server.

Run the tests with the project virtualenv:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

(If your system Python already has pytest, dateutil, and PyYAML,
`python3 -m pytest` from the repo root works as-is; `pyproject.toml`
puts `src/` on the test path.)

## Licence

Apache-2.0. See [LICENSE](LICENSE).
