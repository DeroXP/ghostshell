"""Run two read-only parts of the app for real and time them: the dashboard's
system information (what the first screen waits for) and one pass of the
hardware monitor (the Monitor page).

    python tools/measure/real_reads.py

The app's data folder is redirected to a throwaway folder first, so a live
install's files and log are not touched.  Everything these two functions start
only reads the machine.  The native helper is left out, so this times the old
way (PowerShell, nvidia-smi, ping) even where the helper has been built.
"""
import json
import os
import statistics
import sys
import tempfile
import time

root = tempfile.mkdtemp(prefix="vispora_measure_")
for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local")):
    os.environ[var] = os.path.join(root, sub)
    os.makedirs(os.environ[var], exist_ok=True)
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config                                           # noqa: E402

assert config.APPDATA_DIR.startswith(root), config.APPDATA_DIR
import core.utils as U                                  # noqa: E402
from core import native                                 # noqa: E402

native.call = lambda query, /, **args: None             # the old way only
native.available = lambda: False
calls = []
_ps, _cmd = U.run_ps, U.run_cmd


def run_ps(command, timeout=60):
    t = time.perf_counter()
    r = _ps(command, timeout)
    calls.append(("powershell", time.perf_counter() - t, r["ok"], command.strip()[:60]))
    return r


def run_cmd(args, timeout=60):
    t = time.perf_counter()
    r = _cmd(args, timeout)
    calls.append((os.path.basename(str(args[0])), time.perf_counter() - t, r["ok"], " ".join(map(str, args))[:60]))
    return r


U.run_ps, U.run_cmd = run_ps, run_cmd                   # before the modules below bind them
from core import hw_monitor, system_info                # noqa: E402

out = {"system_info": [], "hw_monitor_pass": []}
for _ in range(2):
    del calls[:]
    t = time.perf_counter()
    system_info.get_system_info()
    out["system_info"].append({
        "seconds": round(time.perf_counter() - t, 2), "process_starts": len(calls),
        "by_program": {k: sum(1 for c in calls if c[0] == k) for k in sorted({c[0] for c in calls})},
        "slowest": sorted(((round(c[1], 2), c[3]) for c in calls), reverse=True)[:4]})
for _ in range(4):
    del calls[:]
    t = time.perf_counter()
    cpu, gpu = hw_monitor._sample_cpu(), hw_monitor._sample_gpu()
    hw_monitor._sample_ram()
    dpc, ping = hw_monitor._sample_dpc(), hw_monitor._sample_ping("1.1.1.1")
    out["hw_monitor_pass"].append({
        "seconds": round(time.perf_counter() - t, 2), "process_starts": len(calls),
        "calls": [(c[0], round(c[1], 2), c[2], c[3][:44]) for c in calls],
        "readings": {"cpu_temp": cpu["temp"], "cpu_load": cpu["load"], "cpu_clock": cpu["clock"],
                     "gpu_temp": gpu["temp"], "dpc_pct": dpc.get("dpc_pct"), "dpc_rate": dpc.get("dpc_rate"),
                     "ping": ping}})
out["hw_monitor_summary"] = {
    "median_seconds_per_pass": statistics.median(p["seconds"] for p in out["hw_monitor_pass"]),
    "process_starts_per_pass": statistics.median(p["process_starts"] for p in out["hw_monitor_pass"])}
print(json.dumps(out, indent=1))
