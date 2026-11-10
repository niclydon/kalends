# Changes

## 0.6.0

- New `kalends_delete_source` MCP tool: deleting a source errors
  cleanly when the source is unknown, and is refused while the
  source still has dependent rows (series, instances, bands,
  windows, actuals) unless `force=true` is passed; with force the
  dependents are deleted with the source in FK-safe order inside
  one transaction. Returns
  `{deleted, source_id, force, deleted_dependents}`. Implemented in
  the service layer and both stores (in-memory and Postgres).

## 0.5.0

- Server-side owner/shape filters on the MCP list tools:
  `kalends_list_instances` gains optional `owner_id`, `owner_kind`,
  and `shape` parameters, and `kalends_list_bands` gains an optional
  `owner_kind` parameter. Filtering is real filtering in both store
  implementations (equality predicates in the in-memory store, WHERE
  clauses in the Postgres store), composes with the existing
  filters and with `limit` / `offset` paging, and an unknown filter
  value returns an empty result rather than an error. Unfiltered
  calls behave exactly as before.

## 0.4.1

- gmirror: rows whose computed end is at or before their start (empty intervals, e.g. a hold released before its future start) are never mirror-eligible.

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
