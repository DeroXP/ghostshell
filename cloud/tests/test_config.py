"""Settings derivation and the production preflight."""

from __future__ import annotations

from main import preflight
from settings import Settings

_DEFAULT_DB = Settings.model_fields["database_url"].default


def _settings(**kw) -> Settings:
    # _env_file=None: ignore any .env; explicit kwargs beat process env vars.
    return Settings(_env_file=None, **kw)


class TestDerivedSettings:
    def test_development_defaults(self):
        s = _settings(app_env="development", log_format="")
        assert not s.is_production
        assert s.docs_enabled
        assert s.proxy_hops == 0

    def test_production_defaults(self):
        s = _settings(app_env="production", log_format="")
        assert s.is_production
        assert not s.docs_enabled
        assert s.json_logs
        assert s.proxy_hops == 1

    def test_explicit_overrides_win(self):
        s = _settings(
            app_env="production", expose_docs=True, log_format="text", trusted_proxy_hops=3
        )
        assert s.docs_enabled
        assert not s.json_logs
        assert s.proxy_hops == 3

    def test_prod_alias(self):
        assert _settings(app_env="PROD").is_production

    def test_cors_list_parsing(self):
        s = _settings(cors_origins=" https://a.example , https://b.example,, ")
        assert s.cors_origin_list == ["https://a.example", "https://b.example"]
        assert _settings(cors_origins="").cors_origin_list == []

    def test_log_level_normalised(self):
        assert _settings(log_level=" WARNING ").log_level == "warning"

    def test_invalid_log_level_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="LOG_LEVEL"):
            _settings(log_level="loud")


class TestPreflight:
    def test_development_is_never_blocked(self):
        s = _settings(app_env="development", database_url=_DEFAULT_DB)
        assert preflight(s, insecure_peppers=True) == []

    def test_production_without_peppers_is_blocked(self):
        s = _settings(app_env="production", database_url="postgresql://u:p@db/x")
        problems = preflight(s, insecure_peppers=True)
        assert len(problems) == 1
        assert "PEPPER" in problems[0]

    def test_production_with_default_database_url_is_blocked(self):
        s = _settings(app_env="production", database_url=_DEFAULT_DB)
        problems = preflight(s, insecure_peppers=False)
        assert len(problems) == 1
        assert "DATABASE_URL" in problems[0]

    def test_production_properly_configured_passes(self):
        s = _settings(app_env="production", database_url="postgresql://u:p@db/x")
        assert preflight(s, insecure_peppers=False) == []
