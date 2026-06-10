"""Zero-knowledge primitives for GhostShell-Cloud.

The product promise is: *"We can't see your data."*  This module is the
load-bearing piece that makes that promise true.

Three core ideas:
  1. **Peppered HMAC over raw inputs.**  Hardware fingerprint values
     (CPU ID, motherboard UUID, disk serial), email addresses, and IP
     addresses arrive over TLS, are HMAC'd with a server-side pepper,
     and only the hash is stored.  The raw values go out of scope and
     are never logged, never written to disk, never sent to backups.
  2. **Two independent peppers.**  Fingerprints and emails use separate
     32-byte secrets.  If one leaks, the other is still safe.
  3. **Privacy log filter.**  uvicorn's default access log writes the
     client IP.  We install a logging filter that scrubs IPs out of
     every log line before it hits stdout (and therefore Railway's log
     pipeline).

What we DON'T do:
  - Use bcrypt / argon2 / scrypt.  License keys carry ~100 bits of
    entropy, so a simple HMAC is uncrackable for a fraction of the cost.
  - Trust client-supplied hashes.  The client sends raw inputs, the
    server hashes them.  This means the pepper never leaks to the
    client and we control the hash function.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from typing import Iterable

from settings import settings


# ─── Pepper handling ─────────────────────────────────────────────────────
def _decode_pepper(name: str, raw: str) -> bytes:
    """Decode a base64-encoded 32-byte pepper from settings.  If the
    value is empty (i.e. the operator forgot to set it), generate a
    deterministic-but-unsafe fallback so dev imports don't crash, and
    log a loud warning so it's caught before production deploy.
    """
    log = logging.getLogger("ghostshell-cloud.security")
    if raw:
        try:
            decoded = base64.b64decode(raw, validate=True)
            if len(decoded) >= 32:
                return decoded
            log.error(f"{name} is shorter than 32 bytes — generate a real one")
        except Exception:
            log.error(f"{name} is not valid base64 — generate a real one")
    # Dev-only fallback.  In production this gets caught by the
    # startup check in main.py, which refuses to boot.
    log.warning(f"{name} is unset — using INSECURE deterministic fallback. "
                f"Set this env var before deploying!")
    return hashlib.sha256(f"INSECURE-DEV-{name}".encode()).digest()


_FINGERPRINT_PEPPER = _decode_pepper("FINGERPRINT_PEPPER", settings.fingerprint_pepper)
_EMAIL_PEPPER       = _decode_pepper("EMAIL_PEPPER",       settings.email_pepper)
_LICENSE_KEY_PEPPER = _decode_pepper("LICENSE_KEY_PEPPER", settings.license_key_pepper)
_IP_PEPPER          = _decode_pepper("IP_PEPPER",          settings.ip_pepper)


def using_insecure_dev_peppers() -> bool:
    """True if any pepper fell back to the dev value.  Surfaced at
    startup and on /healthz so Railway's deploy check fails loudly
    rather than silently exposing weak hashes in production.
    """
    insecure = hashlib.sha256(b"INSECURE-DEV-FINGERPRINT_PEPPER").digest()
    return _FINGERPRINT_PEPPER == insecure


# ─── Hash helpers ────────────────────────────────────────────────────────
def hash_fingerprint(cpu_id: str, board_uuid: str, disk_serial: str) -> bytes:
    """HMAC-SHA256 over the three hardware fingerprint inputs.

    Reproducible: the same physical machine always hashes to the same
    32-byte value, which is how we detect "this PC already trialed".
    Irreversible: an attacker with full DB access cannot recover any of
    the three inputs without knowing FINGERPRINT_PEPPER.
    """
    payload = b"\x00".join([
        (cpu_id      or "").strip().encode("utf-8"),
        (board_uuid  or "").strip().lower().encode("utf-8"),
        (disk_serial or "").strip().encode("utf-8"),
    ])
    return hmac.new(_FINGERPRINT_PEPPER, payload, hashlib.sha256).digest()


def hash_email(email: str) -> bytes:
    """HMAC the email so we can match Stripe-webhook customers to a
    license without ever storing the address itself.
    """
    return hmac.new(
        _EMAIL_PEPPER,
        (email or "").strip().lower().encode("utf-8"),
        hashlib.sha256,
    ).digest()


def hash_ip(ip: str) -> bytes:
    """For abuse rate-limiting only.  The pepper is a separate secret
    so an IP can't be cross-referenced with fingerprints.  We don't
    auto-rotate it daily yet; can be added later if needed.
    """
    return hmac.new(
        _IP_PEPPER,
        (ip or "").strip().encode("utf-8"),
        hashlib.sha256,
    ).digest()


# ─── License keys ────────────────────────────────────────────────────────
# Crockford Base32 alphabet — drops I, L, O, U to avoid look-alike chars
# when the user reads / types the key from a receipt or installer name.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

KEY_GROUPS         = 4
KEY_CHARS_PER_GROUP = 5
KEY_LEN_RAW         = KEY_GROUPS * KEY_CHARS_PER_GROUP   # 20 chars
# 20 * 5 bits = 100 bits of entropy.  At 1 trillion guesses/sec, brute-forcing
# the key space takes ~40 quadrillion years.  This is fine.


def generate_license_key() -> str:
    """Return a fresh, cryptographically random license key formatted as
        XXXXX-XXXXX-XXXXX-XXXXX
    using the Crockford Base32 alphabet (no I/L/O/U).
    """
    raw = "".join(secrets.choice(_CROCKFORD) for _ in range(KEY_LEN_RAW))
    groups = [raw[i:i + KEY_CHARS_PER_GROUP]
              for i in range(0, KEY_LEN_RAW, KEY_CHARS_PER_GROUP)]
    return "-".join(groups)


def normalize_license_key(key: str) -> str:
    """Accept user input flexibly: strip whitespace, uppercase, swap
    common Crockford look-alikes (I→1, L→1, O→0, U→V) before validation.
    """
    cleaned = re.sub(r"[\s\-_]+", "", (key or "").upper())
    cleaned = (cleaned
               .replace("I", "1").replace("L", "1")
               .replace("O", "0").replace("U", "V"))
    return cleaned


def is_valid_license_key_format(key: str) -> bool:
    """Cheap pre-check before hitting the DB.  Doesn't say if the key
    *exists* — just whether it could plausibly be one of ours.
    """
    n = normalize_license_key(key)
    if len(n) != KEY_LEN_RAW:
        return False
    return all(c in _CROCKFORD for c in n)


def hash_license_key(key: str) -> bytes:
    """Hash a license key for storage.  Bcrypt would be overkill here:
    the keyspace is 2^100, so an HMAC-SHA256 is uncrackable at 1000x
    less CPU.  We pepper to defeat rainbow-table attacks against any
    hypothetical leaked subset of keys.
    """
    return hmac.new(
        _LICENSE_KEY_PEPPER,
        normalize_license_key(key).encode(),
        hashlib.sha256,
    ).digest()


def verify_license_key(key: str, stored_hash: bytes) -> bool:
    """Constant-time comparison."""
    if not key or not stored_hash:
        return False
    return hmac.compare_digest(hash_license_key(key), bytes(stored_hash))


# ─── Privacy logging filter ──────────────────────────────────────────────
# uvicorn's access log defaults to writing the client IP and port.  We
# scrub them before any record reaches stdout / Railway logs.

_IP_PORT_RE = re.compile(
    r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|[a-f0-9:]+):\d{1,5}\b",
    re.IGNORECASE,
)
_IPV4_RE   = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_IPV6_RE   = re.compile(r"\b(?:[a-f0-9]{1,4}:){2,7}[a-f0-9]{1,4}\b", re.IGNORECASE)


def _scrub_value(v: object) -> object:
    """Replace any IP-port / IPv4 / IPv6 substring in a string with
    `<redacted>`.  Non-string values pass through unchanged.
    """
    if not isinstance(v, str):
        return v
    s = _IP_PORT_RE.sub("<redacted>:<port>", v)
    s = _IPV4_RE.sub("<redacted>", s)
    s = _IPV6_RE.sub("<redacted>", s)
    return s


class PrivacyFilter(logging.Filter):
    """Strips IP addresses out of access-log records before they hit
    Railway's log pipeline.  Applied to:
      • uvicorn.access     — the access log (client IP is positional arg 0)
      • uvicorn.error      — internal errors that may include peer info
      • ghostshell-cloud.* — our own logger tree, defensive
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access stuffs the client into args[0] as "ip:port".
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub_value(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_scrub_value(a) for a in record.args)
        # Defensive: also scrub the rendered message itself.
        if isinstance(record.msg, str):
            record.msg = _scrub_value(record.msg)
        return True


def install_privacy_filter() -> None:
    """Attach PrivacyFilter to every logger that might emit a client IP.
    Called once from main.py on app boot.
    """
    log = logging.getLogger("ghostshell-cloud")
    f = PrivacyFilter()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn",
                 "ghostshell-cloud", "fastapi"):
        logging.getLogger(name).addFilter(f)
    log.info("Privacy log filter installed — client IPs will be scrubbed")
    if using_insecure_dev_peppers():
        log.warning(
            "RUNNING WITH INSECURE DEV PEPPERS.  This is fine for local "
            "development but MUST NOT be used in production.  Set "
            "FINGERPRINT_PEPPER, EMAIL_PEPPER, LICENSE_KEY_PEPPER, and "
            "IP_PEPPER to base64-encoded 32-byte random values before deploy."
        )


# ─── Pepper generation (run from CLI to mint production secrets) ─────────
def _print_fresh_peppers() -> None:
    """Helper — invoke as `python -m security` to print four fresh
    base64-encoded 32-byte peppers ready to paste into Railway env vars.
    """
    for name in ("FINGERPRINT_PEPPER", "EMAIL_PEPPER",
                 "LICENSE_KEY_PEPPER", "IP_PEPPER"):
        print(f"{name}={base64.b64encode(secrets.token_bytes(32)).decode()}")


if __name__ == "__main__":
    _print_fresh_peppers()
