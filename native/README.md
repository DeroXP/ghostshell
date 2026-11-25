# The native helper

A small Rust library that answers the questions Vispora asks most often,
inside the app's own process. Each of them used to start a program:
PowerShell, `nvidia-smi`, `ping`, `powercfg`, `sc` or `tasklist`.

It is the first two steps of the move to Rust described in
[docs/rust-port.md](../docs/rust-port.md): the readings the app takes all the
time (step 1) and the ones a start waits for (step 2). The numbers that
decided what goes in it are in [docs/performance.md](../docs/performance.md).

## What it does

It answers **questions**, and it carries out two **actions**. The two are kept
apart all the way down.

### Questions

Every question only reads the machine. No question can change a setting, a
process or the graphics card.

| Question | Answer | What the app did before |
| --- | --- | --- |
| `windowed_processes` | Every process that owns a visible main window: id, name, memory | `Get-Process` in PowerShell, every 3 seconds while idle |
| `process_io` | Every process: id, name, bytes of "other" I/O (which includes network traffic) | psutil opening each process in turn, every second |
| `gpu_live` | The NVIDIA card's temperature, load, clocks, power, fan and memory, read from the driver's NVML library | `nvidia-smi`, once a second or more |
| `gpu_release` | Lets go of the NVIDIA library for `hold_s` seconds (for a driver install) | not needed: `nvidia-smi` never stayed loaded |
| `cpu_clock` | The processor's current clock in MHz | `Get-CimInstance Win32_Processor` in PowerShell |
| `cpu_times` | Processor time in total, idle, in DPCs and in interrupts | `Get-Counter` in PowerShell |
| `memory` | How much of the memory is in use, and how many pages are on the standby list (read, never emptied) | not asked by the app: its hardware monitor reads both itself, and the Rust shell's asks here |
| `dpc_rate` | Deferred procedure calls a second, over all processors | the same |
| `ping` | One echo to an IPv4 address: answered or not, and how fast | `ping.exe` |
| `gpu_identity` | The NVIDIA card's name, the driver's version and the card's memory | `nvidia-smi`, at every start |
| `wmi` | The rows of one WMI `SELECT` query | `Get-CimInstance` in PowerShell (seven of them for the first screen) |
| `service_state` | Is a Windows service installed, and is it running | `sc query` |
| `power_scheme` | The active power plan, and every plan there is | `powercfg /getactivescheme`, `powercfg /list` |
| `power_setting` | One setting of one plan: its AC and DC values and its range | `powercfg /qh`, a dozen times when a game starts |
| `abi` | The library's version number | |

A question is a small piece of JSON and so is the answer:

```
{"q": "ping", "target": "1.1.1.1", "timeout_ms": 500}
{"ok": true, "data": {"ok": true, "ms": 11}}
```

A question that cannot be answered says why: `{"ok": false, "err": "..."}`.

### Actions

Two things here change something. Both free memory, both are what the app's
start-up preparation has always done (by starting PowerShell and compiling a
small C# program in it, twice at every start), and neither changes a setting:
nothing has to be put back afterwards.

| Action | What it does |
| --- | --- |
| `trim_working_sets` | Asks Windows to trim every process's working set. The pages come back as each process uses them. |
| `purge_standby_list` | Drops the standby list (cached file pages nobody is using). Needs administrator rights. |

They are in `src/actions.rs` and nowhere else, and they are reached through
their own exported function, `vispora_native_act`, with their own kind of
request (`{"do": "trim_working_sets"}`). A question, whatever it says, goes
through `vispora_native_call`, which has no way to them. The command-line tool
only asks questions. In the app, only `core/boot_prep.py` asks for an action,
and a test checks that this stays so.

A privilege an action needs is switched on for the calling thread only and
dropped when the action returns. The app's own security token is not touched.

An action is never carried out twice: the library refuses a reply buffer
under 4096 bytes before doing anything, and `core/native.py` asks once.

## How the app uses it

`core/native.py` loads `vispora_native.dll` and is the only code that talks to
it. Each function there returns `None` when the library is missing, is another
version, or could not answer, and the caller then asks the old way. So:

* a build without the library still works, only slower;
* a source checkout that never ran `cargo build` still works;
* the test suite never loads the real library (`tests/sandbox.py` answers for
  it, questions and actions alike), apart from one test that asks it a few
  read-only questions on a PC where it has been built and has it trim that
  test's own process.

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
there is one, the power plans, a service, a ping to this PC itself); all of it
read-only. The one action they carry out is trimming the test program itself.
No test trims another process or clears the standby list.

This makes `native/target/release/vispora_native.dll` (the library) and
`vispora-native-ask.exe` (the same answers from a terminal, for checking by
hand: `vispora-native-ask gpu_live`).

Build it before `pyinstaller build.spec`. The spec bundles the library when it
is there and prints a warning when it is not. A library that is there but is
an older version than `core/native.py` asks for stops the build: that is a
forgotten rebuild, and the app would refuse the library at run time.

## Adding a question

1. Write it in its own file under `src/`, returning `Result<Json, String>`,
   and add it to `dispatch` in `src/lib.rs`.
2. Add a function for it to `core/native.py` that checks the shape of the
   answer and returns `None` for anything else.
3. Use it in the app with the old way kept as the fallback, and add tests to
   `tests/test_native.py` that feed the same machine through both ways and
   compare the results.
4. Raise `ABI` in `src/lib.rs` and in `core/native.py` together whenever the
   app starts to rely on something an older library does not have: a new
   question it needs, or an answer that changed shape. A test checks that the
   two numbers match.

A new action needs a stronger reason than a new question. It goes in
`src/actions.rs`, is listed in `dispatch_action` (never in `dispatch`), and
its Python function calls `act()`, never `call()`.

## Checking it against the old way

```
python native/check_parity.py
```

asks the built library every question on this PC and holds each answer against
PowerShell, `nvidia-smi`, `ping`, psutil, `powercfg` and `sc`. It only reads
the machine, with one exception: it starts two scratch processes of its own
and trims one the old way and one through the library, to see that both do the
same. Run it after changing a question, and on any PC whose hardware is
different enough to matter (another GPU vendor, no LibreHardwareMonitor).

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
