"""Does NVIDIA's library (nvml.dll) leave a process that lets go of it?

    python tools/measure/nvml_release.py

A graphics driver's installer has to replace that file, so the app asks its
native helper to let go of it before it starts one (core/driver_updater.py,
_release_gpu_library).  This shows what letting go does, on this PC, in two
processes of their own:

  * NVIDIA's library by itself: loaded and freed at once; then loaded,
    started, shut down and freed, which is all a program can do to let go.
  * The app's helper, if it has been built (native/target/release): asked
    for the card's reading, then asked to let go, as the app asks it.

After every step it lists the files of that name that Windows holds open as
loaded libraries of the process.

Read-only: it starts the library and shuts it down, which is what
nvidia-smi does every time it runs.  It imports nothing of the app, reads no
setting, and changes nothing.  Needs an NVIDIA driver; without one it says
so.

What it showed on the PC this was written on (RTX 5070 Ti, driver 621.40,
Windows 11): freed without having been started, the library is gone.  Once
it has been started, it stays in the process after it is shut down and
freed, and after it is freed again and again; the helper's letting go leaves
it there as well.  Only the end of the process takes it out.  That is why
the Rust shell, which lives as long as the app does, never opens it
(shell_rs/core/src/moved/mod.rs).
"""
import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HELPER = os.path.join(REPO, "native", "target", "release", "vispora_native.dll")
SYSTEM32_ONLY = 0x800                                # LOAD_LIBRARY_SEARCH_SYSTEM32

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.LoadLibraryExW.restype = wintypes.HMODULE
kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
kernel32.GetProcAddress.restype = ctypes.c_void_p
kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.K32EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]


def held():
    """The files named nvml.dll among this process's loaded libraries."""
    modules = (wintypes.HMODULE * 4096)()
    needed = wintypes.DWORD(0)
    if not kernel32.K32EnumProcessModules(kernel32.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(needed)):
        return ["(the list could not be read)"]
    found = []
    for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        name = ctypes.create_unicode_buffer(1024)
        kernel32.GetModuleFileNameW(module, name, 1024)
        if os.path.basename(name.value).lower() == "nvml.dll":
            found.append(name.value)
    return found


def say(step):
    files = held()
    print(f"  {step:<46} " + ("not in the process" if not files else "IN THE PROCESS: " + ", ".join(files)), flush=True)
    return bool(files)


def by_itself():
    print("NVIDIA's library by itself:")
    say("before anything")
    module = kernel32.LoadLibraryExW("nvml.dll", None, SYSTEM32_ONLY)
    if not module:
        print("  no NVIDIA driver library (nvml.dll) on this PC: nothing to measure")
        return 2
    say("loaded, not started")
    kernel32.FreeLibrary(module)
    say("freed at once")
    module = kernel32.LoadLibraryExW("nvml.dll", None, SYSTEM32_ONLY)
    call = ctypes.CFUNCTYPE(ctypes.c_int)
    start, stop = kernel32.GetProcAddress(module, b"nvmlInit_v2"), kernel32.GetProcAddress(module, b"nvmlShutdown")
    if not start or not stop:
        print("  this nvml.dll has no nvmlInit_v2 / nvmlShutdown: nothing to measure")
        return 2
    status = call(start)()
    say(f"loaded again and started (status {status})")
    status = call(stop)()
    say(f"shut down (status {status})")
    kernel32.FreeLibrary(module)
    stays = say("freed")
    for again in range(1, 4):
        still = kernel32.GetModuleHandleW("nvml.dll")
        if not still:
            break
        kernel32.FreeLibrary(still)
        stays = say(f"freed once more ({again})")
    print("  -> " + ("it stays until the process ends" if stays else "it is gone"))
    return 0


def through_the_helper():
    print("The app's helper (native/target/release/vispora_native.dll):")
    if not os.path.isfile(HELPER):
        print("  not built here: cargo build --release --manifest-path native/Cargo.toml")
        return 2
    helper = ctypes.WinDLL(HELPER)
    helper.vispora_native_call.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
    helper.vispora_native_call.restype = ctypes.c_int32

    def ask(question, **more):
        request = json.dumps({**more, "q": question}, separators=(",", ":")).encode("utf-8")
        answer = ctypes.create_string_buffer(1 << 16)
        length = helper.vispora_native_call(request, len(request), answer, 1 << 16)
        said = json.loads(answer.raw[:length].decode("utf-8")) if length > 0 else {}
        return said.get("data") if said.get("ok") else None

    say("before anything")
    reading = ask("gpu_live")
    if reading is None:
        print("  the helper has no reading of a card on this PC: nothing to measure")
        return 2
    say(f"asked for the card's reading ({reading.get('temp_c')} degrees)")
    took = ask("gpu_release", hold_s=0)
    stays = say("asked to let go" + ("" if took is not None else " (it did not take the call)"))
    held_for = ask("gpu_release", hold_s=5)
    reading = ask("gpu_live")
    stays = say("let go for 5 s, and asked meanwhile (" + ("no reading" if reading is None else "a reading") + ")") or stays
    del held_for
    print("  -> " + ("it stays until the process ends" if stays else "it is gone"))
    return 0


if __name__ == "__main__":
    if sys.platform != "win32":
        print("Windows only")
        sys.exit(2)
    if sys.argv[1:] == ["--by-itself"]:
        sys.exit(by_itself())
    if sys.argv[1:] == ["--through-the-helper"]:
        sys.exit(through_the_helper())
    # Each in a process of its own: what one leaves behind is not the other's.
    codes = []
    for part in ("--by-itself", "--through-the-helper"):
        codes.append(subprocess.run([sys.executable, os.path.abspath(__file__), part]).returncode)
        print(flush=True)
    sys.exit(max(codes))
