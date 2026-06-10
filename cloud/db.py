"""Postgres connection pool + idempotent migration runner.

The pool is created on FastAPI startup (see main.py lifespan) and torn
down on shutdown.  Routers grab connections via `Depends(get_db)`.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import asyncpg

from settings import settings

log = logging.getLogger("ghostshell-cloud.db")

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Create the global pool.  Idempotent — second call returns the same pool."""
    global _pool
    if _pool is not None:
        return _pool
    log.info("Creating Postgres pool...")
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=1,
        max_size=10,
        command_timeout=30,
        # Railway's Postgres is on a managed network — use SSL.  asyncpg
        # picks this up from the URL's `?sslmode=require` (Railway adds it).
    )
    log.info("Postgres pool ready")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Postgres pool not initialised — did the lifespan run?")
    return _pool


@asynccontextmanager
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """Convenience: `async with conn() as c: await c.execute(...)`."""
    async with get_pool().acquire() as c:
        yield c


# ── FastAPI dependency ────────────────────────────────────────────────────
async def get_db() -> AsyncIterator[asyncpg.Connection]:
    """Yields a single connection per request.  Used as `Depends(get_db)`."""
    async with get_pool().acquire() as c:
        yield c


# ── Migration runner ──────────────────────────────────────────────────────
async def run_migrations() -> None:
    """Apply schema.sql idempotently.  Called once on startup.

    The schema is written entirely with `IF NOT EXISTS` guards so running
    it on an already-migrated database is a no-op.  When we add columns
    in later phases we'll append to schema.sql with `ALTER TABLE ... ADD
    COLUMN IF NOT EXISTS`.
    """
    schema_path = Path(__file__).parent / "schema.sql"
    if not schema_path.exists():
        log.error(f"schema.sql not found at {schema_path}")
        return
    sql = schema_path.read_text(encoding="utf-8")
    log.info(f"Running migrations from {schema_path.name} ({len(sql)} bytes)")
    async with conn() as c:
        # asyncpg's `execute` runs the entire string as one batch.  Postgres
        # handles multiple statements separated by semicolons fine.
        await c.execute(sql)
    log.info("Migrations applied successfully")
