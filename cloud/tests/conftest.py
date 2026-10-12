"""Test bootstrap.

Environment must be fixed BEFORE any cloud module is imported: settings.py
builds its singleton and security.py decodes the peppers at import time.

Two kinds of tests live here:
  • No-database tests (middleware, config, security primitives) build the app
    without running its lifespan, so no Postgres is needed.
  • Route tests that need Postgres run only when TEST_DATABASE_URL points at a
    disposable database (CI's service container, or `make test-db` locally)
    and are skipped otherwise.  They never touch a real Railway database.
"""

from __future__ import annotations

import base64
import os
import secrets

import pytest

# ── Environment, before any project import ──────────────────────────────
os.environ["VISPORA_ENV_FILE"] = ""  # never read a developer's .env in tests
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "warning")
os.environ.setdefault("LOG_FORMAT", "text")
os.environ.setdefault("RATE_LIMIT_PER_MIN", "100000")  # throttle tests build their own app
for _name in ("FINGERPRINT_PEPPER", "EMAIL_PEPPER", "LICENSE_KEY_PEPPER", "IP_PEPPER"):
    os.environ.setdefault(_name, base64.b64encode(secrets.token_bytes(32)).decode())

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from fastapi.testclient import TestClient  # noqa: E402

from db import get_db  # noqa: E402
from main import create_app  # noqa: E402

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set — see Makefile `test-db`"
)


async def _no_db():
    """Dependency override for tests that must never reach the pool."""
    yield None


@pytest.fixture
def app():
    """A fresh app with the DB dependency stubbed out and no lifespan."""
    a = create_app()
    a.dependency_overrides[get_db] = _no_db
    return a


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="session")
def db_client():
    """Real app, real lifespan (pool + migrations) against TEST_DATABASE_URL."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c
