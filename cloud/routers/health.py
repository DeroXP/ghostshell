"""Health + version endpoint.  Railway hits /healthz to know the service is up."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
import asyncpg

from db import get_db
from settings import settings

log = logging.getLogger("ghostshell-cloud.health")

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(db: asyncpg.Connection = Depends(get_db)) -> dict:
    """Ping Postgres and return service info.

    Railway requires this to return 200 within 30 s of deploy.
    """
    db_ok = False
    try:
        v = await db.fetchval("SELECT 1")
        db_ok = (v == 1)
    except Exception as e:
        log.warning(f"DB health check failed: {e}")

    return {
        "ok":      True,
        "service": "ghostshell-cloud",
        "version": settings.version,
        "db":      "ok" if db_ok else "error",
    }
