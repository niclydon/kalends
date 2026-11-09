# Adopting Kalends

This guide is written for a coder agent wiring Kalends into an
existing platform — its own platform, surveyed by itself. Read
`docs/DESIGN.md` for the model in full, `docs/ADAPTERS.md` for the
adapter contract, and `docs/HOLDS.md` for the holds lease contract.
This document is the operational path: survey, map, write everything
through the MCP tools, verify.

The one rule that governs the whole adoption: **all Kalends CRUD
goes through `kalends-mcp`**. Adapters and projection lanes do not
write the `calendar_*` tables directly, and readers do not query
them directly. The MCP tools are the contract; the tables are an
implementation detail behind them. If a tool is missing, that is a
bug to file against this repository, not a reason to open a side
door.

## 1. What Kalends is, in five shapes

Kalends is a per-agent calendar: a projection over the schedulers a
fleet already has, never a new scheduler. It executes nothing. It
answers, across every scheduler at once: what is happening at a
given time, what is a given agent doing / has it done / is it about
to do, and what collides.

Five shapes cover everything a fleet's sources produce:

- **Series** — a recurring definition: a cron line, a timer, a
  registry row. Never rendered directly; the projector expands it.
- **Instance** — one materialised occurrence of a series, with
  planned start/end and, where observed, actual start/end. A
  **one-off** is an instance with no series: a single future run, a
  due date, an expiry.
- **Band** — long-running work with a start and a heartbeat but no
  scheduled end: a service, a session, work held under a lease. A
  band ends when its heartbeat stops or its lease expires.
- **Window** — availability or its inverse: maintenance, quiet
  hours, a declared hold on time itself (RFC 7953 vocabulary).
- **Actual** — one observed firing, kept separately because fire
  logs are large: a `firing` (a run that happened, with the time it
  would have fired) or a `point` (a completion moment with no
  duration; a point may be flagged `is_empty` when the cycle
  completed having done nothing — shown, never hidden).

Behind the shapes sit two registries and two cross-cutting fields:

- A **source** is one upstream system an adapter reads (a host's
  timers, a job registry, a fire log). Every row carries its
  source, and every source row carries freshness: when it was last
  synced, and the newest timestamp the source itself reported.
  Stale data shows as stale.
- Every entry has exactly one **owner**: an owner id plus an owner
  kind — `agent`, `worker`, `system`, or `human-facing`. Owner kind
  is data, not a naming convention.
- Every entry carries a **plane**: the machine or runtime it runs
  on. Planes are configuration, never hard-coded. A plane may be
  logical (the platform itself) when work is placed dynamically; a
  host pattern in a source is a constraint, not a placement.
- Definitions and occurrences are separate objects, and planned and
  actual times are both first-class. An instance that was planned
  and never fired is a visible fact, not an absence.

## 2. Get the code

```bash
git clone <url-of-this-repository> kalends
cd kalends
python3 -m venv .venv
.venv/bin/pip install -e ".[mcp]"
```

The `mcp` extra installs the Python MCP SDK (`mcp>=2.3,<3`),
psycopg 3, and uvicorn. The core library stays dependency-light
(dateutil and PyYAML) for embedding without the server.

Apply the schema to your own Postgres — a scratch database is fine
for the first pass:

```bash
psql "$DATABASE_URL" -f schema/001_kalends.sql
```

`001` is the complete current schema for a fresh install (it
already carries the Phase 2 columns). `002_anchors_and_claims.sql`
exists only to bring a Phase 1 install forward; do not run it on a
fresh database.

Set the connection string for the server:

```bash
export KALENDS_DSN="postgresql://kalends:…@localhost:5432/kalends"
# DATABASE_URL is accepted as a fallback when KALENDS_DSN is unset.
```

## 3. Survey your own platform first

Before writing any adapter, answer the scheduler census. Do not
guess: inspect the platform you are standing on, and count before
wiring anything.

Questions to answer:

- What schedulers exist here? Cron (which users, which files),
  systemd timers, launchd jobs, a Postgres (or other) job registry,
  a worker queue, in-process schedulers (interval loops, self-
  chaining jobs whose next run is set when the previous finishes),
  hosted agents with cadences of their own.
- Where do the definitions live for each? A crontab file, unit
  files, a registry table, code constants. The definition store is
  the source of truth an adapter reads — never a rendered view of
  it.
- What counts as a source? One adapter per upstream system, bounded
  the way that system is bounded: one host's systemd, one crontab,
  one registry. If one logical source spans several planes and
  native ids could collide, the plane goes into the series id.
- Which things are bands, not series? Long-running services,
  sessions, and claimed work: anything with a start and a heartbeat
  and no honest scheduled end. If it has a cadence, it is a series;
  if it is simply *running*, it is a band.
- Where are firings and completions logged? The fire log, the queue
  history, agent-cycle completion records. That log is where
  actuals come from; note its key (so firings join to series), its
  retention, and its volume — a busy fire log is never copied
  wholesale, only bounded extracts or aggregates cross into
  actuals.
- Which cadences have no honest RRULE? Self-chaining and relative
  timers (on-boot, on-unit-active) do not; record the dialect
  honestly and let the projector work from the source's own
  next-run field. Never fabricate an RRULE to make a row look
  uniform.
- What are the owner kinds and planes here? Enumerate the agents,
  worker systems, and hosts that actually exist, and the logical
  plane name for dynamically placed work.

How to answer them, concretely:

- Service managers: `systemctl list-timers --all` and the unit
  files on each host; `launchctl list` on macOS hosts.
- Crontabs: system crontab, cron.d directories, and per-user
  crontabs, on every host — not just the one you are running on.
- Job tables: look for registry-plus-queue schema pairs (a
  definitions table and a pending/processing/history set of queue
  rows). Count definitions by kind and lifecycle state.
- In-process schedulers: search the codebase for interval timers,
  scheduler libraries, and "next run = completion + delay"
  patterns; these are invisible to every external listing.
- Fire/completion logs: find the tables or files recording that
  work ran, and sample-join them against the definitions by hand.

Record the census as data: per scheduler, its definition count,
its shapes (which of series / instance / band / window / actual it
can express), its fire-log join key, and its freshness signal. That
table is your adoption plan, and its counts are the numbers section
7 reconciles against.

## 4. Map findings to the model

The mapping discipline (full contract: `docs/ADAPTERS.md`):

- One **source** row per upstream system, written first — every
  other row references it. `adapter` names the adapter kind,
  `plane` the source's home plane.
- One **series** per recurring definition, including retired and
  parked ones, with lifecycle recorded. Filtering happens at
  projection, never at collection: a definition the adapter hides
  is a definition nobody can audit. `series_id` is
  `source_id + source-native id` (plus plane where a source spans
  planes). Always keep the cadence exactly as the source states it
  in `cadence_expr`; `rrule` is a normalisation layered alongside,
  null where no exact mapping exists. Where the source reports a
  real occurrence (a last or next firing), set `anchor_at` so
  expansion phase-locks to it.
- **Instances** come from the projector expanding series over the
  horizon, plus occurrences the source itself knows (a timer's next
  elapse, a queue's pending rows). Adapters never expand cadences
  themselves.
- One **band** per service (from its status-since timestamp) and
  per piece of claimed work (under its claim lease, closing as
  expired at the lease time when a claim is never released).
- **Actuals** join at projection: firing actuals carry
  `would_fire_at` so variance is computable; firings the scheduler
  only observed are `observed_only` and are never presented as
  executed; completion moments are point actuals.
- A source's slot group, queue name, or lane is a **resource
  claim**: `resource_claims` on the series (copied onto its
  instances by the projector), on the instance, or on the band —
  e.g. `{"slot_group": "survey"}`.
- Event triggers are not schedules: they ride on the series'
  `event_triggers` and project nothing.

### Worked example: one adapter, end to end

The fictional example fleet (all names invented; hosts `host-a`
and `host-b`) wires its crontab on `host-a` through the reference
`CronAdapter`. The flow an adopting agent reproduces with its own
scheduler is:

1. Census: `host-a` has one crontab with a nightly survey job,
   `7 3 * * * /opt/survey/run.sh`, run by the worker pool. The
   scheduler's own count: 1 active definition.
2. Source: the sync ensures the source row exists through the MCP
   tools (never by hand):

   ```json
   {"source": {"source_id": "cron-host-a", "adapter": "cron",
               "plane": "host-a", "sync_interval_sec": 300}}
   ```
   → `kalends_upsert_source`
3. Series: the adapter reads the crontab, normalises the cadence,
   and the sync batch-upserts what it found — one series,
   `cron-host-a:nightly`, owner kind `worker`, owner id
   `worker-3`, `cadence_expr` `"7 3 * * *"`,
   `rrule` `"FREQ=DAILY;BYHOUR=3;BYMINUTE=7"`, resource claims
   `{"slot_group": "survey"}` → `kalends_batch_upsert_series`.
4. Projection: the projector expands the series over the horizon
   and the lane writes occurrences with
   `kalends_batch_replace_instances` scoped to `cron-host-a` and
   the horizon it just projected — the scoped replace is what makes
   re-projection idempotent.
5. Actuals: the nightly fire log is extracted for the same window
   and written with `kalends_batch_replace_actuals`, so each
   planned 03:07 instance gains its fired-at and variance.
6. Freshness: the sync finishes with
   `kalends_mark_source_synced{source_id: "cron-host-a"}`, so every
   reader can see how old this slice of the calendar is.

The repository's `examples/example-fleet/` runs this whole story —
cron, systemd, a YAML cadence calendar, a registry plus queue,
services, claims, and holds — against invented data; it is the
fastest way to see each mapping before writing your own.

## 5. Run kalends-mcp

```bash
# stdio (the default): a client launches the server per session
KALENDS_DSN="postgresql://…" kalends-mcp

# streamable HTTP: for a standing deployment
TRANSPORT=http BIND_HOST=127.0.0.1 PORT=7899 \
  ORIGIN_SHARED_SECRET="…" KALENDS_DSN="postgresql://…" kalends-mcp
```

Environment:

- `KALENDS_DSN` (or `DATABASE_URL`) — Postgres connection string.
  Required; the server refuses to start without it.
- `TRANSPORT` — `stdio` (default) or `http`.
- `BIND_HOST` / `PORT` — HTTP bind, defaults `127.0.0.1` / `7899`.
  A deployment overrides both.
- `ORIGIN_SHARED_SECRET` — in HTTP mode, when set, every request
  except `GET /health` must carry it as the `x-origin-secret`
  header. When unset, the server still runs (front it with your
  own auth) but logs a warning that the origin guard is off.

The rule, again, because everything else follows from it: **all
CRUD goes through these tools** — sync lanes, projection lanes,
holds, human fixes, everything. The tool groups:

- Reads: `kalends_list_sources`, `kalends_list_series`,
  `kalends_get_series`, `kalends_list_instances`,
  `kalends_list_bands`, `kalends_list_windows`,
  `kalends_list_actuals`, `kalends_list_holds`
- Single writes (upsert by primary key, returning the stored row):
  `kalends_upsert_source`, `kalends_upsert_series`,
  `kalends_upsert_instance`, `kalends_upsert_band`,
  `kalends_upsert_window`, `kalends_upsert_actual`
- Deletes (returning `{deleted: bool}`): `kalends_delete_series`,
  `kalends_delete_instance`, `kalends_delete_band`,
  `kalends_delete_window`, `kalends_delete_actual`
- Batch and freshness: `kalends_batch_upsert_series`,
  `kalends_batch_upsert_instances`,
  `kalends_batch_replace_instances`, `kalends_batch_upsert_bands`,
  `kalends_batch_upsert_actuals`, `kalends_batch_replace_actuals`,
  `kalends_mark_source_synced`
- Holds: `kalends_declare_hold`, `kalends_heartbeat_hold`,
  `kalends_release_hold` (plus `kalends_list_holds` above)

Timestamps cross the tools as ISO 8601 strings. List tools page
with `limit` / `offset` (default limit 100, maximum 1000) and take
`from` / `to` as a half-open range on the row's primary timestamp.
`kalends_list_instances` also filters server-side by `owner_id`,
`owner_kind`, and `shape`, and `kalends_list_bands` by
`owner_kind`; the filters compose with each other, with the other
list filters, and with paging, and an unknown filter value returns
an empty result rather than an error.
Holds need no adapter and no sync: a running thing calls
`kalends_declare_hold` with a title, owner, plane, and lease, then
`kalends_heartbeat_hold` while it works; declaring auto-registers
the `holds` source. A hold whose lease lapses reads as closed with
end reason `expired`, persisted the first time anyone looks.

## 6. Batch-ingest guidance for high-volume lanes

A busy fleet's projection pass is tens of thousands of rows. The
batch tools exist so that pass is a handful of calls, each one
transaction:

- Sync definitions with `kalends_batch_upsert_series` /
  `kalends_batch_upsert_bands` / `kalends_batch_upsert_actuals`:
  one call per source per pass, rows as a single list argument.
  Each batch is one transaction over one connection, executed with
  a multi-row `executemany` — it lands whole or not at all.
- Publish a freshly projected horizon with
  `kalends_batch_replace_instances{source_id, horizon_start,
  horizon_end, instances}`: in one transaction it deletes that
  source's instances whose `planned_start` falls inside the
  horizon (both ends inclusive) and inserts the replacements,
  returning `{deleted, inserted}`. Rows outside the horizon and
  rows belonging to other sources are untouched. This is what
  makes re-projection safe to repeat: project, replace, stamp the
  source synced.
- Use `kalends_batch_replace_actuals{source_id, window_start,
  window_end, actuals}` the same way for a fire-log window: the
  delete is scoped by source and by `fired_at` inside the window.
  Every row in a replace must belong to the scoped source; a
  mismatch is an error, not a partial write.
- Per-row calls are wrong at this scale: 10k rows as 10k
  single-upsert calls is 10k transactions, 10k chances to leave a
  half-written horizon behind, and orders of magnitude slower. If
  a lane finds itself looping single writes, the lane is wrong,
  not the toolset.
- Keep batches source-shaped: one source per call keeps failures
  attributable and retries idempotent (upserts by primary key;
  replaces scoped to exactly the rows the pass regenerated).
- Finish every pass with `kalends_mark_source_synced`. A pass that
  wrote rows but never stamped freshness leaves the calendar
  looking stale even though it is not — the stamp is part of the
  write, not bookkeeping after it.

## 7. Verification checklist

After each stage, check evidence, not exit codes. Worked against
the example above; substitute your own source ids and counts.

- **Source wired**: `kalends_list_sources` shows the source row;
  after a sync, `kalends_mark_source_synced` has landed —
  `last_synced_at` is the sync time, not null, and advances on the
  next pass.
- **Counts reconcile**: `kalends_list_series{source_id: …}` with a
  high `limit` returns a count equal to the source system's own
  count from your census (active, retired, and parked definitions
  all included). A shortfall means the adapter is filtering, which
  the contract forbids.
- **A sampled instance matches**: pick one series, read its next
  instance with `kalends_list_instances{series_id: …, from: now}`,
  and compare its `planned_start` against the source's own
  next-fire (the timer's next elapse, the queue's scheduled time).
  They agree, in the series' timezone.
- **Actuals pair**: for a past window,
  `kalends_list_actuals{source_id: …}` rows pair to planned
  instances by series and would-fire time; the pairing rate is the
  number to watch (firings with no plan are observed work or a
  missing definition; plans with no firing are missed runs — both
  are findings, not noise).
- **A proof hold lives and dies**: `kalends_declare_hold` a
  one-minute hold; it appears in `kalends_list_holds`
  with a `lease_expires_at` one minute out; after the lease lapses
  it reads `status: done`, `end_reason: expired`, `ended_at` equal
  to the lease expiry — and a heartbeat against it now errors.
- **No side doors**: grep the adopting codebase for direct writes
  to the calendar tables. The only write path is the tools above;
  anything else is a defect to fix before the adoption is done.
