# The native helper

A small Rust library that answers the questions Vispora asks most often,
inside the app's own process. Each of them used to start a program:
PowerShell, `nvidia-smi` or `ping`.

It is the first piece of the move to Rust described in
[docs/rust-port.md](../docs/rust-port.md). The numbers that decided what goes
in it are in [docs/performance.md](../docs/performance.md).

## What it does

Every question only reads the machine. Nothing in this folder can change a
setting, a process or the graphics card.

| Question | Answer | What the app did before |
| --- | --- | --- |
| `windowed_processes` | Every process that owns a visible main window: id, name, memory | `Get-Process` in PowerShell, every 3 seconds while idle |
| `process_io` | Every process: id, name, bytes of "other" I/O (which includes network traffic) | psutil opening each process in turn, every second |
| `gpu_live` | The NVIDIA card's temperature, load, clocks, power, fan and memory, read from the driver's NVML library | `nvidia-smi`, once a second or more |
| `gpu_release` | Lets go of the NVIDIA library for `hold_s` seconds (for a driver install) | not needed: `nvidia-smi` never stayed loaded |
| `cpu_clock` | The processor's current clock in MHz | `Get-CimInstance Win32_Processor` in PowerShell |
| `cpu_times` | Processor time in total, idle, in DPCs and in interrupts | `Get-Counter` in PowerShell |
| `ping` | One echo to an IPv4 address: answered or not, and how fast | `ping.exe` |
| `wmi` | The rows of one WMI `SELECT` query | `Get-CimInstance` in PowerShell |
| `abi` | The library's version number | |

A question is a small piece of JSON and so is the answer:

```
{"q": "ping", "target": "1.1.1.1", "timeout_ms": 500}
{"ok": true, "data": {"ok": true, "ms": 11}}
```

A question that cannot be answered says why: `{"ok": false, "err": "..."}`.

## How the app uses it

`core/native.py` loads `vispora_native.dll` and is the only code that talks to
it. Each function there returns `None` when the library is missing, is another
version, or could not answer, and the caller then asks the old way. So:

* a build without the library still works, only slower;
* a source checkout that never ran `cargo build` still works;
* the test suite never loads the real library (`tests/sandbox.py` answers for
  it), apart from one test that asks it a few read-only questions on a PC where
  it has been built.

The library is loaded from one fixed place: beside the app's bundled files in a
built exe, `native/target/release` in a checkout. There is no setting and no
environment variable for the path, because the app runs elevated.

## Building

Needs Rust (stable) with the MSVC toolchain, and nothing else: the build is a
few seconds and needs no network.

```
cargo build --release --manifest-path native/Cargo.toml
cargo test  --release --manifest-path native/Cargo.toml
```

The tests ask the real machine (the process list, WMI, the graphics card if
there is one, a ping to this PC itself); all of it read-only.

This makes `native/target/release/vispora_native.dll` (the library) and
`vispora-native-ask.exe` (the same answers from a terminal, for checking by
hand: `vispora-native-ask gpu_live`).

Build it before `pyinstaller build.spec`. The spec bundles the library when it
is there and prints a warning when it is not.

## Adding a question

1. Write it in its own file under `src/`, returning `Result<Json, String>`,
   and add it to `dispatch` in `src/lib.rs`.
2. Add a function for it to `core/native.py` that checks the shape of the
   answer and returns `None` for anything else.
3. Use it in the app with the old way kept as the fallback, and add tests to
   `tests/test_native.py` that feed the same machine through both ways and
   compare the results.
4. If an existing answer changes shape, raise `ABI` in `src/lib.rs` and in
   `core/native.py` together. A test checks that they match.

## Checking it against the old way

```
python native/check_parity.py
```

asks the built library every question on this PC and holds each answer against
PowerShell, `nvidia-smi`, `ping` and psutil. It only reads the machine. Run it
after changing a question, and on any PC whose hardware is different enough to
matter (another GPU vendor, no LibreHardwareMonitor).

`python tools/measure/helper_cost.py` times one answer of each kind.

## No outside code

The crate has no dependencies: `cargo build` downloads nothing and works
offline. The library is loaded into a program that runs as administrator, so
everything in it is either written in this folder or part of Windows:

* Windows functions are declared by hand in `src/win32.rs`;
* the JSON it reads and writes is `src/json.rs`;
* WMI is called through COM directly in `src/wmi_query.rs`;
* NVIDIA's `nvml.dll`, which the driver installs, is loaded from System32 only.

Keep it that way unless there is a strong reason: a dependency here is code
that runs elevated on every user's PC.
