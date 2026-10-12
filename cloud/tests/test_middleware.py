"""The hardening stack: request ids, security headers, rate limiting, body
limits, error shapes, docs exposure, compression.  No database involved."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from main import create_app
from middleware import client_ip
from settings import settings

STATUS_PATH = "/v1/profiles/_status"  # a real route that needs no database


# ── Request id + response time ──────────────────────────────────────────
def test_response_carries_request_id_and_timing(client):
    r = client.get("/")
    assert r.status_code == 200
    rid = r.headers["x-request-id"]
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)
    assert float(r.headers["x-response-time-ms"]) >= 0


def test_wellformed_incoming_request_id_is_echoed(client):
    r = client.get("/", headers={"X-Request-ID": "trace-abc.123_XYZ"})
    assert r.headers["x-request-id"] == "trace-abc.123_XYZ"


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "<script>", "a;b"])
def test_malformed_incoming_request_id_is_replaced(client, bad):
    r = client.get("/", headers={"X-Request-ID": bad})
    assert r.headers["x-request-id"] != bad
    assert len(r.headers["x-request-id"]) == 32


# ── Security headers ────────────────────────────────────────────────────
def test_security_headers_on_api_responses(client):
    r = client.get("/")
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert h["cache-control"] == "no-store"
    assert h["content-security-policy"].startswith("default-src 'none'")
    assert h["strict-transport-security"].startswith("max-age=")
    assert "permissions-policy" in h
    assert "server" not in h  # TestClient never adds one; uvicorn runs with --no-server-header


def test_security_headers_on_error_responses_too(client):
    r = client.get("/definitely-not-a-route")
    assert r.status_code == 404
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "x-request-id" in r.headers


def test_docs_page_gets_a_renderable_csp(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "cdn.jsdelivr.net" in r.headers["content-security-policy"]


def test_hsts_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "hsts_max_age", 0)
    r = TestClient(create_app()).get("/")
    assert "strict-transport-security" not in r.headers


# ── Error shapes ────────────────────────────────────────────────────────
def test_404_is_json_with_ok_false(client):
    r = client.get("/nope")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["err"] == "Not Found"
    assert body["detail"] == "Not Found"  # kept for the desktop client


def test_validation_error_shape_does_not_echo_input(client):
    r = client.post("/v1/devices/register", json={"device_id": "not-a-uuid", "secret": "hunter2"})
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False and body["err"] == "validation_error"
    assert all(set(e) == {"loc", "msg", "type"} for e in body["detail"])
    assert "hunter2" not in r.text


def test_unhandled_exception_is_opaque_500(app):
    async def boom():
        raise RuntimeError("database password is swordfish")

    app.add_api_route("/_boom", boom)
    r = TestClient(app, raise_server_exceptions=False).get("/_boom")
    assert r.status_code == 500
    body = r.json()
    assert body == {"ok": False, "err": "internal_error", "request_id": r.headers["x-request-id"]}
    assert "swordfish" not in r.text and "RuntimeError" not in r.text
    # Even the 500 goes through the full pipeline.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


# ── Body limit ──────────────────────────────────────────────────────────
def test_declared_oversized_body_is_rejected_before_reading(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 1024)
    c = TestClient(create_app(), raise_server_exceptions=False)
    r = c.post(
        "/v1/devices/register", content=b"x" * 2048, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 413
    body = r.json()
    assert body["ok"] is False and body["err"] == "payload_too_large"
    assert "1024" in body["detail"]


def test_chunked_oversized_body_is_cut_off(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 1024)
    c = TestClient(create_app(), raise_server_exceptions=False)

    def chunks():
        for _ in range(4):
            yield b"x" * 600

    r = c.post(
        "/v1/devices/register", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 413


def test_bad_content_length_is_400(client):
    r = client.post("/v1/devices/register", content=b"{}", headers={"Content-Length": "abc"})
    assert r.status_code == 400


def test_small_body_passes_through(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 1024)
    app = create_app()
    from db import get_db

    async def _no_db():
        yield None

    app.dependency_overrides[get_db] = _no_db
    r = TestClient(app).post("/v1/devices/register", json={"device_id": "x"})
    assert r.status_code == 422  # reached validation, i.e. not blocked by the limit


# ── Rate limiting ───────────────────────────────────────────────────────
def _throttled_app(monkeypatch, per_min=3, hops=0):
    monkeypatch.setattr(settings, "rate_limit_per_min", per_min)
    monkeypatch.setattr(settings, "rate_limit_burst", per_min)
    monkeypatch.setattr(settings, "trusted_proxy_hops", hops)
    return TestClient(create_app(), raise_server_exceptions=False)


def test_rate_limit_kicks_in_after_burst(monkeypatch):
    c = _throttled_app(monkeypatch)
    for i in range(3):
        r = c.get(STATUS_PATH)
        assert r.status_code == 200
        assert r.headers["x-ratelimit-limit"] == "3"
        assert r.headers["x-ratelimit-remaining"] == str(2 - i)
    r = c.get(STATUS_PATH)
    assert r.status_code == 429
    assert r.json()["err"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    assert r.headers["x-ratelimit-remaining"] == "0"
    assert "x-request-id" in r.headers  # outer middleware still applied


def test_health_and_root_are_exempt(monkeypatch):
    c = _throttled_app(monkeypatch)
    for _ in range(4):
        c.get(STATUS_PATH)
    assert c.get(STATUS_PATH).status_code == 429
    assert c.get("/").status_code == 200
    assert c.get("/healthz").status_code == 503  # no pool in these tests, but not 429


def test_rate_limit_keys_on_trusted_proxy_entry_not_client_supplied(monkeypatch):
    c = _throttled_app(monkeypatch, hops=1)
    # The rightmost entry is what the (single, trusted) proxy appended.
    for _ in range(3):
        assert (
            c.get(STATUS_PATH, headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"}).status_code == 200
        )
    # Changing the client-controlled leftmost entry must NOT reset the budget …
    assert c.get(STATUS_PATH, headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"}).status_code == 429
    # … while a genuinely different client has its own budget.
    assert c.get(STATUS_PATH, headers={"X-Forwarded-For": "1.1.1.1, 8.8.8.8"}).status_code == 200


def test_rate_limit_disabled_with_zero(monkeypatch):
    c = _throttled_app(monkeypatch, per_min=0)
    for _ in range(50):
        assert c.get(STATUS_PATH).status_code == 200


# ── client_ip() unit ────────────────────────────────────────────────────
def _scope(xff: str | None, peer="10.0.0.5"):
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return {"type": "http", "headers": headers, "client": (peer, 1234)}


@pytest.mark.parametrize(
    ("xff", "hops", "expected"),
    [
        (None, 0, "10.0.0.5"),
        ("1.1.1.1", 0, "10.0.0.5"),  # hops=0 ignores the header entirely
        ("1.1.1.1", 1, "1.1.1.1"),
        ("1.1.1.1, 2.2.2.2", 1, "2.2.2.2"),
        ("1.1.1.1, 2.2.2.2, 3.3.3.3", 2, "2.2.2.2"),
        ("1.1.1.1", 3, "1.1.1.1"),  # fewer entries than hops → leftmost available
        ("  ,  ", 1, "10.0.0.5"),  # empty header → peer
    ],
)
def test_client_ip_resolution(xff, hops, expected):
    assert client_ip(_scope(xff), hops) == expected


def test_client_ip_without_peer():
    assert client_ip({"type": "http", "headers": []}, 0) == "unknown"


# ── Docs exposure ───────────────────────────────────────────────────────
def test_docs_hidden_in_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "expose_docs", None)
    c = TestClient(create_app())
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404
    assert c.get("/").json()["docs"] is None


def test_docs_can_be_forced_on_in_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "expose_docs", True)
    assert TestClient(create_app()).get("/openapi.json").status_code == 200


def test_docs_available_outside_production(client):
    assert client.get("/openapi.json").status_code == 200


# ── Compression + JSON ──────────────────────────────────────────────────
def test_large_responses_are_gzipped(client):
    r = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    json.loads(r.text)  # httpx transparently decompresses


def test_small_responses_are_not_gzipped(client):
    r = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers


def test_cors_absent_by_default(client):
    r = client.get("/", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", "https://vispora.example")
    c = TestClient(create_app())
    ok = c.get("/", headers={"Origin": "https://vispora.example"})
    assert ok.headers["access-control-allow-origin"] == "https://vispora.example"
    other = c.get("/", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_root_payload(client):
    body = client.get("/").json()
    assert body["service"] == "vispora-cloud"
    assert body["health"] == "/healthz"
    uuid.UUID(hex=client.get("/").headers["x-request-id"])  # a valid uuid4 hex
