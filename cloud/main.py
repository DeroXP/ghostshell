"""Vispora-Cloud — FastAPI service entry point.

Endpoints:
  /healthz                            Railway health check (503 until Postgres answers)
  /v1/devices/register                first-launch registration
  /v1/devices/{id}/heartbeat          refresh last-seen
  /v1/devices/{id}/settings           GET + PUT user settings
  /v1/licenses/activate               bind a key to current hardware
  /v1/licenses/validate               heartbeat the license
  /v1/licenses/deactivate             free a slot
  /v1/licenses/{id}/public            live trust-pitch endpoint
  /v1/trials/start                    start a 7-day trial
  /v1/trials/status                   check trial state
  /v1/{profiles,oc,telemetry,playtime}/_status   stubs for later phases

Request pipeline (outermost first):
  RequestID → SecurityHeaders → UnhandledError → RateLimit → BodyLimit → GZip → [CORS] → routers

Boot sequence (lifespan):
  preflight() → refuse unsafe production config → Postgres pool → migrations.

Local dev:
    cp .env.example .env          # set DATABASE_URL
    make install && make dev      # http://localhost:8000/docs

Railway deploy: see DEPLOY.md.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from db import close_pool, init_pool, run_migrations
from logconfig import configure_logging
from middleware import (
    BodyLimitMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    UnhandledErrorMiddleware,
)
from routers import devices, health, licenses, oc, playtime, profiles, telemetry, trials
from security import install_privacy_filter, using_insecure_dev_peppers
from settings import Settings, settings

# ─── Logging ─────────────────────────────────────────────────────────────
# Configured at import so even boot-time lines are formatted and scrubbed.
configure_logging(settings.log_level, json_logs=settings.json_logs)
install_privacy_filter()
log = logging.getLogger("vispora-cloud")

_DEFAULT_DATABASE_URL: str = Settings.model_fields["database_url"].default
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
_RATE_LIMIT_EXEMPT = frozenset({"/", "/healthz"})


# ─── Preflight ───────────────────────────────────────────────────────────
def preflight(s: Settings, *, insecure_peppers: bool) -> list[str]:
    """Reasons this configuration must not serve traffic (empty = fine).

    Only production is strict.  Everything here is a mistake that would
    otherwise surface as silently weak hashes or a half-working deploy.
    """
    problems: list[str] = []
    if not s.is_production:
        return problems
    if insecure_peppers:
        problems.append(
            "FINGERPRINT_PEPPER / EMAIL_PEPPER / LICENSE_KEY_PEPPER / IP_PEPPER are unset or "
            "invalid — run `python -m security` and set all four in Railway → Variables"
        )
    if s.database_url == _DEFAULT_DATABASE_URL:
        problems.append(
            "DATABASE_URL is not set — reference ${{ Postgres.DATABASE_URL }} in Railway → Variables"
        )
    return problems


# ─── Lifespan: preflight + pool + migrations ─────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Booting Vispora-Cloud v%s (env=%s)", settings.version, settings.app_env)

    problems = preflight(settings, insecure_peppers=using_insecure_dev_peppers())
    if problems:
        for p in problems:
            log.critical("Refusing to start: %s", p)
        # Raising here fails the deploy; Railway keeps the previous one serving.
        raise RuntimeError("unsafe production configuration — see log")

    if using_insecure_dev_peppers():
        log.warning(
            "Insecure dev peppers active — fine locally, never in production "
            "(set FINGERPRINT_PEPPER, EMAIL_PEPPER, LICENSE_KEY_PEPPER, IP_PEPPER)"
        )
    if settings.is_production and settings.docs_enabled:
        log.warning("OpenAPI docs are exposed in production (EXPOSE_DOCS=true)")

    try:
        await init_pool()
        await run_migrations()
    except Exception:
        log.exception("Startup failed")
        raise
    log.info("Ready to serve")
    yield
    log.info("Shutting down")
    await close_pool()


# ─── App factory ─────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    """Build the application.  Reads `settings` at call time, so tests can
    tweak a setting and build a fresh app around it."""
    docs = settings.docs_enabled
    app = FastAPI(
        title="Vispora",
        version=settings.version,
        description=(
            "Backend service for Vispora.  Zero-knowledge license enforcement, "
            "anonymous device identity, opt-in telemetry."
        ),
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    # ── Routers ──
    app.include_router(health.router)
    app.include_router(devices.router)
    app.include_router(licenses.router)
    app.include_router(trials.router)
    app.include_router(profiles.router)
    app.include_router(oc.router)
    app.include_router(telemetry.router)
    app.include_router(playtime.router)

    # ── Middleware ──
    # add_middleware() wraps outside-in: the LAST one added runs FIRST.
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Content-Type", "X-Device-ID", "X-Request-ID"],
            max_age=600,
        )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.rate_limit_per_min,
        burst=settings.rate_limit_burst,
        trusted_proxy_hops=settings.proxy_hops,
        exempt_paths=_RATE_LIMIT_EXEMPT,
    )
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware, hsts_max_age=settings.hsts_max_age, docs_paths=_DOCS_PATHS
    )
    app.add_middleware(RequestIDMiddleware)

    # ── Error handlers: every error is JSON with the same shape ──
    # {"ok": false, "err": <machine-readable>, "detail": <as before>}
    # The desktop client treats the body as an opaque string, so `detail`
    # stays for compatibility and `ok`/`err` are additive.  Unhandled
    # exceptions (500) are rendered by UnhandledErrorMiddleware, not here.
    @app.exception_handler(StarletteHTTPException)
    async def _err_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        err = getattr(exc, "code", None) or (detail if isinstance(detail, str) else "error")
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "err": err, "detail": detail},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _err_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only location / message / type — never echo the offending input back.
        detail = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"ok": False, "err": "validation_error", "detail": detail},
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str | None]:
        """A friendly landing for anyone opening the Railway URL in a browser."""
        return {
            "service": "vispora-cloud",
            "version": settings.version,
            "health": "/healthz",
            "docs": "/docs" if docs else None,
        }

    return app


app = create_app()
