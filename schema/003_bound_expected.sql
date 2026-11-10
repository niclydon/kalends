-- Kalends migration 003: instance bound_expected marker.
--
-- The projector marks instances of a series whose cadence semantics
-- are bound (the series' resource_claims carry
-- cadence_semantics = 'bound'): the occurrence is owed by the
-- cadence, not merely expected. Deployments whose database predates
-- this column keep working with the new code before this migration
-- is applied: the Postgres store feature-detects the column and
-- omits it from writes (reads default it to false) until it exists.
--
-- Fresh installs run the updated 001 (which already carries the
-- column) and do not need this file; it brings a pre-003 database
-- forward. Every statement is idempotent.

BEGIN;

ALTER TABLE calendar_instances
    ADD COLUMN IF NOT EXISTS bound_expected boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN calendar_instances.bound_expected IS
    'True when the projector materialised this instance for a series whose cadence semantics are bound (the series resource_claims carry cadence_semantics = bound): the occurrence is owed by the cadence.';

COMMIT;
