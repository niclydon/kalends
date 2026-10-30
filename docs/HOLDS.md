# Self-declared holds

Design section 6.2: the second intake lane. Projected adapters cover
work a scheduler started; holds cover the rest. A running thing puts
*itself* on the calendar: a band with a title, an owner, a plane,
resource claims, and a lease, kept alive by heartbeats.

Use a hold for work no scheduler knows about:

- a build or test run in a coder pane that will take an hour,
- an agent holding a scarce resource (a slot, a device, a lane)
  while it works,
- a claimed work item whose claim system is not itself a calendar
  source,
- a long migration or backfill started by hand.

## The lease contract

A hold is alive while its lease is in the future.

- `create_hold` opens the hold and starts its lease.
- `heartbeat_hold` renews the lease. Without an explicit
  `lease_seconds`, the hold's original lease length is reused.
- A hold whose lease expires without a heartbeat is **over**. It is
  not deleted and it does not linger as active: when the store is
  listed with a `now`, or materialised as bands, it reads as closed
  with `end_reason` `expired` and `ended_at` at the lease expiry.
  The transition is persisted the first time it is observed, so all
  later readers see the same closed band.
- `release_hold` closes a hold early, with an end reason:
  `released` (default), `completed`, `abandoned`, or `failed`.
- Heartbeating a closed or expired hold is an error, never a
  resurrection. The holder creates a new hold. This is deliberate:
  silently reviving a hold would rewrite the history of who held
  what, when.

## The library

`src/kalends/holds.py` is transport-free: a `HoldStore` over a JSON
file, plus module-level convenience functions of the same names.
Bands it returns are core `Band` objects (source `holds`), ready to
hand to the projector alongside adapter output.

```python
from kalends.holds import HoldStore

store = HoldStore("kalends-holds.json")
band = store.create_hold(
    "Build the harbour chart pack",
    owner_kind="worker",
    owner_id="worker-3",
    plane="host-a",
    lease_seconds=3600,
    resource_claims={"slot_group": "survey"},
)
store.heartbeat_hold(band.band_id)          # lease renewed
store.release_hold(band.band_id, end_reason="completed")
bands = store.bands()                        # for the projector
```

The store file is one JSON object, `{"holds": [band, ...]}`, in
`Band.to_dict` shape, rewritten on each mutation. Holds are few and
human-scale; a whole-file store is the honest implementation.

## The CLI

```bash
kalends hold --store kalends-holds.json create \
    --title "Build the harbour chart pack" \
    --owner-kind worker --owner-id worker-3 --plane host-a \
    --lease-seconds 3600 --claim slot_group=survey
kalends hold --store kalends-holds.json heartbeat holds:hold-ab12cd34
kalends hold --store kalends-holds.json release holds:hold-ab12cd34 \
    --end-reason completed
kalends hold --store kalends-holds.json list
kalends hold --store kalends-holds.json list --active-only
```

The store path defaults to `$KALENDS_HOLDS_FILE` or
`./kalends-holds.json`.

## Wrapping it as MCP tools

A host that speaks MCP wraps the store with four tools, one per
operation. The names and argument schemas below are the contract;
the transport (stdio, HTTP) is the host's business and deliberately
not part of this repository.

### `create_hold`

```json
{
  "name": "create_hold",
  "description": "Put the caller's current work on the fleet calendar as a lease-held band.",
  "inputSchema": {
    "type": "object",
    "required": ["title", "owner_kind"],
    "properties": {
      "title": {"type": "string"},
      "owner_kind": {"type": "string", "enum": ["agent", "worker", "system", "human-facing"]},
      "owner_id": {"type": "string"},
      "plane": {"type": "string"},
      "starts_at": {"type": "string", "format": "date-time"},
      "ends_at": {"type": "string", "format": "date-time",
                  "description": "Fixed end; when given, the lease is the end."},
      "lease_seconds": {"type": "integer", "default": 3600},
      "resource_claims": {"type": "object", "additionalProperties": true}
    }
  }
}
```

Returns the created band (its `band_id` is the handle for the other
three tools).

### `heartbeat_hold`

```json
{
  "name": "heartbeat_hold",
  "description": "Renew a live hold's lease. Errors on a closed or expired hold.",
  "inputSchema": {
    "type": "object",
    "required": ["hold_id"],
    "properties": {
      "hold_id": {"type": "string"},
      "lease_seconds": {"type": "integer",
                        "description": "New lease length from now; default: reuse the hold's original length."}
    }
  }
}
```

### `release_hold`

```json
{
  "name": "release_hold",
  "description": "Close a hold now.",
  "inputSchema": {
    "type": "object",
    "required": ["hold_id"],
    "properties": {
      "hold_id": {"type": "string"},
      "end_reason": {"type": "string",
                     "enum": ["released", "completed", "abandoned", "failed", "expired"],
                     "default": "released"}
    }
  }
}
```

### `list_holds`

```json
{
  "name": "list_holds",
  "description": "List holds, with lease expiry materialised as of now.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "include_closed": {"type": "boolean", "default": true}
    }
  }
}
```

All four return bands in the core `Band` JSON shape (the same shape
calendar dumps carry), so a host can merge tool results straight
into a projection.
