"""Watch the RUNNING app from outside for a while, read-only: how much
processor time it uses, which of its threads use it, and every process it
starts.

    python tools/measure/live_sample.py [seconds]        (default 120)

Everything comes from NtQuerySystemInformation, the call Task Manager uses.
No handle to the app is opened and nothing is changed, so this is safe to run
beside a live install and needs no administrator rights.

Reading the result:
  app_cpu_pct_of_one_core   the app's own process (not the programs it starts)
  threads_by_cpu            its busiest threads; "created" tells which one it
                            is when held against the log (each watcher logs a
                            line when it starts)
  children_by_name          the programs it started in the window.  Each
                            console program also gets a console host
                            (conhost.exe) from Windows, counted separately.
"""
import collections
import ctypes
import datetime
import json
import struct
import sys
import time

WINDOW_S = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
APP_NAMES = ("vispora.exe", "ghostshell.exe")           # (an install updated in place keeps its old file name)
ntdll = ctypes.WinDLL("ntdll")
_size = [4 << 20]


def snapshot(threads_of=None) -> dict:
    """{pid: {...}} for every process; with the thread list for one of them."""
    while True:
        buf = ctypes.create_string_buffer(_size[0])
        need = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(5, buf, _size[0], ctypes.byref(need)) & 0xFFFFFFFF
        if status == 0xC0000004:                        # the list grew: a larger buffer
            _size[0] = max(_size[0] * 2, need.value + 65536)
            continue
        if status != 0:
            raise OSError(hex(status))
        break
    raw, out, off = buf.raw, {}, 0
    while True:                                          # SYSTEM_PROCESS_INFORMATION, 64-bit layout
        nxt, nthreads = struct.unpack_from("<II", raw, off)
        create, user, kernel = struct.unpack_from("<qqq", raw, off + 32)
        name_len, _, name_ptr = struct.unpack_from("<HHxxxxQ", raw, off + 56)
        pid, ppid = struct.unpack_from("<QQ", raw, off + 80)
        handles, = struct.unpack_from("<I", raw, off + 96)
        ws, = struct.unpack_from("<Q", raw, off + 144)
        io = struct.unpack_from("<qqqqqq", raw, off + 208)
        try:
            name = ctypes.wstring_at(name_ptr, name_len // 2) if name_ptr and name_len else ""
        except Exception:
            name = ""
        rec = {"pid": pid, "ppid": ppid, "create": create, "cpu": (user + kernel) / 1e7, "threads": nthreads,
               "handles": handles, "ws": ws, "io": io, "name": name}
        if pid == threads_of:
            rec["thread_list"] = []
            for i in range(nthreads):                    # SYSTEM_THREAD_INFORMATION, 80 bytes each
                t = off + 256 + i * 80
                k, u, c = struct.unpack_from("<qqq", raw, t)
                tid, = struct.unpack_from("<Q", raw, t + 48)
                switches, = struct.unpack_from("<I", raw, t + 64)
                rec["thread_list"].append({"tid": tid, "cpu": (k + u) / 1e7, "kernel": k / 1e7, "create": c,
                                           "switches": switches})
        out[pid] = rec
        if not nxt:
            return out
        off += nxt


def when(filetime: int) -> str:
    stamp = datetime.datetime.fromtimestamp(filetime / 1e7 - 11644473600.0)
    return stamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


first = snapshot()
candidates = [r for r in first.values() if r["name"].lower() in APP_NAMES]
if not candidates:
    print(json.dumps({"err": "the app is not running"}))
    sys.exit(1)
app = max(candidates, key=lambda r: r["create"])        # the program itself; the older one is the unpacker
APP = app["pid"]
start = snapshot(APP)[APP]
t0 = time.perf_counter()
children, hosts = {}, {}                                 # (pid, create time) -> record
polls = 0
while time.perf_counter() - t0 < WINDOW_S:
    procs = snapshot()
    polls += 1
    for pid, r in procs.items():
        if r["create"] <= start["create"] or r["create"] < app["create"]:
            continue
        key = (pid, r["create"])
        if r["ppid"] == APP:
            if r["create"] / 1e7 - 11644473600.0 >= time.time() - (time.perf_counter() - t0) - 0.5:
                rec = children.setdefault(key, {"name": r["name"].lower(), "cpu": 0.0})
                rec["cpu"] = max(rec["cpu"], r["cpu"])
        elif r["name"].lower() == "conhost.exe" and any(k[0] == r["ppid"] for k in children):
            rec = hosts.setdefault(key, {"cpu": 0.0})
            rec["cpu"] = max(rec["cpu"], r["cpu"])
    time.sleep(0.008)
seconds = time.perf_counter() - t0
end = snapshot(APP).get(APP)
if end is None:
    print(json.dumps({"err": "the app exited during the window"}))
    sys.exit(1)

before = {t["tid"]: t for t in start["thread_list"]}
rows = []
for t in end["thread_list"]:
    b = before.get(t["tid"], {"cpu": 0.0, "kernel": 0.0, "switches": 0})
    used = t["cpu"] - b["cpu"]
    rows.append({"cpu_s": round(used, 2), "pct_of_one_core": round(100 * used / seconds, 1),
                 "kernel_share": round((t["kernel"] - b["kernel"]) / used, 2) if used > 0.01 else None,
                 "cpu_s_since_start": round(t["cpu"], 1), "created": when(t["create"]),
                 "context_switches_per_s": round((t["switches"] - b["switches"]) / seconds)})
rows.sort(key=lambda r: -r["cpu_s"])
by_name = collections.Counter(c["name"] for c in children.values())
cpu_by_name = collections.defaultdict(float)
for c in children.values():
    cpu_by_name[c["name"]] += c["cpu"]
child_cpu = sum(cpu_by_name.values()) + sum(h["cpu"] for h in hosts.values())
print(json.dumps({
    "window_s": round(seconds, 1),
    "polls_per_s": round(polls / seconds),
    "app_started": when(app["create"]),
    "app_cpu_s": round(end["cpu"] - start["cpu"], 2),
    "app_cpu_pct_of_one_core": round(100 * (end["cpu"] - start["cpu"]) / seconds, 1),
    "app_cpu_s_since_start": round(end["cpu"], 1),
    "app_threads": end["threads"],
    "app_memory_mb": round(end["ws"] / 2 ** 20),
    "children_started": len(children),
    "children_per_minute": round(len(children) * 60 / seconds, 1),
    "children_by_name": dict(by_name.most_common()),
    "console_hosts_for_them": len(hosts),
    "children_cpu_s": {k: round(v, 2) for k, v in cpu_by_name.items()},
    "children_and_hosts_cpu_pct_of_one_core": round(100 * child_cpu / seconds, 1),
    "threads_by_cpu": rows[:8],
    "threads_idle": sum(1 for r in rows if r["cpu_s"] < 0.01),
}, indent=1))
