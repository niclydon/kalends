-- Kalends core schema (migration 001)
--
-- The calendar is a projection over existing schedulers, stored as six
-- tables: sources (the adapter registry), series (definitions),
-- instances (materialised occurrences), bands (long-running work),
-- windows (availability), and actuals (the optional join table for
-- sources whose fire log lives elsewhere).
--
-- All timestamps are timestamptz. Series also store their local
-- timezone, because a 09:07 cadence means 09:07 local.

BEGIN;

-- ---------------------------------------------------------------------------
-- calendar_sources: adapter registry. Configuration as data: which
-- sources exist, which adapter reads them, which plane they belong to,
-- and how fresh their data is.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_sources (
    source_id         text PRIMARY KEY,
    adapter           text NOT NULL,
    plane             text NOT NULL,
    enabled           boolean NOT NULL DEFAULT true,
    sync_interval_sec integer,
    last_synced_at    timestamptz,
    last_source_at    timestamptz,
    notes             text
);

COMMENT ON TABLE calendar_sources IS
    'Adapter registry: one row per upstream system the calendar reads.';
COMMENT ON COLUMN calendar_sources.adapter IS
    'Adapter kind: systemd, cron, launchd, postgres-job-registry, yaml-calendar, queue, presence, holds-api, ical-feed.';
COMMENT ON COLUMN calendar_sources.last_synced_at IS
    'When the calendar last read this source. Views show sync age; stale sources are visibly stale.';
COMMENT ON COLUMN calendar_sources.last_source_at IS
    'The newest timestamp the source itself reported in the last sync.';

-- ---------------------------------------------------------------------------
-- calendar_series: recurring definitions. Never rendered directly; the
-- projector expands them into instances over a rolling horizon.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_series (
    series_id             text PRIMARY KEY,
    source_id             text NOT NULL REFERENCES calendar_sources (source_id),
    source_native_id      text NOT NULL,
    title                 text NOT NULL,
    owner_kind            text NOT NULL DEFAULT 'system',
    owner_id              text,
    plane                 text NOT NULL,
    cadence_dialect       text NOT NULL DEFAULT 'none',
    cadence_expr          text,
    rrule                 text,
    timezone              text NOT NULL DEFAULT 'UTC',
    expected_duration_sec integer,
    overlap_policy        text NOT NULL DEFAULT 'allow-all',
    enabled               boolean NOT NULL DEFAULT true,
    lifecycle_state       text NOT NULL DEFAULT 'active',
    source_updated_at     timestamptz,
    synced_at             timestamptz,
    anchor_at             timestamptz,
    resource_claims       jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_id, source_native_id)
);

COMMENT ON TABLE calendar_series IS
    'Recurring definitions, one row per source-native schedule.';
COMMENT ON COLUMN calendar_series.owner_kind IS
    'agent, worker, system, or human-facing. Owner kind is data, not naming convention.';
COMMENT ON COLUMN calendar_series.cadence_dialect IS
    'cron, systemd-calendar, systemd-relative, interval, rrule, self-chain, or none.';
COMMENT ON COLUMN calendar_series.cadence_expr IS
    'The cadence exactly as the source states it. Always kept, even when rrule is null.';
COMMENT ON COLUMN calendar_series.rrule IS
    'Normalised iCalendar RRULE. Null for self-chain and relative cadences, which have no honest RRULE.';
COMMENT ON COLUMN calendar_series.lifecycle_state IS
    'active, retired, deprecated, or dormant (parked: exists, projects nothing, can wake).';
COMMENT ON COLUMN calendar_series.anchor_at IS
    'A known occurrence of this series (source-reported last or next firing). Expansion phase-locks to it. Null: expansion falls back to source_updated_at and the phase is approximate. Added in migration 002 for pre-existing installs; included here so fresh installs match.';
COMMENT ON COLUMN calendar_series.resource_claims IS
    'Named resources every instance of this series claims (a host, a GPU, a slot group, a lane). Copied onto instances at projection. Added in migration 002 for pre-existing installs; included here so fresh installs match.';

CREATE INDEX IF NOT EXISTS calendar_series_source_idx
    ON calendar_series (source_id);
CREATE INDEX IF NOT EXISTS calendar_series_owner_idx
    ON calendar_series (owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS calendar_series_plane_idx
    ON calendar_series (plane);
CREATE INDEX IF NOT EXISTS calendar_series_lifecycle_idx
    ON calendar_series (lifecycle_state);

-- ---------------------------------------------------------------------------
-- calendar_instances: materialised occurrences. Views query these,
-- never expand series at read time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_instances (
    instance_id       text PRIMARY KEY,
    series_id         text REFERENCES calendar_series (series_id),
    source_id         text NOT NULL REFERENCES calendar_sources (source_id),
    source_native_id  text NOT NULL,
    title             text NOT NULL,
    owner_kind        text NOT NULL DEFAULT 'system',
    owner_id          text,
    plane             text NOT NULL,
    shape             text NOT NULL DEFAULT 'instance',
    planned_start     timestamptz,
    planned_end       timestamptz,
    actual_start      timestamptz,
    actual_end        timestamptz,
    status            text NOT NULL DEFAULT 'scheduled',
    resource_claims   jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_updated_at timestamptz,
    synced_at         timestamptz
);

COMMENT ON TABLE calendar_instances IS
    'One occurrence of a series, or a standalone planned run. Planned and actual times are both first-class.';
COMMENT ON COLUMN calendar_instances.shape IS
    'instance, or one-off for a standalone planned run with no series.';
COMMENT ON COLUMN calendar_instances.status IS
    'scheduled, dispatched, in-progress, completed, failed, timed-out, skipped, or forecast. Forecast only where a source itself labels an occurrence as estimated.';
COMMENT ON COLUMN calendar_instances.resource_claims IS
    'Named resources this instance needs (a host, a GPU, a slot group, a lane). Version 1 reports contention on shared claims; it does not arbitrate.';

CREATE INDEX IF NOT EXISTS calendar_instances_planned_start_idx
    ON calendar_instances (planned_start);
CREATE INDEX IF NOT EXISTS calendar_instances_owner_idx
    ON calendar_instances (owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS calendar_instances_plane_idx
    ON calendar_instances (plane);
CREATE INDEX IF NOT EXISTS calendar_instances_status_idx
    ON calendar_instances (status);
CREATE INDEX IF NOT EXISTS calendar_instances_series_idx
    ON calendar_instances (series_id);

-- ---------------------------------------------------------------------------
-- calendar_bands: long-running work with a start and a heartbeat but
-- no scheduled end. A band ends when its heartbeat stops or its lease
-- expires, not when a clock says so.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_bands (
    band_id           text PRIMARY KEY,
    source_id         text NOT NULL REFERENCES calendar_sources (source_id),
    source_native_id  text NOT NULL,
    title             text NOT NULL,
    owner_kind        text NOT NULL DEFAULT 'system',
    owner_id          text,
    plane             text NOT NULL,
    started_at        timestamptz,
    last_heartbeat_at timestamptz,
    lease_expires_at  timestamptz,
    ended_at          timestamptz,
    end_reason        text,
    status            text NOT NULL DEFAULT 'active',
    resource_claims   jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE calendar_bands IS
    'Long-running work: services, sessions, claimed work, self-declared holds.';
COMMENT ON COLUMN calendar_bands.status IS
    'active, blocked, idle, or done.';
COMMENT ON COLUMN calendar_bands.end_reason IS
    'completed, released, expired, abandoned, failed, or observed-stopped.';

CREATE INDEX IF NOT EXISTS calendar_bands_status_idx
    ON calendar_bands (status);
CREATE INDEX IF NOT EXISTS calendar_bands_owner_idx
    ON calendar_bands (owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS calendar_bands_plane_idx
    ON calendar_bands (plane);
CREATE INDEX IF NOT EXISTS calendar_bands_source_idx
    ON calendar_bands (source_id);

-- ---------------------------------------------------------------------------
-- calendar_windows: availability and its inverse, modelled after
-- RFC 7953 VAVAILABILITY.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_windows (
    window_id        text PRIMARY KEY,
    owner_kind       text NOT NULL,
    owner_id         text,
    plane            text NOT NULL,
    kind             text NOT NULL DEFAULT 'available',
    starts_at        timestamptz,
    ends_at          timestamptz,
    rrule            text,
    priority         integer NOT NULL DEFAULT 0,
    source_id        text REFERENCES calendar_sources (source_id),
    source_native_id text
);

COMMENT ON TABLE calendar_windows IS
    'Availability windows and their inverse: maintenance, quiet hours, holds.';
COMMENT ON COLUMN calendar_windows.kind IS
    'available, maintenance, quiet, or hold.';
COMMENT ON COLUMN calendar_windows.priority IS
    'Per RFC 7953: higher priority wins when windows overlap.';

CREATE INDEX IF NOT EXISTS calendar_windows_owner_idx
    ON calendar_windows (owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS calendar_windows_starts_idx
    ON calendar_windows (starts_at);

-- ---------------------------------------------------------------------------
-- calendar_actuals: optional join table for sources whose fire log is
-- a separate, usually much larger, table elsewhere. Actuals are
-- aggregated into instances at projection time; a big fire log is
-- never copied wholesale, so deployments with an existing log may keep
-- it in place and skip this table entirely.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_actuals (
    actual_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id        text NOT NULL REFERENCES calendar_sources (source_id),
    source_native_id text NOT NULL,
    series_id        text REFERENCES calendar_series (series_id),
    instance_id      text REFERENCES calendar_instances (instance_id),
    fired_at         timestamptz NOT NULL,
    would_fire_at    timestamptz,
    observed_only    boolean NOT NULL DEFAULT false,
    status           text NOT NULL DEFAULT 'completed',
    kind             text NOT NULL DEFAULT 'firing',
    is_empty         boolean NOT NULL DEFAULT false
);

COMMENT ON TABLE calendar_actuals IS
    'Observed firings. planned-versus-actual joins would_fire_at to fired_at.';
COMMENT ON COLUMN calendar_actuals.observed_only IS
    'True when the scheduler observed a firing it did not cause; never presented as executed by the fleet.';
COMMENT ON COLUMN calendar_actuals.kind IS
    'firing (a run that happened, with a would-fire time) or point (a completion moment with no duration). Added in migration 002 for pre-existing installs; included here so fresh installs match.';
COMMENT ON COLUMN calendar_actuals.is_empty IS
    'Point actuals only: the cycle completed having done nothing. Shown, never hidden.';

CREATE INDEX IF NOT EXISTS calendar_actuals_series_idx
    ON calendar_actuals (series_id);
CREATE INDEX IF NOT EXISTS calendar_actuals_fired_idx
    ON calendar_actuals (fired_at);

COMMIT;
