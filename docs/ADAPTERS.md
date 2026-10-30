# The adapter contract

An adapter reads one source system's own source of truth and reconciles
it into the core model. Adapters are the only code that knows a source's
native shapes; everything downstream (the projector, the views, the
database loader) works purely in core objects.

Every adapter must:

1. **Enumerate definitions.** Return every recurring definition the
   source knows as a `Series`, including retired and parked ones, with
   their lifecycle recorded. Filtering happens at projection, not at
   collection: a definition the adapter hides is a definition nobody can
   audit.
2. **Enumerate occurrences the source knows.** Where the source itself
   tracks upcoming or recent occurrences (a timer's next elapse, a
   queue's pending rows, a last-run timestamp), return them as provided
   `Instance` and `Actual` objects. Do not expand cadences yourself;
   expansion is the projector's job, so that the honesty rules (below)
   live in exactly one place.
3. **Report freshness.** Every result carries a `Source` with
   `last_synced_at` (when the adapter ran) and `last_source_at` (the
   newest timestamp the source itself reported, e.g. a file's mtime, a
   table's max updated_at, a timer's last elapse). Views show both. An
   adapter that cannot know the source's own time says so by leaving
   `last_source_at` null rather than copying the sync time into it.
4. **Declare expressible shapes.** The class attribute `shapes` lists
   which of `series`, `instance`, `one-off`, `band`, `window` the
   adapter can produce. An adapter that cannot express actuals or bands
   says so; the calendar shows the gap rather than implying coverage.
5. **Keep the original cadence expression, always.** `cadence_expr`
   carries the cadence exactly as the source states it (the cron line's
   five fields, the OnCalendar string, the YAML word, the registry's
   cadence column rendered as stated). `rrule` is a normalisation layered
   alongside it, never a replacement for it.
6. **Never fabricate.** Where a cadence has no exact RRULE (systemd
   relative timers, self-chaining jobs), `rrule` is null and the
   dialect is recorded honestly. Where a host field is a pattern or a
   constraint rather than a placement, the series lands on the
   deployment's logical plane, not on a guessed machine name. Where a
   source has no owner, owner_id is null rather than invented.

## Result shape

`collect()` returns an `AdapterResult` with:

- `source`: the `Source` row for this adapter pass
- `series`: list of `Series`
- `instances`: source-provided occurrences (next runs, one-offs)
- `bands`: long-running work the source is tracking (e.g. queue rows
  currently processing, held by a worker under a lease)
- `actuals`: observed firings, to be joined to the plan at projection
- `stats`: free-form tallies (rows read, rows skipped and why), for
  reconciliation against the source's own counts

## Field mapping guidance

- `series_id` is `source_id + source-native id`, with the plane included
  when one source spans several planes and native ids could collide
  (the systemd adapter does this: the same unit name on two hosts is two
  series).
- Owner kind defaults: OS-level schedulers produce `system` work unless
  the deployment maps specific units to an agent or worker owner.
  Registry and queue jobs are `worker` work. YAML calendar rows default
  to `agent` work and may override per row.
- A source's slot group, queue name, lane, or similar one-at-a-time
  grouping is a resource claim: record it in the series'
  `resource_claims` (e.g. `{"slot_group": "survey"}`) and the projector
  copies it onto every generated instance.
- Event triggers are not schedules. Record them on the series'
  `event_triggers`; they project no instances.

## The reference adapters

| Adapter | Source | Shapes | Notes |
| --- | --- | --- | --- |
| `SystemdAdapter` | `systemctl list-timers --all --output=json` data plus a companion units mapping with OnCalendar strings | series, instance | Timers without OnCalendar are systemd-relative: no RRULE, NEXT carried as the one provided instance, LAST as an actual. LAST (else NEXT) also becomes the series anchor. |
| `CronAdapter` | crontab text | series | Comments, blank lines, and environment assignments are skipped. `@reboot` and inexact cron constructs keep `rrule=None`. |
| `YamlCalendarAdapter` | A YAML cadence calendar (list of rows, or a mapping holding one) | series | Field names are renamable via `field_map`; rows may live under any list key. Parked and pending states map to lifecycle `dormant`. Optional `time_of_day` and `weekday` fields (or an explicit `anchor_at`) give a cadence word a phase anchor. |
| `PostgresJobsAdapter` | A job registry table plus a worker queue table, in any Postgres schema | series, instance, band | Fully config-driven column mapping; reads live row sets or a psql JSON dump file. Only recurring and self-chain kinds become series; queue pending rows become next instances (standalone when the job kind has no series); processing rows become bands. An optional `anchor_at` registry column (e.g. a last-run timestamp) phase-locks expansion. |
| `FireLogAdapter` | A fire-log table (job key, fired_at, would_fire_at, run id, observed-only flag, outcome), in any Postgres schema | instance (actuals) | Config-driven like `PostgresJobsAdapter`; consumes a bounded psql JSON extract, never the whole log. Emits firing actuals; observed-only rows stay observed. |
| `CompletionPointsAdapter` | An agent-cycle completion table (agent id, completed_at, run id, is-empty flag) | instance (actuals) | Emits point actuals: moments with no duration. Empty cycles are flagged, not hidden. An optional `series_names` map covers agent keys that differ from their series' native ids. |
| `ServiceStatusAdapter` | A service status table (service, current status, status-since, last-checked) | band | One open band per service from status-since, heartbeat from the last check, the source's status word preserved in the band's extensions and optionally mapped to a core band status. |
| `WorkClaimsAdapter` | A work-claims table (item ref, claim status, runner, claim/heartbeat/lease/release times, outcome) | band | One band per claim; end reasons mapped from claim status. A claim whose lease passed unreleased closes as expired at the lease time. Item titles resolve via an optional title column or the config's `item_titles` map, else the ref itself. |

Anchors: where a source reports a real occurrence of a series (a
timer's LAST, a registry's last-run column, a YAML row's weekday and
time of day), the adapter sets the series' `anchor_at` and expansion
phase-locks to it. Where no source reports one, the projector falls
back to the source's last-update time and marks the series
`phase_approximate` in its extensions, unless the rule pins its own
phase (BYHOUR/BYDAY style). Approximate phase is shown, never hidden.

## Writing a new adapter

Subclass `SourceAdapter`, set `kind` and `shapes`, implement
`collect()`. Keep the source's native reader separate from the mapping
where you can: a `from_files` / `from_rows` constructor that takes
already-read data keeps the adapter testable without the live system,
which is how every reference adapter here is built.
