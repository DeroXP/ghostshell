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
| The network monitor's loop over every process | 26 to 28% of a core, always | Step 1 (done); step 2 stops it while nobody looks (done) |
| A program started for every reading | 80 a minute idle, about 250 in a game on 3.7.0 | Step 1 (done) |
| The Monitor page's loop | 9 programs and 3.3 s a pass | Step 1 (done) |
| The first screen waiting on 8 program starts | 4.4 s alone, a median 14 s in real starts | Step 2 (done) |
| 68 programs started during a start, in a process that throttled itself | 9 PowerShell and 4 `tasklist` among them | Step 2 (done for the readings), then step 4 |
| Unpacking 204 files at every start | 0.3 to 2.9 s, and again deleted at exit | Step 3 |
| Python and Flask loading before anything shows | 0.5 s and more | Step 3 |
| A game starting: 58 programs | mostly `powercfg` and `reg` | Step 2 took the reads (33 left); step 4 takes the writes |
| Quit | not measured yet | Step 3 owns it; measure first |

## Step 1. The hot readings (done, in 3.8.1)

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

## Step 2. The start-up readings (done)

Same library, same pattern, no change of shape. What a start waits for, and
what became of it:

1. **System information** (the first screen): seven PowerShell calls and an
   `nvidia-smi` became seven WMI queries through the `wmi` question the
   library already had, and one new question for the card's name. Measured on
   this PC: 3.6 seconds and 8 programs before, 0.1 seconds and none after,
   with the same data on the screen.
2. **Boot prep's two memory steps**: each compiled C# inside PowerShell at
   every start. They are two direct calls now. These are the first native
   calls that change something (they free memory), so they are the library's
   two **actions**: in a file of their own, behind an exported function of
   their own, which no question can reach.
3. **The dependency check**: `tasklist` and `sc query` became a look at the
   process list the library already had and a new service question, and the
   PowerShell that looks for LibreHardwareMonitor's sensors became a WMI
   query. `winget` is only run to install something that is missing, and
   stays.
4. **Registry and power-plan reads**: registry values are read directly
   (Python's own registry calls, no Rust needed), and the power plan through
   two new questions. The values a start used to write again at every launch
   (17 `reg` starts) are now read first and written only when they differ.

The two things that needed no Rust:

* **Efficiency mode after the start-up work, not before it**, when somebody
  opened the app and is waiting for it. A start at sign-in still throttles
  itself at once.
* **The network monitor only while its page reads it.** It starts with the
  page's first request and stops half a minute after the last. A running game
  does not keep it going, as this document first proposed: its only reader is
  that page, and a burst that is already under way shows there a few seconds
  after the page is opened.

What it bought, counted in the sandbox for a start on a PC the app has run on
before: programs started before the window opens 16 to 3, by the start-up
tasks 33 to 8, for the first screen 8 to 0. A game starting 57 to 33, a quit
during a game 24 to 16. The numbers and how they were taken are in
[performance.md](performance.md).

Not moved in this step, on purpose: the one `nvidia-smi` the overclock page's
capability check runs before the window opens. It belongs to the GPU overclock
module, which moves last.

If the first screen is still slow after this step, the rest is the window
itself and the unpacking before it, which is step 3.

## Step 3. The Rust shell

This is the step that changes what the app is made of.

**The updater first (written, not released).** `updater/` is a second, small
program (Flask and pywebview, its own exe). It is self-contained, so it is
rewritten whole, as a small Tauri app: `updater_rs/`. It is the practice run
for the shell: same window technology, a tenth of the surface, and it has to
change anyway because the app's files on disk are about to change.

What came of it (the details are in
[updater_rs/README.md](../updater_rs/README.md)):

* Two crates. `updater_rs/core` is everything the updater does, with no
  outside crates, tested case for case against the Python updater's tests.
  `updater_rs/app` is the window around it: Tauri 2.12.7, pinned, its lock
  file committed.
* One exe of 5.6 MB where the Python one is 14.5 MB. Same command line, same
  administrator prompt, same window; version 2.0.0, so the app keeps it over
  any Python updater once a release carries it.
* What Tauri costs: 219 crates from crates.io are compiled into the exe (418
  are in the lock file, which also covers other systems). 31 of them run a
  program of their own while building and 19 are extensions to the compiler.
* The Python updater stays in the repository, and stays the one that is
  built and shipped, until a release carries the Rust one and it has worked
  on a real PC. Nobody has run the Rust exe with administrator rights yet.

What the practice run showed, for the shell:

* The page did not have to change. Its `fetch('/api/...')` calls are answered
  inside the process, through a custom protocol, with no server and no open
  port. That is the shell's router in small: answer in Rust where a handler
  exists, pass the rest on.
* A title bar drawn by the page moves the window by its style sheet alone
  (`-webkit-app-region`), with no script.
* Where WebView2 is missing, opening the window does not fail, it waits for
  ever. The shell has to ask for WebView2 first, as the updater now does,
  and say what is wrong in a plain Windows message.
* No Tauri command line tool, no Node and no installer builder were needed:
  `cargo build` makes the one exe.
* A window can be tested without showing it: the updater's real exe is run
  against a made-up install by `updater_rs/app/try_it.py`.

**Then the shell (its core is written; nothing ships yet).** `Vispora.exe`
becomes a Rust program (Tauri on WebView2) that:

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

Where the shell stands. It is cut into three pieces, each a pull request of
its own. `main` stays releasable after each, because no release uses any of
it before the last one is in and the owner says so; `build.spec` is not
touched until then.

1. **The core, without a window (written).** `shell_rs/core`: which request
   is answered where, the backend's start and stop, quitting with a time
   limit. The app can run as the shell's backend (`--backend`): without
   window or tray, on a port the shell picks, leaving when the shell does.
   Tested against a stand-in, and tried for real against the app on the
   fake machine of the tests. No outside crates.
2. **The window.** Tauri, as for the updater; the tray, telling a second
   copy from the first, autostart; the shim for `window.pywebview.api`.
3. **One file.** `Vispora.exe` carrying the backend, and unpacking it once
   per version.

Decided while writing the core (the reasons are in
[shell_rs/README.md](../shell_rs/README.md)):

* The backend listens on a port the shell picks, not on the fixed 5987. A
  port that was taken or reserved used to leave the window blank.
* The secret that tells the app's own page from a stranger is made by the
  shell and handed to both sides. The backend gets it in its environment
  and takes it out again at once.
* A backend that ends with exit code 0 without having been asked takes the
  shell with it: that is how the app leaves for an update. One that ends
  any other way is started again, a few times.
* Quitting has a limit of 30 seconds, after which a backend that has not
  ended is ended. The window is out of sight from the first moment. How
  long a real quit takes is still not measured
  ([performance.md](performance.md)); the limit is meant to be far above
  it and should be looked at again once it is.
* If the shell dies, the backend notices, puts the system back and ends.

Settled before this step starts (2026-11-24):

* The shell is Tauri. Its versions are pinned and the lock file is committed.
  `native/` itself stays free of outside crates.
* The download stays one file named `Vispora.exe` if that can be done: the
  GitHub release asset, the standalone updater (it swaps one exe) and the
  local update folder all depend on it. One way: the shell carries the
  backend and unpacks it once per version, into a folder only administrators
  can write to, where today it is unpacked at every start. If a single file
  turns out not to work, that is the owner's decision, and the step stops
  there until it is made.
* The shell keeps the name `Vispora.exe`, so firewall rules, the autostart
  task and shortcuts keep working.

Still to be said out loud before it ships: a new exe starts with no reputation
at virus scanners and SmartScreen.

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
  reads it**: in separate modules, and in the library behind a separate
  exported function, as the two memory actions of step 2 are.
