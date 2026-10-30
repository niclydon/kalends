"""Self-declared holds: the second intake lane (design section 6.2).

Projected adapters cover work a scheduler started. Holds cover the
rest: work no scheduler knows about, where a running thing puts
*itself* on the calendar. A build in a coder pane, an agent holding a
scarce resource, a claimed work item: the holder creates a band with a
title, an owner, a plane, resource claims, and a lease, then
heartbeats it while the work lives.

Lease semantics are the whole contract:

* A hold is alive while its lease is in the future.
* ``heartbeat_hold`` renews the lease (to ``now + lease_seconds``).
* A hold whose lease expires without a heartbeat is over. It is not
  deleted and it does not linger as active: when the store is listed
  with a ``now``, or materialised as bands, an expired hold reads as
  closed with ``end_reason`` ``expired`` and ``ended_at`` at the lease
  expiry. That transition is persisted the first time it is observed,
  so every later reader sees the same closed band.
* ``release_hold`` closes a hold early, with the caller's end reason
  (``released`` by default; ``completed``, ``abandoned`` and
  ``failed`` are equally valid).

This module is transport-free library code over the core model, with a
JSON file as its store. A host wraps it however it likes: the CLI in
this repo (``kalends hold ...``), or an MCP server exposing the four
operations as tools (their shapes are specified in docs/HOLDS.md). No
server and no network code live here, on purpose: the lease rules are
the reusable part.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from .model import Band

HOLDS_SOURCE_ID = "holds"
DEFAULT_LEASE_SECONDS = 3600
_END_REASONS = ("released", "completed", "abandoned", "failed", "expired")


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _now(now: Optional[datetime]) -> datetime:
    return _aware(now) or datetime.now(timezone.utc)


class HoldError(Exception):
    """A hold operation could not be carried out (unknown or closed hold)."""


class HoldStore:
    """The holds API over a JSON file store.

    The store file holds one object: ``{"holds": [band, ...]}`` with
    bands in :meth:`kalends.model.Band.to_dict` shape. The file is
    rewritten on every mutation; holds are few and human-scale, so a
    whole-file store is the honest implementation.
    """

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)

    # -- persistence -----------------------------------------------------

    def _load(self) -> list[Band]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text())
        rows = data.get("holds", []) if isinstance(data, dict) else data
        return [Band.from_dict(row) for row in rows]

    def _save(self, bands: list[Band]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"holds": [b.to_dict() for b in bands]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n")

    # -- lease materialisation ------------------------------------------

    @staticmethod
    def _materialise(band: Band, now: datetime) -> bool:
        """Close ``band`` in place if its lease has expired by ``now``.
        Returns True when the band changed."""
        if band.ended_at is not None or band.status == "done":
            return False
        lease = _aware(band.lease_expires_at)
        if lease is not None and lease <= now:
            band.status = "done"
            band.end_reason = "expired"
            band.ended_at = lease
            return True
        return False

    def _load_materialised(self, now: datetime) -> list[Band]:
        bands = self._load()
        changed = False
        for band in bands:
            changed = self._materialise(band, now) or changed
        if changed:
            self._save(bands)
        return bands

    def _find(self, bands: list[Band], hold_id: str) -> Band:
        for band in bands:
            if band.band_id == hold_id or band.source_native_id == hold_id:
                return band
        raise HoldError(f"unknown hold: {hold_id}")

    # -- the four operations ---------------------------------------------

    def create_hold(
        self,
        title: str,
        owner_kind: str,
        owner_id: Optional[str] = None,
        plane: str = "",
        starts_at: Optional[datetime] = None,
        ends_at: Optional[datetime] = None,
        lease_seconds: Optional[int] = None,
        resource_claims: Optional[dict] = None,
        now: Optional[datetime] = None,
        hold_id: Optional[str] = None,
    ) -> Band:
        """Create a hold band and return it.

        ``starts_at`` defaults to now. The lease runs from the start
        (or from now, for a future hold) for ``lease_seconds``
        (default one hour), unless ``ends_at`` is given, in which case
        the hold is a fixed block and its lease is its end.
        """
        moment = _now(now)
        start = _aware(starts_at) or moment
        if _aware(ends_at) is not None:
            lease_expires = _aware(ends_at)
        else:
            lease_base = max(start, moment)
            lease_expires = lease_base + timedelta(
                seconds=lease_seconds or DEFAULT_LEASE_SECONDS
            )
        native = hold_id or f"hold-{secrets.token_hex(4)}"
        band = Band(
            band_id=f"{HOLDS_SOURCE_ID}:{native}",
            source_id=HOLDS_SOURCE_ID,
            source_native_id=native,
            title=title,
            owner_kind=owner_kind,
            owner_id=owner_id,
            plane=plane,
            started_at=start,
            last_heartbeat_at=moment,
            lease_expires_at=lease_expires,
            status="active",
            resource_claims=dict(resource_claims or {}),
            extensions={"band_kind": "hold"},
        )
        bands = self._load_materialised(moment)
        bands.append(band)
        self._save(bands)
        return band

    def heartbeat_hold(
        self,
        hold_id: str,
        now: Optional[datetime] = None,
        lease_seconds: Optional[int] = None,
    ) -> Band:
        """Renew a live hold's lease and stamp its heartbeat.

        With ``lease_seconds`` the lease extends that far from now;
        without it, the hold's original lease length is reused.
        Heartbeating a closed or expired hold is an error, not a
        resurrection: the holder starts a new hold instead.
        """
        moment = _now(now)
        bands = self._load_materialised(moment)
        band = self._find(bands, hold_id)
        if band.ended_at is not None or band.status == "done":
            raise HoldError(
                f"hold {hold_id} is closed ({band.end_reason or band.status}); "
                "create a new hold instead of heartbeating a closed one"
            )
        previous_lease = _aware(band.lease_expires_at)
        previous_beat = _aware(band.last_heartbeat_at) or _aware(band.started_at)
        if lease_seconds is not None:
            span = timedelta(seconds=lease_seconds)
        elif previous_lease is not None and previous_beat is not None:
            span = previous_lease - previous_beat
        else:
            span = timedelta(seconds=DEFAULT_LEASE_SECONDS)
        band.last_heartbeat_at = moment
        band.lease_expires_at = moment + span
        self._save(bands)
        return band

    def release_hold(
        self,
        hold_id: str,
        now: Optional[datetime] = None,
        end_reason: str = "released",
    ) -> Band:
        """Close a hold now, with the given end reason."""
        if end_reason not in _END_REASONS:
            raise HoldError(f"end_reason must be one of {_END_REASONS}")
        moment = _now(now)
        bands = self._load_materialised(moment)
        band = self._find(bands, hold_id)
        if band.ended_at is not None or band.status == "done":
            # Idempotent in effect, explicit in result: releasing an
            # already-closed hold reports the closure that stands.
            return band
        band.status = "done"
        band.end_reason = end_reason
        band.ended_at = moment
        self._save(bands)
        return band

    def list_holds(
        self,
        now: Optional[datetime] = None,
        include_closed: bool = True,
    ) -> list[Band]:
        """All holds, with lease expiry materialised as of ``now``."""
        moment = _now(now)
        bands = self._load_materialised(moment)
        if not include_closed:
            bands = [b for b in bands if b.ended_at is None and b.status != "done"]
        return sorted(
            bands, key=lambda b: _aware(b.started_at) or moment
        )

    def bands(self, now: Optional[datetime] = None) -> list[Band]:
        """The holds as calendar bands, for handing to the projector."""
        return self.list_holds(now=now, include_closed=True)


# Functional conveniences over a store path, for hosts that prefer
# plain functions to a store object.

def create_hold(path: Union[str, Path], **kwargs: Any) -> Band:
    return HoldStore(path).create_hold(**kwargs)


def heartbeat_hold(path: Union[str, Path], hold_id: str, **kwargs: Any) -> Band:
    return HoldStore(path).heartbeat_hold(hold_id, **kwargs)


def release_hold(path: Union[str, Path], hold_id: str, **kwargs: Any) -> Band:
    return HoldStore(path).release_hold(hold_id, **kwargs)


def list_holds(path: Union[str, Path], **kwargs: Any) -> list[Band]:
    return HoldStore(path).list_holds(**kwargs)
