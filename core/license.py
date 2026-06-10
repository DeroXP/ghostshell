"""Client-side license + trial state machine.

This is the gate between "you can use GhostShell" and "you can't."  It:
  • Captures hardware fingerprint inputs on demand (via core.hwfp)
  • Talks to the cloud for activation, validation, and trial state
  • Persists the result to a DPAPI-encrypted cache so we work offline
  • Implements 14-day grace mode if the cloud is unreachable

States returned to the UI (the only thing app.py needs to read):
  UNKNOWN          — first launch, haven't checked yet
  DEV_BYPASS       — no cloud_url configured (developer mode)
  NEVER_TRIALED    — server says no trial exists for this fingerprint
  TRIAL_ACTIVE     — trial running, has expiry
  TRIAL_EXPIRED    — trial elapsed, no purchase → buy splash
  TRIAL_BLOCKED    — VPN/VM detected at trial start
  LICENSED         — has a valid paid license
  LICENSE_REFUNDED — paid license was refunded → revert to read-only
  LICENSE_REVOKED  — paid license was admin-revoked
  OFFLINE_GRACE    — cloud unreachable but cache is recent enough
  OFFLINE_LOCKED   — cloud unreachable AND cache is stale → buy splash
  ERROR            — something else broke; treated like OFFLINE_LOCKED

`is_entitled(state)` is True only for: TRIAL_ACTIVE, LICENSED,
OFFLINE_GRACE, DEV_BYPASS.

The rest of the app reads `is_entitled()` and either runs normally or
shows the buy splash.  Entitlement is the only thing app.py needs from
this module.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from config import APPDATA_DIR
from core import anti_vm, hwfp
from core.cloud_client import _load_settings as _cc_settings
from core.utils import get_logger

log = get_logger("license")


# ─── Constants ───────────────────────────────────────────────────────────
LICENSE_CACHE_PATH = os.path.join(APPDATA_DIR, "license.dat")

# How long a cached LICENSED result is good for if cloud is unreachable.
GRACE_PERIOD_SEC = 14 * 24 * 3600

# How often to validate while running.
VALIDATE_INTERVAL_SEC = 6 * 3600

# HTTP timeouts.
ACTIVATE_TIMEOUT_SEC = 15
VALIDATE_TIMEOUT_SEC = 8
TRIAL_TIMEOUT_SEC    = 12


# ─── State enum ──────────────────────────────────────────────────────────
class LicenseState(str, Enum):
    UNKNOWN          = "unknown"
    DEV_BYPASS       = "dev_bypass"
    NEVER_TRIALED    = "never_trialed"
    TRIAL_ACTIVE     = "trial_active"
    TRIAL_EXPIRED    = "trial_expired"
    TRIAL_BLOCKED    = "trial_blocked"
    LICENSED         = "licensed"
    LICENSE_REFUNDED = "license_refunded"
    LICENSE_REVOKED  = "license_revoked"
    OFFLINE_GRACE    = "offline_grace"
    OFFLINE_LOCKED   = "offline_locked"
    ERROR            = "error"


_ENTITLED = {
    LicenseState.DEV_BYPASS,
    LicenseState.TRIAL_ACTIVE,
    LicenseState.LICENSED,
    LicenseState.OFFLINE_GRACE,
}


def is_entitled(state: LicenseState | str) -> bool:
    if isinstance(state, str):
        try:
            state = LicenseState(state)
        except ValueError:
            return False
    return state in _ENTITLED


# ─── DPAPI roundtrip (Windows only, no extra deps) ───────────────────────
class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_call(fn_name: str, blob: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    fn = getattr(crypt32, fn_name)
    fn.restype = wintypes.BOOL

    in_buf = ctypes.create_string_buffer(blob, len(blob))
    in_blob = _DataBlob(len(blob), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()

    success = fn(
        ctypes.byref(in_blob),
        None,                   # description
        None,                   # entropy
        None,                   # reserved
        None,                   # prompt struct
        0,                      # flags
        ctypes.byref(out_blob),
    )
    if not success:
        err = ctypes.get_last_error()
        raise OSError(f"{fn_name} failed (GetLastError={err})")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_protect(plaintext: bytes) -> bytes:
    return _dpapi_call("CryptProtectData", plaintext)


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    return _dpapi_call("CryptUnprotectData", ciphertext)


# ─── Cache file ──────────────────────────────────────────────────────────
def _load_cache() -> dict[str, Any]:
    try:
        if not os.path.exists(LICENSE_CACHE_PATH):
            return {}
        with open(LICENSE_CACHE_PATH, "rb") as f:
            ciphertext = f.read()
        if not ciphertext:
            return {}
        plaintext = _dpapi_unprotect(ciphertext)
        return json.loads(plaintext.decode("utf-8"))
    except Exception as e:
        log.warning(f"Could not load license cache ({e}); starting fresh")
        return {}


def _save_cache(data: dict[str, Any]) -> None:
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        plaintext = json.dumps(data).encode("utf-8")
        ciphertext = _dpapi_protect(plaintext)
        # Atomic write so a crash mid-save doesn't corrupt the cache.
        tmp = LICENSE_CACHE_PATH + ".tmp"
        with open(tmp, "wb") as f:
            f.write(ciphertext)
        os.replace(tmp, LICENSE_CACHE_PATH)
    except Exception as e:
        log.warning(f"Could not save license cache: {e}")


# ─── HTTP wrapper ────────────────────────────────────────────────────────
def _cloud_url() -> str:
    return (_cc_settings().get("cloud_url") or "").rstrip("/")


def _post(path: str, body: dict, timeout: float) -> dict:
    url = _cloud_url()
    if not url:
        return {"ok": False, "status": 0, "data": None, "err": "no cloud_url"}
    full = url + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        full, data=data, method="POST",
        headers={
            "Accept":       "application/json",
            "Content-Type": "application/json",
            "User-Agent":   "GhostShell-Client/license",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return {"ok": 200 <= resp.status < 300, "status": resp.status,
                    "data": json.loads(raw), "err": ""}
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return {"ok": False, "status": e.code, "data": None,
                "err": f"HTTP {e.code}: {err_body[:300]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "status": 0, "data": None,
                "err": f"network: {e.reason}"}
    except Exception as e:
        return {"ok": False, "status": 0, "data": None,
                "err": f"{type(e).__name__}: {e}"}


# ─── License manager ─────────────────────────────────────────────────────
class LicenseManager:
    """The single source of truth for licensing state on the client.

    Thread-safe.  Kept as a singleton via `get_manager()`.  The UI calls:
      • `manager.refresh()`      on app launch + periodically
      • `manager.activate(key)`  when the user pastes a license key
      • `manager.start_trial()`  when the user clicks "Try free for 7 days"
      • `manager.deactivate(slot)` from the in-app device manager
      • `manager.state` / `manager.is_entitled()` for gating
    """

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._cache = _load_cache()
        # Last-known state.  Will be UNKNOWN until refresh() runs once.
        self.state: LicenseState = LicenseState.UNKNOWN
        # Bookkeeping for the UI banner.
        self.last_validated: datetime | None = None
        self.trial_expires:  datetime | None = None
        self.license_id:     int  | None = None
        self.slot_no:        int  | None = None
        self.slots_used:     int  | None = None
        self.slots_max:      int  | None = None
        # Apply cached state immediately so the UI has something to show.
        self._apply_cache()

    # ── helpers ──────────────────────────────────────────────────────
    def _apply_cache(self) -> None:
        c = self._cache
        try:
            if c.get("state"):
                self.state = LicenseState(c["state"])
            if c.get("trial_expires"):
                self.trial_expires = datetime.fromisoformat(c["trial_expires"])
            if c.get("last_validated"):
                self.last_validated = datetime.fromisoformat(c["last_validated"])
            self.license_id = c.get("license_id")
            self.slot_no    = c.get("slot_no")
            self.slots_used = c.get("slots_used")
            self.slots_max  = c.get("slots_max")
        except Exception as e:
            log.warning(f"License cache had invalid fields ({e}); ignoring")

    def _persist(self) -> None:
        self._cache = {
            "state":          self.state.value,
            "license_id":     self.license_id,
            "slot_no":        self.slot_no,
            "slots_used":     self.slots_used,
            "slots_max":      self.slots_max,
            "trial_expires":  self.trial_expires.isoformat() if self.trial_expires else None,
            "last_validated": self.last_validated.isoformat() if self.last_validated else None,
            # NB: license_key persists too, encrypted alongside everything
            # else.  See activate().
            "license_key":    self._cache.get("license_key"),
        }
        _save_cache(self._cache)

    def is_entitled(self) -> bool:
        return is_entitled(self.state)

    def has_cloud(self) -> bool:
        return bool(_cloud_url())

    # ── core actions ─────────────────────────────────────────────────
    def refresh(self) -> LicenseState:
        """Update state by talking to the cloud.

        Decision tree:
          - No cloud_url → DEV_BYPASS (lets devs run the app locally)
          - Have cached license_key → /v1/licenses/validate
          - No cached license_key → /v1/trials/status
          - Cloud unreachable → OFFLINE_GRACE if cache is fresh, else OFFLINE_LOCKED
        """
        with self._lock:
            if not self.has_cloud():
                self.state = LicenseState.DEV_BYPASS
                return self.state

            fp = hwfp.collect()
            cached_key = (self._cache.get("license_key") or "").strip()

            if cached_key:
                state = self._validate_paid(cached_key, fp)
            else:
                state = self._refresh_trial(fp)

            self.state = state
            self._persist()
            return state

    def _validate_paid(self, key: str, fp: dict) -> LicenseState:
        r = _post("/v1/licenses/validate", {
            "license_key": key,
            "hardware":    fp,
        }, timeout=VALIDATE_TIMEOUT_SEC)

        if r["ok"] and r.get("data"):
            d = r["data"]
            self.license_id = d.get("license_id")
            self.slot_no    = d.get("slot_no")
            self.last_validated = datetime.now(timezone.utc)
            status = d.get("status", "active")
            if status == "active":
                return LicenseState.LICENSED
            if status == "refunded":
                return LicenseState.LICENSE_REFUNDED
            if status == "revoked":
                return LicenseState.LICENSE_REVOKED
            return LicenseState.ERROR

        # Cloud said no — could be wrong key, removed slot, or net error.
        if r.get("status") in (403, 404):
            log.warning(f"License rejected by cloud ({r['err']}); "
                        "discarding cached key")
            self._cache["license_key"] = None
            return LicenseState.ERROR

        # Network failure → check grace
        return self._grace_or_locked()

    def _refresh_trial(self, fp: dict) -> LicenseState:
        r = _post("/v1/trials/status", {"hardware": fp},
                  timeout=TRIAL_TIMEOUT_SEC)
        if not r["ok"] or not r.get("data"):
            return self._grace_or_locked()
        d = r["data"]
        s = (d.get("state") or "unknown").lower()
        try:
            self.trial_expires = (
                datetime.fromisoformat(d["expires_at"]) if d.get("expires_at") else None
            )
        except Exception:
            self.trial_expires = None
        self.last_validated = datetime.now(timezone.utc)

        if s == "never":
            return LicenseState.NEVER_TRIALED
        if s == "active":
            return LicenseState.TRIAL_ACTIVE
        if s == "expired":
            return LicenseState.TRIAL_EXPIRED
        if s == "blocked":
            return LicenseState.TRIAL_BLOCKED
        if s == "converted":
            # The fingerprint converted to a license, but we don't have
            # the key locally — user needs to re-enter it.
            return LicenseState.NEVER_TRIALED
        return LicenseState.ERROR

    def _grace_or_locked(self) -> LicenseState:
        """Cloud unreachable.  Decide based on cache age."""
        if not self.last_validated:
            return LicenseState.OFFLINE_LOCKED
        age = (datetime.now(timezone.utc) - self.last_validated).total_seconds()
        if age < GRACE_PERIOD_SEC and self._cache.get("license_key"):
            return LicenseState.OFFLINE_GRACE
        return LicenseState.OFFLINE_LOCKED

    # ── user-initiated actions ───────────────────────────────────────
    def activate(self, license_key: str) -> dict:
        """User pasted a license key (or the pre-activated installer
        provided one).  Sends to cloud, persists on success.
        """
        with self._lock:
            if not self.has_cloud():
                return {"ok": False, "err": "no cloud_url configured"}

            fp = hwfp.collect()
            buckets = self._buckets_from_fp(fp)

            r = _post("/v1/licenses/activate", {
                "license_key": license_key,
                "hardware":    fp,
                "buckets":     buckets,
            }, timeout=ACTIVATE_TIMEOUT_SEC)

            if not r["ok"]:
                log.warning(f"Activation failed: {r['err']}")
                return {"ok": False, "err": r["err"], "status": r.get("status", 0)}

            d = r.get("data") or {}
            self._cache["license_key"] = license_key
            self.license_id = d.get("license_id")
            self.slot_no    = d.get("slot_no")
            self.slots_used = d.get("slots_used")
            self.slots_max  = d.get("slots_max")
            self.last_validated = datetime.now(timezone.utc)
            self.state = LicenseState.LICENSED
            self._persist()
            log.info(f"Activated license {self.license_id} on slot {self.slot_no} "
                     f"({self.slots_used}/{self.slots_max})")
            return {"ok": True, **d}

    def start_trial(self) -> dict:
        """Mint a 7-day trial for this hardware.  Server-enforced."""
        with self._lock:
            if not self.has_cloud():
                return {"ok": False, "err": "no cloud_url configured"}

            fp = hwfp.collect()
            buckets = self._buckets_from_fp(fp)
            vm = anti_vm.detect()

            r = _post("/v1/trials/start", {
                "hardware":    fp,
                "buckets":     buckets,
                "vm_detected": vm["is_vm"],
            }, timeout=TRIAL_TIMEOUT_SEC)

            if not r["ok"]:
                return {"ok": False, "err": r["err"], "status": r.get("status", 0)}

            d = r.get("data") or {}
            s = (d.get("state") or "unknown").lower()
            try:
                self.trial_expires = (
                    datetime.fromisoformat(d["expires_at"]) if d.get("expires_at") else None
                )
            except Exception:
                self.trial_expires = None
            self.last_validated = datetime.now(timezone.utc)

            if s == "active":
                self.state = LicenseState.TRIAL_ACTIVE
            elif s == "blocked":
                self.state = LicenseState.TRIAL_BLOCKED
            elif s == "expired":
                self.state = LicenseState.TRIAL_EXPIRED
            elif s == "converted":
                self.state = LicenseState.NEVER_TRIALED
            else:
                self.state = LicenseState.ERROR

            self._persist()
            log.info(f"Trial start → state={self.state.value}")
            return {"ok": True, **d}

    def deactivate(self, slot_no: int) -> dict:
        """Free one of the user's own slots."""
        with self._lock:
            key = (self._cache.get("license_key") or "").strip()
            if not key:
                return {"ok": False, "err": "no license cached"}
            r = _post("/v1/licenses/deactivate", {
                "license_key": key,
                "slot_no":     slot_no,
            }, timeout=ACTIVATE_TIMEOUT_SEC)
            if r["ok"]:
                d = r.get("data") or {}
                self.slots_used = d.get("slots_used", self.slots_used)
                self._persist()
            return {"ok": r["ok"], **(r.get("data") or {}), "err": r.get("err", "")}

    def forget(self) -> None:
        """Clear local state (for testing or 'sign me out').  Cloud
        retains the license — re-enter the key to come back.
        """
        with self._lock:
            self._cache = {}
            self.state = LicenseState.UNKNOWN
            self.last_validated = None
            self.trial_expires  = None
            self.license_id = None
            self.slot_no    = None
            self.slots_used = None
            self.slots_max  = None
            try:
                if os.path.exists(LICENSE_CACHE_PATH):
                    os.remove(LICENSE_CACHE_PATH)
            except Exception as e:
                log.warning(f"Could not delete license cache: {e}")

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _buckets_from_fp(_fp: dict) -> dict:
        """Reuse the same bucket logic as core.device_id but pulled in
        lazily to avoid a hard import dependency.  Falls back to {}."""
        try:
            from core.device_id import collect_hardware_fingerprint
            hw = collect_hardware_fingerprint()
            return {
                "gpu_model_norm": _norm_gpu(hw.get("gpu_model")),
                "cpu_family":     hw.get("cpu_family"),
                "ram_gb_bucket":  _ram_bucket(hw.get("ram_gb")),
            }
        except Exception:
            return {}

    def status(self) -> dict:
        """Snapshot of state for the UI / Settings panel."""
        return {
            "state":          self.state.value,
            "entitled":       self.is_entitled(),
            "license_id":     self.license_id,
            "slot_no":        self.slot_no,
            "slots_used":     self.slots_used,
            "slots_max":      self.slots_max,
            "trial_expires":  self.trial_expires.isoformat() if self.trial_expires else None,
            "last_validated": self.last_validated.isoformat() if self.last_validated else None,
            "has_cloud":      self.has_cloud(),
        }


# ─── Bucket helpers (mirror of cloud-side) ───────────────────────────────
def _norm_gpu(name: str | None) -> str | None:
    if not name:
        return None
    n = name.lower().strip()
    for prefix in ("nvidia ", "amd ", "intel ", "geforce "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n.strip() or None


def _ram_bucket(gb: int | None) -> str | None:
    if gb is None:
        return None
    if gb < 16:
        return "<16"
    if gb < 32:
        return "16"
    if gb < 64:
        return "32-64"
    return "64+"


# ─── Singleton ───────────────────────────────────────────────────────────
_manager: LicenseManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> LicenseManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LicenseManager()
        return _manager


# ─── Background validate loop (optional) ─────────────────────────────────
_bg_stop = threading.Event()
_bg_thread: threading.Thread | None = None


def _bg_loop() -> None:
    log.info("license background validate loop started")
    while not _bg_stop.is_set():
        try:
            mgr = get_manager()
            if mgr.has_cloud():
                mgr.refresh()
        except Exception as e:
            log.warning(f"license loop tick failed: {e}")
        if _bg_stop.wait(VALIDATE_INTERVAL_SEC):
            break
    log.info("license background validate loop stopped")


def start_background_loop() -> None:
    """Idempotent.  Call from app startup once you've decided to wire
    licensing into the launch flow."""
    global _bg_thread
    if _bg_thread is not None and _bg_thread.is_alive():
        return
    _bg_stop.clear()
    _bg_thread = threading.Thread(
        target=_bg_loop, name="license-validate-loop", daemon=True,
    )
    _bg_thread.start()


def stop_background_loop() -> None:
    _bg_stop.set()
