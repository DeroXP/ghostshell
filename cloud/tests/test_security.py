"""Zero-knowledge primitives: license keys, peppered hashes, privacy filter."""

from __future__ import annotations

import logging

from security import (
    KEY_LEN_RAW,
    PrivacyFilter,
    generate_license_key,
    hash_email,
    hash_fingerprint,
    hash_ip,
    hash_license_key,
    is_valid_license_key_format,
    normalize_license_key,
    using_insecure_dev_peppers,
    verify_license_key,
)


def test_test_environment_uses_real_peppers():
    # conftest sets four random peppers; the insecure fallback must not be active.
    assert not using_insecure_dev_peppers()


class TestLicenseKeys:
    def test_format(self):
        key = generate_license_key()
        assert key.count("-") == 3
        assert len(normalize_license_key(key)) == KEY_LEN_RAW
        assert is_valid_license_key_format(key)

    def test_garbage_rejected(self):
        assert not is_valid_license_key_format("garbage-not-a-key")
        assert not is_valid_license_key_format("")

    def test_entropy(self):
        assert len({generate_license_key() for _ in range(100)}) == 100

    def test_lookalikes_folded(self):
        n = normalize_license_key("ABCDE-FGHII-LMNOO-PQRUU")
        assert not set("ILOU") & set(n)

    def test_hash_and_verify(self):
        key = generate_license_key()
        h = hash_license_key(key)
        assert len(h) == 32
        assert verify_license_key(key, h)
        assert verify_license_key(key.lower().replace("-", ""), h)
        assert not verify_license_key(generate_license_key(), h)
        assert not verify_license_key("", h)
        assert not verify_license_key(key, b"")


class TestFingerprint:
    def test_deterministic_and_distinct(self):
        a = hash_fingerprint("CPU-XYZ", "BOARD-ABC", "DISK-123")
        assert len(a) == 32
        assert a == hash_fingerprint("CPU-XYZ", "BOARD-ABC", "DISK-123")
        assert a != hash_fingerprint("CPU-XYZ", "BOARD-ABC", "DISK-DIFFERENT")

    def test_normalisation(self):
        a = hash_fingerprint("CPU-XYZ", "BOARD-ABC", "DISK-123")
        assert a == hash_fingerprint("  CPU-XYZ ", " BOARD-ABC ", " DISK-123 ")
        assert a == hash_fingerprint("CPU-XYZ", "board-abc", "DISK-123")


def test_email_hash():
    assert hash_email("test@example.com") == hash_email("TEST@EXAMPLE.COM")
    assert hash_email("test@example.com") != hash_email("other@example.com")
    assert len(hash_email("x@y.z")) == 32


def test_ip_hash():
    assert hash_ip("203.0.113.42") == hash_ip("203.0.113.42")
    assert hash_ip("203.0.113.42") != hash_ip("203.0.113.43")
    assert len(hash_ip("::1")) == 32


def _record(msg: str, args=()) -> logging.LogRecord:
    return logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0, msg=msg, args=args, exc_info=None
    )


class TestPrivacyFilter:
    f = PrivacyFilter()

    def test_scrubs_uvicorn_access_args(self):
        r = _record("%s - %s %s %d", ("203.0.113.42:54321", "GET", "/", 200))
        self.f.filter(r)
        assert "203.0.113.42" not in r.args[0]

    def test_scrubs_ipv4_in_message(self):
        r = _record("client 192.168.1.5 connected")
        self.f.filter(r)
        assert "192.168.1.5" not in r.msg

    def test_scrubs_ipv6_in_message(self):
        r = _record("client 2001:db8:85a3::8a2e:370:7334 connected")
        self.f.filter(r)
        assert "2001:db8:85a3::8a2e:370:7334" not in r.msg

    def test_leaves_other_text_alone(self):
        r = _record("license 12345 activated on slot 3")
        self.f.filter(r)
        assert r.msg == "license 12345 activated on slot 3"
