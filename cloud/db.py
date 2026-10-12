"""Postgres connection pool + idempotent migration runner.

The pool is created on FastAPI startup (see main.py lifespan) and torn down
on shutdown.  Routers grab connections via `Depends(get_db)`.

Tuning notes (all overridable through settings):
  • `min_size` 1 / `max_size` 10 — Railway's Postgres plans cap connections
    fairly low; ten per replica leaves room for a second replica and a
    `psql` session.
  • Acquiring a connection waits at most `db_acquire_timeout_s`; past that
    the request fails fast with 503 instead of queueing behind a stall.
  • `jit=off` per session — the queries here are small OLTP lookups, for
    which Postgres' JIT only adds compile latency.
  • Migrations take a transaction-scoped advisory lock, so several workers
    (WEB_CONCURRENCY > 1) or an overlapping redeploy cannot race each other
    on `CREATE … IF NOT EXISTS`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import HTTPException

from settings import settings

log = logging.getLogger("vispora-cloud.db")

# Any fixed 64-bit constant; only has to be the same in every process.
_MIGRATION_LOCK_KEY = 0x5649_5350_4F52_4131  # "VISPORA1"

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Create the global pool.  Idempotent — a second call returns the same pool."""
    global _pool
    if _pool is not None:
        return _pool
    log.info("Creating Postgres pool (min=%d max=%d)", settings.db_pool_min, settings.db_pool_max)
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        max_inactive_connection_lifetime=300.0,
        command_timeout=settings.db_command_timeout_s,
        timeout=settings.db_connect_timeout_s,
        server_settings={
            "application_name": f"vispora-cloud/{settings.version}",
            "jit": "off",
        },
        # TLS: asyncpg follows the URL's sslmode.  Railway's private-network
        # URL (postgres.railway.internal) is plaintext inside the project
        # network; its public URL carries sslmode=require.
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
    """Yields one connection per request.  Used as `Depends(get_db)`.

    Fails fast with 503 when the pool is saturated, so a database stall
    produces a crisp error (and a Retry-After) instead of a pile of hung
    requests.
    """
    pool = get_pool()
    try:
        c = await pool.acquire(timeout=settings.db_acquire_timeout_s)
    except TimeoutError as e:
        log.warning("Pool acquire timed out after %.1fs", settings.db_acquire_timeout_s)
        raise HTTPException(
            503, "database busy, retry shortly", headers={"Retry-After": "2"}
        ) from e
    try:
        yield c
    finally:
        await pool.release(c)


# ── Migration runner ──────────────────────────────────────────────────────
async def run_migrations() -> None:
    """Apply schema.sql idempotently.  Called once on startup.

    The schema is written entirely with `IF NOT EXISTS` guards so running
    it on an already-migrated database is a no-op.  When adding columns in
    later phases, append `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
    """
    schema_path = Path(__file__).parent / "schema.sql"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.sql not found at {schema_path}")
    sql = schema_path.read_text(encoding="utf-8")
    log.info("Running migrations from %s (%d bytes)", schema_path.name, len(sql))
    async with conn() as c, c.transaction():
        # Serialise concurrent boots: whoever gets the lock runs the script,
        # the others wait and then find everything already exists.
        await c.execute("SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK_KEY)
        # asyncpg runs the whole multi-statement string in one round trip.
        await c.execute(sql)
    log.info("Migrations applied successfully")
