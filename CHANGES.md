# Changes

## 0.4.0

- Google Calendar mirror (Phase 4, public core): new
  `kalends.gmirror` publish adapter mirroring a curated subset of
  Kalends rows one-way to Google Calendar. Bands owned by a
  configured owner overlapping the sync window and one-off
  instances owned by a configured owner starting in the window are
  eligible; recurring instances, windows, and actuals never mirror.
  Includes `stable_key` (deterministic `kalends:band:` /
  `kalends:oneoff:` keys stored in an event's private extended
  properties), `event_body` (Google Calendar API event resources
  with offset-bearing RFC 3339 times in a named timezone, the
  private key, and no attendees), the `GoogleClient` protocol a
  deployment implements with its own credentials (no Google SDK and
  no credential handling in the core), and an idempotent
  `reconcile` that creates, patches, and deletes only events
  carrying the mirror's key, with a zero-write dry-run mode.
- New `kalends gmirror` CLI subcommand with `--dry-run`,
  `--owners`, `--timezone` (default `UTC`), `--calendar-id`, and
  `--window-days-back` / `--window-days-ahead`. Without a
  deployment-supplied client it exits nonzero with an explanation;
  in `--dry-run` mode it prints the desired set (and, with a
  client, the planned actions) without one. The client factory is
  named by the `KALENDS_GMIRROR_CLIENT_FACTORY` environment
  variable. See `docs/MIRROR.md`.

## 0.3.0

- Phase 3 views: band view, hour drill-down, and agent pages.

## 0.2.0

- Phase 2: actuals, bands, holds API, anchors, table window; the
  `kalends-mcp` server and adoption guide.

## 0.1.0

- Phase 1: schema, projector, reference adapters, table view.
