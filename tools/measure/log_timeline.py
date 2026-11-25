"""Time every start recorded in the app's own log: seconds from the first line
of a start ("Starting Vispora v...") to a few later lines.

    python tools/measure/log_timeline.py [path to vispora.log]

Only reads the log (by default the live one in %APPDATA%\\Vispora).  The log
has one-second resolution and starts once Python is running, so what happens
before that (unpacking, imports) is not in it.
"""
import json
import os
import re
import statistics
import sys

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ["APPDATA"], "Vispora", "vispora.log")
lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
START = re.compile(r"app  Starting (Vispora|GhostShell) v")
MARKS = {
    "window created": r"WebView2 GPU policy|crosshair start tied",
    "capture hotkeys wired": r"Capture hotkeys wired",
    "dependency check complete": r"dependency boot check complete",
    "system info collected (the first screen has its data)": r"sysinfo  System info collected",
    "boot prep done": r"BOOT PREP: done",
    "game monitor started": r"Game monitor started",
    "update check answered": r"Already up to date|Update available|update check failed",
}


def seconds(line):
    m = re.match(r"\[(\d\d):(\d\d):(\d\d)\]", line)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


starts = [i for i, line in enumerate(lines) if START.search(line)]
found = {kind: {k: [] for k in MARKS} for kind in ("manual", "autostart")}
prep = {"manual": [], "autostart": []}
count = {"manual": 0, "autostart": 0}
for n, i in enumerate(starts):
    kind = "autostart" if "autostart" in lines[i] else "manual"
    count[kind] += 1
    t0 = seconds(lines[i])
    end = min(starts[n + 1] if n + 1 < len(starts) else len(lines), i + 400)
    seen = set()
    for j in range(i, end):
        t = seconds(lines[j])
        if t is None:
            continue
        passed = (t - t0) % 86400
        if passed > 900:
            break
        for mark, pattern in MARKS.items():
            if mark not in seen and re.search(pattern, lines[j]):
                seen.add(mark)
                found[kind][mark].append(passed)
        took = re.search(r"BOOT PREP: done in ([\d.]+)s", lines[j])
        if took:
            prep[kind].append(float(took.group(1)))
out = {}
for kind in found:
    out[kind] = {"starts": count[kind]}
    for mark, values in found[kind].items():
        if values:
            out[kind][mark] = {"starts": len(values), "median_s": statistics.median(values), "fastest_s": min(values),
                               "slowest_s": max(values)}
    if prep[kind]:
        out[kind]["boot prep, its own duration"] = {"median_s": round(statistics.median(prep[kind]), 1),
                                                    "slowest_s": max(prep[kind])}
scans = [float(m.group(1)) for line in lines for m in [re.search(r"integrity scan complete in ([\d.]+)s", line)] if m]
if scans:
    out["integrity scans"] = {"runs": len(scans), "median_s": statistics.median(scans), "slowest_s": max(scans),
                              "longer_than_a_minute": sum(1 for s in scans if s > 60)}
print(json.dumps(out, indent=1))
