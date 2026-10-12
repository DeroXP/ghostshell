"""Health + version endpoint.  Railway hits /healthz to decide whether a new
deploy may receive traffic."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db import get_pool
from settings import settings

log = logging.getLogger("vispora-cloud.health")

router = APIRouter(tags=["health"])

_DB_PING_TIMEOUT_S = 2.0


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Ping Postgres and report service info.

    Returns 200 only when the database answers.  A 503 makes Railway keep the
    previous deploy serving instead of switching traffic to an instance that
    cannot do anything useful; the check is bounded so a stalled database
    cannot hang the probe.
    """
    db_ok = False
    try:
        async with get_pool().acquire(timeout=_DB_PING_TIMEOUT_S) as c:
            db_ok = (await c.fetchval("SELECT 1", timeout=_DB_PING_TIMEOUT_S)) == 1
    except Exception as e:
        log.warning("DB health check failed: %s", type(e).__name__)

    body = {
        "ok": db_ok,
        "service": "vispora-cloud",
        "version": settings.version,
        "env": settings.app_env,
        "db": "ok" if db_ok else "error",
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)
