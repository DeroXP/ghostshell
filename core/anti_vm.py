"""Virtual machine detection.

We need to tell *Windows running as a guest inside VMware/VirtualBox/
KVM/Hyper-V/QEMU* (almost always piracy or trial abuse) apart from *any
real PC* (the customer).

**This module does NOT check `IsHypervisorPresent`, VBS state, or any
hypervisor-related flag.**  Those are the wrong signals — they flip
based on whether VBS/HVCI is enabled, which GhostShell's own kernel
tab toggles off as part of its tuning.  A real PC with VBS on, a real
PC with VBS off, and a real PC with VBS toggled in either direction
all read identically here.

The signals we *do* check are the **vendor identity strings** that
hypervisors stamp on themselves and that real OEM hardware reports
truthfully:
  • Win32_ComputerSystem.Manufacturer / Model
  • Win32_BIOS.Manufacturer / Version / SerialNumber
  • Win32_DiskDrive.Model / Manufacturer

A real Dell/Asus/MSI/HP/etc. box always reports its OEM vendor in those
fields.  A VMware guest reports "VMware, Inc." / "VMware Virtual
Platform"; a VirtualBox guest reports "innotek GmbH" / "VirtualBox";
a Hyper-V guest reports "Microsoft Corporation" + a model containing
"Virtual Machine" (which a real OEM never names its product).

Detection returns:
  • is_vm:      bool
  • signals:    list of {axis, value, vendor} dicts (for diagnostics)
  • confidence: 0..100 — each unique vendor match adds 35
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from core.utils import get_logger, run_ps

log = get_logger("anti_vm")


# ─── Vendor signature table ──────────────────────────────────────────────
# Each entry: (substring, vendor_label).  Matched case-insensitively
# against manufacturer / model / BIOS / disk strings.
_VM_SIGNATURES: list[tuple[str, str]] = [
    # VMware
    ("vmware",          "VMware"),
    # VirtualBox
    ("virtualbox",      "VirtualBox"),
    ("vbox",            "VirtualBox"),
    ("innotek",         "VirtualBox"),  # pre-Oracle name
    # KVM / QEMU
    ("kvm",             "KVM"),
    ("qemu",            "QEMU"),
    ("bochs",           "Bochs/QEMU"),
    ("red hat, inc.",   "KVM/QEMU"),
    # Microsoft Hyper-V (when Windows is the GUEST, manufacturer is
    # "Microsoft Corporation" + model "Virtual Machine"; on a regular
    # Win11 host with VBS, manufacturer is the OEM e.g. "Dell Inc.")
    ("microsoft corporation", "Hyper-V (guest)"),  # combined w/ model below
    # Xen
    ("xen",             "Xen"),
    ("xenvmm",          "Xen"),
    # Parallels
    ("parallels",       "Parallels"),
    # Cloud-only hypervisors
    ("amazon ec2",      "AWS EC2"),
    ("google",          "GCP"),
    ("docker",          "Docker"),
]


def _match_vendor(value: str) -> str | None:
    if not value:
        return None
    v = value.lower()
    for needle, label in _VM_SIGNATURES:
        if needle in v:
            return label
    return None


# ─── Probe helpers ───────────────────────────────────────────────────────
@dataclass
class Probe:
    axis:    str   # "manufacturer", "model", "bios", "disk"
    value:   str
    vendor:  str | None = None   # populated by detector


def _probe_computer_system() -> tuple[str, str]:
    r = run_ps(
        "Get-CimInstance Win32_ComputerSystem | "
        "Select-Object Manufacturer, Model | ConvertTo-Json"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            return ((d.get("Manufacturer") or "").strip(),
                    (d.get("Model") or "").strip())
        except Exception:
            pass
    return "", ""


def _probe_bios() -> tuple[str, str, str]:
    r = run_ps(
        "Get-CimInstance Win32_BIOS | "
        "Select-Object Manufacturer, Version, SerialNumber | ConvertTo-Json"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            return ((d.get("Manufacturer") or "").strip(),
                    (d.get("Version") or "").strip(),
                    (d.get("SerialNumber") or "").strip())
        except Exception:
            pass
    return "", "", ""


def _probe_disks() -> list[str]:
    r = run_ps(
        "Get-CimInstance Win32_DiskDrive | "
        "Select-Object Model, Manufacturer | ConvertTo-Json"
    )
    out: list[str] = []
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if not isinstance(d, list):
                d = [d]
            for disk in d:
                model = (disk.get("Model") or "").strip()
                mfr   = (disk.get("Manufacturer") or "").strip()
                if model:
                    out.append(model)
                if mfr and mfr.lower() != "(standard disk drives)":
                    out.append(mfr)
        except Exception:
            pass
    return out


# ─── Detection ───────────────────────────────────────────────────────────
def detect() -> dict:
    """Returns:
      {
        "is_vm":      bool,
        "confidence": int (0..100),
        "signals":    [{"axis": str, "value": str, "vendor": str}, ...]
      }
    """
    signals: list[dict] = []

    # Computer system
    mfr, model = _probe_computer_system()
    for axis, value in (("cs.manufacturer", mfr), ("cs.model", model)):
        v = _match_vendor(value)
        if v:
            signals.append({"axis": axis, "value": value, "vendor": v})

    # Special case: Hyper-V guest signature is "Microsoft Corporation"
    # manufacturer + "Virtual Machine" model.  A real OEM never names its
    # model "Virtual Machine".
    if (mfr.lower().startswith("microsoft")
            and "virtual" in model.lower()):
        signals.append({"axis": "cs.combo", "value": f"{mfr} / {model}",
                        "vendor": "Hyper-V (guest)"})

    # BIOS
    bios_mfr, bios_ver, bios_sn = _probe_bios()
    for axis, value in (("bios.manufacturer", bios_mfr),
                        ("bios.version",      bios_ver),
                        ("bios.serial",       bios_sn)):
        v = _match_vendor(value)
        if v:
            signals.append({"axis": axis, "value": value, "vendor": v})

    # Disks
    for disk_str in _probe_disks():
        v = _match_vendor(disk_str)
        if v:
            signals.append({"axis": "disk.model", "value": disk_str, "vendor": v})

    # Each unique vendor signal contributes 35 confidence; cap at 100.
    unique_vendors = {s["vendor"] for s in signals}
    confidence = min(100, 35 * len(unique_vendors))
    is_vm = len(unique_vendors) > 0

    if is_vm:
        log.info(f"VM detected (confidence={confidence}, "
                 f"vendors={sorted(unique_vendors)})")
    else:
        log.debug("VM detection: no signals")

    return {"is_vm": is_vm, "confidence": confidence, "signals": signals}


def is_virtual_machine() -> bool:
    """Convenience wrapper for callers that just want a bool."""
    return detect()["is_vm"]
