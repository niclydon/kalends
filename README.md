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
[docs/ADAPTERS.md](docs/ADAPTERS.md).

## Quickstart: the example fleet

The repository ships a fictional example fleet (agents named for
colours or ships, hosts named host-a and host-b): systemd timers, a
crontab, a YAML patrol calendar, and a small job registry plus worker
queue. Run it end to end:

```bash
examples/example-fleet/run_demo.sh
```

or directly:

```bash
PYTHONPATH=src python3 -m kalends.cli demo
```

The demo runs all four adapters, projects the horizon, and prints the
table view: every instance and band in a two-day window, all series
definitions, and a freshness footer showing each source's sync age.
Flags filter it: `--plane host-b`, `--owner-kind agent`, `--source
patrol-calendar`, `--from 2026-10-30 --to 2026-11-06`, `--no-series`.

To work with your own projection:

```bash
# write a projection to a JSON calendar dump
PYTHONPATH=src python3 -m kalends.cli demo --dump-out fleet.json

# render any dump later, with filters
PYTHONPATH=src python3 -m kalends.cli table --dump fleet.json --plane host-a
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
- Load projections into Postgres with `schema/001_kalends.sql` (the
  `db` extra, `pip install kalends[db]`, provides psycopg for that
  path), or keep dumps as JSON.

## Repository layout

- `src/kalends/` — the package: `model.py` (core dataclasses),
  `cadence.py` (dialect normalisers), `expand.py` (RRULE expansion),
  `projector.py` (definitions in, instances out), `tableview.py` and
  `cli.py` (the table view), `adapters/` (the reference adapters and
  the base contract).
- `schema/` — Postgres DDL for the six core tables.
- `docs/` — the design and the adapter contract.
- `examples/example-fleet/` — the fictional fleet every example and
  test runs against.
- `tests/` — the pytest suite.

## Development

Python 3.11+. Runtime dependencies are only `python-dateutil` and
`PyYAML`; `psycopg` is an optional extra for database loading.

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
