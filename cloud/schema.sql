-- ─────────────────────────────────────────────────────────────────────────
-- GhostShell-Cloud — v3 Postgres schema
-- ─────────────────────────────────────────────────────────────────────────
-- Idempotent: every CREATE uses IF NOT EXISTS, every ALTER guards against
-- already-exists.  Safe to run on every server boot.
--
-- Phase A only USES the `devices` and `device_settings` tables; the rest
-- exist so we don't have to migrate later.  Empty tables cost ~16 KB each.
-- ─────────────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ─── Identity ────────────────────────────────────────────────────────────
-- A device is GhostShell's anonymous unit of identity.  No usernames, no
-- emails — just a UUID generated client-side on first run.
CREATE TABLE IF NOT EXISTS devices (
    id              UUID PRIMARY KEY,
    -- Hardware fingerprint.  Used only for "users with similar hardware"
    -- aggregations; never linked to a real identity.
    gpu_model       TEXT,
    gpu_model_norm  TEXT,           -- lowercased + stripped of whitespace
    cpu_family      TEXT,           -- "intel core ultra 9", "amd ryzen 7"
    cpu_model       TEXT,
    ram_gb          INT,
    ram_gb_bucket   TEXT,           -- "16", "32-64", "64+", etc.
    os_build        TEXT,
    app_version     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ip_hash    TEXT            -- SHA256 of IP, for abuse-prevention only
);
CREATE INDEX IF NOT EXISTS idx_devices_last_seen      ON devices(last_seen);
CREATE INDEX IF NOT EXISTS idx_devices_gpu_model_norm ON devices(gpu_model_norm);
CREATE INDEX IF NOT EXISTS idx_devices_cpu_family     ON devices(cpu_family);

CREATE TABLE IF NOT EXISTS device_settings (
    device_id        UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    telemetry_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
    sync_opt_in      BOOLEAN NOT NULL DEFAULT FALSE,
    news_opt_in      BOOLEAN NOT NULL DEFAULT TRUE,
    update_channel   TEXT    NOT NULL DEFAULT 'stable',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Game catalogue (Phase B) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS games (
    id              SERIAL PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    steam_appid     INT UNIQUE,
    epic_app_id     TEXT,
    classification  TEXT,           -- "competitive" | "visual" | NULL
    cover_url       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Map any of the ~830 detected exe names to a single game record.
CREATE TABLE IF NOT EXISTS game_exe_aliases (
    exe_name        TEXT PRIMARY KEY,        -- always lowercase .exe
    game_id         INT NOT NULL REFERENCES games(id) ON DELETE CASCADE
);

-- ─── Per-device per-game profile (Phase B/D, synced if opted in) ─────────
CREATE TABLE IF NOT EXISTS user_game_profiles (
    device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id         INT  NOT NULL REFERENCES games(id)   ON DELETE CASCADE,
    oc_core         INT  NOT NULL DEFAULT 0,
    oc_mem          INT  NOT NULL DEFAULT 0,
    power_pct       INT  NOT NULL DEFAULT 100,
    cpu_uv_offset   INT  NOT NULL DEFAULT 0,
    custom_tweaks   JSONB,                 -- per-game CPU affinity, DNS, DSCP, etc.
    adaptive_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    adaptive_mode   TEXT,                   -- "competitive" | "visual"
    last_used_ts    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, game_id)
);
CREATE INDEX IF NOT EXISTS idx_ugp_game ON user_game_profiles(game_id);

-- ─── Adaptive Tuning persistent state (Phase I) ──────────────────────────
CREATE TABLE IF NOT EXISTS adaptive_tuning_state (
    device_id           UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id             INT  NOT NULL REFERENCES games(id)   ON DELETE CASCADE,
    current_core_offset INT  NOT NULL DEFAULT 0,
    current_mem_offset  INT  NOT NULL DEFAULT 0,
    baseline_core       INT  NOT NULL DEFAULT 0,
    baseline_mem        INT  NOT NULL DEFAULT 0,
    blacklisted         JSONB,             -- [{core, mem, kind, ts}, ...]
    step_history        JSONB,             -- bandit action log
    total_play_hours    NUMERIC(10,2) NOT NULL DEFAULT 0,
    converged           BOOLEAN NOT NULL DEFAULT FALSE,
    last_session_ts     TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, game_id)
);

-- ─── Playtime ledger (Phase B/D) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS game_sessions (
    id              BIGSERIAL PRIMARY KEY,
    device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id         INT  NOT NULL REFERENCES games(id)   ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    duration_s      INT,
    crashed         BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_game_sessions_device     ON game_sessions(device_id);
CREATE INDEX IF NOT EXISTS idx_game_sessions_started_at ON game_sessions(started_at);

-- ─── Telemetry (opt-in, Phase H) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id              BIGSERIAL PRIMARY KEY,
    device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id         INT  REFERENCES games(id) ON DELETE SET NULL,
    oc_core         INT  NOT NULL,
    oc_mem          INT  NOT NULL,
    power_pct       INT  NOT NULL,
    avg_fps         REAL,
    low_1pct_fps    REAL,
    frametime_std   REAL,
    max_temp_c      REAL,
    score           REAL,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bench_game        ON benchmark_runs(game_id);
CREATE INDEX IF NOT EXISTS idx_bench_run_at      ON benchmark_runs(run_at);
CREATE INDEX IF NOT EXISTS idx_bench_score       ON benchmark_runs(score);

CREATE TABLE IF NOT EXISTS adaptive_steps (
    id              BIGSERIAL PRIMARY KEY,
    device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id         INT  REFERENCES games(id) ON DELETE SET NULL,
    action          TEXT NOT NULL,            -- "core+25", "mem-100", "hold"
    score_delta     REAL,
    accepted        BOOLEAN NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crash_reports (
    id              BIGSERIAL PRIMARY KEY,
    device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id         INT  REFERENCES games(id) ON DELETE SET NULL,
    oc_core         INT,
    oc_mem          INT,
    kind            TEXT NOT NULL,            -- "context_loss"|"hang"|"tdr"|"artifacts"
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crash_kind ON crash_reports(kind);

-- ─────────────────────────────────────────────────────────────────────────
-- ─── Phase B — commercial licensing layer ────────────────────────────────
-- ─────────────────────────────────────────────────────────────────────────
-- Zero-knowledge by design: every column that could identify a real-world
-- person is a peppered HMAC.  See cloud/security.py.
--   • Hardware fingerprints (CPU ID + board UUID + disk serial) → BYTEA(32)
--   • Email addresses                                            → BYTEA(32)
--   • License keys                                               → BYTEA(32)
--   • IP addresses                                               → BYTEA(32)
-- We never store any of those values in plaintext.
-- ─────────────────────────────────────────────────────────────────────────

-- ─── Licenses (one row per Stripe purchase) ──────────────────────────────
CREATE TABLE IF NOT EXISTS licenses (
    id                       BIGSERIAL PRIMARY KEY,
    -- Hash of the 20-char license key.  The user holds the only plaintext.
    key_hash                 BYTEA NOT NULL UNIQUE,
    -- Hash of the email collected by Stripe at checkout.  Used for matching
    -- the Stripe Customer Portal magic-link flow back to a license.
    email_hash               BYTEA,
    -- Lifecycle.  'active' once paid; 'refunded' or 'revoked' kills the
    -- license on the next client validate() call.
    status                   TEXT NOT NULL DEFAULT 'active'
                                   CHECK (status IN ('active', 'refunded', 'revoked')),
    paid_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    refunded_at              TIMESTAMPTZ,
    revoked_at               TIMESTAMPTZ,
    revoke_reason            TEXT,
    -- Stripe references — these are NOT secrets; they're public to the
    -- buyer via their receipt.  Stored for the webhook idempotency check
    -- and so we can correlate refunds back to the license.
    stripe_customer_id       TEXT,
    stripe_payment_intent_id TEXT UNIQUE,
    currency                 TEXT,
    amount_cents             INT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_licenses_email_hash ON licenses(email_hash);
CREATE INDEX IF NOT EXISTS idx_licenses_status     ON licenses(status);

-- ─── License → device slots (max 5) ──────────────────────────────────────
-- A device "activates" against a license, consuming a slot.  Slots are
-- numbered 1..N so the user can deactivate "device 3" via the portal.
CREATE TABLE IF NOT EXISTS license_devices (
    id                BIGSERIAL PRIMARY KEY,
    license_id        BIGINT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    slot_no           INT    NOT NULL,
    device_fp_hash    BYTEA  NOT NULL,
    -- Bucket fingerprint values, NOT identifiers.  Used for the
    -- recommender ("users on similar hardware") AND for the user's own
    -- portal so they can see "Slot 2 — RTX 5070 Ti, last seen 3 days ago".
    gpu_model_norm    TEXT,
    cpu_family        TEXT,
    ram_gb_bucket     TEXT,
    activated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at    TIMESTAMPTZ,
    UNIQUE (license_id, slot_no),
    UNIQUE (license_id, device_fp_hash)
);
CREATE INDEX IF NOT EXISTS idx_lic_devices_license   ON license_devices(license_id);
CREATE INDEX IF NOT EXISTS idx_lic_devices_fp        ON license_devices(device_fp_hash);
CREATE INDEX IF NOT EXISTS idx_lic_devices_last_seen ON license_devices(last_seen);

-- ─── Trials (7-day, 1 per hardware fingerprint, ever) ────────────────────
-- Keyed by the fingerprint hash so you can't reset by reinstalling Windows
-- or wiping AppData — the server remembers your hardware.
CREATE TABLE IF NOT EXISTS trials (
    device_fp_hash    BYTEA PRIMARY KEY,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    -- Bucket info for diagnostics (no identity).
    gpu_model_norm    TEXT,
    cpu_family        TEXT,
    ram_gb_bucket     TEXT,
    app_version       TEXT,
    -- Anti-abuse signals captured at trial start.  IP hash uses the
    -- IP_PEPPER (separate from fingerprint pepper).
    ip_hash           BYTEA,
    vpn_flag          BOOLEAN NOT NULL DEFAULT FALSE,
    vm_flag           BOOLEAN NOT NULL DEFAULT FALSE,
    -- If the trial converted to a paid license, link it here.
    converted_to      BIGINT REFERENCES licenses(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_trials_expires_at ON trials(expires_at);
CREATE INDEX IF NOT EXISTS idx_trials_started_at ON trials(started_at);

-- ─── Stripe webhook event log (idempotency) ──────────────────────────────
-- Stripe retries webhooks.  Storing the event_id keeps us from minting a
-- second license on the same payment.  We store the JSON payload hash, NOT
-- the payload itself — the payload contains the buyer's email.
CREATE TABLE IF NOT EXISTS payments (
    stripe_event_id   TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,
    payload_sha256    BYTEA NOT NULL,
    license_id        BIGINT REFERENCES licenses(id) ON DELETE SET NULL,
    processed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payments_event_type ON payments(event_type);

-- ─── Releases (license-gated update fetch) ───────────────────────────────
CREATE TABLE IF NOT EXISTS releases (
    id                BIGSERIAL PRIMARY KEY,
    version           TEXT NOT NULL,
    channel           TEXT NOT NULL DEFAULT 'stable'
                            CHECK (channel IN ('stable', 'insiders')),
    file_url          TEXT NOT NULL,        -- R2 / Railway-hosted .exe URL
    file_sha256       BYTEA NOT NULL,
    signed            BOOLEAN NOT NULL DEFAULT TRUE,
    notes             TEXT,
    min_supported     TEXT,                  -- below this, force-update
    published_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (version, channel)
);
CREATE INDEX IF NOT EXISTS idx_releases_channel_published
    ON releases(channel, published_at DESC);

-- ─── Manual VPN / VM appeal queue ────────────────────────────────────────
-- When a legitimate user gets blocked by anti-abuse heuristics they can
-- submit an appeal.  Reviewed manually.
CREATE TABLE IF NOT EXISTS device_appeals (
    id              BIGSERIAL PRIMARY KEY,
    device_fp_hash  BYTEA NOT NULL,
    reason          TEXT NOT NULL CHECK (reason IN ('vpn', 'vm', 'other')),
    user_note       TEXT,
    contact_hash    BYTEA,         -- HMAC of email if user provided one
    status          TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'approved', 'denied')),
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_device_appeals_status ON device_appeals(status);

-- ─── Recommendation aggregates (refreshed nightly) ───────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS oc_recommendations AS
SELECT
    d.gpu_model_norm,
    d.cpu_family,
    d.ram_gb_bucket,
    p.game_id,
    p.oc_core,
    p.oc_mem,
    COUNT(DISTINCT p.device_id)               AS unique_users,
    SUM(p.last_used_ts IS NOT NULL::INT)::INT AS active_users,
    NOW()                                     AS computed_at
FROM user_game_profiles p
JOIN devices d ON d.id = p.device_id
GROUP BY d.gpu_model_norm, d.cpu_family, d.ram_gb_bucket, p.game_id, p.oc_core, p.oc_mem
HAVING COUNT(DISTINCT p.device_id) >= 3;

CREATE INDEX IF NOT EXISTS idx_oc_recs_lookup
    ON oc_recommendations(gpu_model_norm, cpu_family, game_id);
