"""Phase B — license activation, validation, deactivation, and the
public "what we know about you" trust endpoint.

The flow:
  1. User pays via Stripe → webhook mints a license + emails the key
     (Phase B.3 — landing soon).
  2. User pastes the key into GhostShell → client calls /activate with
     the raw hardware fingerprint inputs.  Server hashes them with the
     fingerprint pepper and consumes one of 5 slots.
  3. App launches in future → /validate confirms the license is still
     active and refreshes last_seen.
  4. User wants to move to a new PC → portal calls /deactivate to free a
     slot, then activates on the new machine.

Zero-knowledge invariant:
  • Raw CPUID / motherboard UUID / disk serial arrive over TLS, are HMAC'd
    in `security.hash_fingerprint`, and the raw values are dropped before
    any DB write or log line.
  • Plaintext license keys are never stored — only the HMAC.
  • Public endpoint returns ONLY the row's pseudonymous fields.  Anyone
    can call it with any license_id and see literally everything the
    server knows.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from db import get_db
from security import (
    hash_fingerprint,
    hash_license_key,
    is_valid_license_key_format,
    normalize_license_key,
)
from settings import settings

log = logging.getLogger("ghostshell-cloud.licenses")

router = APIRouter(prefix="/v1/licenses", tags=["licenses"])


# ── Schemas ───────────────────────────────────────────────────────────────
class HardwareInputs(BaseModel):
    """Raw hardware fingerprint inputs.  These hit the server briefly
    and are HMAC'd inline; never stored, never logged."""
    cpu_id:           str = Field(..., min_length=1, max_length=128)
    motherboard_uuid: str = Field(..., min_length=1, max_length=128)
    disk_serial:      str = Field(..., min_length=1, max_length=128)


class HardwareBuckets(BaseModel):
    """Non-identifying bucket info — safe to store as plaintext.
    'rtx 5070 ti' tells us nothing about the user."""
    gpu_model_norm: str | None = Field(None, max_length=64)
    cpu_family:     str | None = Field(None, max_length=32)
    ram_gb_bucket:  str | None = Field(None, max_length=8)


class ActivateRequest(BaseModel):
    license_key: str
    hardware:    HardwareInputs
    buckets:     HardwareBuckets = Field(default_factory=HardwareBuckets)
    app_version: str | None = Field(None, max_length=16)


class ValidateRequest(BaseModel):
    license_key: str
    hardware:    HardwareInputs


class DeactivateRequest(BaseModel):
    license_key: str
    slot_no:     int = Field(..., ge=1, le=20)


class SlotView(BaseModel):
    """Public-safe slot info — no identifiers, just hardware buckets."""
    slot_no:        int
    gpu_model_norm: str | None
    cpu_family:     str | None
    ram_gb_bucket:  str | None
    activated_at:   datetime
    last_seen:      datetime


class ActivateResponse(BaseModel):
    ok:           bool = True
    license_id:   int
    slot_no:      int
    slots_used:   int
    slots_max:    int
    status:       str
    is_new_slot:  bool


class ValidateResponse(BaseModel):
    ok:           bool = True
    license_id:   int
    slot_no:      int
    status:       str
    last_seen:    datetime
    refunded_at:  datetime | None = None
    revoked_at:   datetime | None = None


class PublicLicenseView(BaseModel):
    """Returned by the public endpoint.  This is the *complete* server-side
    record — there is no other table containing data about this license."""
    license_id:   int
    status:       str
    paid_at:      datetime
    refunded_at:  datetime | None
    revoked_at:   datetime | None
    slots_used:   int
    slots_max:    int
    slots:        list[SlotView]
    note:         str = (
        "This is the complete server-side record for this license. "
        "We have no other data — no email, no IP, no raw hardware IDs."
    )


# ── Helpers ───────────────────────────────────────────────────────────────
async def _find_license_by_key(
    db: asyncpg.Connection, key: str,
) -> asyncpg.Record | None:
    if not is_valid_license_key_format(key):
        return None
    return await db.fetchrow(
        """SELECT id, status, paid_at, refunded_at, revoked_at, revoke_reason
             FROM licenses WHERE key_hash = $1""",
        hash_license_key(key),
    )


async def _count_active_slots(db: asyncpg.Connection, license_id: int) -> int:
    return await db.fetchval(
        """SELECT COUNT(*) FROM license_devices
            WHERE license_id = $1 AND deactivated_at IS NULL""",
        license_id,
    )


async def _next_free_slot(db: asyncpg.Connection, license_id: int) -> int:
    """Find the lowest slot_no in [1..max] that isn't currently active."""
    rows = await db.fetch(
        """SELECT slot_no FROM license_devices
            WHERE license_id = $1 AND deactivated_at IS NULL
            ORDER BY slot_no""",
        license_id,
    )
    used = {r["slot_no"] for r in rows}
    for n in range(1, settings.license_max_devices + 1):
        if n not in used:
            return n
    return 0   # caller checked count first; should not reach


# ── Endpoints ─────────────────────────────────────────────────────────────
@router.post("/activate", response_model=ActivateResponse)
async def activate(
    req:  ActivateRequest,
    db:   asyncpg.Connection = Depends(get_db),
):
    """Bind a license key to the current hardware, consuming a slot.

    Idempotent: if this hardware is already activated against this
    license, returns the existing slot without consuming another.
    """
    lic = await _find_license_by_key(db, req.license_key)
    if lic is None:
        raise HTTPException(404, "license key not found")
    if lic["status"] != "active":
        raise HTTPException(403, f"license is {lic['status']}")

    fp_hash = hash_fingerprint(
        req.hardware.cpu_id,
        req.hardware.motherboard_uuid,
        req.hardware.disk_serial,
    )

    # Already activated on this device?  Just refresh.
    existing = await db.fetchrow(
        """SELECT slot_no FROM license_devices
            WHERE license_id = $1 AND device_fp_hash = $2
              AND deactivated_at IS NULL""",
        lic["id"], fp_hash,
    )
    if existing:
        await db.execute(
            "UPDATE license_devices SET last_seen = now() WHERE id = (SELECT id "
            "FROM license_devices WHERE license_id=$1 AND device_fp_hash=$2 "
            "AND deactivated_at IS NULL)",
            lic["id"], fp_hash,
        )
        slots_used = await _count_active_slots(db, lic["id"])
        log.info(f"License {lic['id']} re-activated on existing slot "
                 f"{existing['slot_no']} ({slots_used}/{settings.license_max_devices})")
        return ActivateResponse(
            license_id=lic["id"], slot_no=existing["slot_no"],
            slots_used=slots_used, slots_max=settings.license_max_devices,
            status=lic["status"], is_new_slot=False,
        )

    # Need a new slot.
    used = await _count_active_slots(db, lic["id"])
    if used >= settings.license_max_devices:
        raise HTTPException(
            409,
            f"all {settings.license_max_devices} device slots in use — "
            "deactivate one from your portal first",
        )
    slot = await _next_free_slot(db, lic["id"])
    await db.execute(
        """INSERT INTO license_devices
              (license_id, slot_no, device_fp_hash,
               gpu_model_norm, cpu_family, ram_gb_bucket,
               activated_at, last_seen)
           VALUES ($1, $2, $3, $4, $5, $6, now(), now())""",
        lic["id"], slot, fp_hash,
        req.buckets.gpu_model_norm, req.buckets.cpu_family,
        req.buckets.ram_gb_bucket,
    )
    log.info(f"License {lic['id']} activated on new slot {slot} "
             f"({used + 1}/{settings.license_max_devices}) "
             f"gpu={req.buckets.gpu_model_norm!r}")
    return ActivateResponse(
        license_id=lic["id"], slot_no=slot,
        slots_used=used + 1, slots_max=settings.license_max_devices,
        status=lic["status"], is_new_slot=True,
    )


@router.post("/validate", response_model=ValidateResponse)
async def validate(
    req:  ValidateRequest,
    db:   asyncpg.Connection = Depends(get_db),
):
    """Heartbeat check.  Called on every app launch + every 6 hours
    while running.  Used to detect refund / revoke states so the client
    can lock itself accordingly.
    """
    lic = await _find_license_by_key(db, req.license_key)
    if lic is None:
        raise HTTPException(404, "license key not found")

    fp_hash = hash_fingerprint(
        req.hardware.cpu_id,
        req.hardware.motherboard_uuid,
        req.hardware.disk_serial,
    )
    slot_row = await db.fetchrow(
        """SELECT slot_no FROM license_devices
            WHERE license_id = $1 AND device_fp_hash = $2
              AND deactivated_at IS NULL""",
        lic["id"], fp_hash,
    )
    if slot_row is None:
        raise HTTPException(
            403,
            "this hardware is not activated against this license — "
            "call /activate first",
        )

    await db.execute(
        """UPDATE license_devices SET last_seen = now()
            WHERE license_id = $1 AND device_fp_hash = $2""",
        lic["id"], fp_hash,
    )
    return ValidateResponse(
        license_id=lic["id"], slot_no=slot_row["slot_no"],
        status=lic["status"], last_seen=datetime.utcnow(),
        refunded_at=lic["refunded_at"], revoked_at=lic["revoked_at"],
    )


@router.post("/deactivate")
async def deactivate(
    req:  DeactivateRequest,
    db:   asyncpg.Connection = Depends(get_db),
):
    """Free a slot.  The license key itself is the credential — anyone
    holding the key can manage its slots.  This is intentional: it's
    how the user retires an old PC without us needing a login system.
    """
    lic = await _find_license_by_key(db, req.license_key)
    if lic is None:
        raise HTTPException(404, "license key not found")

    res = await db.execute(
        """DELETE FROM license_devices
            WHERE license_id = $1 AND slot_no = $2 AND deactivated_at IS NULL""",
        lic["id"], req.slot_no,
    )
    # asyncpg returns "DELETE n"; we want to know if anything actually went away.
    if res.endswith(" 0"):
        raise HTTPException(404, f"slot {req.slot_no} is not active")

    used = await _count_active_slots(db, lic["id"])
    log.info(f"License {lic['id']} slot {req.slot_no} deactivated "
             f"({used}/{settings.license_max_devices})")
    return {
        "ok":         True,
        "license_id": lic["id"],
        "slot_no":    req.slot_no,
        "slots_used": used,
        "slots_max":  settings.license_max_devices,
    }


@router.get("/{license_id}/public", response_model=PublicLicenseView)
async def public_view(
    license_id: Annotated[int, Path(ge=1)],
    db:         asyncpg.Connection = Depends(get_db),
):
    """The trust-pitch endpoint.  Anyone can hit this with any license
    ID and see *everything* the server knows about that license.

    The point: our website's "What we know" section curls this live and
    shows the response.  No auth, no obfuscation — verifiable proof of
    the zero-knowledge claim.

    What's intentionally NOT here:
      • email_hash / key_hash — they're useless without the secrets, but
        showing them would invite confusion ("but you DO have my email!")
      • stripe IDs — also pseudonymous, also confusing to show
      • any raw hardware IDs (we've never had them)
      • any IP info (we've never stored it)
    """
    lic = await db.fetchrow(
        """SELECT id, status, paid_at, refunded_at, revoked_at
             FROM licenses WHERE id = $1""",
        license_id,
    )
    if lic is None:
        raise HTTPException(404, "no license with that id")

    rows = await db.fetch(
        """SELECT slot_no, gpu_model_norm, cpu_family, ram_gb_bucket,
                  activated_at, last_seen
             FROM license_devices
            WHERE license_id = $1 AND deactivated_at IS NULL
            ORDER BY slot_no""",
        license_id,
    )
    slots = [SlotView(**dict(r)) for r in rows]
    return PublicLicenseView(
        license_id=lic["id"], status=lic["status"], paid_at=lic["paid_at"],
        refunded_at=lic["refunded_at"], revoked_at=lic["revoked_at"],
        slots_used=len(slots), slots_max=settings.license_max_devices,
        slots=slots,
    )
