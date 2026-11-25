"""What one answer from the native helper costs on this PC, beside what the
old way cost for the same question.

    python tools/measure/helper_cost.py [repeats]        (default 200)

Loads the built DLL directly (native/target/release) and imports nothing of
the app.  Read-only.  The old way's figures for comparison come from
tools/measure/process_cost.py; the one thing compared here directly is the
network monitor's reading, where the old way is not a program start but a
loop in Python (psutil), if psutil is installed for this Python.
"""
import ctypes
import json
import os
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DLL = os.path.join(REPO, "native", "target", "release", "vispora_native.dll")
if not os.path.isfile(DLL):
    print(f"not built: {DLL}\nrun: cargo build --release --manifest-path native/Cargo.toml")
    sys.exit(2)
lib = ctypes.WinDLL(DLL)
lib.vispora_native_call.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
lib.vispora_native_call.restype = ctypes.c_int32
_size = [1 << 16]


def ask(question, /, **args):
    """The way core/native.py asks: JSON in, JSON out, parsed."""
    request = json.dumps({**args, "q": question}, separators=(",", ":")).encode("utf-8")
    while True:
        buf = ctypes.create_string_buffer(_size[0])
        n = lib.vispora_native_call(request, len(request), buf, _size[0])
        if n < 0:
            _size[0] = -n + 1024
            continue
        answer = json.loads(buf.raw[:n].decode("utf-8")) if n else {}
        return answer.get("data") if answer.get("ok") else None


def timed(fn, repeats):
    """(median ms on the clock, ms of this process's processor time per call)."""
    fn()                                            # the first call opens whatever it needs
    walls = []
    cpu0 = time.process_time()
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        walls.append(time.perf_counter() - t)
    cpu = (time.process_time() - cpu0) / repeats
    return round(1000 * statistics.median(walls), 3), round(1000 * cpu, 3)


repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 200
QUESTIONS = {
    "windowed_processes (game monitor, every 3 s)": lambda: ask("windowed_processes"),
    "process_io (network monitor, every second)": lambda: ask("process_io"),
    "gpu_live (temperature ceiling, every second)": lambda: ask("gpu_live"),
    "cpu_clock": lambda: ask("cpu_clock"),
    "cpu_times": lambda: ask("cpu_times"),
    "ping 127.0.0.1": lambda: ask("ping", target="127.0.0.1", timeout_ms=500),
    "wmi: Win32_OperatingSystem": lambda: ask("wmi", namespace="root\\cimv2",
                                              query="SELECT Caption, Version FROM Win32_OperatingSystem"),
    "wmi: a namespace that is not there": lambda: ask("wmi", namespace="root\\NoSuchNamespace",
                                                      query="SELECT Name FROM Sensor"),
}
out = {"repeats": repeats, "helper_ms_per_answer": {}}
for name, fn in QUESTIONS.items():
    n = repeats if "wmi" not in name else max(10, repeats // 10)
    wall, cpu = timed(fn, n)
    out["helper_ms_per_answer"][name] = {"ms": wall, "cpu_ms": cpu}

try:
    import psutil
except ImportError:
    psutil = None
    out["network_monitor_reading"] = "psutil is not installed for this Python: the old loop was not timed"
if psutil:
    def old_way():
        rows = 0
        for p in psutil.process_iter(["pid", "name", "io_counters"]):
            io = p.info.get("io_counters")
            if io:
                rows += 1
        return rows

    def new_way():
        return len(ask("process_io") or [])
    n = max(10, repeats // 10)
    old_wall, old_cpu = timed(old_way, n)
    new_wall, new_cpu = timed(new_way, n)
    out["network_monitor_reading"] = {
        "processes": new_way(),
        "psutil_loop": {"ms": old_wall, "cpu_ms": old_cpu},
        "helper": {"ms": new_wall, "cpu_ms": new_cpu},
        "times_less_processor_time": round(old_cpu / new_cpu, 1) if new_cpu > 0 else None,
    }
print(json.dumps(out, indent=1))
