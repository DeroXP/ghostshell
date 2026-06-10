"""Phase B — 7-day trial state, keyed by hardware fingerprint.

The trial is the user's only path to evaluating GhostShell before paying
$20.  It's also the prime piracy vector, so the rules are strict but
honest:

  • One trial per physical machine, ever.  Reinstalling Windows or
    wiping AppData doesn't reset it — the server remembers the
    fingerprint hash forever.
  • VPN / datacenter IPs are blocked at trial start.  Paid users can
    use VPNs freely; the block exists to stop trial-farming via cheap
    VPS scripts.
  • VM detection happens on the client, but the server records the
    flag so we can spot abuse patterns later.

If a trial fingerprint later converts to a paid license, we remember
that too (`trials.converted_to`) so the recommender can correlate
"users who tried then bought" stats without ever seeing identities.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from db import get_db
from routers.licenses import HardwareBuckets, HardwareInputs
from security import hash_fingerprint, hash_ip
from settings import settings

log = logging.getLogger("ghostshell-cloud.trials")

router = APIRouter(prefix="/v1/trials", tags=["trials"])


# ── Schemas ───────────────────────────────────────────────────────────────
class TrialStartRequest(BaseModel):
    hardware:    HardwareInputs
    buckets:     HardwareBuckets = Field(default_factory=HardwareBuckets)
    app_version: str | None = Field(None, max_length=16)
    # Client-side VM detection result.  The server may still reject the
    # trial if its own checks disagree.
    vm_detected: bool = False


class TrialStatusRequest(BaseModel):
    hardware: HardwareInputs


class TrialResponse(BaseModel):
    ok:               bool
    state:            str               # "active" | "expired" | "blocked" | "converted"
    started_at:       datetime | None = None
    expires_at:       datetime | None = None
    seconds_left:     int     | None = None
    converted_to_id:  int     | None = None
    block_reason:     str     | None = None


# ── VPN check (stub) ─────────────────────────────────────────────────────
async def _is_vpn_or_datacenter(client_ip: str) -> tuple[bool, str]:
    """Returns (is_blocked, reason).  Wires up to IPQualityScore /
    proxycheck.io in Phase B.3.  Until then, returns (False, "") so dev
    works locally — production will set VPN_CHECK_PROVIDER and the real
    check engages.

    The IP itself is never persisted: it's checked, used for the
    decision, and discarded.  Only the verdict ends up in the trial row.
    """
    if not settings.vpn_check_provider:
        return False, ""
    # TODO Phase B.3: wire real provider.
    log.debug(f"VPN check provider={settings.vpn_check_provider} not yet wired")
    return False, ""


# ── Endpoints ─────────────────────────────────────────────────────────────
@router.post("/start", response_model=TrialResponse)
async def start_trial(
    req:     TrialStartRequest,
    request: Request,
    db:      asyncpg.Connection = Depends(get_db),
):
    """Start a 7-day trial bound to this hardware fingerprint.

    Idempotent return semantics:
      • First call from a never-seen fingerprint → state="active"
      • Subsequent calls within the trial window → state="active" with
        the original `started_at` and `expires_at`
      • After expiry without purchase → state="expired"
      • If the fingerprint converted to a paid license → state="converted"
      • If anti-abuse triggers (VPN/VM) → state="blocked"
    """
    fp_hash = hash_fingerprint(
        req.hardware.cpu_id,
        req.hardware.motherboard_uuid,
        req.hardware.disk_serial,
    )

    # Existing trial?
    existing = await db.fetchrow(
        """SELECT started_at, expires_at, converted_to
             FROM trials WHERE device_fp_hash = $1""",
        fp_hash,
    )
    if existing:
        if existing["converted_to"]:
            return TrialResponse(
                ok=True, state="converted",
                started_at=existing["started_at"],
                expires_at=existing["expires_at"],
                converted_to_id=existing["converted_to"],
            )
        now = datetime.now(timezone.utc)
        if now < existing["expires_at"]:
            seconds_left = int((existing["expires_at"] - now).total_seconds())
            return TrialResponse(
                ok=True, state="active",
                started_at=existing["started_at"],
                expires_at=existing["expires_at"],
                seconds_left=seconds_left,
            )
        return TrialResponse(
            ok=False, state="expired",
            started_at=existing["started_at"],
            expires_at=existing["expires_at"],
        )

    # First-time fingerprint.  Run anti-abuse checks before starting.
    client_ip = (request.client.host if request.client else "0.0.0.0") or "0.0.0.0"
    is_blocked, vpn_reason = await _is_vpn_or_datacenter(client_ip)

    block_reason: str | None = None
    if is_blocked:
        block_reason = f"vpn_or_datacenter: {vpn_reason}"
    elif req.vm_detected:
        block_reason = "virtual_machine"

    if block_reason:
        log.info(f"Trial blocked: {block_reason} "
                 f"(buckets gpu={req.buckets.gpu_model_norm!r})")
        return TrialResponse(ok=False, state="blocked", block_reason=block_reason)

    # Mint the trial.
    started_at = datetime.now(timezone.utc)
    expires_at = started_at + timedelta(days=settings.trial_duration_days)
    ip_hash    = hash_ip(client_ip)

    await db.execute(
        """INSERT INTO trials
              (device_fp_hash, started_at, expires_at,
               gpu_model_norm, cpu_family, ram_gb_bucket, app_version,
               ip_hash, vpn_flag, vm_flag)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
        fp_hash, started_at, expires_at,
        req.buckets.gpu_model_norm, req.buckets.cpu_family,
        req.buckets.ram_gb_bucket, req.app_version,
        ip_hash, is_blocked, req.vm_detected,
    )
    log.info(f"Trial started ({settings.trial_duration_days} days, "
             f"gpu={req.buckets.gpu_model_norm!r})")
    return TrialResponse(
        ok=True, state="active",
        started_at=started_at, expires_at=expires_at,
        seconds_left=settings.trial_duration_days * 86400,
    )


@router.post("/status", response_model=TrialResponse)
async def trial_status(
    req: TrialStatusRequest,
    db:  asyncpg.Connection = Depends(get_db),
):
    """Look up the trial state for the current hardware without starting
    one.  Called from the app on every launch to decide whether to show
    the buy splash, the trial countdown, or full functionality.
    """
    fp_hash = hash_fingerprint(
        req.hardware.cpu_id,
        req.hardware.motherboard_uuid,
        req.hardware.disk_serial,
    )
    row = await db.fetchrow(
        """SELECT started_at, expires_at, converted_to
             FROM trials WHERE device_fp_hash = $1""",
        fp_hash,
    )
    if row is None:
        # Never trialed.  Caller should offer the "Start free trial" CTA.
        return TrialResponse(ok=True, state="never")

    if row["converted_to"]:
        return TrialResponse(
            ok=True, state="converted",
            started_at=row["started_at"], expires_at=row["expires_at"],
            converted_to_id=row["converted_to"],
        )
    now = datetime.now(timezone.utc)
    if now < row["expires_at"]:
        seconds_left = int((row["expires_at"] - now).total_seconds())
        return TrialResponse(
            ok=True, state="active",
            started_at=row["started_at"], expires_at=row["expires_at"],
            seconds_left=seconds_left,
        )
    return TrialResponse(
        ok=False, state="expired",
        started_at=row["started_at"], expires_at=row["expires_at"],
    )
