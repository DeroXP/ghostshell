"""Anonymous device identity for GhostShell-Cloud.

The cloud backend never sees a user account, an email, or a real name.  The
only thing that ties a request to a "device" is a UUID4 that lives in a
plain text file under %APPDATA%\\GhostShell\\device.id.  If the user nukes
their AppData, they become a brand-new device — no recovery story, by
design.

This module also provides `collect_hardware_fingerprint()` which gathers
non-identifying info (GPU model, CPU family, RAM size, OS build, app
version) used by the recommender to find "users on similar hardware".

Usage:
    from core.device_id import get_device_id, collect_hardware_fingerprint
    did = get_device_id()                      # UUID, persists across runs
    hw  = collect_hardware_fingerprint()       # dict, refreshed on demand
"""
from __future__ import annotations

import os
import re
import uuid

from config import APPDATA_DIR, APP_VERSION
from core.utils import get_logger, atomic_write_text

log = get_logger("device_id")

DEVICE_ID_FILE = os.path.join(APPDATA_DIR, "device.id")


# ─── UUID persistence ────────────────────────────────────────────────────
def get_device_id() -> str:
    """Return the device's UUID4 as a lower-case string.  Generates and
    persists one on first call, and returns the same value on every
    subsequent call until the file is deleted.
    """
    # Fast path: file already exists.
    try:
        if os.path.exists(DEVICE_ID_FILE):
            with open(DEVICE_ID_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            # Validate it's actually a UUID — guard against corruption.
            try:
                return str(uuid.UUID(raw))
            except (ValueError, AttributeError):
                log.warning(f"device.id contained invalid UUID {raw!r}; regenerating")
    except Exception as e:
        log.warning(f"Could not read device.id: {e}")

    # Slow path: generate + persist.
    new_id = str(uuid.uuid4())
    if atomic_write_text(DEVICE_ID_FILE, new_id):
        log.info(f"Generated new anonymous device ID: {new_id}")
    else:
        log.error("Failed to persist device.id; using ephemeral ID for this run")
    return new_id


def reset_device_id() -> str:
    """Delete the existing UUID and generate a fresh one.  Exposed so a
    user worried about being uniquely tracked can wipe their identity from
    Settings → Privacy.  Returns the new UUID.
    """
    try:
        if os.path.exists(DEVICE_ID_FILE):
            os.remove(DEVICE_ID_FILE)
            log.info("Reset device ID at user request")
    except Exception as e:
        log.warning(f"Could not delete device.id: {e}")
    return get_device_id()


# ─── Hardware fingerprint ────────────────────────────────────────────────
# Used as join keys for "users with similar hardware" aggregations.  None
# of this is PII.  CPU/GPU model strings are public retail SKU names.

_INTEL_FAMILY_RX = re.compile(r"\b(i\d|ultra|xeon|core)\b", re.I)
_AMD_FAMILY_RX   = re.compile(r"\b(ryzen|threadripper|epyc|athlon)\b", re.I)


def _cpu_family_from_name(name: str) -> str | None:
    """'13th Gen Intel(R) Core(TM) i7-13700K' → 'intel core i7'.
    'AMD Ryzen 9 7950X3D'                    → 'amd ryzen 9'.
    """
    if not name:
        return None
    n = name.strip()
    nl = n.lower()
    if "intel" in nl:
        m = _INTEL_FAMILY_RX.search(nl)
        if m:
            tier = m.group(1)
            return f"intel core {tier}".strip() if tier in ("i3", "i5", "i7", "i9") else f"intel {tier}"
        return "intel"
    if "amd" in nl:
        m = _AMD_FAMILY_RX.search(nl)
        if m:
            fam = m.group(1)
            tier_match = re.search(rf"{fam}\s+(\d)", nl)
            if tier_match:
                return f"amd {fam} {tier_match.group(1)}"
            return f"amd {fam}"
        return "amd"
    return None


def _safe_get_system_info() -> dict:
    """Wrap system_info in a try-block — it shells out to PowerShell and
    can be slow on first call.  We never want fingerprint collection to
    block app startup.
    """
    try:
        from core.system_info import get_system_info
        return get_system_info() or {}
    except Exception as e:
        log.warning(f"system_info unavailable for fingerprint: {e}")
        return {}


def collect_hardware_fingerprint() -> dict:
    """Return a dict matching the cloud's HardwareFingerprint schema.

    Fields:
      gpu_model    — full retail name, e.g. "NVIDIA GeForce RTX 5070 Ti"
      cpu_family   — coarse bucket, e.g. "intel core i7", "amd ryzen 9"
      cpu_model    — full processor name
      ram_gb       — int, total physical memory
      os_build     — Windows build number, e.g. "26100"
      app_version  — current GhostShell version
    """
    info = _safe_get_system_info()
    gpu = info.get("gpu") or {}
    cpu = info.get("cpu") or {}
    ram = info.get("ram") or {}
    os_  = info.get("os")  or {}

    cpu_name = (cpu.get("name") or "").strip() or None
    return {
        "gpu_model":   (gpu.get("name") or "").strip() or None,
        "cpu_family":  _cpu_family_from_name(cpu_name) if cpu_name else None,
        "cpu_model":   cpu_name,
        "ram_gb":      int(round(float(ram.get("total_gb") or 0))) or None,
        "os_build":    (os_.get("build") or "").strip() or None,
        "app_version": APP_VERSION,
    }
