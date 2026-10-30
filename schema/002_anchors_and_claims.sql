-- Kalends migration 002: series anchors, series resource claims,
-- actual kinds.
--
-- Phase 2 additions:
--
-- * calendar_series.anchor_at: a known occurrence of the series (a
--   source-reported last or next firing). Interval and RRULE
--   expansion phase-locks to it; without one, expansion falls back to
--   the source's last-update time and the series is marked
--   phase-approximate, so views can be honest about the phase.
-- * calendar_series.resource_claims: promoted from adapter extension
--   metadata to a first-class column. The projector copies a series'
--   claims onto every instance it materialises.
-- * calendar_actuals.kind / is_empty: an actual is either a firing
--   (a run that happened) or a point (an agent cycle completion: a
--   moment, no duration), and a point may be flagged empty when the
--   cycle completed having done nothing.
--
-- Fresh installs run the updated 001 (which already carries these
-- columns) and do not need this file; it brings a 001-era database
-- forward. Every statement is idempotent.

BEGIN;

ALTER TABLE calendar_series
    ADD COLUMN IF NOT EXISTS anchor_at timestamptz;

COMMENT ON COLUMN calendar_series.anchor_at IS
    'A known occurrence of this series (source-reported last or next firing). Expansion phase-locks to it. Null: expansion falls back to source_updated_at and the phase is approximate.';

ALTER TABLE calendar_series
    ADD COLUMN IF NOT EXISTS resource_claims jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN calendar_series.resource_claims IS
    'Named resources every instance of this series claims (a host, a GPU, a slot group, a lane). Copied onto instances at projection.';

ALTER TABLE calendar_actuals
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'firing';

COMMENT ON COLUMN calendar_actuals.kind IS
    'firing (a run that happened, with a would-fire time) or point (a completion moment with no duration).';

ALTER TABLE calendar_actuals
    ADD COLUMN IF NOT EXISTS is_empty boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN calendar_actuals.is_empty IS
    'Point actuals only: the cycle completed having done nothing. Shown, never hidden.';

COMMIT;
