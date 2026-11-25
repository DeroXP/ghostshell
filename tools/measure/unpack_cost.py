"""Replay what a one-file build does at every start (unpack every bundled file
into a fresh temporary folder) and at every exit (delete that folder), timed.

    python tools/measure/unpack_cost.py <path to the built exe> <scratch folder>

Reads the exe; writes only under the scratch folder, and removes what it
wrote.  Needs PyInstaller (run it with the Python releases are built with).

This is the floor: the files are already in the disk cache and are never
loaded, so the virus scanner's look at 200 freshly written DLLs is not in it.
The real figure is the gap between the two processes of a running app
(tools/measure/live_sample.py prints when the second one started; the first
one is the unpacker).
"""
import json
import os
import shutil
import statistics
import sys
import time

from PyInstaller.archive.readers import CArchiveReader

exe, scratch = sys.argv[1], sys.argv[2]
archive = CArchiveReader(exe)
names = [n for n, entry in archive.toc.items() if entry[4] in ("b", "x")]       # binaries and data files
runs = []
for i in range(4):
    dest = os.path.join(scratch, f"_unpack_replay_{i}")
    shutil.rmtree(dest, ignore_errors=True)
    t0, c0, size = time.perf_counter(), time.process_time(), 0
    for name in names:
        data = archive.extract(name)
        path = os.path.join(dest, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        size += len(data)
    t1, c1 = time.perf_counter(), time.process_time()
    shutil.rmtree(dest)
    runs.append({"unpack_s": round(t1 - t0, 2), "unpack_cpu_s": round(c1 - c0, 2),
                 "delete_s": round(time.perf_counter() - t1, 2)})
print(json.dumps({"exe_mb": round(os.path.getsize(exe) / 2 ** 20, 1), "files_unpacked": len(names),
                  "unpacked_mb": round(size / 2 ** 20, 1), "runs": runs,
                  "median_unpack_s": statistics.median(r["unpack_s"] for r in runs),
                  "median_delete_s": statistics.median(r["delete_s"] for r in runs)}, indent=1))
