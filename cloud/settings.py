"""Vispora-Cloud — configuration, loaded from environment variables.

Every knob the service has is a field on `Settings`.  Railway injects env vars
at runtime; locally a `.env` file next to this module is read too.  Values are
parsed and validated at import, so a typo in Railway's Variables tab fails the
deploy at boot instead of at the first request that trips over it.

Some settings are derived rather than set:
  • `is_production` comes from APP_ENV, or — when APP_ENV is unset — from
    Railway's own RAILWAY_ENVIRONMENT_NAME.  A Railway environment called
    "production" is therefore production with no extra configuration.
  • Production-only defaults (OpenAPI docs off, JSON logs, trust one proxy
    hop) switch on automatically; each can still be forced with its own
    variable.
  • The four peppers are decoded and checked in `security.py`; `main.py`'s
    preflight refuses to boot a production instance without them.
"""

from __future__ import annotations

import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
_LOG_FORMATS = ("", "json", "text")


def _default_app_env() -> str:
    # Railway names its environments ("production", "staging", PR environments…).
    return os.environ.get("RAILWAY_ENVIRONMENT_NAME", "development")


def on_railway() -> bool:
    """True when running inside a Railway deployment (any environment)."""
    return bool(os.environ.get("RAILWAY_ENVIRONMENT_NAME") or os.environ.get("RAILWAY_PROJECT_ID"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # VISPORA_ENV_FILE="" disables .env loading.  The test suite does this so
        # a developer's local .env can never leak into a test run.
        env_file=os.environ.get("VISPORA_ENV_FILE", ".env") or None,
        extra="ignore",
    )

    # ─── Deployment ─────────────────────────────────────────────────────
    app_env: str = Field(default_factory=_default_app_env)
    version: str = "0.3.0"
    # Railway sets DATABASE_URL when a Postgres service is referenced.  The
    # default only exists for local development; production refuses it.
    database_url: str = "postgresql://localhost/vispora_cloud"
    port: int = 8000
    log_level: str = "info"
    log_format: str = ""  # "json" | "text" | "" → json on Railway/production
    expose_docs: bool | None = None  # None → docs only outside production

    # ─── HTTP hardening ─────────────────────────────────────────────────
    # Reverse-proxy hops in front of the app that append X-Forwarded-For.
    # None → 1 on Railway / in production (Railway's edge), 0 elsewhere.
    trusted_proxy_hops: int | None = None
    max_body_bytes: int = Field(default=256 * 1024, ge=1024)
    rate_limit_per_min: int = Field(default=120, ge=0)  # 0 disables
    rate_limit_burst: int | None = Field(default=None, ge=1)  # None → per-min value
    cors_origins: str = ""  # comma-separated browser origins; empty → no CORS
    hsts_max_age: int = Field(default=31_536_000, ge=0)  # 0 omits the header

    # ─── Database pool ──────────────────────────────────────────────────
    db_pool_min: int = Field(default=1, ge=0)
    db_pool_max: int = Field(default=10, ge=1)
    db_connect_timeout_s: float = Field(default=10.0, gt=0)
    db_acquire_timeout_s: float = Field(default=5.0, gt=0)
    db_command_timeout_s: float = Field(default=30.0, gt=0)

    # ─── Behaviour ──────────────────────────────────────────────────────
    device_ttl_days: int = 180

    # ─── Zero-knowledge peppers ─────────────────────────────────────────
    # Each is a base64-encoded 32-byte random secret.  Generate with:
    #   python -m security        (or `make peppers`)
    # Set in Railway as service variables.  If unset, security.py falls back
    # to deterministic INSECURE values; main.py refuses to boot production
    # that way.
    fingerprint_pepper: str = ""  # for CPUID + board UUID + disk serial
    email_pepper: str = ""  # for purchase email lookup
    license_key_pepper: str = ""  # for storing the hash of license keys
    ip_pepper: str = ""  # for abuse-rate-limit IP hashes

    # ─── Trial / license behaviour ──────────────────────────────────────
    trial_duration_days: int = Field(default=7, ge=1)
    license_max_devices: int = Field(default=5, ge=1, le=20)

    # ─── Stripe (not wired yet) ─────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""  # the lifetime SKU
    stripe_tax_enabled: bool = True

    # ─── VPN / VM detection (not wired yet) ─────────────────────────────
    # API key for IPQualityScore or proxycheck.io; empty disables checks.
    vpn_check_provider: str = ""  # "ipqs" | "proxycheck" | ""
    vpn_check_api_key: str = ""

    # ─── Validators ─────────────────────────────────────────────────────
    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {list(_LOG_LEVELS)}")
        return v

    @field_validator("log_format")
    @classmethod
    def _normalise_log_format(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _LOG_FORMATS:
            raise ValueError("LOG_FORMAT must be 'json', 'text' or empty")
        return v

    # ─── Derived values ─────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in ("production", "prod")

    @property
    def docs_enabled(self) -> bool:
        if self.expose_docs is not None:
            return self.expose_docs
        return not self.is_production

    @property
    def json_logs(self) -> bool:
        if self.log_format:
            return self.log_format == "json"
        return on_railway() or self.is_production

    @property
    def proxy_hops(self) -> int:
        if self.trusted_proxy_hops is not None:
            return max(0, self.trusted_proxy_hops)
        return 1 if (on_railway() or self.is_production) else 0

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
