# Mirroring to Google Calendar

Kalends can mirror a curated subset of its projection one-way to a
Google Calendar. The mirror exists for people who live in Google
Calendar: selected fleet work becomes visible there, next to
everything else on their calendar, without Google Calendar becoming
a source for Kalends and without this package ever holding a Google
credential.

The code is `src/kalends/gmirror.py`. It is a generic publish
adapter: it decides what should be on the calendar, builds Google
Calendar API event bodies, and reconciles the two. The transport —
the actual Google API calls, with the adopter's own credentials —
is supplied by the deployment, as described under
[Supplying a client](#supplying-a-client) below.

## What mirrors

Only this subset, and nothing else:

- **Bands** owned by a configured owner whose interval overlaps the
  sync window. The band interval runs from its start to its end if
  it has ended, else to its lease expiry if it has one, else to
  thirty minutes after its start.
- **One-off instances** owned by a configured owner whose planned
  start lies inside the sync window. A one-off with no planned end
  mirrors as thirty minutes.

And, plainly, what does not mirror:

- Recurring instances never mirror, for any owner. A recurring
  definition expanded into dozens of occurrences would flood a
  personal calendar; the fleet calendar is where recurrence lives.
- Work owned by anyone outside the configured owner set never
  mirrors.
- Windows (availability, maintenance, quiet hours, holds on time)
  are not events and never mirror.
- Actuals (what did fire, and when) never mirror. The mirror shows
  the plan and long-running work, not the fire history.

The owner set and the window are configuration, not code. For
example, a deployment might mirror owners `crimson` and `azure`
over a window from one day back to fourteen days ahead, and nothing
else.

## One-way semantics

The mirror is strictly one-way: Kalends to Google Calendar, never
back.

- Every event the mirror creates carries, in
  `extendedProperties.private`, a `kalends_key` (for example
  `kalends:band:b-1` or `kalends:oneoff:i-1`), plus the owner and
  the kind. That key is the event's identity for reconciliation.
- The mirror owns only events carrying its private key. It creates,
  updates, and deletes those, and never touches any other event on
  the calendar — personal events, events from other tools, and
  events whose key was removed are all left alone.
- Edits made in Google Calendar do not flow back to Kalends. If a
  mirrored event's title or times are edited in Google Calendar,
  the next reconcile sees a summary or time that differs from the
  desired item and patches the event back to what Kalends says.
  Treat edits to mirrored events as temporary annotations, not as
  changes; change the work in Kalends (or in the scheduler behind
  it) instead.
- Deleting a mirrored event in Google Calendar does not delete
  anything in Kalends; the next reconcile recreates it while its
  Kalends row is still in the eligible subset.
- When a Kalends row leaves the subset — the band ends outside the
  window, the owner is removed from the configured set, the one-off
  passes — its mirrored event has a key with no desired counterpart,
  and reconcile deletes it.

### Free-busy note

Mirrored events are ordinary timed events on the target calendar,
so they participate in that calendar's free-busy behaviour exactly
as the calendar's own sharing settings dictate: anyone who can see
free-busy for the calendar will see these times as busy. If a band
is long-running, its mirror is a long busy block. Choose the owner
set and the target calendar with that in mind; a dedicated calendar
for mirrored fleet work keeps free-busy legible and lets the owner
set be widened safely later.

## Reconciliation and idempotency

A sync run is a reconcile, not a push:

1. Compute the desired set with `eligible_items` for the configured
   owners and window.
2. List the target calendar's events in the window through the
   client.
3. Match existing events to desired items by `kalends_key`.
   Insert the missing, patch those whose summary, start, or end
   differ (instants are compared, so the same moment written with a
   different UTC offset is not a difference), delete keyed events
   with no desired counterpart, and leave unkeyed events alone.

Reconcile is idempotent: running it twice over the same inputs
creates nothing the second time, because the events the first run
created carry the keys the second run matches on. A crashed or
interrupted run is safe to repeat. Stable keys are what make this
work — `stable_key(kind, native_id)` is deterministic and depends
only on the kind and the Kalends row id, never on titles or times.

A dry run computes the desired set and, when a client is available,
the same create / update / delete / unchanged decisions, while
making zero write calls.

## Supplying a client

The public core never sees Google credentials. There is no Google
SDK dependency, no credential file format, and no token handling in
this package: credentials are the adopter's own, held wherever the
adopter's deployment holds them.

A deployment supplies one object implementing the
`kalends.gmirror.GoogleClient` protocol:

```python
class GoogleClient(Protocol):
    def list_events(self, calendar_id, time_min, time_max) -> list[dict]: ...
    def insert_event(self, calendar_id, body) -> dict: ...
    def patch_event(self, calendar_id, event_id, body) -> dict: ...
    def delete_event(self, calendar_id, event_id) -> None: ...
```

An implementation typically wraps the Google Calendar API's
`events.list` / `events.insert` / `events.patch` / `events.delete`
calls for one calendar, authenticating with the adopter's own
service account or OAuth credentials, and handling paging inside
`list_events` so the caller sees one list. `time_min` and
`time_max` arrive as RFC 3339 timestamps; returned events are API
event resources (at least `id`, `summary`, `start`, `end`, and
`extendedProperties` where present).

With that object, a deployment's sync is a few lines:

```python
from kalends.gmirror import eligible_items, reconcile

desired = eligible_items(
    bands, instances,
    owners={"crimson", "azure"},
    window_start=window_start,
    window_end=window_end,
)
existing = client.list_events(
    calendar_id, window_start.isoformat(), window_end.isoformat()
)
result = reconcile(desired, existing, client, calendar_id,
                   timezone_name="UTC")
print(result.created, result.updated, result.deleted, result.unchanged)
```

### Calendar mapping

Map owners to calendars in deployment configuration, outside the
core: for example, owner `crimson` to one calendar and owner
`azure` to another, or several owners to one shared fleet calendar.
One reconcile call targets exactly one calendar; a deployment
mirroring to several calendars groups the desired items by owner,
looks up each owner's calendar, and reconciles each group against
that calendar. Keep the mapping in the deployment's own config so
reassigning a calendar never requires a core change.

### The CLI

```bash
# Preview the desired set; needs no client and makes no calls.
kalends gmirror --dry-run --owners crimson,azure

# Preview against a real calendar when a client factory is wired.
kalends gmirror --dry-run --owners crimson --calendar-id primary

# Sync (requires a client; see below).
kalends gmirror --owners crimson --calendar-id primary \
    --timezone UTC --window-days-back 1 --window-days-ahead 14
```

`--owners` takes comma-separated owner ids and may be repeated;
with no `--owners`, every owner present in the input is eligible.
`--dump` / `--examples` select the input, exactly as the other view
commands do (a JSON dump, or the example fleet by default).
`--timezone` names the zone event times are written in and defaults
to `UTC`, a neutral public default: a deployment in one place
should set its own zone explicitly.

The core ships no client, so a non-dry-run `kalends gmirror` with
no client configured explains that a deployment supplies the client
(see this document) and exits nonzero. To wire one, point the
environment variable `KALENDS_GMIRROR_CLIENT_FACTORY` at a
zero-argument factory in the deployment's own code, in
`some.module:factory` (or `some.module.factory`) form; the factory
returns a `GoogleClient`. In `--dry-run` mode the command works
with or without that factory: without one it prints the desired set
and states that no existing calendar was read.

### Suggested cadence

Reconcile is cheap and idempotent, so cadence is a freshness
choice, not a correctness one. Every 15 to 60 minutes is a sensible
default for a fleet mirror: often enough that the Google Calendar
view feels current, rarely enough that API quotas and calendar
churn stay trivial. Widen the window's back edge if bands that
started earlier must stay visible; bands are matched by overlap,
so a band that started before the window but is still running
mirrors as long as its interval reaches into the window.
