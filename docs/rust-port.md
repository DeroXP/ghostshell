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

Python is no longer packed as one self-unpacking exe. The shell carries the
backend as a folder of files and unpacks it itself, into a folder only
administrators can write to: once per version, into a folder that stays
(the owner's choice; piece 4 below has the other way, which a build can
still be made for). The window is on screen before Python has finished
loading, and before the unpacking is done.

In between, the app is made of: the Rust shell (window, tray, start, quit,
routing), the Python backend (every route not yet moved), and the step 1
library, which the shell uses as an ordinary Rust crate and Python still loads
as a DLL.

Where the shell stands. It is cut into four pieces, each a pull request of
its own. `main` stays releasable after each, because no release uses any of
it before the last one is in and the owner says so; `build.spec` is not
touched until then.

1. **The core, without a window (written).** `shell_rs/core`: which request
   is answered where, the backend's start and stop, quitting with a time
   limit. The app can run as the shell's backend (`--backend`): without
   window or tray, on a port the shell picks, leaving when the shell does.
   Tested against a stand-in, and tried for real against the app on the
   fake machine of the tests. No outside crates.
2. **The window (written).** `shell_rs/app`: Tauri, as for the updater, on
   the app's own page; the few lines that stand in for
   `window.pywebview.api`; telling a second copy from the first, before
   anything asks for administrator rights; a start at sign-in out of sight.
   What a program without the app's rights could otherwise use is closed:
   the window is a private one in a folder made for one run and removed
   after it, WebView2 takes no settings from the environment, the window
   stays on the app's own page, a link is handed to Explorer and not opened
   with the app's rights, and signing out does not skip the quit.
   Tried for real against the app on the fake machine.
3. **The tray (written).** Its icon and menu, as today's: the same three
   pictures, the same entries, the same clicks. What an entry does is still
   the backend's to carry out, through calls that were added for it and
   that an ordinary start of today's app does not have. The entry that
   starts the app at sign-in names the shell when the app runs behind it.
   Tried for real against the app on the fake machine, without a mouse.
4. **One file (written; the owner chose to keep the backend unpacked).** The shell's
   exe carries the app: built as a folder (`backend.spec`), squeezed into
   one piece and compiled into the exe, and unpacked by the shell before it
   starts it. It is unpacked only into a folder the shell made itself in
   that run, or one it has checked and holds: the administrators' own,
   closed to everybody else, and holding exactly the files the exe carries.
   One switch says how it is kept, and the owner chose on 2026-11-25:
   (b), unpacked once per version into a folder that stays
   (`C:\ProgramData\Vispora-backend-kept-<version>-...`) and checked at
   later starts. That is what the build that ships does. (a), unpacked at
   every start and taken away at the quit so that nothing stays on the PC,
   is what a build made with `--features unpack-at-every-start` does.
   Measured with the real backend's files (57 MB in 206 files, in a trial
   that starts a stand-in in their place): (b) costs 108 ms once and 11 ms
   at later starts, and leaves 57 MB on the PC, also after `Vispora.exe`
   is deleted ("What stays on the PC" in
   [shell_rs/README.md](../shell_rs/README.md) says how to take them
   away); (a) costs 94 ms at every start. Either way the
   download is one file of 32.8 MB where today's is 28.2 MB, and the window
   is on screen after 36 ms without waiting for the unpacking. An update is
   for the shell's exe. The build that ships was built once with the real
   backend in it and never started.

**The first real start is the owner's.** Nothing here was ever run with
administrator rights, and the one-file exe was never started. Before a
release, on a real PC, in this order (stop at the first thing that is not
as described, and keep `%APPDATA%\Vispora\shell.log` and `vispora.log`):

1. Quit the installed Vispora. Build the one file (the three commands under
   "One file" in [shell_rs/README.md](../shell_rs/README.md); nothing has
   to be said for how the backend is kept: a build keeps it), and put it
   somewhere else than the installed copy, under the name `Vispora.exe`.
2. Start it by hand. Expected: Windows' prompt for administrator rights,
   then the window at once, then the first screen filling in. `shell.log`
   has one line about the unpacking: "the background program was unpacked
   into C:\ProgramData\Vispora-backend-kept-<version>-<16 letters>, to
   stay: 207 files, ... bytes, ... ms" (the app's 206 files and the
   graphics card's reader), and one that says that the graphics card has
   a reader of its own in that folder.
3. Look in `C:\ProgramData` while it runs: that one folder is there, with
   about 57 MB in it. An ordinary, not elevated, Explorer window or
   terminal must be refused when it tries to open it, and it must not let
   itself be renamed or deleted while the app runs. In `C:\Windows\Temp`
   there is one `Vispora-window-...` folder, closed in the same way, and
   no `Vispora-backend-run-...` folder.
4. Use the app as usual for a while: Game Mode on and off, the tray's
   menu with the mouse, "Clean now" on the cleaner page (the app must go
   on working afterwards), a link from the Drivers page (the browser must
   not run elevated). Open the Monitor page with Task Manager beside it:
   while the page is open there is a process `VisporaGpuReader.exe`
   (about 30 MB), the live charts move, and `shell.log` says "the
   graphics card's reader was started". About ten seconds after the page
   is left, that process is gone, and the log says why. (On a PC without
   an NVIDIA card it comes and goes at once, and the log says that it
   gave no reading.)
5. Quit from the tray. Expected: both processes gone within a few
   seconds, the settings put back, the window's folder gone from
   `C:\Windows\Temp`, and the folder in `C:\ProgramData` still there.
6. Start it again. `shell.log` must now say that the folder was found and
   checked, not unpacked: "the background program is in
   C:\ProgramData\Vispora-backend-kept-... from an earlier start, and is
   what the exe carries: 207 files, ... bytes, checked in ... ms". No line
   about unpacking for this start, and no second folder.
7. Start it a second time while it runs: the first copy's window must come
   to the front, with no prompt.
8. Switch "start at sign-in" on, sign out and in: the app must come up in
   the tray, without a prompt.
9. Quit it and take the folder away by hand, as "What stays on the PC" in
   [shell_rs/README.md](../shell_rs/README.md) says. The next start must
   unpack it again and say so in `shell.log`, as at step 2.
10. An update: only once a second one-file build with a higher version
    exists. The updater must swap the one exe and start it again. The new
    version's first start unpacks a folder of its own and takes the old
    version's away (`shell.log`: "folders of the background program from
    other versions: 1 taken away, ..."), so that one folder is left in
    `C:\ProgramData`, and nothing of either version in `C:\Windows\Temp`
    once the app has quit.

Decided while writing the core (the reasons are in
[shell_rs/README.md](../shell_rs/README.md)):

* The backend serves on a socket the shell makes and keeps, not on the
  fixed port 5987 and not on a port of its own. A port that was taken or
  reserved used to leave the window blank; and a port that is free for a
  moment is somebody else's to listen on, who is then sent the secret.
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

### Item 1, cut into pieces

Looked at again before the first module moved (2026-11-25), the seven are
not alike any more. Since steps 1 and 2 only three of them run all the
time: the game monitor (a scan every three seconds), the temperature
ceiling (the card's temperature every second) and the popup watchdog (every
visible window every second). Idle Mode is off unless a person switches it
on. The network monitor and the performance sampler run only while their
page is open; the hardware monitor from the first visit to its page until
its Stop button or the quit (found with piece 2: the page starts it and
does not stop it when it is left, so that its tiles are current when the
page comes back). And they differ in what they do: three only read the
PC, four change it.

Item 1 is cut by that, and what only reads goes first. One module to a pull
request, in this order:

| | Module | What it reads, and when | What it changes | Where it stops |
| --- | --- | --- | --- | --- |
| 1 | The performance sampler (`core/perf_monitor.py`) | the graphics card, five times a second, while the Monitor page is open | nothing | **moved**: `shell_rs/core/src/moved/perf.rs` |
| | The graphics card's own reader (not a module of the backend: what pieces 1, 2 and 5 read the card with) | the card, while something in the shell wants its readings | nothing | **written**: `VisporaGpuReader`, a small program the shell starts and ends (below). The backend's reading where it is away or has none |
| 2 | The hardware monitor (`core/hw_monitor.py`) | processor, card, memory, DPC time, ping: once a second while the Monitor page asks for its snapshot (it sleeps half a minute after the last one) | nothing | **moved**: its loop and its start, stop and snapshot, `shell_rs/core/src/moved/hw.rs`. Its benchmark (it writes a file) and its list of programs (PowerShell) stay the backend's (below) |
| 3 | The network monitor (`core/net_monitor.py`) | bytes per program and per network card, once a second while the Network page reads it | nothing in its loop | **moved**: its sampler and live, history and top, `shell_rs/core/src/moved/net.rs`. Pausing and resuming a program stay the backend's (process tuning, item 3); which game runs is asked of the backend until piece 7 (below) |
| 4 | The popup watchdog (`core/popup_killer.py`) | every visible window, once a second, always | closes Windows' own "get an app to open this link" windows | the first piece that changes something: looking and closing are kept apart, and the closing is never run against a PC by a test or a trial |
| 5 | The temperature ceiling (`core/temp_ceiling.py`) | the card's temperature, once a second, always | when it trips: resets the overclock | the reset is GPU overclock's and stays in Python: the shell watches and decides, and asks the backend to carry it out |
| 6 | Idle Mode (`core/idle_mode.py`) | whether a game runs, every twenty seconds, when switched on | the power plan, and the overclock | the power plan moves; the overclock is GPU overclock's and stays |
| 7 | The game monitor's looking (`core/game_profiles.py`, the scan) | which programs have a window, every three seconds, always | nothing, while no game runs | what a game's start changes is item 4 of the plan (game mode), and it reads Adaptive Tuning and GPU overclock at its border |

After piece 7 nothing in the backend runs while the app idles with no page
open, and item 1's last step can be taken: the backend is started only when
a page needs it. One thing stands before that step, found with the first
piece: the graphics card's reading while a driver is being installed
(below). The hardware monitor does not: it reads only while somebody looks
at it (it sleeps half a minute after the Monitor page's last snapshot,
piece 2), so the parts of a pass it asks of the backend (on a PC whose card
the reader cannot read, the card's, once a second) are asked while that
page is on screen and for half a minute after, and then no more.

Why the sampler first, though it is the coldest of the seven: it is the
smallest module that has everything a move needs (a loop of its own, state,
five calls, numbers that have to come out the same). So what every later
module stands on was laid down with the least else in the way:

* **The door to the machine** (`shell_rs/core/src/moved/mod.rs`,
  `Readings`): a question to the native helper by its name, and the time.
  It has a real side (the helper, compiled into the shell as an ordinary
  Rust library: the code the Python app loads as a DLL) and a made-up side
  (a table and a clock set by hand). Nothing that changes the PC can be
  reached through it: the helper's two actions are behind another function
  of its own, which the shell names nowhere. And the graphics card cannot
  be asked through it: the real side refuses every question about the
  card before the helper hears it.
* **The graphics card has a reader of its own.** To read the card, a
  program starts NVIDIA's library (`nvml.dll`) in its own process. A
  driver's installer has to replace that file, which is why today's app
  asks its helper to let go of it before it starts one, for fifteen
  minutes (`core/driver_updater.py`). Measured on the way
  (`python tools/measure/nvml_release.py`, on an RTX 5070 Ti with driver
  621.40): once that library has been started in a process, it stays in
  that process whatever is shut down or freed afterwards. Only the end of
  the process takes it out. The shell lives as long as the app does, so
  its own process never opens the library. The card is read by a small
  program of the shell's own, `VisporaGpuReader.exe`
  (`shell_rs/core/src/bin/gpu_reader.rs`; when it runs is decided in
  `shell_rs/core/src/moved/card.rs`), which is the native helper asked its
  one question about the card, in a process that can be ended:
  * **It runs only while a reading is wanted.** It is started for the first
    reading somebody asks for, and ended when nobody asked for one for 10
    seconds. Why 10: longer than the gap between two readings of anything
    that reads the card regularly (once a second at the slowest), so that
    it is not ended and started between two of them, and short enough
    that the library is held for seconds, not minutes, after the Monitor
    page was left.
  * **Before a driver's install it is gone.** The install begins with one
    call of the page (`POST /api/driver/install`), which goes through the
    shell. The shell ends the reader, waits until Windows says it is gone,
    and only then passes the call on. While such a call is on its way no
    reader is started, however many are on their way at once. If the
    backend answers that it started the installer, no reader is started
    for 900 seconds from that answer, which is how long the backend keeps
    off its own copy of the library. If it answers anything else, the call
    leaves nothing behind, and the next reading starts a reader again.
  * **Its times are counted on a clock that does not jump.** How long it
    has been idle, how long it stays away, how long a pause lasts: on
    Windows' own count of time gone by, as the helper counts its hold, and
    not on the PC's clock, which a person or Windows can set forward or
    back in the middle of an install. (A reading is stamped with the PC's
    clock, as Python stamps it.)
  * **Where it is away or has no reading, the reading is the backend's**
    (`GET /api/gpu/oc/live`, which is `gpu_overclock.read_live_state()`
    and nothing else, asked with the run's secret as the shell asks for
    the tray): during those 900 seconds, on a PC whose card answers only
    to `nvidia-smi`, on a PC without an NVIDIA card, in a shell whose pack
    carries no reader. The reader's own answer is the helper's, read the
    way Python reads it. So either way a moved module's reading is the one
    Python would have taken, on every PC and at all times.
  * **A reading never waits for it without a limit.** Its first answer has
    3 seconds (its program is started and the driver's library loaded: 40
    ms on a warm disk), every later one 1 second (one takes a tenth of a
    millisecond). A reader that does not answer in time, answers what
    cannot be read, or says that it has no reading is ended; that reading
    is the backend's; and a reader is tried again after 30 seconds, as the
    helper itself waits after a driver stopped answering, then after 60,
    and so on up to 8 minutes, so that a PC without an NVIDIA card starts
    one now and then and not twice a minute for ever.
  * **It never outlives the shell.** It ends when its input ends, which
    Windows sees to when the shell is gone, and it is in a job of Windows'
    that is ended with the shell, also a shell that is ended by force and
    also a reader that hangs inside the driver. At a quit it is ended
    first.
  * **It gets two pipe ends and nothing else of the shell's**: no
    listening socket, no port, no named pipe, no file, no word on its
    command line. It is started as nothing else the shell starts is:
    Windows is given the list of the two handles by name.
  * **A stranger gets nothing from it.** It reads nothing of its command
    line, does not look at what comes in on its input (a line is a line,
    and gets the card's reading for an answer), and takes the one
    variable of the environment away in which the helper would look for a
    second place for the library. Started by anybody by hand, it reads the
    card and does nothing else. The helper's door for the things that
    change the PC is not named in it.
  * **It is carried in the pack, and started from where the pack is
    unpacked.** The shell runs with administrator rights, and so does what
    it starts. Its own exe is in a folder any program of the person's can
    write to, and a file under its name can be exchanged while the app
    runs: so the reader is not the shell's exe started with a word, and
    is never started by a path beside it. It is one more file of the pack
    (`_internal\VisporaGpuReader.exe`), unpacked with the backend into the
    administrators' folder, checked against the exe and held like every
    file there. The download is still the one `Vispora.exe`; it grows by
    0.1 MB.
  * **The one library it needs that is not Windows' own is beside it, or
    it is not started.** A Rust program asks Windows for
    `VCRUNTIME140.dll` when it starts, and Windows looks beside the
    program first, then in its own folder, then along the PATH. The app
    carries its own copy in `_internal`, which is why the reader is put
    there. So that this holds and is not luck: the exe that ships is not
    built with a pack that has the reader and not that file, and a shell
    whose pack lacks it does not start the reader. The other way would
    have been to build the reader so that it needs no such library. That
    takes either a made-up empty library put in the way of the real one
    for every program of the core, or a second build command for the
    reader alone, which is easy to forget and would need this check all
    the same.
* **What the shell takes, and what it passes on.** The shell answers a
  moved call only where it gives the answer Python gives. Anything else of
  the same path is passed on, so the backend answers it as before: another
  method, a body Python stops at before it does anything, and a number
  that Python may read and the shell cannot (more digits than the shell
  counts with, the digits of another script, a blank that is not a plain
  one).
* **The old way stays.** The Python module and its routes are not changed.
  They answer when the app is started as it always was, and behind the
  shell when it is started with `--all-to-backend`.
* **The same answers**, proven before anything is removed:
  `tests/test_moved_perf.py` asks the Python module (through its routes)
  and the shell's (in the trial tool) the same things on the same made-up
  machine, and compares every answer: 992 of them, among them the score
  for cards that idle, work steadily, jump about, come and go, and the
  samples of a card that answers only to `nvidia-smi`, or to nothing; of
  a reader that is away for a driver's install, that does not answer,
  that cannot be started. The made-up shell has a reader that says what
  the backend's helper says, and is told what the backend's call answers;
  the library in its own process says something else of the card
  throughout, and is never asked.
* **Tried for real, and measured**: `python shell_rs/app/try_it.py --moved`
  (the app's own page in the real window against the stand-in backend,
  with the reader unpacked from a pack and reading this PC's card: the
  driver's library is in the reader's process and never in the shell's,
  and no reader runs at the moment the backend hears of a driver's
  install), the same with the trial tool in `tests/test_moved_perf.py`,
  and `python shell_rs/try_it.py --moved` (the numbers, in
  [performance.md](performance.md)).

Two things are left open by the card's reading, and neither is decided
here:

* **A reading during a driver's install, once the backend is not always
  there.** For the 900 seconds the reader is away, a reading is the
  backend's, which then reads the card with `nvidia-smi` (a program
  started for every reading: 300 a minute for the sampler, 60 for the
  temperature ceiling). That is right while there is a backend. Item 1's
  last step takes the backend away while no page is open, and the
  temperature ceiling (piece 5) reads the card every second for as long
  as the app runs and must never be weaker than today, also in those 900
  seconds. What was found on the way, for piece 5 to decide with:
  * The 900 seconds are a guess at how long an install takes, made for
    the backend's helper. Nothing in the app sees the installer end.
  * The shell cannot start `nvidia-smi` itself without becoming what the
    port set out to end (a started program for every reading); the
    backend can be kept, or started, for exactly that time.
  * A reader that is started again too early holds the file the
    installer is replacing. A reader that is started late costs nothing
    but the backend's readings meanwhile.
  * On a PC whose card answers the helper not at all (only
    `nvidia-smi` does), the same question is there at all times, not
    only during an install.
* **The clean install's script.** `POST /api/gpu/clean-prepare` writes a
  script that the person runs by hand later, and that script removes
  NVIDIA's drivers. Today's app does not let go of the library for it,
  and neither the backend nor the shell can know when the script is run:
  a reader may be running then, as the backend's helper may be. Seen in
  the reading of this piece; not solved.
* **Today's app keeps the library too.** The same measurement, through the
  app's own helper: asked to let go, it shuts the library down and frees
  it, and both copies of the file (`System32` and the driver's own folder)
  are still loaded in the process afterwards. The helper has read the card
  for the app since 3.8.1 (before that, `nvidia-smi` was started for every
  reading, and ended). The temperature ceiling reads the card every
  second, so today's backend holds the file from soon after its start,
  through a driver's install as well. Whether NVIDIA's installer minds was
  not tried (no installer is ever started from here). It is a matter of
  the released app, left as it is here, and the owner's to decide. Behind
  the shell the same is true of the backend's own copy for as long as
  something in the backend reads the card (the temperature ceiling, until
  piece 5): the reader takes the shell's part of it away, not the
  backend's.

### Piece 2: the hardware monitor

`core/hw_monitor.py`, the Monitor page's tiles and sparklines: the
processor (temperature, load, clock), the graphics card (temperature,
load, clock, memory), the memory (in use, the standby list), the time
spent in DPCs and interrupts and DPCs a second, and a ping, once a
second, a minute of each kept. In the shell:
`shell_rs/core/src/moved/hw.rs`. The page starts it when it opens and
does not stop it when it is left (`static/js/app.js`, "Hardware Monitor
auto-runs"), and asks for its snapshot every three seconds while it is on
screen and the window is not hidden. Since the owner's yes of 2026-11-25
(below, "What was fixed after this piece") the monitor sleeps half a minute
after the last snapshot, in today's app as behind the shell, and the card's
reader goes ten seconds after its last reading. Until then it ran from the
first visit to the page until its Stop button or the quit, and kept the
reader running for as long.

* **What moves, and what stays.** The loop and the three calls that
  belong to it move: `POST /api/monitor/start`, `POST /api/monitor/stop`,
  `GET /api/monitor/snapshot`. Two calls of the module's stay the
  backend's, behind the shell too: the list of programs
  (`GET /api/monitor/processes`, one PowerShell `Get-Process`) and the
  benchmark (`POST /api/monitor/benchmark`, three PowerShell scripts, one
  of which writes 50 MB to a file in the temporary folder and deletes it,
  and a ping). Neither is a reading that is taken all the time: each is
  one button, pressed once. They move when a reading of their own is
  written for them, with the pages that only read (item 2), and the
  benchmark's file is a change to the PC, kept apart from what reads.
* **Where each reading comes from: where Python reads it.**
  * The processor's load and its time in DPCs and interrupts: the
    helper's `cpu_times` (Python reads the load through a call of its own
    to the same Windows function, and the two shares through the helper).
    The first pass, with nothing to compare with, reads twice a quarter
    of a second apart, as Python does; so does a pass after more than ten
    seconds without one (for the shares, as in Python).
  * Its clock: the helper's `cpu_clock`.
  * Its temperature: WMI through the helper, the four places Python asks
    in Python's order (LibreHardwareMonitor, OpenHardwareMonitor, the
    ACPI zone, the thermal zone's counter), the same questions word for
    word, each asked as the place allows (a place that had nothing is
    left alone for half a minute: below).
  * The memory in use and the standby list, and DPCs a second: two new
    questions of the helper's, `memory` and `dpc_rate` (`native/src/
    memory.rs`), which make the calls Python makes itself through ctypes
    (`GlobalMemoryStatusEx`, `GetSystemInfo`, `NtQuerySystemInformation`
    classes 80 and 23). Measured side by side on this PC: the same
    standby list to the megabyte, the same DPC rate. They only read.
  * The graphics card: its own reader (`Card::from_the_reader`, all of
    the helper's answer, the card's memory included), never the shell's
    own door; then LibreHardwareMonitor for what the card does not say,
    as Python does (a place too, left alone as the four are).
  * The ping: one echo request through the helper (below).
* **Where Python would start a program, the backend is asked for that
  part.** The shell starts none: not PowerShell, not `nvidia-smi`, not
  `ping.exe` (nothing in `moved/` can start a program, and a test holds
  that). Python starts one where its helper has no answer: PowerShell
  for the load, the clock, a temperature, the standby list or the DPC
  counters when those give nothing; `nvidia-smi` for a card the helper
  cannot read; `ping.exe` for a name or an address written another way.
  In each of those places the shell asks its backend for that part of
  the pass, `GET /api/monitor/part?name=...` (and `&target=` for the
  ping), a call that is there only behind the shell. The parts: `cpu`
  (the load and the clock), `temp_lhm`, `temp_ohm`, `temp_acpi`,
  `temp_thermal` (one place of the temperature each), `gpu` (the card's
  own numbers), `gpu_lhm` (LibreHardwareMonitor's sensors of the card),
  `ram`, `dpc`, `ping`. The backend takes the part with the very function
  its own loop would have used (`_cpu_load_and_clock()`,
  `_read_cpu_temp_via_lhm()`, `_gpu_from_the_card()`, ...), so the page is
  shown Python's answer, and the program is the backend's to start, as it
  is today. A place is read as it is: which places are quiet is the
  shell's to keep track of, as it is the loop's in Python. The same where Python would read a value in a way of its own
  that the shell does not copy (a sensor's value written as text, a name
  that is a list). The card's part is asked where the reader is away (a
  driver's install) or has no reading (no NVIDIA card, a card that
  answers only to `nvidia-smi`), and in a shell whose pack carries no
  reader. If the backend's function raises, its own loop would have
  thrown the pass away; the shell does the same. With no backend to ask,
  that reading is nothing for that pass.
* **One of each.** The backend's own monitor is started by its start call
  and by nothing else. Behind the shell that call reaches the backend
  only where Python stops before it starts anything (a body that is no
  JSON, or JSON that is something and no object): those are passed on,
  and the backend says no in its own words. A body that Python might read
  and the shell cannot is refused by the shell, not passed on (below).
  The calls are known however their path is written (`%73tart`,
  `/api%2Fmonitor%2Fstop`), as the backend's web framework reads a path.
  The shell takes the calls from the moment it is made, before its
  backend is started and before a page is shown; told to leave everything
  to the backend (`--all-to-backend`), it takes none of them and asks for
  no part, and the backend's own monitor runs as it does today. What the
  monitor keeps (its minute of readings, what a pass compares with) is in
  the shell: a backend that is ended and started again changes nothing
  of it (tried in the window). An ordinary start of today's app has no
  shell and no part call (it answers as a path that does not exist).
* **It only reads.** The standby list is read, never emptied (emptying it
  is one of the helper's two actions, behind its other door, which the
  shell names nowhere). Starting and stopping the monitor starts and
  stops a thread of the shell's own.
* **The ping target comes from the page's start call** (`ping_target`;
  the page sends none, so it is `1.1.1.1`, as in Python). The shell sends
  one echo request a second, 32 bytes of the helper's own, with half a
  second for the answer, to that target, and only if the target is a
  text that is an IPv4 address written plainly: four numbers of one to
  three digits, none above 255, none with a nought before it, nothing
  around them. It looks no name up and sends nothing anywhere else. For
  any other text Python starts `ping.exe`, and the shell asks the backend
  for that part, with that text. A target that is no text (a number, a
  list, `null`) gets no ping, from Python or from the shell. What the
  shell refuses (answers 400 and does not pass on, so that nothing is
  started anywhere): a start whose body is JSON in UTF-16 or UTF-32, has
  half of a character pair in it (escaped, or written as UTF-8 writes
  characters, which Python lets through), or is nested deeper than the
  shell reads (64 levels). No page sends any of these.
* **The same answers**, proven before anything is removed:
  `tests/test_moved_hw.py` asks the Python module (its routes, its loop
  run a round at a time) and the shell's (in the trial tool) the same
  things on the same made-up machine, and compares every answer, the
  snapshot after every pass included, a whole number where Python has one
  and a fraction where Python has that: 568 answers, among them 509
  snapshots, over 787 passes (and 20 rounds in which a monitor that slept
  took none), and 31 calls passed on and 7 refused. The loop is taken a
  round at a time on both sides (a pass, the rest of its second, and
  whether anybody still looks), and a pass may be taken without a look
  after it, so that the monitor can be left alone; how often each place
  was asked, and how often PowerShell was started for one, is counted on
  both sides and compared. The machine is the processor's times counting up, its clock,
  the memory, the DPC rate, WMI's rows for each of the four questions,
  the echo requests, the card, and what PowerShell, `nvidia-smi` and
  `ping.exe` print; Python's own calls to Windows read the same machine.
  Among the scripts: no LibreHardwareMonitor, no OpenHardwareMonitor, no
  ACPI zone, a thermal zone of the placeholder kind, WMI that gives no
  answer, a counter that gives nothing (and PowerShell that gives nothing
  either), a card that answers only to `nvidia-smi`, no NVIDIA card
  (LibreHardwareMonitor's sensors of an AMD card), no card at all, a
  reader that is silent, that garbles, that is away for an install, a
  pass that raises in Python, the PC's clock set back and forward, and
  eighteen kinds of ping target; and since the fixes below: a sleep and a
  wake (the PC busier while it sleeps, so that a first pass after the wake
  that compared with a reading from before the sleep would show), a Stop
  during a sleep, a start during one, a pass at the very
  edge of the half minute, the PC's clock far ahead while it sleeps,
  places that had nothing for a minute, a place that comes, one that
  misses once and one that goes, and the same where the helper cannot ask
  WMI and PowerShell is started instead. In every pass in which Python's own
  loop started a program, the shell asked its backend, and the page got
  Python's answer.
* **Tried for real, and measured.** `python shell_rs/app/try_it.py
  --moved`: the real window against the stand-in backend; the monitor
  reads this PC through the shell's own door (load, clock, memory, the
  standby list, DPCs a second, WMI, one echo request a second to
  127.0.0.1, which the trial names as the target), the card through the
  backend's part (the stand-in's fake machine); a target that is a name
  goes to the backend with that name; the backend is ended and started
  again and the monitor goes on; once stopped, the backend is asked
  nothing more; told to leave everything to the backend, the backend's
  own monitor runs and no part is asked. `python shell_rs/try_it.py
  --monitor`: the numbers, in [performance.md](performance.md).

What was fixed after this piece, on the owner's yes of 2026-11-25 ("Yes,
fix both"), in `core/hw_monitor.py` and `moved/hw.rs` together:

* **What it cost.** On this PC no source has a processor temperature, and
  the four questions for one were asked at every pass, by Python and by
  the shell alike: about half of a pass, and nearly all of the 3% of a
  core the monitor's own process used while it ran. Most of the work is
  not in that process: WMI's own process (`WmiPrvSE.exe`) answers the
  questions, and used about 17% of one core more while the monitor ran
  (measured from Windows' own list of every process's processor time).
  And it ran from the first visit to the Monitor page until its Stop
  button or the quit: about a fifth of a core for as long as the app ran
  after one look at that page.
* **It sleeps when nobody looks.** Its only reader is the Monitor page:
  of the module's five routes three are the loop's (start, stop,
  snapshot), and the page is all that calls them (when it opens, and every
  three seconds while it is on screen and the window is not hidden). The
  measuring tool that runs the loop for a minute (`shell_rs/try_it.py
  --monitor`) now asks as the page does; the two in `tools/measure/` take
  their passes by hand, without the loop. A snapshot, and a start, are
  somebody looking. When nobody has looked for 30 seconds (`IDLE_STOP_S`,
  the network monitor's), the loop ends by itself after its pass, by the
  clock that does not jump
  (`time.monotonic`, `steady`). "running" in the snapshot stays what the
  person set, on from Start until Stop, also while the loop sleeps, so
  the page needs nothing new. A snapshot that finds the monitor on and
  its loop asleep forgets what was read before the sleep (the tiles, the
  alerts and the minute of history are as in the first second after a
  start, and the first pass compares with nothing older than the sleep)
  and wakes the loop. A monitor that was stopped is never started by a
  snapshot. A start while it sleeps wakes it, and answers "Already
  running" as a start while it runs does. One of each holds through a
  sleep and a wake: the wake is decided with the monitor's state held
  (Python's `_run_lock`), so that eight who look at the same moment wake
  one loop (a test with real threads on each side). Behind the shell the
  card's reader then goes ten seconds after the monitor's last reading,
  by the rule it already had, and comes back with the next snapshot
  (tried in the window).
* **A place that had nothing is not asked again every second.** The four
  places of the processor's temperature and LibreHardwareMonitor's
  sensors of the card are each a place: one that had nothing (no
  namespace, no rows, no real temperature) is left alone for 30 seconds
  (`QUIET_S`), unless it gave a reading within the last 30 seconds, so
  that one miss of a source that works does not blank a tile for half a
  minute. The rule is the place's however it is asked: through the
  helper, or through PowerShell where Python has none. While a place is
  quiet the pass is what it would have been had the place been asked and
  had nothing, and the order of the places is as before. On a PC with no
  source the four are asked twice a minute instead of sixty times. A
  LibreHardwareMonitor that is started while the monitor runs is seen
  within half a minute instead of within a second.
* **What it costs now**, measured on this PC with WMI's process and
  service counted (the numbers in [performance.md](performance.md)):
  about 23% of one core before, in today's app and behind the shell
  alike; about 2 to 3% while the page is on screen; nothing half a minute
  after it is left. Of the places, the thermal zone's counter was nearly
  all of it: 242 ms a question, and about a sixth of a core of WMI's work
  when asked every second. The other four take 6 to 14 ms and no work of
  WMI's that can be told from the PC's own.

What is left open by this piece:

* **The backend's part keeps a previous reading of its own.** For the
  processor's load and the two shares, the backend's functions compare
  with their own reading before. The shell asks for those parts only
  where its own reading of the processor's times gave nothing, and then
  the backend's gives nothing too (the same Windows call), so the answer
  is PowerShell's, as in Python. A made-up machine could make the two
  differ (processor times that ran, then stood still between two parts);
  a running PC's processor times do not stand still.
* **What was never run by anybody**: the monitor with administrator
  rights; beside the real backend (its part call has answered only the
  stand-in, on the fake machine, and the trial's ping on a fake
  `ping.exe`); the part call answered by a backend that really starts
  PowerShell, `nvidia-smi` or `ping.exe` (the same-answers test runs the
  real functions with those programs made up); the card's part during a
  real driver's install; an echo request from the shell to anything but
  127.0.0.1 (the app's own target, 1.1.1.1, was pinged only by the
  measuring of Python's helper before this piece); a PC where WMI's
  LibreHardwareMonitor namespace is there (this PC has none, so the
  temperature was never read for real by either, and a LibreHardwareMonitor
  that is started while the monitor runs was seen within half a minute
  only on the made-up machine); the sleep and the wake beside the real
  backend, or in the released app (today's app ran them in its tests on
  the fake machine, and the shell in the window against the stand-in);
  and the two new questions of the helper's in the released app (they are
  not asked by Python).

### Piece 3: the network monitor

`core/net_monitor.py`, the Network page's live figures: the bytes a second
the network cards receive and send, the programs that move the most, a
minute of totals for the sparkline, and a warning when a program other
than the game moves 5 MB a second or more for three seconds in a row while
a game runs. In the shell: `shell_rs/core/src/moved/net.rs`.

* **What moves, and what stays.** The sampler and the three calls that
  read it move: `GET /api/network/live`, `/history`, `/top` (and HEAD of
  them: the backend's web framework answers HEAD by running the route,
  which would start the backend's own sampler). Pausing and resuming a
  program (`POST /api/network/pause-process`, `/resume-process`) change
  the PC: they stay the backend's, and no test or trial ever makes them.
  The other calls under `/api/network/` are the network optimizer's, no
  part of this module, and are passed on as they were.
* **It runs while something reads it, and stops by itself**, as Python's:
  a read starts it (and is answered with noughts until the second sample
  is in), it samples once a second, and it stops once nobody has read it
  for 30 seconds, counted on the clock that does not jump. A start forgets
  the programs and the cards' last counts, and keeps the minute of totals
  and the time of the last sample, as Python does. The rates are worked
  out over the time between two samples on the PC's own clock, as
  Python's are (a clock set back in between gives a rate over a
  thousandth of a second, in both).
* **Where each reading comes from: where Python reads it.**
  * The network cards' totals: a new question of the helper's,
    `network_cards` (`native/src/nics.rs`), which makes the calls psutil's
    `net_io_counters()` makes (`GetAdaptersAddresses`, then `GetIfEntry2`
    for each adapter) and fails where psutil would raise. It asks for the
    list without the addresses and DNS details psutil has Windows add to
    each adapter: with them, every call made Windows' DNS Client service
    work (measured below), and the list, its order and its names are the
    same either way (a test holds that). What psutil then
    does in Python is done in the shell the same way: two adapters of one
    name, the last counts; a count that goes back is taken to have started
    again, and the old count is added on from then on (psutil's
    `_WrapNumbers`, kept for the process's life as psutil keeps it). Held
    three ways: a made-up row with a number of its own at every place;
    this PC's rows against Windows' older call, which lays the row out
    another way; and side by side with psutil on this PC (same adapters,
    same order, each count between psutil's reading before and after;
    also in `native/check_parity.py`).
  * Every program's bytes: the helper's `process_io`, read as Python reads
    it. Where it has no answer Python reads them with psutil, one program
    at a time, in the backend; the shell asks its backend for that part
    (`GET /api/network/part?name=processes`, behind the shell only), which
    takes it with the module's own `_process_io_rows`. If that raises (or
    there is no backend), the pass is dropped after the cards were read,
    as a pass of Python's whose list of programs raised.
  * **Whether a game runs, and which**: the game monitor knows, and it is
    the backend's until piece 7. The shell asks once a pass, as Python
    reads it once a pass (`GET /api/network/part?name=game`, the module's
    own `_get_active_game_exe()`). That is one call to the backend a
    second while the Network page reads the monitor, and none otherwise.
    Where there is no backend to ask, no game is known for that pass: no
    program is taken for the game, and the page is told of none.
* **One of each.** The backend's sampler is started by a read of it and
  by nothing else, and behind the shell every read is the shell's: GET
  and HEAD of the three paths, written in whatever way the backend's web
  framework reads as them. A number of seconds or programs that Python
  may read and the shell cannot is refused (400), not passed on. Told to
  leave everything to the backend (`--all-to-backend`), the shell takes
  none of them, and the backend's sampler runs as today (tried in the
  window). What the sampler keeps is the shell's.
* **It only reads, and starts no program.** Both questions to the helper
  take no words; nothing of a call's text reaches the helper.
* **The same answers**, proven before anything is removed:
  `tests/test_moved_net.py` asks the Python module (its routes, its loop
  a round at a time) and the shell's (in the trial tool) the same things
  on the same made-up machine: 130 answers and 113 rounds compared, 9
  calls passed on, 2 refused. Python's psutil there is a made-up one that
  reads that machine; what psutil does with the counts in Python is
  psutil's own code where psutil is installed (the Python releases are
  built with) and the test's copy of it elsewhere, held against psutil's
  own. Among the scripts: the Network page for 75 seconds while a game
  starts and a download wakes up, programs that come and go, the page
  left and opened again; the helper without a list of programs (psutil's:
  a program it cannot read, one with no name, a listing that raises),
  cards that cannot be read, none, two of one name, a count that goes
  back, a card that goes and comes back; a program's count that goes
  back, an id given to another program; a burst with and without a game,
  one at exactly the line; the PC's clock set back and forward.
* **Tried for real, and measured**: `python shell_rs/app/try_it.py
  --moved` (the real window against the stand-in backend: the shell reads
  this PC's cards and programs, asks the backend once a pass which game
  runs, takes HEAD, passes another method on, stops by itself half a
  minute after the last read, and starts again on the next); `python
  shell_rs/try_it.py --network` (the numbers, in
  [performance.md](performance.md)).

What is left open by this piece:

* **The backend is asked which game runs once a second** while the
  Network page reads the monitor. That ends with piece 7, when the game
  monitor's looking moves into the shell.
* **A rate across a clock that was set**: worked out on the PC's own
  clock, as Python works it out, so a clock set back between two samples
  gives one enormous rate. Not changed: both sides would change together.
* **Behind the shell, the app's own programs are not all on the list of
  programs never taken for a burst**: it names `Vispora.exe`, the updater
  and `python.exe`, and not the backend behind the shell
  (`VisporaBackend.exe`) or the card's reader. Neither moves bytes to
  speak of. Not changed: the list is Python's, and the same in both.
* **What was never run by anybody**: the monitor with administrator
  rights, or beside the real backend (its part call has answered only the
  stand-in); the programs' part answered by a real backend with psutil;
  a real game during a real burst; the new question of the helper's in
  the released app (Python does not ask it); pausing or resuming a
  program, by anybody, from this piece.

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
