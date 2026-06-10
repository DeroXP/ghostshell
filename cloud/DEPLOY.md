# Deploying GhostShell-Cloud to Railway

This walks through deploying the `cloud/` subdirectory to Railway with a
managed Postgres database.  Total time: ~10 minutes.

---

## 1.  Prereqs

- A [Railway](https://railway.app) account (Hobby plan is fine — Phase A
  fits comfortably under the free tier).
- The GhostShell repo on GitHub.

---

## 2.  Create the project

1. Railway dashboard → **New Project** → **Deploy from GitHub repo**.
2. Pick your `ghostshell` repo.
3. After the initial empty deploy, open the service → **Settings** →
   **Source** → **Root Directory** → set it to `cloud`.
   - This is what tells Railway to build `cloud/` instead of the whole
     repo.  The Windows client at the root is irrelevant to the backend.

Railway auto-detects `Procfile` + `requirements.txt` via Nixpacks.  No
custom Dockerfile required.

---

## 3.  Add Postgres

1. In the same project: **+ New** → **Database** → **Add PostgreSQL**.
2. Railway provisions an instance and exposes a `DATABASE_URL` private
   variable on the project.

---

## 4.  Wire DATABASE_URL into the API service

The Postgres plugin's `DATABASE_URL` is on the project but not yet on the
API service.  Reference it explicitly:

1. API service → **Variables** → **+ New Variable**.
2. Name: `DATABASE_URL`
3. Value: click the variable picker → **Postgres → DATABASE_URL**.
   Railway stores it as a reference (`${{ Postgres.DATABASE_URL }}`) so
   it stays in sync if the DB credentials rotate.

Optional variables (sensible defaults exist):

| Var                  | Default | Notes                                    |
|----------------------|---------|------------------------------------------|
| `LOG_LEVEL`          | `info`  | `debug` / `info` / `warning` / `error`   |
| `RATE_LIMIT_PER_MIN` | `120`   | Per-IP-hash request budget               |
| `DEVICE_TTL_DAYS`    | `180`   | Devices unseen this long get pruned      |

`PORT` is set automatically by Railway — don't override it.

---

## 5.  Deploy

Push to your main branch (or click **Deploy** in the Railway UI).
First deploy takes ~90s.

Watch the **Deploy logs** for:

```
INFO     Booting GhostShell-Cloud v0.1.0
INFO     Ready to serve
INFO     Uvicorn running on http://0.0.0.0:8080
```

If you see `Startup failed` — check that `DATABASE_URL` is set and
reachable.  Railway's private network usually works on the first try.

---

## 6.  Smoke test

Generate a public URL: API service → **Settings** → **Networking** →
**Generate Domain**.

```bash
export URL=https://your-service.up.railway.app

curl -s $URL/healthz
# → {"ok":true,"service":"ghostshell-cloud","version":"0.1.0","db":"ok"}

curl -s $URL/
# → {"service":"ghostshell-cloud","version":"0.1.0","docs":"/docs",…}

# Register a fake device (use any UUIDv4):
DID=$(python -c "import uuid; print(uuid.uuid4())")
curl -s -X POST $URL/v1/devices/register \
  -H "Content-Type: application/json" \
  -d "{\"device_id\":\"$DID\",\"hardware\":{\"gpu_model\":\"NVIDIA GeForce RTX 5070 Ti\",\"ram_gb\":32,\"app_version\":\"0.1.0\"}}"
# → {"ok":true,"device_id":"…","is_new":true,"settings":{…}}
```

If all three succeed, Phase A is live.

---

## 7.  Wire it into the client

Once you have the public URL, in GhostShell:

> **Settings → Cloud → Cloud URL** → paste the Railway domain → **Save**.

The client will register itself on the next launch (or immediately, if
you keep the Settings panel open — `set_cloud_url` triggers an eager
`register()` thread).

That's it — Phase A complete.  The client is now sending anonymous
device fingerprints, which seeds the `devices` table that Phase H's
recommender will later read from.

---

## Cost expectations

For Phase A traffic (low; a heartbeat every 6h per active install) you
should comfortably stay on Railway's Hobby tier.  When telemetry lands
in Phase H, expect ~5 KB per benchmark run + ~2 KB per crash report;
even with 10k installs that's <1 GB/month.

---

## Rollback

Railway keeps the previous deploys.  In the **Deployments** tab click
any past deploy → **Redeploy**.  The schema is idempotent and only
adds, never drops, so rolling the API back is safe.
