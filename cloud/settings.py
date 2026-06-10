"""GhostShell-Cloud — settings loaded from env vars (Railway-friendly)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres connection — Railway sets this automatically when you attach
    # a Postgres service.  For local dev, copy .env.example to .env.
    database_url: str = "postgresql://localhost/ghostshell_cloud"

    # Server
    port: int = 8000
    log_level: str = "info"

    # Behaviour
    rate_limit_per_min: int = 120
    device_ttl_days: int = 180

    # ─── Zero-knowledge peppers ─────────────────────────────────────────
    # Each is a base64-encoded 32-byte random secret.  Generate with:
    #   python -m security
    # Set in Railway as project-scoped env vars.  If unset, security.py
    # falls back to deterministic INSECURE values and logs a warning.
    fingerprint_pepper:  str = ""   # for CPUID + board UUID + disk serial
    email_pepper:        str = ""   # for purchase email lookup
    license_key_pepper:  str = ""   # for storing the hash of license keys
    ip_pepper:           str = ""   # for abuse-rate-limit IP hashes

    # ─── Trial / license behaviour ──────────────────────────────────────
    trial_duration_days: int = 7
    license_max_devices: int = 5

    # ─── Stripe (Phase B.3 — wired in next sub-phase) ───────────────────
    stripe_secret_key:     str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id:       str = ""   # the $20 lifetime SKU
    stripe_tax_enabled:    bool = True

    # ─── VPN / VM detection (Phase B.3) ─────────────────────────────────
    # API key for IPQualityScore or proxycheck.io; empty disables checks.
    vpn_check_provider: str = ""        # "ipqs" | "proxycheck" | ""
    vpn_check_api_key:  str = ""

    # Build version (purely informational; surfaced at /healthz)
    version: str = "0.2.0"


settings = Settings()
