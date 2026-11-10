-- Kalends migration 004: index calendar_actuals.instance_id.
--
-- calendar_actuals.instance_id references calendar_instances, but
-- the column was never indexed, so every delete on
-- calendar_instances paid a referential-integrity check that
-- sequentially scanned calendar_actuals once per deleted row.
-- Deployments that churn instances (scoped replaces that rewrite a
-- source's whole horizon each cycle) slow down as the actuals table
-- grows. This index makes the foreign-key check an index probe.
--
-- Fresh installs run the updated 001 (which already carries the
-- index) and do not need this file; it brings a pre-004 database
-- forward. Every statement is idempotent.

BEGIN;

CREATE INDEX IF NOT EXISTS calendar_actuals_instance_id_idx
    ON calendar_actuals (instance_id);

COMMENT ON INDEX calendar_actuals_instance_id_idx IS
    'Supports the calendar_actuals instance_id foreign-key check on calendar_instances deletes: without it every instance delete seq-scans calendar_actuals.';

COMMIT;
