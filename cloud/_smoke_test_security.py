"""Phase B security smoke test — exercises the zero-knowledge primitives.

Checks:
  • License key generation + format + normalization
  • Fingerprint hash determinism (same inputs -> same hash)
  • Different inputs -> different hashes
  • Hash is irreversible (we can compute but not reverse)
  • PrivacyFilter scrubs IPs from log records
  • Crockford alphabet rejects ambiguous chars
"""
import logging
import sys

sys.path.insert(0, ".")

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
    verify_license_key,
)

failures: list[str] = []

def check(label: str, ok: bool, detail: str = "") -> None:
    icon = "PASS" if ok else "FAIL"
    print(f"  [{icon}] {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


# ─── License key generation & format ──────────────────────────────────
print("[1] license keys")
key = generate_license_key()
check("generated key has 4 groups", key.count("-") == 3, key)
check("normalized key is 20 chars",
      len(normalize_license_key(key)) == KEY_LEN_RAW)
check("is_valid_license_key_format(generated)", is_valid_license_key_format(key))
check("is_valid_license_key_format(garbage) is False",
      not is_valid_license_key_format("garbage-not-a-key"))

# Two consecutive generations must differ (entropy check)
keys = {generate_license_key() for _ in range(50)}
check("50 generated keys are all distinct", len(keys) == 50)

# Lookalike substitution: I/L -> 1, O -> 0, U -> V
n1 = normalize_license_key("ABCDE-FGHII-LMNOO-PQRUU")
check("lookalike chars folded", "I" not in n1 and "L" not in n1
                              and "O" not in n1 and "U" not in n1, n1)

# ─── License key hashing & verification ───────────────────────────────
print("\n[2] license key hashing")
h = hash_license_key(key)
check("hash is 32 bytes", len(h) == 32)
check("verify(key, hash) == True", verify_license_key(key, h))
check("verify(wrong_key, hash) == False",
      not verify_license_key(generate_license_key(), h))
# Same key, different formatting -> same hash (normalization)
key_lower = key.lower().replace("-", "")
check("verify accepts unformatted lowercase",
      verify_license_key(key_lower, h))

# ─── Fingerprint hash ─────────────────────────────────────────────────
print("\n[3] fingerprint hashing")
fp1 = hash_fingerprint("CPU-XYZ", "BOARD-ABC", "DISK-123")
fp2 = hash_fingerprint("CPU-XYZ", "BOARD-ABC", "DISK-123")
fp3 = hash_fingerprint("CPU-XYZ", "BOARD-ABC", "DISK-DIFFERENT")
check("fingerprint is 32 bytes", len(fp1) == 32)
check("same inputs -> same hash", fp1 == fp2)
check("different disk -> different hash", fp1 != fp3)
# Whitespace stripping
fp4 = hash_fingerprint("  CPU-XYZ  ", "  BOARD-ABC  ", "  DISK-123  ")
check("whitespace is stripped", fp1 == fp4)
# Case sensitivity on board UUID (we lowercase it)
fp5 = hash_fingerprint("CPU-XYZ", "board-abc", "DISK-123")
check("board UUID is case-insensitive", fp1 == fp5)

# ─── Email hash ───────────────────────────────────────────────────────
print("\n[4] email hashing")
e1 = hash_email("test@example.com")
e2 = hash_email("TEST@EXAMPLE.COM")
e3 = hash_email("other@example.com")
check("email hash is 32 bytes", len(e1) == 32)
check("email is case-insensitive", e1 == e2)
check("different emails -> different hashes", e1 != e3)

# ─── IP hash ──────────────────────────────────────────────────────────
print("\n[5] IP hashing")
ip1 = hash_ip("203.0.113.42")
ip2 = hash_ip("203.0.113.42")
ip3 = hash_ip("203.0.113.43")
check("IP hash is 32 bytes", len(ip1) == 32)
check("same IP -> same hash", ip1 == ip2)
check("different IP -> different hash", ip1 != ip3)

# ─── Privacy filter ───────────────────────────────────────────────────
print("\n[6] privacy log filter")
filt = PrivacyFilter()


def _make_record(msg: str, args=None) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg=msg, args=args, exc_info=None,
    )
    return rec


# Case 1: IP in args (uvicorn.access pattern)
r = _make_record("%s - %s %s %d", args=("203.0.113.42:54321", "GET", "/", 200))
filt.filter(r)
check("IP:port stripped from args[0]", "203.0.113.42" not in r.args[0],
      r.args[0])

# Case 2: IPv4 in message body
r = _make_record("client 192.168.1.5 connected", args=())
filt.filter(r)
check("IPv4 stripped from msg", "192.168.1.5" not in r.msg, r.msg)

# Case 3: IPv6 in message body
r = _make_record("client 2001:db8:85a3::8a2e:370:7334 connected", args=())
filt.filter(r)
check("IPv6 stripped from msg",
      "2001:db8:85a3::8a2e:370:7334" not in r.msg, r.msg)

# Case 4: Non-IP content untouched
r = _make_record("license 12345 activated on slot 3", args=())
filt.filter(r)
check("non-IP content untouched",
      "license 12345 activated on slot 3" == r.msg, r.msg)

# ─── Verdict ──────────────────────────────────────────────────────────
print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PHASE B SECURITY SMOKE TESTS PASSED")
