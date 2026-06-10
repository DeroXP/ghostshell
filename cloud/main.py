"""GhostShell-Cloud — FastAPI service entry point.

Phase A — anonymous device identity layer:
  • /healthz                          Railway health check
  • /v1/devices/register              first-launch registration
  • /v1/devices/{id}/heartbeat        refresh last-seen
  • /v1/devices/{id}/settings         GET + PUT user settings

Phase B — commercial licensing layer (zero-knowledge):
  • /v1/licenses/activate             bind a key to current hardware
  • /v1/licenses/validate             heartbeat the license
  • /v1/licenses/deactivate           free a slot
  • /v1/licenses/{id}/public          live trust-pitch endpoint
  • /v1/trials/start                  start a 7-day trial
  • /v1/trials/status                 check trial state

Future phases add /v1/profiles, /v1/oc, /v1/telemetry, /v1/playtime,
/v1/stripe/webhook, /v1/releases endpoints.

Local dev:
    cp .env.example .env       # set DATABASE_URL
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Then:
    curl http://localhost:8000/healthz
    open http://localhost:8000/docs

Railway deploy: see DEPLOY.md.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from db import close_pool, init_pool, run_migrations
from routers import (
    devices, health, licenses, oc, playtime, profiles,
    telemetry, trials,
)
from security import install_privacy_filter, using_insecure_dev_peppers
from settings import settings


# ─── Logging setup ───────────────────────────────────────────────────────
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ghostshell-cloud")

# Scrub client IPs out of every log line before they hit stdout / Railway.
# Installed once at import time so even boot-time logs go through the filter.
install_privacy_filter()


# ─── Lifespan: pool + migrations ─────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Booting GhostShell-Cloud v{settings.version}")
    if using_insecure_dev_peppers():
        log.warning("Insecure dev peppers active — set FINGERPRINT_PEPPER, "
                    "EMAIL_PEPPER, LICENSE_KEY_PEPPER, IP_PEPPER before deploy")
    try:
        await init_pool()
        await run_migrations()
    except Exception as e:
        log.error(f"Startup failed: {e}")
        # Re-raise so Railway / uvicorn marks the deploy as failed
        raise
    log.info("Ready to serve")
    yield
    log.info("Shutting down")
    await close_pool()


# ─── App ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="GhostShell-Cloud",
    version=settings.version,
    description=(
        "Backend service for GhostShell v3.  Zero-knowledge license "
        "enforcement, anonymous device identity, opt-in telemetry."
    ),
    lifespan=lifespan,
)

# ─── Routers ─────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(devices.router)
app.include_router(licenses.router)
app.include_router(trials.router)
app.include_router(profiles.router)
app.include_router(oc.router)
app.include_router(telemetry.router)
app.include_router(playtime.router)


# ─── Global error handler ────────────────────────────────────────────────
# Mirrors GhostShell client's policy: every error returns clean JSON, never
# an HTML stack trace.  Makes the client-side reporter happy.
@app.exception_handler(Exception)
async def _err_unhandled(request: Request, exc: Exception):
    log.exception(f"Unhandled error in {request.method} {request.url.path}")
    return JSONResponse(
        status_code=500,
        content={
            "ok":   False,
            "err":  f"{type(exc).__name__}: {exc}",
            "path": str(request.url.path),
        },
    )


# Quick "you've reached the API root" page so a curious user opening the
# Railway URL sees something friendly.
@app.get("/")
async def root() -> dict:
    return {
        "service": "ghostshell-cloud",
        "version": settings.version,
        "docs":    "/docs",
        "health":  "/healthz",
    }
