# Kalends — Design

Status: v0.2. This document describes the model. The code in this
repository implements Phases 1 and 2 (see section 11).

## 1. Problem

A multi-agent fleet runs on schedules nobody can see in one place. Work
is spread across cron, systemd timers, launchd, Postgres job registries,
agent heartbeat cadences, YAML commissioning calendars, worker queues,
long-running sessions, and hosted agents with no visible scheduler at
all. Each source knows its own slice. Nobody, human or agent, can answer
three simple questions:

- What is happening at a given time, across the whole fleet?
- What is a given agent doing, has it done, and is it about to do?
- What collides: two agents or jobs wanting the same host, resource, or
  hour?

Scale is not the problem; a fleet of twenty agents easily accumulates a
thousand scheduled definitions. Fragmentation and incompatible temporal
shapes are.

## 2. What this is, and is not

Kalends is a projection and presentation layer over existing schedulers.
It does not replace cron, systemd, or any job engine, and it does not
execute work. Schedulers keep scheduling. The calendar makes their
combined behaviour visible, queryable, and presentable as time.

It is not a booking system for humans, not a meeting tool, and not a new
scheduler competing with the ones already running.

## 3. Borrowed, not invented

The design adopts proven pieces instead of inventing new ones:

- Temporal Schedules: the schedule object model. Interval and calendar
  expressions, exclusions, jitter, timezone, overlap policies (skip,
  buffer, cancel other, allow all), catch-up windows. Concepts only; the
  engine is not adopted.
- JSCalendar (RFC 8984): the JSON shape for calendar objects, mapped to
  iCalendar on export.
- iCalendar (RFC 5545): RRULE expresses the recurring cadences already in
  use (weekly, monthly by weekday, quarterly) without a custom cadence
  language.
- RFC 7953 VAVAILABILITY: the model for availability windows, adopted in
  vocabulary even though mainstream tooling barely implements it.
- Task lifecycle vocabulary: scheduled, dispatched, in progress,
  completed, failed, timed out.
- Temporal holds: a self-declared block of time with an expiry, as a
  first-class concept.
- The definitions/runtime-state split: job definitions and their runtime
  state are different objects with different lifetimes, so they live in
  different tables.

## 4. Core concepts

### 4.1 Owner kinds

Every calendar entry has exactly one owner, and the owner kind is data,
not naming convention:

- agent: a named AI agent's own work (a standing agent's cycle, an
  assistant's scheduled check-in)
- worker: a job executed by a worker system on behalf of a queue
  (backlog claims, worker-queue jobs)
- system: infrastructure work with no agent owner (ingest, backups,
  watchdogs, syncs)
- human-facing: scheduled touches a human will see (briefings, digests,
  reminders), whoever produces them

### 4.2 Host planes

Every entry carries a host plane: the machine or runtime the work runs
on. Planes are configuration data in the core, never hard-coded, because
every deployment has different ones.

A plane may be logical rather than physical. Many registry jobs carry no
host constraint and run wherever the executor places them; their plane is
the platform itself, recorded as such rather than guessed. A host
pattern in a source is a constraint, not a placement, and is never
rendered as if it were a machine name.

### 4.3 Temporal shapes

Five shapes cover everything a fleet's sources produce. The schema holds
all five; a source that cannot express a shape does not use it.

- Series: a recurring definition with a cadence. Never rendered directly.
- Instance: one occurrence of a series, or a standalone planned run, with
  a start and an end (or an expected duration).
- One-off: a single future point or block: a run-once schedule, a due
  date, an expiry, a renewal.
- Band: long-running work with a start and a heartbeat but no scheduled
  end: a service, a session, a claimed piece of work. A band ends when
  its heartbeat stops or its lease expires, not when a clock says so.
- Window: availability or its inverse (maintenance, quiet hours),
  modelled after VAVAILABILITY.

### 4.4 Definitions and instances are separate tables

Series are definitions. Instances are materialised occurrences, expanded
by the projector over a rolling horizon: the next 14 days of planned
instances, the last 7 days of actuals. Views query instances, never
expand series at read time. Retention: instances age out of the
materialised window; definitions and actuals history persist.

One cadence family cannot be pre-expanded honestly: self-chaining jobs,
whose next run is set when the previous run completes (completion time
plus a delay). For those, the projector materialises only the next
instance, taken from the worker queue. The design does not fabricate
clock times for work that is not clock-scheduled.

### 4.5 Planned and actual are both first-class

Every instance carries planned start and end where known, and actual
start and end where observed. Fire logs and completion records usually
already exist; the calendar joins them to the plan instead of showing
intent alone. An instance that was planned and never fired is a visible
fact, not an absence.

### 4.6 Provenance and freshness on every row

Every calendar row carries: source system, source identifier, adapter
that produced it, source timestamp, and sync timestamp. A stale source
produces visibly stale calendar data with its age shown, never silent
old data presented as current. Freshness is a display field, not a log
line.

## 5. Core schema (logical)

Storage: Postgres. Format inspiration: JSCalendar. All timestamps are
timestamptz; series also store their local timezone. The DDL ships in
`schema/001_kalends.sql`.

### calendar_sources (adapter registry, config as data)

- source_id (text, primary key)
- adapter (text: systemd, cron, launchd, postgres-job-registry,
  yaml-calendar, queue, presence, holds-api, ical-feed)
- plane (text)
- enabled (boolean)
- sync_interval_sec (integer)
- last_synced_at (timestamptz)
- last_source_at (timestamptz)
- notes (text)

### calendar_series

- series_id (text, primary key: source_id + source-native id)
- source_id, source_native_id (text)
- title (text)
- owner_kind (agent, worker, system, human-facing)
- owner_id (text, nullable for system work)
- plane (text)
- cadence_dialect (cron, systemd-calendar, systemd-relative, interval,
  rrule, self-chain, none)
- cadence_expr (text, as the source states it)
- rrule (text, normalised, nullable for self-chain and none)
- timezone (text)
- expected_duration_sec (integer, nullable; learned from actuals when
  not declared)
- overlap_policy (skip, buffer, cancel-other, allow-all)
- enabled (boolean)
- lifecycle_state (active, retired, deprecated, dormant)
- source_updated_at, synced_at (timestamptz)
- anchor_at (timestamptz, nullable: a known occurrence of the series,
  a source-reported last or next firing. Expansion phase-locks to it.
  Without one, expansion falls back to source_updated_at and the
  series is marked phase-approximate, so views stay honest about the
  phase)
- resource_claims (jsonb: named resources every instance of this
  series claims; copied onto instances at projection)

### calendar_instances

- instance_id (text, primary key)
- series_id (nullable for one-offs and ad-hoc instances)
- source_id, source_native_id (text)
- title (text)
- owner_kind, owner_id, plane (denormalised from series for query speed)
- shape (instance, one-off)
- planned_start, planned_end (timestamptz)
- actual_start, actual_end (timestamptz, nullable)
- status (scheduled, dispatched, in-progress, completed, failed,
  timed-out, skipped, forecast)
- resource_claims (jsonb: named resources this instance needs, e.g. a
  host, a GPU, a lane)
- source_updated_at, synced_at (timestamptz)

### calendar_bands

- band_id (text, primary key)
- source_id, source_native_id (text)
- title (text)
- owner_kind, owner_id, plane
- started_at (timestamptz)
- last_heartbeat_at (timestamptz)
- lease_expires_at (timestamptz, nullable)
- ended_at (timestamptz, nullable)
- end_reason (completed, released, expired, abandoned, failed,
  observed-stopped)
- status (active, blocked, idle, done)
- resource_claims (jsonb)

### calendar_windows

- window_id (text, primary key)
- owner_kind, owner_id, plane
- kind (available, maintenance, quiet, hold)
- starts_at, ends_at (timestamptz) or an rrule for repeating windows
- priority (integer, per RFC 7953)
- source_id, source_native_id

### calendar_actuals (optional join table)

- series or instance reference, fired_at, would_fire_at, observed_only
  flag, source-native run id
- kind (firing, or point: a completion moment with no duration) and
  is_empty (points only: the cycle completed having done nothing)

Where a source's fire log is large, actuals are aggregated into
instances at projection time, never copied wholesale.

## 6. Intake: three lanes

### 6.1 Projected (the default)

An adapter per source system reads that system's own source of truth
and reconciles series, instances, and bands into the calendar tables.
Nothing has to opt in, and nothing can forget to register. Adapters are
read-only against sources. The adapter contract is specified in
`docs/ADAPTERS.md`.

### 6.2 Self-declared (holds)

A small API by which a running thing puts itself on the calendar:
create a band or a future hold with a title, owner, plane, expected end
or lease, and resource claims. Bands live on heartbeat plus lease
expiry, the same pattern worker leases and claim systems already
implement. This lane covers work no scheduler started: a claimed work
item, a long build, an agent holding a scarce resource.

Implemented (Phase 2) as a transport-free library over a JSON file
store: `kalends.holds.HoldStore` with `create_hold`,
`heartbeat_hold`, `release_hold`, and `list_holds`, a CLI
(`kalends hold ...`), and specified MCP tool shapes for hosts that
wrap it. A hold whose lease expires without a heartbeat closes with
end reason `expired` when materialised; heartbeating a closed hold is
an error, not a resurrection. See `docs/HOLDS.md`.

### 6.3 Observed

Pollers and existing logs supply actuals and ephemeral work: session
lists from a process supervisor, service status with a since timestamp,
presence records for hosted agents. Observed bands are marked observed;
when the poller stops seeing the thing, the band closes as
observed-stopped.

## 7. Cadence dialects

Sources do not share a cadence language, and pretending otherwise
corrupts data. A real fleet will contain, at once: cron expressions,
systemd calendar expressions, systemd relative timers (on unit active,
on boot), plain interval strings ("every 30m", "daily at 09:07"),
self-chain freshness delays, and YAML cadence words (weekly, monthly,
quarterly). Each adapter ships a normaliser from its dialect to RRULE
where the mapping is exact, and keeps the original expression
alongside, always. Where the mapping is not exact (relative timers,
self-chain), the series records the dialect honestly and the projector
works from the source's own next-run field instead of an expansion.

## 8. Conflicts and resources

Version 1 conflict detection is descriptive, not enforcing: overlapping
instances or bands that share an owner, a plane, or a named resource
claim are computed and shown, with the overlap policy from the series
attached. Nothing is rescheduled by the calendar. No existing system
models cross-agent contention for a shared resource; the
resource_claims field is where that model starts, and v1 only reports
contention, it does not arbitrate it.

## 9. Views

Built in this order:

- Table view: every series, instance, and band, filterable by plane,
  owner kind, owner, source, shape, and time range, with freshness per
  source. This is the audit surface and ships first. (Implemented:
  `kalends table`, and `kalends demo` for the example fleet.) Because
  the materialised horizon is 21 days and a busy fleet fills it with
  tens of thousands of rows, the table view's default display window
  is bounded: when the caller gives no time range, the view shows the
  previous 24 hours through the next 48 hours (UTC) and states that
  window in its output header. An unbounded render is an explicit
  choice (`--from`/`--to`), never the default. The view also carries
  the actuals story per instance: actual time and variance where a
  firing joined, an observed mark where the firing was only observed,
  point and empty-cycle marks for completion records, and a MISSED
  mark on a past instance that stayed scheduled with no actual.
- Band view (resource timeline): one row per owner, toggleable to one
  row per plane. Day and week scales. Bands and instances drawn on the
  same row axis, so a service, a session, and a job belonging to one
  agent read as one timeline.
- Hour drill-down: pick an hour, see everything overlapping it across
  all planes, planned and actual.
- Planned versus actual toggle on every view.
- Agent page: one agent, its series, its next instances, its active
  bands, its windows, its recent actuals, including empty or no-op
  cycles flagged as such.

Views query materialised instances, so the row count per view is
bounded by the horizon, not by the fleet's total definitions.

## 10. Calendar mirror (optional, selective)

A deployment may mirror a curated subset to a human calendar system:
only human-facing owners, only planned touches, holds a human should
see, and deadlines. Machine series never mirror. The mirror is one-way
out; edits made in the human's calendar do not flow back.

## 11. Phasing

- Phase 0: design plus a source inventory with a field mapping per
  source. Done when every source has a verdict.
- Phase 1: schema, projector, and the reference adapters covering the
  bulk of definitions: systemd, cron, a generic Postgres job-registry
  and queue adapter, and a YAML cadence calendar adapter. Table view
  only. (v0.1.)
- Phase 2: actuals joins at scale (generic fire-log and
  completion-points adapters, variance and observed marks, missed
  instances left visible), bands from services and claim systems
  (generic config-driven adapters), the holds API, series anchors
  with honest phase marking, series-level resource claims, and the
  table view's default display window. (This repository, v0.2.)
- Phase 3: band view, hour drill-down, agent pages, session and
  presence observation.
- Phase 4: the selective mirror to a human calendar for human-facing
  owners.

## 12. Public core / private instance split

This repository is the public core: the schema, the adapter contract,
the reference adapters, the views, and a fictional example fleet
(agents named for colours or ships, hosts named host-a and host-b) used
by every example and test fixture.

A deployment keeps private: its real calendar data, deployment-specific
adapters (for its own internal platforms), its host and agent names as
configured, credentials, and any mirror configuration.

The stranger test: a stranger clones the core, points the systemd and
cron adapters at their own machines, and gets a useful calendar the
same day.

Licence: Apache-2.0.

## 13. Open questions

- Whether queued asks (agent inboxes) and goal due dates join the
  calendar as one-offs, or stay in their own waiting-on surfaces.
- Where the UI lives: standalone in the public core, with deployments
  embedding it, is the design assumption.
- Retention for aggregated actuals.
- Whether the mirror includes holds or only scheduled touches.
