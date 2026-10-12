"""End-to-end route tests against a real (disposable) Postgres.

Skipped unless TEST_DATABASE_URL is set.  The app's lifespan runs for real:
pool creation, schema migration (idempotent — this also proves schema.sql
applies cleanly to an empty database and again to a migrated one).
Every test uses fresh UUIDs / fingerprints so order and re-runs don't matter.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from security import generate_license_key, hash_license_key
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db


def _sql(query: str, *args):
    """Run one statement on a fresh connection (tests are sync)."""

    async def go():
        c = await asyncpg.connect(TEST_DATABASE_URL)
        try:
            return await c.fetchrow(query, *args)
        finally:
            await c.close()

    return asyncio.run(go())


def _fp(tag: str) -> dict:
    return {
        "cpu_id": f"CPU-{tag}",
        "motherboard_uuid": f"BOARD-{tag}",
        "disk_serial": f"DISK-{tag}",
    }


def _mint_license() -> tuple[str, int]:
    key = generate_license_key()
    row = _sql(
        "INSERT INTO licenses (key_hash, status, currency, amount_cents) "
        "VALUES ($1, 'active', 'USD', 2000) RETURNING id",
        hash_license_key(key),
    )
    return key, row["id"]


# ── Health ──────────────────────────────────────────────────────────────
def test_healthz_reports_db_ok(db_client):
    r = db_client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["db"] == "ok"
    assert body["service"] == "vispora-cloud"


def test_migrations_are_idempotent(db_client):
    # The lifespan already ran them once; running again must be a no-op.
    from db import run_migrations

    db_client.portal.call(run_migrations)
    assert db_client.get("/healthz").status_code == 200


# ── Devices ─────────────────────────────────────────────────────────────
def test_device_register_heartbeat_settings_flow(db_client):
    did = str(uuid.uuid4())
    hw = {"gpu_model": "NVIDIA GeForce RTX 5070 Ti", "cpu_family": "amd ryzen 7", "ram_gb": 32}

    r = db_client.post("/v1/devices/register", json={"device_id": did, "hardware": hw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_new"] is True and body["device_id"] == did
    assert body["settings"]["update_channel"] == "stable"

    r = db_client.post("/v1/devices/register", json={"device_id": did, "hardware": hw})
    assert r.json()["is_new"] is False

    row = _sql(
        "SELECT gpu_model_norm, ram_gb_bucket, last_ip_hash FROM devices WHERE id = $1",
        uuid.UUID(did),
    )
    assert row["gpu_model_norm"] == "rtx 5070 ti"
    assert row["ram_gb_bucket"] == "32-64"
    assert len(row["last_ip_hash"]) == 64  # hex of a 32-byte peppered HMAC, never an IP

    assert db_client.post(f"/v1/devices/{did}/heartbeat", json={}).json() == {"ok": True}
    assert (
        db_client.post(
            f"/v1/devices/{did}/heartbeat", json={"hardware": {"ram_gb": 64}}
        ).status_code
        == 200
    )

    new = {
        "telemetry_opt_in": True,
        "sync_opt_in": False,
        "news_opt_in": False,
        "update_channel": "insiders",
    }
    assert db_client.put(f"/v1/devices/{did}/settings", json=new).json() == new
    assert db_client.get(f"/v1/devices/{did}/settings").json() == new

    bad = dict(new, update_channel="nightly")
    r = db_client.put(f"/v1/devices/{did}/settings", json=bad)
    assert r.status_code == 400 and r.json()["ok"] is False


def test_unknown_device_is_404(db_client):
    did = uuid.uuid4()
    assert db_client.post(f"/v1/devices/{did}/heartbeat", json={}).status_code == 404
    assert db_client.get(f"/v1/devices/{did}/settings").status_code == 404


# ── Licenses ────────────────────────────────────────────────────────────
def test_license_activate_validate_deactivate_public(db_client):
    key, lic_id = _mint_license()
    tag = uuid.uuid4().hex[:8]
    buckets = {
        "gpu_model_norm": "rtx 5070 ti",
        "cpu_family": "amd ryzen 7",
        "ram_gb_bucket": "32-64",
    }

    r = db_client.post(
        "/v1/licenses/activate", json={"license_key": key, "hardware": _fp(tag), "buckets": buckets}
    )
    assert r.status_code == 200, r.text
    act = r.json()
    assert act["license_id"] == lic_id and act["slot_no"] == 1 and act["is_new_slot"] is True

    # Same hardware again → same slot, nothing consumed.
    r = db_client.post("/v1/licenses/activate", json={"license_key": key, "hardware": _fp(tag)})
    assert r.json()["is_new_slot"] is False and r.json()["slots_used"] == 1

    # Key typed with lookalike characters and no dashes still validates.
    sloppy = key.lower().replace("-", "").replace("0", "o")
    r = db_client.post("/v1/licenses/validate", json={"license_key": sloppy, "hardware": _fp(tag)})
    assert r.status_code == 200 and r.json()["status"] == "active"

    # Different hardware that never activated → 403.
    r = db_client.post("/v1/licenses/validate", json={"license_key": key, "hardware": _fp("other")})
    assert r.status_code == 403

    # Public trust endpoint shows buckets only.
    pub = db_client.get(f"/v1/licenses/{lic_id}/public").json()
    assert pub["slots_used"] == 1 and pub["slots"][0]["gpu_model_norm"] == "rtx 5070 ti"
    assert "email" not in pub and "key" not in pub

    r = db_client.post("/v1/licenses/deactivate", json={"license_key": key, "slot_no": 1})
    assert r.status_code == 200 and r.json()["slots_used"] == 0
    assert (
        db_client.post(
            "/v1/licenses/deactivate", json={"license_key": key, "slot_no": 1}
        ).status_code
        == 404
    )


def test_license_slot_cap(db_client):
    from settings import settings

    key, _ = _mint_license()
    for i in range(settings.license_max_devices):
        r = db_client.post(
            "/v1/licenses/activate", json={"license_key": key, "hardware": _fp(f"cap{i}")}
        )
        assert r.status_code == 200, r.text
    r = db_client.post(
        "/v1/licenses/activate", json={"license_key": key, "hardware": _fp("cap-extra")}
    )
    assert r.status_code == 409


def test_unknown_license(db_client):
    r = db_client.post(
        "/v1/licenses/validate", json={"license_key": generate_license_key(), "hardware": _fp("x")}
    )
    assert r.status_code == 404
    assert db_client.get("/v1/licenses/2147483647/public").status_code == 404


def test_revoked_license_cannot_activate(db_client):
    key, lic_id = _mint_license()
    _sql("UPDATE licenses SET status='revoked', revoked_at=now() WHERE id=$1", lic_id)
    r = db_client.post("/v1/licenses/activate", json={"license_key": key, "hardware": _fp("rv")})
    assert r.status_code == 403


# ── Trials ──────────────────────────────────────────────────────────────
def test_trial_lifecycle(db_client):
    tag = uuid.uuid4().hex[:8]

    assert (
        db_client.post("/v1/trials/status", json={"hardware": _fp(tag)}).json()["state"] == "never"
    )

    r = db_client.post("/v1/trials/start", json={"hardware": _fp(tag), "app_version": "3.6.0"})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["state"] == "active" and first["seconds_left"] > 0

    again = db_client.post("/v1/trials/start", json={"hardware": _fp(tag)}).json()
    assert again["state"] == "active" and again["started_at"] == first["started_at"]

    status = db_client.post("/v1/trials/status", json={"hardware": _fp(tag)}).json()
    assert status["state"] == "active" and status["expires_at"] == first["expires_at"]


def test_trial_blocked_for_vm(db_client):
    r = db_client.post(
        "/v1/trials/start", json={"hardware": _fp(uuid.uuid4().hex[:8]), "vm_detected": True}
    )
    assert r.status_code == 200
    assert r.json()["state"] == "blocked" and r.json()["block_reason"] == "virtual_machine"


def test_trial_expired(db_client):
    tag = uuid.uuid4().hex[:8]
    db_client.post("/v1/trials/start", json={"hardware": _fp(tag)})
    from security import hash_fingerprint

    fp = hash_fingerprint(f"CPU-{tag}", f"BOARD-{tag}", f"DISK-{tag}")
    _sql("UPDATE trials SET expires_at = now() - interval '1 day' WHERE device_fp_hash = $1", fp)
    r = db_client.post("/v1/trials/status", json={"hardware": _fp(tag)})
    assert r.json()["state"] == "expired" and r.json()["ok"] is False


@pytest.mark.parametrize(
    "path",
    ["/v1/profiles/_status", "/v1/oc/_status", "/v1/telemetry/_status", "/v1/playtime/_status"],
)
def test_stub_routers_respond(db_client, path):
    assert db_client.get(path).json()["implemented"] is False
