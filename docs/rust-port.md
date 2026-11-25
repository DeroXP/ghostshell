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
on. The network monitor, the hardware monitor and the performance sampler
run only while their page is open. And they differ in what they do: three
only read the PC, four change it.

Item 1 is cut by that, and what only reads goes first. One module to a pull
request, in this order:

| | Module | What it reads, and when | What it changes | Where it stops |
| --- | --- | --- | --- | --- |
| 1 | The performance sampler (`core/perf_monitor.py`) | the graphics card, five times a second, while the Monitor page is open | nothing | **moved**: `shell_rs/core/src/moved/perf.rs` |
| | The graphics card's own reader (not a module of the backend: what pieces 1, 2 and 5 read the card with) | the card, while something in the shell wants its readings | nothing | **written**: `VisporaGpuReader`, a small program the shell starts and ends (below). The backend's reading where it is away or has none |
| 2 | The hardware monitor (`core/hw_monitor.py`) | processor, card, memory, DPC time, ping: once a second while the Monitor page is open | nothing | its benchmark writes a file, and its list of programs starts PowerShell: both stay in Python until they have a reading of their own |
| 3 | The network monitor (`core/net_monitor.py`) | bytes per program and per network card, once a second while the Network page is open | nothing in its loop | pausing a program is process tuning (item 3 of the plan) and stays; which game is running is the game monitor's to say, and is asked of the backend until piece 7 |
| 4 | The popup watchdog (`core/popup_killer.py`) | every visible window, once a second, always | closes Windows' own "get an app to open this link" windows | the first piece that changes something: looking and closing are kept apart, and the closing is never run against a PC by a test or a trial |
| 5 | The temperature ceiling (`core/temp_ceiling.py`) | the card's temperature, once a second, always | when it trips: resets the overclock | the reset is GPU overclock's and stays in Python: the shell watches and decides, and asks the backend to carry it out |
| 6 | Idle Mode (`core/idle_mode.py`) | whether a game runs, every twenty seconds, when switched on | the power plan, and the overclock | the power plan moves; the overclock is GPU overclock's and stays |
| 7 | The game monitor's looking (`core/game_profiles.py`, the scan) | which programs have a window, every three seconds, always | nothing, while no game runs | what a game's start changes is item 4 of the plan (game mode), and it reads Adaptive Tuning and GPU overclock at its border |

After piece 7 nothing in the backend runs while the app idles with no page
open, and item 1's last step can be taken: the backend is started only when
a page needs it. One thing stands before that step, found with the first
piece: the graphics card's reading while a driver is being installed
(below).

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
    and only then passes the call on. If the backend answers that it
    started the installer, no reader is started for 900 seconds, which is
    how long the backend keeps off its own copy of the library. If it
    answers anything else, things are as they were, and the next reading
    starts a reader again.
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
    (`_internal\VisporaGpuReader.exe`, beside the app's own copy of the C
    runtime it needs), unpacked with the backend into the administrators'
    folder, checked against the exe and held like every file there. The
    download is still the one `Vispora.exe`; it grows by 0.1 MB.
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
