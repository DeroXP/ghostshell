# Vispora-Cloud

FastAPI backend for Vispora, deployed on [Railway](https://railway.app) straight
from this directory.

The service is **optional** for the desktop app — Vispora works fully offline —
and adds licensing, trials, an anonymous device registry and, later, profile
sync and crowd-sourced overclock recommendations.  No accounts, no emails,
no PII: see [Security model](#security-model).

This directory is also the framework for everything that comes next: the
request pipeline, error shape, config, tests, CI and deployment are all in
place, so a new feature is a router, a schema append and a test.
[Adding a feature](#adding-a-feature) is the recipe.

---

## Status

| Area | Endpoints | Status |
| --- | --- | --- |
| Health | `GET /healthz` | live |
| Devices | `POST /v1/devices/register`, `POST …/{id}/heartbeat`, `GET`/`PUT …/{id}/settings` | live |
| Licenses | `POST /v1/licenses/activate` · `validate` · `deactivate`, `GET /v1/licenses/{id}/public` | live |
| Trials | `POST /v1/trials/start` · `status` | live (VPN check still a stub) |
| Profiles · playtime · OC · telemetry | `GET /v1/…/_status` | stubs, schema already present |

---

## Layout

```
cloud/
├── main.py              app factory, middleware stack, error shapes, lifespan + preflight
├── settings.py          every env var, validated at import
├── security.py          peppered HMACs, license keys, privacy log filter
├── middleware.py        request id, security headers, 500 envelope, rate limit, body limit
├── logconfig.py         JSON (Railway) / text (local) logging, request id on every line
├── db.py                asyncpg pool, fail-fast acquire, locked idempotent migrations
├── schema.sql           the whole schema — IF NOT EXISTS everywhere, applied at boot
├── routers/             one module per API area
├── tests/               pytest: unit, middleware and Postgres-backed route tests
├── admin_mint.py        CLI: mint / list / revoke licenses (until Stripe lands)
├── Dockerfile           what Railway builds: multi-stage, non-root, hash-verified deps
├── railway.json         Railway build + health-check configuration
├── docker-compose.yml   local Postgres (and, optionally, the API in its real image)
├── requirements.in      direct runtime deps   →  requirements.txt      (pinned + hashed, generated)
├── requirements-dev.in  tooling               →  requirements-dev.txt  (generated)
├── pyproject.toml       ruff + pytest configuration
└── Makefile             `make help`
```

---

## Local development

Prerequisites: Python 3.13, Docker (only for the local Postgres), GNU make.

```bash
cd cloud
make install            # virtualenv + hash-verified dependencies
make db-up              # Postgres 16 in Docker on 127.0.0.1:5432
cp .env.example .env    # DATABASE_URL already points at that Postgres
make dev                # http://localhost:8000/docs
```

On **Windows (PowerShell)** there is no `make`; the equivalents are:

```powershell
cd cloud
python -m venv .venv
.venv\Scripts\pip install --require-hashes -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m security          # = make peppers
.venv\Scripts\uvicorn main:app --reload   # = make dev   (DATABASE_URL from .env)
.venv\Scripts\pytest                      # = make test
.venv\Scripts\ruff check .                # = make lint
```

(`uvloop` has no Windows build and is skipped there automatically; the
server falls back to asyncio's default loop, which is fine for development.)

Without Docker, any Postgres will do — point `DATABASE_URL` in `.env` at it.
Peppers may stay empty locally; the service logs a warning and uses
deterministic dev values.  `make help` lists every target.

### Tests

```bash
make test       # unit + middleware tests, no database, ~1 s
make test-db    # everything, including the Postgres-backed route tests
make check      # lint + test — what a pull request must pass
```

CI (`.github/workflows/cloud-ci.yml`) runs exactly these against a Postgres
service container, plus a dependency audit and a build of the production image.

The database tests run the real lifespan: pool, `schema.sql` migration on an
empty database, and a second migration to prove idempotency.  They are skipped
unless `TEST_DATABASE_URL` is set, and never touch anything else.

### Dependencies

Direct dependencies live in `requirements.in` / `requirements-dev.in`.
`make compile` resolves them into `requirements*.txt` with every transitive
package pinned and hashed; Docker and CI install with `--require-hashes`, so a
package that does not match its recorded hash fails the build.  Dependabot
opens a grouped PR each week and regenerates the hashed files itself.

---

## Adding a feature

1. **Schema** — append to `schema.sql` with `CREATE TABLE IF NOT EXISTS` /
   `ALTER TABLE … ADD COLUMN IF NOT EXISTS`.  It runs at every boot under an
   advisory lock, so it must stay idempotent and additive.  Never drop or
   rename in place: add the new column, backfill, leave the old one.
2. **Settings** — a new knob is a field on `Settings` in `settings.py`
   (validated, with a safe default), a line in `.env.example`, and a row in
   `DEPLOY.md`'s variables table.
3. **Router** — a module in `routers/` with `APIRouter(prefix="/v1/…")`;
   request models are Pydantic with `max_length` / bounds on every field;
   database access is `db: asyncpg.Connection = Depends(get_db)`.  Register it
   in `create_app()`.  Raise `HTTPException` for expected failures — it comes
   out as `{"ok": false, "err": …, "detail": …}`.  Anything else becomes an
   opaque, logged 500.
4. **Identity** — anything that could identify a person is stored only as a
   peppered HMAC via `security.py` (a new category gets its own pepper), and is
   never logged raw.  The client IP is `request_client_ip(request)`, never
   `request.client.host`.
5. **Tests** — no-database tests use the `client` fixture; route tests use
   `db_client` and the `requires_db` marker.  Fresh UUIDs per test, no shared
   state.
6. `make check`, open the PR; the template carries the checklist.

---

## Request pipeline

Every request passes, outermost first:

| Layer | What it does |
| --- | --- |
| `RequestIDMiddleware` | honours a well-formed `X-Request-ID` or mints one; echoes it; stamps every log line; adds `X-Response-Time-Ms` |
| `SecurityHeadersMiddleware` | `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, `Cache-Control: no-store`, `Content-Security-Policy: default-src 'none'` (a renderable CSP only on the docs pages) |
| `UnhandledErrorMiddleware` | anything that escapes → `{"ok": false, "err": "internal_error", "request_id": …}`, traceback logged under that id, nothing about the exception sent |
| `RateLimitMiddleware` | token bucket per client (peppered IP hash), `429` + `Retry-After`, `X-RateLimit-*` on every response; `/` and `/healthz` exempt |
| `BodyLimitMiddleware` | `413` for bodies over `MAX_BODY_BYTES`, declared or streamed |
| `GZipMiddleware` | responses over 1 KiB |
| `CORSMiddleware` | only when `CORS_ORIGINS` is set |

All middleware is raw ASGI — no `BaseHTTPMiddleware`, which costs a task hop
per request and breaks streaming.

**Error shape.**  Every non-2xx body is JSON:
`{"ok": false, "err": "<machine-readable>", "detail": <message or validation list>}`.
`detail` is what FastAPI always sent, kept for the desktop client; `ok`/`err`
are additive.  Validation errors carry `loc`/`msg`/`type` only — never the
offending input.

---

## Security model

**Zero knowledge.**  Hardware fingerprints, emails, license keys and IP
addresses are HMAC-SHA256'd with four independent server-side peppers before
they touch the database; raw values exist only for the duration of a request.
`GET /v1/licenses/{id}/public` returns the *complete* server-side record for a
license so the claim is verifiable.  Client IPs are scrubbed from every log
line before it leaves the process.

**Boot refuses an unsafe production.**  In production (`APP_ENV`, or
Railway's environment name) the service will not start without all four
peppers and a real `DATABASE_URL`; Railway keeps the previous deploy serving.
The OpenAPI UI is off in production unless `EXPOSE_DOCS=true`.

**Transport.**  Railway terminates TLS at its edge; the app sends HSTS.  The
real client address is the X-Forwarded-For entry appended by the trusted edge
(`TRUSTED_PROXY_HOPS`, default 1 on Railway), never the leftmost one a client
can forge — so the rate limiter cannot be dodged with a spoofed header.

**Abuse limits.**  Per-client token bucket, request-body cap, fail-fast
database acquire (503 instead of a pile-up), bounded health probe.

**Supply chain.**  Dependencies pinned and hash-verified; base image pinned by
digest; GitHub Actions pinned by commit SHA with a read-only token; weekly
`pip-audit`, CodeQL and Dependabot.  The runtime image has no compiler and runs
as UID 10001.

**Not covered yet** (tracked in [Roadmap](#roadmap)): the device endpoints
treat the device UUID as a bearer credential (anyone who knows a UUID can read
or change that device's opt-in flags); the VPN/datacenter check is a stub; the
Stripe webhook is not implemented; the rate limiter is per process.

---

## Performance notes

- uvicorn with uvloop + httptools; `WEB_CONCURRENCY` sets worker processes.
- Raw ASGI middleware; FastAPI serialises typed responses straight through
  pydantic-core (no `jsonable_encoder` pass); `orjson` on the hot error paths.
- asyncpg pool with prepared-statement caching, `jit=off` for these small OLTP
  queries, 5 s acquire timeout, 30 s statement timeout.
- gzip above 1 KiB; `Cache-Control: no-store` because every response is
  per-client state.
- `X-Response-Time-Ms` on every response is the cheapest latency telemetry;
  JSON logs carry `request_id` for correlation.
- Image: `python:3.13-slim`, multi-stage, no bytecode writes — cold start is a
  couple of seconds, bounded by the Postgres handshake.

---

## Operations

- **Logs** are JSON on Railway.  Filter on `request_id` — the same value the
  client saw in `X-Request-ID`.
- **Admin CLI**: `python admin_mint.py list|mint|show|revoke|refund` needs a
  `DATABASE_URL` it can reach — the Postgres service's *public* URL from a
  laptop, or the private one from inside the service (`railway ssh`).
- **Rotating a pepper** invalidates every hash made with it: fingerprint or
  license-key peppers → every trial and activation; IP pepper → only the
  abuse counters.  Keep them in a password manager alongside the DB backups.
- **Scaling out** (replicas or `WEB_CONCURRENCY` > 1) multiplies the
  per-client rate budget; move the limiter to a shared store first.

---

## Roadmap

Open items, roughly in order:

1. Stripe webhook (`payments` table and idempotency design are already in the schema).
2. VPN / datacenter check provider wiring in `routers/trials.py`.
3. Per-device secret for the `/v1/devices/*` endpoints instead of the bare UUID.
4. Shared rate-limit store (Redis) before running more than one replica.
5. Nightly jobs: refresh `oc_recommendations`, prune devices past `DEVICE_TTL_DAYS`.
6. Profiles / playtime / OC / telemetry routers on the existing tables.
