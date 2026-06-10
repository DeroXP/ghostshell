# GhostShell-Cloud

FastAPI backend for GhostShell v3.

This service is **completely optional**.  GhostShell works fully offline; the
cloud only adds:

- Per-game profile sync across devices
- Crowd-sourced overclock recommendations ("users on similar hardware…")
- Opt-in benchmark + crash telemetry that powers the recommender

No accounts, no email, no PII.  The only thing the server sees is an
anonymous UUID4 the client generates locally on first run.

---

## Status

| Phase | Endpoints                                                    | Status |
|-------|--------------------------------------------------------------|--------|
| **A** | `/healthz`, `/v1/devices/register`, `…/heartbeat`, `…/settings` | ✅ live |
| B     | `/v1/profiles/*`                                             | stubbed |
| D     | `/v1/playtime/*`                                             | stubbed |
| H     | `/v1/oc/*`, `/v1/telemetry/*`                                | stubbed |

Phase A is everything we need to ship the v3 client and start collecting
anonymous device fingerprints.  Later phases extend the schema (which
ships idempotent in `schema.sql`) and replace the stub routers with real
implementations.

---

## Layout

```
cloud/
├── main.py              FastAPI app entrypoint, lifespan, error handler
├── settings.py          Pydantic env-based config
├── db.py                asyncpg pool + idempotent migration runner
├── schema.sql           Full v3 schema (CREATE … IF NOT EXISTS everywhere)
├── routers/
│   ├── health.py        GET /healthz
│   ├── devices.py       Phase A — register / heartbeat / settings
│   ├── profiles.py      Phase B — stub
│   ├── playtime.py      Phase B/D — stub
│   ├── oc.py            Phase H — stub
│   └── telemetry.py     Phase H — stub
├── requirements.txt
├── Procfile             web: uvicorn main:app …
├── railway.json         Railway build/health config
└── .env.example
```

---

## Local development

```bash
cd cloud
cp .env.example .env       # set DATABASE_URL=postgresql://…
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then:

```bash
curl http://localhost:8000/healthz
open http://localhost:8000/docs   # OpenAPI UI
```

`run_migrations()` applies `schema.sql` on every boot and is idempotent —
safe to run a hundred times on a fresh DB or an upgraded one.

---

## Deploy

See **DEPLOY.md** for the full Railway walkthrough.
