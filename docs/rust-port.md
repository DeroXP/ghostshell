# Moving Vispora to Rust: the order

The goal is an app that starts, quits and idles fast, and stops slowing the
PC. The numbers in [performance.md](performance.md) decide the order: what
costs the most moves first.

The shape, as agreed:

* The interface stays as it is (`templates/index.html`, `static/js/app.js`).
* A Rust shell replaces pywebview and the PyInstaller start.
* The Python backend keeps running behind the shell while its parts move to
  Rust one module at a time, each with its tests.
* No rewrite in one go. After every step there is an app that works and can be
  released.

## What the numbers say

| What hurts | How much | Fixed by |
| --- | --- | --- |
| The network monitor's loop over every process | 26 to 28% of a core, always | Step 1 (done here) |
| A program started for every reading | 80 a minute idle, about 250 in a game on 3.7.0 | Step 1 (done here) |
| The Monitor page's loop | 9 programs and 3.3 s a pass | Step 1 (done here) |
| The first screen waiting on 8 program starts | 4.4 s alone, a median 14 s in real starts | Step 2 |
| 68 programs started during a start, in a process that throttled itself | 9 PowerShell and 4 `tasklist` among them | Step 2, then step 4 |
| Unpacking 204 files at every start | 0.3 to 2.9 s, and again deleted at exit | Step 3 |
| Python and Flask loading before anything shows | 0.5 s and more | Step 3 |
| A game starting: 58 programs | mostly `powercfg` and `reg` | Step 4 |
| Quit | not measured yet | Step 3 owns it; measure first |

## Step 1. The hot readings (this change)

A small Rust library, `native/`, loaded into the Python app, answers the
questions that were asked all the time: which processes have a window, every
process's I/O counters, the graphics card's live readings, the processor's
clock and DPC time, a ping, a WMI query.

The app is unchanged in shape. It is still Python, with one DLL beside it, and
every caller keeps its old way as the fallback.

What it buys, counted in the sandbox: programs started per idle minute 80 to
0, in a game 70 to 7, on the Monitor page 9 a pass to 0, and the per-process
loop becomes one call. Measured with the real library: a GPU reading 62 ms to
0.14 ms, the per-process reading 270 ms to 9 ms, a Monitor pass 3.3 s to
0.32 s.

The library uses no crates from outside the repository, so building it
downloads nothing. That is worth keeping for as long as it is loaded into the
elevated Python app. The shell in step 3 is another matter: Tauri brings a
few hundred crates with it, and choosing it is choosing that.

## Step 2. The start-up readings

Same library, same pattern, no change of shape. The things a start waits for:

1. **System information** (the first screen): seven PowerShell calls become
   seven WMI queries through the `wmi` question the library already has. This
   is the 4.4 seconds.
2. **Boot prep's two memory steps**: each compiles C# inside PowerShell at
   every start. They become two direct calls. These are the first native calls
   that change something (they trim memory), so they go in a separate, clearly
   named part of the library, not among the read-only questions.
3. **The dependency check**: `tasklist`, `sc query` and `winget` probes become
   a look at the process list the library already has and a service query.
4. **Registry reads and power-plan reads**: 20 `reg` and several `powercfg`
   starts before the window opens become direct calls.

Two things in this step need no Rust at all and are worth doing with it:

* Do the start-up work first and enter efficiency mode after it, not before.
* Run the network monitor only while something uses it (its page is open, or a
  game is running), not from the first second of every start.

Measure again after this step. If the first screen is still slow, the rest is
the window itself, which is step 3.

## Step 3. The Rust shell

This is the step that changes what the app is made of.

**The updater first.** `updater/` is a second, small program (Flask and
pywebview, its own exe). It is self-contained, so it is rewritten whole, as a
small Tauri app. It is the practice run for the shell: same window technology,
a tenth of the surface, and it has to change anyway because the app's files on
disk are about to change.

**Then the shell.** `Vispora.exe` becomes a Rust program (Tauri on WebView2)
that:

* opens the window straight away and shows the same interface files;
* owns the tray icon, the single-instance check, autostart, hide and show;
* starts the Python backend as a child process in the background;
* answers `/api/...` calls itself where a Rust handler exists, and passes
  every other call on to the Python backend unchanged;
* owns quitting: it tells the backend to put the system back, with a time
  limit, and exits.

The interface does not notice. A small shim keeps `window.pywebview.api`
working, so `app.js` is not touched.

Python is no longer packed as one self-unpacking exe. The backend ships as a
plain folder next to the shell, so nothing is unpacked at a start and nothing
is deleted at exit. The window is on screen before Python has finished
loading.

In between, the app is made of: the Rust shell (window, tray, start, quit,
routing), the Python backend (every route not yet moved), and the step 1
library, which the shell uses as an ordinary Rust crate and Python still loads
as a DLL.

Decisions this step needs from the owner before it starts:

* The install becomes a folder, not a single file. The updater, the autostart
  task and the "where am I installed" record all learn that.
* The shell keeps the name `Vispora.exe`, so firewall rules, the autostart
  task and shortcuts keep working.
* A new exe starts with no reputation at virus scanners and SmartScreen.

## Step 4. The backend, one module at a time

Each module moves behind the shell's router: write the Rust handler, port the
module's tests, switch its routes over, delete the Python. The order is by how
hot the code is and how much depends on it.

1. **Everything that runs all the time**: the game monitor, the temperature
   ceiling, the network monitor, Idle Mode, the popup watchdog, the hardware
   monitor, the performance sampler. Once these are in the shell, the Python
   backend has nothing to do while the app idles, and it can be started only
   when a page needs it. From then on an idle Vispora is one small Rust
   process. This is the largest single win after step 1.
2. **The pages that only read**: system information, capabilities, the
   dashboard, logs, settings files.
3. **Process tuning** (priorities, efficiency mode, affinity, the background
   pauser). It is already all direct Windows calls.
4. **Game mode**: the session itself, the registry and power-plan records and
   Competitive Latency. In Rust it writes the registry and the power plan
   directly, which takes the 58 program starts out of a game starting and the
   24 out of quitting during one.
5. **The crosshair overlay**. Also all direct Windows calls, and the part
   where steady timing matters most.
6. **The optimizer pages**: tweaks, network, privacy, services, debloat,
   scheduler, interrupts, timers, DNS, cleaner, disk analyzer, integrity scan.
   Large, but cold: they run when a button is pressed. Some keep calling
   Windows' own tools (DISM, bcdedit) from Rust, which is fine for something
   that runs once.
7. **Capture**: clips, screenshots, OBS, hotkeys.

## What goes last

* **GPU overclock and Adaptive Tuning.** They write clocks to the card, their
  logic is the most intricate in the app (about 10,000 lines, and as many
  again of tests), and a mistake costs a crash in a game. They move when
  the Rust side has a record behind it, tests first.
* **The vault and the license.** Stored data must stay readable across the
  move, so these wait until the pattern is routine.
* **Python itself.** When the last route has moved, the backend folder and
  the DLL bridge are deleted and the app is one Rust program and its
  interface files.

## Rules for every step

* **Measure before and after** with the scripts in `tools/measure/`, and put
  the numbers in [performance.md](performance.md).
* **Same answers.** A moved part is proven against the old one on the same
  fake machine before the old one is removed, as `tests/test_native.py` does
  for step 1.
* **The fake machine moves too.** The tests work because the registry, the
  command runners and the process API can be swapped for fakes
  (`tests/sandbox.py`). The Rust side needs the same from its first handler:
  system access behind an interface that has a real and a fake
  implementation, so the 1,200 tests have somewhere to go.
* **The old way stays until the new one has shipped.** Step 1 keeps
  PowerShell as the fallback; later steps keep the Python route reachable
  until a release has run on the Rust one.
* **Anything that changes the system is kept apart from anything that only
  reads it**, in separate modules, as it is in step 1.
