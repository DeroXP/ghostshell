"""Hardware fingerprint capture (Windows).

Three values, hashed server-side into a single 32-byte device identifier:
  • CPU ID         — `Win32_Processor.ProcessorId`        (Intel/AMD CPU
                     model + stepping; same across reinstalls)
  • Motherboard UUID — `Win32_ComputerSystemProduct.UUID` (SMBIOS UUID,
                     unique per motherboard; persists through CPU/RAM
                     swaps)
  • Disk serial    — `Win32_DiskDrive.SerialNumber`       (system drive;
                     persists through Windows reinstalls but not drive
                     swaps)

Together they're stable enough that a fresh Windows install on the same
hardware reproduces the same fingerprint hash, but distinct enough that
a real hardware change (new motherboard, new boot drive) reads as a
different device.

These values are sent to the cloud over TLS exactly once per request,
HMAC'd server-side with FINGERPRINT_PEPPER, and discarded.  See
cloud/security.py for the zero-knowledge guarantee.
"""
from __future__ import annotations

import hashlib
import re

from core.utils import get_logger, run_cmd, run_ps

log = get_logger("hwfp")


# ─── CPU ID ──────────────────────────────────────────────────────────────
def _get_cpu_id() -> str:
    """ProcessorId — typically a 16-char hex string like 'BFEBFBFF000B0671'."""
    # PowerShell + Get-CimInstance is the modern path (wmic is deprecated
    # in newer Win11 builds).
    r = run_ps(
        "Get-CimInstance Win32_Processor | "
        "Select-Object -First 1 -ExpandProperty ProcessorId"
    )
    if r["ok"] and r["out"]:
        out = r["out"].strip()
        if out and out.upper() != "NULL":
            return out

    # Legacy fallback if PowerShell is broken.
    r = run_cmd(["wmic", "cpu", "get", "ProcessorId", "/value"])
    if r["ok"] and r["out"]:
        for line in r["out"].splitlines():
            line = line.strip()
            if line.startswith("ProcessorId="):
                v = line.split("=", 1)[1].strip()
                if v:
                    return v

    log.warning("Could not read CPU ProcessorId; using empty string")
    return ""


# ─── Motherboard UUID ────────────────────────────────────────────────────
_NULL_UUIDS = {
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
}


def _get_motherboard_uuid() -> str:
    """SMBIOS UUID.  This is the most stable axis — survives Windows
    reinstalls, RAM swaps, GPU swaps, disk swaps.  Only changes if the
    physical motherboard changes.
    """
    r = run_ps(
        "Get-CimInstance Win32_ComputerSystemProduct | "
        "Select-Object -ExpandProperty UUID"
    )
    if r["ok"] and r["out"]:
        v = r["out"].strip().lower()
        if v and v not in _NULL_UUIDS:
            return v

    # Older fallback path.
    r = run_cmd(["wmic", "csproduct", "get", "UUID", "/value"])
    if r["ok"] and r["out"]:
        for line in r["out"].splitlines():
            line = line.strip().lower()
            if line.startswith("uuid="):
                v = line.split("=", 1)[1].strip()
                if v and v not in _NULL_UUIDS:
                    return v

    log.warning("Could not read motherboard UUID; fingerprint will be weak")
    return ""


# ─── Disk serial ─────────────────────────────────────────────────────────
def _get_primary_disk_serial() -> str:
    """Serial of the system drive (the disk Windows boots from).

    On modern systems this is reliably the disk where C:\\ lives.  We use
    Get-PhysicalDisk + Get-Partition to find the drive backing C:, then
    pull its SerialNumber.  Falls back to "first non-empty serial" if
    the chain breaks.
    """
    # Preferred: walk C:\ → partition → physical disk → serial.
    ps = (
        "$drv = Get-PSDrive -Name C; "
        "$part = Get-Partition -DriveLetter C -ErrorAction SilentlyContinue | "
        "Select-Object -First 1; "
        "if ($part) { "
        "  $disk = Get-Disk -Number $part.DiskNumber -ErrorAction SilentlyContinue; "
        "  if ($disk) { Write-Output $disk.SerialNumber } "
        "}"
    )
    r = run_ps(ps)
    if r["ok"] and r["out"]:
        v = r["out"].strip().strip(".")
        # Some drivers return whitespace-padded serials; collapse them.
        v = re.sub(r"\s+", "", v)
        if v:
            return v

    # Fallback: any non-empty disk serial we can find.
    r = run_ps(
        "Get-CimInstance Win32_DiskDrive | "
        "Where-Object { $_.SerialNumber -and $_.SerialNumber.Trim() } | "
        "Select-Object -First 1 -ExpandProperty SerialNumber"
    )
    if r["ok"] and r["out"]:
        v = re.sub(r"\s+", "", r["out"].strip())
        if v:
            return v

    log.warning("Could not read disk serial; fingerprint will be weak")
    return ""


# ─── Public API ──────────────────────────────────────────────────────────
def collect() -> dict:
    """Return the three raw fingerprint inputs as a dict.

    The caller (license.py) sends this exactly once to the cloud over
    TLS as part of /v1/licenses/activate or /v1/trials/start.  The cloud
    immediately HMAC's them and the raw values go out of scope.

    These values stay only briefly in this Python process and are NEVER
    written to disk on the client side either.  The only thing the
    client persists is the response from the cloud (license_id, slot_no,
    last_validated timestamp), which is itself encrypted via DPAPI.
    """
    return {
        "cpu_id":           _get_cpu_id(),
        "motherboard_uuid": _get_motherboard_uuid(),
        "disk_serial":      _get_primary_disk_serial(),
    }


def is_complete(fp: dict | None = None) -> bool:
    """True iff all three axes are populated.  An incomplete fingerprint
    means the user's hardware doesn't expose one of the IDs (rare —
    happens with very locked-down OEM systems or some VMs).  We let
    incomplete fingerprints proceed but flag them server-side; trial
    abuse mitigations are stricter for incomplete fps.
    """
    fp = fp or collect()
    return bool(fp["cpu_id"] and fp["motherboard_uuid"] and fp["disk_serial"])


def local_id_short(fp: dict | None = None) -> str:
    """A short, client-only display ID for the Settings screen.

    SHA-256 of the three values, first 12 hex chars.  This is NOT the
    same value the server stores (no pepper, no HMAC), so showing it
    leaks nothing — it's purely a "show the user something stable" UI
    hook so they can confirm "yes, this is my PC" in a portal flow.
    """
    fp = fp or collect()
    payload = "|".join((fp["cpu_id"], fp["motherboard_uuid"], fp["disk_serial"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
