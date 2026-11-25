"""What the start-up preparation's "trim working sets" step costs the old way
(PowerShell, which compiles a small C# program first) and through the native
helper.

    python tools/measure/memory_step_cost.py [repeats]       (default 5)

Both are tried on ONE scratch process this script starts itself, never on
anything else: the app trims every process, which on a PC somebody is using
would be felt.  So this times the start of the old way (PowerShell, the
compile, one process) against the same one process through the helper.  What
it leaves out, on both sides, is the other three hundred processes.

The standby list is not cleared by this script at all.
"""
import ctypes
import json
import os
import statistics
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DLL = os.path.join(REPO, "native", "target", "release", "vispora_native.dll")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
if not os.path.isfile(DLL):
    print(f"not built: {DLL}\nrun: cargo build --release --manifest-path native/Cargo.toml")
    sys.exit(2)
lib = ctypes.WinDLL(DLL)
lib.vispora_native_act.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
lib.vispora_native_act.restype = ctypes.c_int32

SCRATCH = ("import time\nb = bytearray(64 * 1024 * 1024)\nfor i in range(0, len(b), 4096):\n    b[i] = 1\n"
           "print('ready', flush=True)\ntime.sleep(300)\n")
# core/boot_prep.py's PowerShell, for one process id where the app's goes through every process.
OLD = r"""
try {
    Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Diagnostics;
public class WS {
    [DllImport("kernel32.dll")] public static extern bool SetProcessWorkingSetSize(IntPtr p, int a, int b);
    public static int Go(int pid) { int c=0; Process p = Process.GetProcessById(pid); try{SetProcessWorkingSetSize(p.Handle,-1,-1);c++;}catch{} return c; }
}
'@ -ErrorAction SilentlyContinue
    [WS]::Go(PID_HERE) | Out-Null
    'OK'
} catch { 'FAIL' }
"""


def old_way(pid):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", OLD.replace("PID_HERE", str(pid))],
                       capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    return "OK" in r.stdout


def new_way(pid):
    request = json.dumps({"do": "trim_working_sets", "pids": [pid]}).encode("utf-8")
    buf = ctypes.create_string_buffer(8192)
    n = lib.vispora_native_act(request, len(request), buf, 8192)
    return n > 0 and json.loads(buf.raw[:n].decode("utf-8")).get("ok") is True


def timed(fn, pid, repeats):
    out = []
    for _ in range(repeats):
        t = time.perf_counter()
        ok = fn(pid)
        out.append(time.perf_counter() - t)
        assert ok, fn.__name__
    return round(1000 * statistics.median(out), 2)


repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 5
child = subprocess.Popen([sys.executable, "-c", SCRATCH], stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
try:
    child.stdout.readline()
    print(json.dumps({
        "repeats": repeats,
        "old_way_ms": timed(old_way, child.pid, repeats),
        "helper_ms": timed(new_way, child.pid, repeats),
        "note": "one scratch process each; the app's own run goes through every process",
    }, indent=1))
finally:
    child.kill()
