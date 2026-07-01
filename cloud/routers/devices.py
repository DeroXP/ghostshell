"""Phase A — device registration, heartbeat, and settings sync.

Anonymous identity model:
  • The client generates a UUID locally on first run (`core/device_id.py`).
  • That UUID is the only credential — we don't issue tokens, don't track
    real users, don't link devices to emails.
  • Every cloud request carries the UUID in the `X-Device-ID` header.
  • The server creates a row on first sight and refreshes `last_seen` on
    each request.

What we DO store:
  • Hardware fingerprint (GPU model, CPU family, RAM size) — for the
    "users with similar hardware" recommendation aggregations.
  • Per-device settings (telemetry/sync/news opt-in flags, update channel).

What we don't store:
  • Anything that ties back to a real-world identity.  The IP address is
    hashed (SHA256) and used only for abuse rate-limiting, never logged.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from db import get_db

log = logging.getLogger("ghostshell-cloud.devices")

router = APIRouter(prefix="/v1/devices", tags=["devices"])


# ── Schemas ───────────────────────────────────────────────────────────────
class HardwareFingerprint(BaseModel):
    gpu_model:   str | None = None
    cpu_family:  str | None = None
    cpu_model:   str | None = None
    ram_gb:      int | None = None
    os_build:    str | None = None
    app_version: str | None = None


class DeviceSettings(BaseModel):
    telemetry_opt_in: bool = False
    sync_opt_in:      bool = False
    news_opt_in:      bool = True
    update_channel:   str  = "stable"


class RegisterRequest(BaseModel):
    device_id:   UUID
    hardware:    HardwareFingerprint = Field(default_factory=HardwareFingerprint)


class RegisterResponse(BaseModel):
    ok:        bool   = True
    device_id: UUID
    is_new:    bool
    settings:  DeviceSettings


class HeartbeatRequest(BaseModel):
    hardware:    HardwareFingerprint | None = None


# ── Helpers ───────────────────────────────────────────────────────────────
def _norm_gpu_model(name: str | None) -> str | None:
    """'NVIDIA GeForce RTX 5070 Ti' → 'rtx 5070 ti'.  Used as the join key
    when looking up "similar hardware" recommendations."""
    if not name:
        return None
    n = name.lower().strip()
    for prefix in ("nvidia ", "amd ", "intel ", "geforce "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n.strip()


def _ram_bucket(gb: int | None) -> str | None:
    if gb is None:
        return None
    if gb < 16:
        return "<16"
    if gb < 32:
        return "16"
    if gb < 64:
        return "32-64"
    return "64+"


def _ip_hash(req: Request) -> str:
    """Hash the client IP for rate-limiting.  Never store the raw IP."""
    ip = (req.client.host if req.client else "0.0.0.0") or "0.0.0.0"
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


async def _device_exists(db: asyncpg.Connection, device_id: UUID) -> bool:
    return await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM devices WHERE id = $1)", device_id
    )


async def _get_settings(db: asyncpg.Connection, device_id: UUID) -> DeviceSettings:
    row = await db.fetchrow(
        """SELECT telemetry_opt_in, sync_opt_in, news_opt_in, update_channel
             FROM device_settings WHERE device_id = $1""",
        device_id,
    )
    if row is None:
        return DeviceSettings()
    return DeviceSettings(**dict(row))


# ── Endpoints ─────────────────────────────────────────────────────────────
@router.post("/register", response_model=RegisterResponse)
async def register_device(
    req: RegisterRequest,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """Idempotent registration.  Called on first launch and any time the
    client wants to refresh its hardware fingerprint after a hardware
    change (e.g. user upgraded their GPU).

    Returns the current settings so the client can sync its UI on launch.
    """
    hw = req.hardware
    is_new = not await _device_exists(db, req.device_id)
    ip_h   = _ip_hash(request)

    # Both writes must land together: if the process dies/times out between
    # them, `is_new` would come back False on retry (the devices row already
    # exists) and the device_settings insert below would never be attempted
    # again, permanently missing a settings row for that device. Wrapping in
    # a transaction means either both writes land or neither does, so a
    # retry after a crash correctly re-attempts both.
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO devices
                (id, gpu_model, gpu_model_norm, cpu_family, cpu_model, ram_gb,
                 ram_gb_bucket, os_build, app_version, last_ip_hash, last_seen)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, NOW())
            ON CONFLICT (id) DO UPDATE SET
                gpu_model      = COALESCE(EXCLUDED.gpu_model,      devices.gpu_model),
                gpu_model_norm = COALESCE(EXCLUDED.gpu_model_norm, devices.gpu_model_norm),
                cpu_family     = COALESCE(EXCLUDED.cpu_family,     devices.cpu_family),
                cpu_model      = COALESCE(EXCLUDED.cpu_model,      devices.cpu_model),
                ram_gb         = COALESCE(EXCLUDED.ram_gb,         devices.ram_gb),
                ram_gb_bucket  = COALESCE(EXCLUDED.ram_gb_bucket,  devices.ram_gb_bucket),
                os_build       = COALESCE(EXCLUDED.os_build,       devices.os_build),
                app_version    = COALESCE(EXCLUDED.app_version,    devices.app_version),
                last_ip_hash   = EXCLUDED.last_ip_hash,
                last_seen      = NOW()
            """,
            req.device_id, hw.gpu_model, _norm_gpu_model(hw.gpu_model),
            hw.cpu_family, hw.cpu_model, hw.ram_gb,
            _ram_bucket(hw.ram_gb), hw.os_build, hw.app_version, ip_h,
        )
        if is_new:
            await db.execute(
                "INSERT INTO device_settings (device_id) VALUES ($1) ON CONFLICT DO NOTHING",
                req.device_id,
            )

    settings_row = await _get_settings(db, req.device_id)
    log.info(f"Device {'registered' if is_new else 'refreshed'}: {req.device_id} "
             f"gpu={hw.gpu_model!r} cpu={hw.cpu_family!r} ram={hw.ram_gb}gb")
    return RegisterResponse(device_id=req.device_id, is_new=is_new, settings=settings_row)


@router.post("/{device_id}/heartbeat")
async def heartbeat(
    device_id: UUID,
    req: HeartbeatRequest,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """Refresh `last_seen` and optionally update hardware fingerprint.

    The client calls this every ~6 hours while running.  Server side it's
    a no-op if the device record is current — useful for both keeping the
    device alive (don't get pruned by the TTL job) and for the recommender
    to know who's "currently active".
    """
    if not await _device_exists(db, device_id):
        raise HTTPException(404, "Device not registered — call /register first")

    if req.hardware:
        hw = req.hardware
        await db.execute(
            """
            UPDATE devices SET
                gpu_model      = COALESCE($2, gpu_model),
                gpu_model_norm = COALESCE($3, gpu_model_norm),
                cpu_family     = COALESCE($4, cpu_family),
                cpu_model      = COALESCE($5, cpu_model),
                ram_gb         = COALESCE($6, ram_gb),
                ram_gb_bucket  = COALESCE($7, ram_gb_bucket),
                os_build       = COALESCE($8, os_build),
                app_version    = COALESCE($9, app_version),
                last_seen      = NOW()
            WHERE id = $1
            """,
            device_id, hw.gpu_model, _norm_gpu_model(hw.gpu_model),
            hw.cpu_family, hw.cpu_model, hw.ram_gb,
            _ram_bucket(hw.ram_gb), hw.os_build, hw.app_version,
        )
    else:
        await db.execute("UPDATE devices SET last_seen = NOW() WHERE id = $1", device_id)
    return {"ok": True}


@router.get("/{device_id}/settings", response_model=DeviceSettings)
async def get_device_settings(
    device_id: UUID,
    db: asyncpg.Connection = Depends(get_db),
):
    if not await _device_exists(db, device_id):
        raise HTTPException(404, "Device not registered")
    return await _get_settings(db, device_id)


@router.put("/{device_id}/settings", response_model=DeviceSettings)
async def put_device_settings(
    device_id: UUID,
    body: DeviceSettings,
    db: asyncpg.Connection = Depends(get_db),
):
    if not await _device_exists(db, device_id):
        raise HTTPException(404, "Device not registered — call /register first")
    if body.update_channel not in ("stable", "insiders"):
        raise HTTPException(400, "update_channel must be 'stable' or 'insiders'")
    await db.execute(
        """
        INSERT INTO device_settings
            (device_id, telemetry_opt_in, sync_opt_in, news_opt_in, update_channel, updated_at)
        VALUES ($1,$2,$3,$4,$5, NOW())
        ON CONFLICT (device_id) DO UPDATE SET
            telemetry_opt_in = EXCLUDED.telemetry_opt_in,
            sync_opt_in      = EXCLUDED.sync_opt_in,
            news_opt_in      = EXCLUDED.news_opt_in,
            update_channel   = EXCLUDED.update_channel,
            updated_at       = NOW()
        """,
        device_id, body.telemetry_opt_in, body.sync_opt_in,
        body.news_opt_in, body.update_channel,
    )
    return body
