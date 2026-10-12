# Deploying Vispora-Cloud to Railway

Railway builds `cloud/Dockerfile` straight from GitHub and runs the image
behind its TLS edge.  Everything below is done once; after that, merging to
`main` deploys — and only after CI is green, if you switch on *Wait for CI*.

---

## 1. Prerequisites

- A [Railway](https://railway.app) account with the Railway GitHub app
  installed on this repository.
- This repository on GitHub, with `main` as the default branch.

## 2. Create the project

1. Railway dashboard → **New Project** → **Deploy from GitHub repo** → pick
   this repository.
2. Open the service → **Settings** → **Source** → **Root Directory**: `cloud`.
   Railway now reads `cloud/railway.json`, which selects the Dockerfile
   builder and the `/healthz` health check.
3. Same page → **Branch**: `main`.
4. **Wait for CI**: on.  Railway then holds the build until the commit's
   GitHub checks pass, so a red `ci-ok` never reaches production.  (Without
   it Railway deploys on push, CI or not.)

## 3. Add Postgres

**+ New** → **Database** → **Add PostgreSQL**.  Railway provisions it with
private networking; the API talks to it over `postgres.railway.internal`
without leaving the project.

## 4. Variables

Service → **Variables**.  Required:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | pick **Postgres → DATABASE_URL** from the variable picker; Railway stores it as `${{ Postgres.DATABASE_URL }}` so credential rotations follow |
| `FINGERPRINT_PEPPER` | from `make peppers` — 32 random bytes, base64 |
| `EMAIL_PEPPER` | "" |
| `LICENSE_KEY_PEPPER` | "" |
| `IP_PEPPER` | "" |

Run `make peppers` (or `python -m security`) **locally**, paste the four
values, and store them in a password manager — see *Rotating a pepper* below.
Railway can **seal** a variable so its value is never shown in the UI again;
do that for the peppers.  Never commit them.

The service is production when the Railway environment is named
`production` (the default).  For a differently named environment that should
behave as production, set `APP_ENV=production` explicitly.

Optional (defaults in brackets; full list with comments in `.env.example`):

| Variable | Default | Notes |
| --- | --- | --- |
| `LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error` / `critical` |
| `LOG_FORMAT` | json on Railway | `json` or `text` |
| `EXPOSE_DOCS` | off in production | `true` publishes `/docs` + `/openapi.json` |
| `WEB_CONCURRENCY` | `1` | uvicorn workers; each has its own rate-limit table |
| `TRUSTED_PROXY_HOPS` | `1` on Railway | reverse proxies that append `X-Forwarded-For` |
| `MAX_BODY_BYTES` | `262144` | request-body cap |
| `RATE_LIMIT_PER_MIN` | `120` | per client; `0` disables |
| `RATE_LIMIT_BURST` | = per-min | bucket capacity |
| `CORS_ORIGINS` | empty | comma-separated browser origins; empty = no CORS |
| `HSTS_MAX_AGE` | `31536000` | `0` omits the header |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `1` / `10` | per worker |
| `DB_CONNECT_TIMEOUT_S` / `DB_ACQUIRE_TIMEOUT_S` / `DB_COMMAND_TIMEOUT_S` | `10` / `5` / `30` | |
| `TRIAL_DURATION_DAYS` / `LICENSE_MAX_DEVICES` | `7` / `5` | |
| `DEVICE_TTL_DAYS` | `180` | devices unseen this long are prune candidates |

`PORT` is set by Railway — don't override it.

## 5. Deploy

Push to `main` (or **Deploy** in the UI).  The first build takes about two
minutes; later builds reuse the dependency layer and are faster.  Expected
deploy log (JSON, one object per line):

```
… "message":"Booting Vispora-Cloud v0.3.0 (env=production)"
… "message":"Creating Postgres pool (min=1 max=10)"
… "message":"Postgres pool ready"
… "message":"Running migrations from schema.sql (… bytes)"
… "message":"Migrations applied successfully"
… "message":"Ready to serve"
```

If instead you see `"level":"CRITICAL","message":"Refusing to start: …"`, the
preflight found a missing pepper or `DATABASE_URL`.  Fix the variable and
redeploy; the previous deployment keeps serving meanwhile.

Railway polls `/healthz` for up to 60 s.  It returns `503` until Postgres
answers, so traffic is only switched to an instance that can actually work.

## 6. Smoke test

Service → **Settings** → **Networking** → **Generate Domain**, then:

```bash
export URL=https://<your-service>.up.railway.app

curl -si "$URL/healthz"
#  HTTP/2 200 … x-request-id, strict-transport-security, x-ratelimit-limit …
#  {"ok":true,"service":"vispora-cloud","version":"0.3.0","env":"production","db":"ok"}

curl -s "$URL/docs" -o /dev/null -w '%{http_code}\n'      # 404 in production

DID=$(python -c "import uuid; print(uuid.uuid4())")
curl -s -X POST "$URL/v1/devices/register" \
  -H "Content-Type: application/json" \
  -d "{\"device_id\":\"$DID\",\"hardware\":{\"gpu_model\":\"NVIDIA GeForce RTX 5070 Ti\",\"ram_gb\":32,\"app_version\":\"3.6.0\"}}"
#  {"ok":true,"device_id":"…","is_new":true,"settings":{…}}
```

## 7. GitHub repository settings (one-time)

These turn the CI into an enforced gate rather than a suggestion.

**Branch protection on `main`** (Settings → Branches → Add rule, or Rulesets):

- Require a pull request before merging; require review from Code Owners
  (`.github/CODEOWNERS`).
- Require status checks to pass before merging: **`ci-ok`** (and
  **`analyze (python)`** once CodeQL is running).  Require branches to be
  up to date.
- Require conversation resolution; block force pushes and deletions.

**Code security** (Settings → Code security and analysis): enable Dependabot
alerts and security updates, secret scanning with push protection, and
private vulnerability reporting (what `SECURITY.md` points people at).
CodeQL runs from `.github/workflows/codeql.yml`; on a private repository it
needs GitHub Advanced Security — delete the workflow if you don't have it.

**Actions** (Settings → Actions → General): workflow permissions
*Read repository contents and packages*.  The workflows already declare the
minimum they need; this is the belt to their braces.

## 8. Wire the client

Vispora → **Settings → Cloud → Cloud URL** → paste the Railway domain → Save.
The client registers on the next launch (or at once, while the panel is open).

---

## Operations

**Rollback.**  Deployments → any earlier deploy → **Redeploy**.  The schema
only ever adds, so an older API against a newer schema is safe.

**Logs.**  JSON lines; Railway's explorer indexes the keys.  Filter on
`request_id` to follow one request — it is the same value the client got in
`X-Request-ID`.  Client IPs never appear.

**Admin CLI.**  `admin_mint.py` needs a `DATABASE_URL` it can reach: the
Postgres service's **public** connection URL from a laptop
(`DATABASE_URL='postgresql://…' python admin_mint.py list`), or the private
one from a shell inside the service (`railway ssh`).

**Rotating a pepper** invalidates every hash made with it.  Rotating
`FINGERPRINT_PEPPER` or `LICENSE_KEY_PEPPER` orphans every trial and
activation; rotating `IP_PEPPER` only resets abuse counters.  Treat the
peppers like the database: back them up, don't lose them.

**Scaling out.**  Each replica (and each `WEB_CONCURRENCY` worker) keeps its
own rate-limit table, so N of them means N× the per-client budget.  Move the
limiter to a shared store before scaling beyond one.

**Cost.**  Current traffic (a heartbeat every 6 h per install, a license
check per launch) fits Railway's Hobby tier with room to spare.
