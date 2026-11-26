# Where the time goes

Measured on 2026-11-24, before any of the app moved to Rust. This is the
baseline the port is steered by: [rust-port.md](rust-port.md) is the order that
follows from it, and [native/](../native/README.md) is the first piece.

Each step of the port adds a section with the same things measured again:
[the first piece](#what-the-first-native-piece-changes) (the readings taken
all the time) and [the second](#what-the-second-piece-changes) (the readings a
start waits for).

Nothing here is an estimate from reading the code. Where something could not
be measured it says so.

## The short version

1. **One thread used a quarter of a processor core all the time.** The network
   monitor reads every process's I/O counters once a second by opening each
   process in turn. On the running app that thread alone was 26 to 28% of one
   core and nine tenths of all the processor time the app had used in three
   days.
2. **The app starts a program for almost every reading.** About 258 a minute
   on the running app during a game (251 of them `nvidia-smi`), and 80 a minute
   doing nothing at all in the current code. Each one also gets a console host
   from Windows, so twice as many processes are really created. Together they
   used another 16% of a core.
3. **The first screen waits on eight program starts.** System information is
   seven PowerShell calls and one `nvidia-smi`: 4.4 seconds when nothing else
   is running. In real starts the first screen had its data a median 14 seconds
   after the app logged its first line.
4. **Every start unpacks the whole app.** 204 files, 46 MB, into a new
   temporary folder, deleted again at exit. That was 2.9 seconds on the last
   real start.
5. **The app throttles itself first.** The first thing a start does is put its
   own process in efficiency mode (below normal priority, slow cores), and
   then it does all of its start-up work in that state.
6. **The Monitor page cannot keep its own pace.** Its "once a second" loop
   starts nine programs a pass and takes 3.3 seconds a pass.

Quitting could not be measured without quitting the owner's running app. What
is known about it, and how to measure it, is at the end.

## How it was measured

Four ways, all of which leave a live install alone:

* **The running app, watched from outside.** The installed 3.7.0 had been
  running for 67 hours. `tools/measure/live_sample.py` reads its processor
  time, its threads and every program it starts from the same Windows call
  Task Manager uses. It opens nothing and changes nothing.
* **The app's own log.** `tools/measure/log_timeline.py` times every start in
  it (13 by hand, 169 at sign-in, several versions).
* **The test sandbox.** `tools/measure/count_starts.py` calls each start-up
  task, runs the watchers for a minute, starts a game and quits, on the fake
  machine the tests use, and counts every program the code asks to start. This
  is the current code (3.8.0), not the installed version.
* **Single commands, for real.** `tools/measure/process_cost.py` times the
  read-only commands the app runs most. `tools/measure/real_reads.py` runs the
  system information query and one Monitor pass for real, with the data folder
  redirected. `tools/measure/unpack_cost.py` replays the self-unpack.

The PC: a desktop with 24 logical processors and an NVIDIA card, Windows 11.

**One thing to keep in mind.** A game was running the whole time, and the
installed app's game mode had put background programs in efficiency mode,
the measuring scripts included. Single-command timings are therefore on the
slow side of what an idle PC would show. The counts are not affected.

## Start-up

### Before Python runs

| | |
| --- | --- |
| Files unpacked at every start | 204 files, 46.2 MB (the exe is 26.9 MB) |
| Time between the unpacker starting and the app starting, last real start | 2.9 s |
| The same unpack replayed with everything already in the disk cache | 0.3 s |
| Deleting the folder again at exit (replayed) | 0.03 s |
| Importing the app's modules (from source) | 0.5 s |

The 2.9 seconds was a start at sign-in, the worst moment for the disk. The 0.3
second replay is the floor: it does not include the virus scanner looking at
200 freshly written DLLs as they are loaded.

`build.spec` asks for UPX compression, but UPX is not installed on the build
PC, so it has never been in effect.

Of the 0.5 second import, Flask is 135 ms and the app's own modules 240 ms
(the crosshair, GPU overclock, Adaptive Tuning and the vault are the largest).

### From the first log line to a usable window

From the log, seconds after "Starting Vispora":

| | Started by hand (13 starts) | At sign-in (169 starts) |
| --- | --- | --- |
| Window created | 1 (1 to 5) | 3 (0 to 8) |
| First screen has its data | **14 (6 to 44)** | 11 (6 to 33) |
| Dependency check finished | 12 (8 to 60) | 22 (16 to 47) |
| Boot prep finished | 3 (2 to 9) | 64, by design (it waits a minute) |

Medians, with the fastest and slowest in brackets.

### Programs started during a start

Counted in the sandbox, current code:

| When | Programs started | What they are |
| --- | --- | --- |
| Before the window opens, one after the other on the main thread | 28 | 20 `reg`, 3 `nvidia-smi`, 2 `powercfg`, 2 `ffmpeg` probes, 1 PowerShell |
| Background tasks | 40 | 12 `reg`, 8 PowerShell, 4 `tasklist`, 4 `powercfg`, 4 `ffmpeg` probes, 3 `sc`, 3 `winget`, and others |
| The first screen's data | 8 | 7 PowerShell, 1 `nvidia-smi` |

The first screen's query, run for real with nothing else going on: **4.4
seconds**. Its slowest parts are one PowerShell call for the processor (1.3 s)
and one for the disks (1.0 s). During a real start it competes with the 40
background starts above, in a process that has throttled itself.

Boot prep compiles a small C# program inside PowerShell twice at every start
(to trim memory and to clear the standby list). Its own duration in the log:
median 3.3 seconds, up to 8.

## Doing nothing

### The running app (3.7.0, a game running)

Two minutes, watched from outside (two samples, twenty minutes apart; the
second is shown, the first agreed within two points):

| | |
| --- | --- |
| The app's own processor time | 35.4 s in 120 s: **30% of one core** |
| Of which one thread, the network monitor | 31.4 s: 26% of one core, 98% of it inside Windows, 3,800 context switches a second |
| Next busiest threads | game monitor 1.2%, a thread started that afternoon 1.1%, temperature ceiling 0.3% |
| Since the app started 67 hours earlier | 38,108 s: 15.7% of one core on average. That one thread: 34,181 s of it |
| Programs it started | 517 in 120 s: **258 a minute** |
| ... `nvidia-smi` | 251 a minute |
| ... `powercfg` | 4.5 a minute |
| ... PowerShell | 3 a minute |
| Console hosts Windows created for them | 530, one each |
| Processor time of those programs and their console hosts | 19.7 s in 120 s: 16% of one core |

So the app and what it starts were using about 46% of one core, continuously,
while a game was running. On 24 processors that is under 2% of the machine,
but it is a steady stream of process creations, each of which the virus
scanner and Windows itself also have to handle.

### The current code (3.8.0), counted in the sandbox

Programs started per minute:

| State | Per minute | From |
| --- | --- | --- |
| Idle, default settings | **80** | 60 `nvidia-smi` (the temperature ceiling asks every second) and 20 PowerShell (the game monitor looks for a game every 3 seconds) |
| Idle, with Idle Mode on | 83 | 3 more PowerShell |
| In a game | 70 | 60 `nvidia-smi`, 6 `powercfg`, 3 PowerShell, 1 `reg` |

The running 3.7.0 started more in a game than this because its game mode also
kept the performance sampler running, which asks `nvidia-smi` as fast as it can
answer. In 3.8.0 that sampler only runs while the Monitor page is open.

The temperature ceiling is on by default, so the 60 a minute applies to every
user with an NVIDIA card.

### What one program start costs here

| Command | Time | Processor time |
| --- | --- | --- |
| PowerShell, doing nothing | 217 ms | 266 ms |
| PowerShell: which processes have a window (the game monitor) | 327 ms | 406 ms |
| PowerShell: the processor's clock | 1,305 ms | 344 ms |
| PowerShell: DPC and interrupt time | 370 ms | 438 ms |
| `nvidia-smi`, one query | 62 ms | 31 ms |
| `tasklist` with a filter | 236 ms | 16 ms |
| `ping`, one echo | 30 ms | under 16 ms |
| `reg query`, `powercfg` | 16 ms | under 16 ms |
| Any program at all (`cmd /c exit`) | 21 ms | under 16 ms |

At idle that is 60 x 31 ms + 20 x 406 ms, about 10 seconds of processor time
every minute for readings nobody is looking at.

## The Monitor page

One pass of its loop, run for real on this PC:

| | |
| --- | --- |
| Programs started | 9: 7 PowerShell, `nvidia-smi`, `ping` |
| Time | **3.3 s** |
| Processor time of those programs | about 2.4 s, three quarters of a core for as long as the page is open |

The loop is written to run once a second, so the page really gets a sample
every 3.3 seconds, and starts about 160 programs a minute.

Two things turned up on the way:

* Four of the seven PowerShell calls were looking for a processor temperature
  in four places. Measured without administrator rights none of them found
  one. Inside the app LibreHardwareMonitor answers the first and the other
  three are not run.
* The DPC query failed every time on this PC (its performance-counter library
  is damaged, as an older comment in the code already says), so it costs 0.4
  seconds a pass for nothing.
* The GPU tile was blank. The current NVIDIA driver prints the temperature as
  `55.10`, the code read it with `int()`, which raises, and all four GPU
  numbers were dropped. Fixed in this change (see the first native piece).

## Quitting

**Not measured for the current version.** A real quit means quitting the
owner's running app, which is theirs to do.

What is known:

* Counted in the sandbox, the quit path starts no program at all when no game
  is running, and 24 when one is (22 `powercfg`, 2 PowerShell): about a second.
* Deleting the unpacked folder took 0.03 seconds in the replay.
* The log holds four quits in its whole history. In one (version 3.6.2) Quit
  was pressed twice, eight seconds apart, and the process was still running
  the first one. That version still had the controller emulator, which is
  gone, so it says little about today.

What the quit path does, from reading it (not measured): it undoes things one
after the other on a single thread, and several of the steps wait on something
else: Idle Mode puts the overclock back and waits for the card to confirm, the
clip buffer waits for FFmpeg to stop, frame telemetry waits for PresentMon.
Then it sleeps half a second, exits, and the unpacker deletes its folder.

**To measure it:** with the app in the state it is normally quit from, note the
time, choose Quit from the tray icon, and note when the tray icon is gone.
Then send the last 40 lines of `%APPDATA%\Vispora\vispora.log`. The log lines
between "shutting down" and the end show which step the seconds went to.

## What the first native piece changes

Counted the same way, with the native helper answering:

| | Before | With the helper |
| --- | --- | --- |
| Idle, programs started per minute | 80 | **0** |
| Idle with Idle Mode on | 83 | **0** |
| In a game, per minute | 70 | **7** (6 `powercfg`, 1 `reg`) |
| Monitor page, per pass | 5 in the sandbox, 9 on this PC | **0** |
| A game starting | 58 | 57 |
| Quit during a game | 24 | 24 |
| Start-up and the first screen | 76 | 76 (the next piece) |

And the network monitor's loop over every process becomes one call that
returns all of them.

Those are counts, from the sandbox with a stand-in for the helper. What one
answer costs was then measured with the real library on this PC
(`tools/measure/helper_cost.py`, 200 answers each):

| Question | The old way, per answer | The helper, per answer |
| --- | --- | --- |
| The graphics card's readings | `nvidia-smi`: 62 ms and a process | 0.14 ms |
| Processes with a window | PowerShell: 327 ms, 406 ms of processor time | 8 ms |
| Every process's I/O counters | psutil's loop: about 270 ms a pass in the running app (26% of a core at one pass a second) | 9 ms |
| Processor clock | PowerShell: 1,305 ms | 0.01 ms |
| DPC and interrupt time | PowerShell: 370 ms, and no answer on this PC | 0.1 ms |
| Ping | `ping.exe`: 30 ms | 0.2 ms plus the echo itself |
| A WMI query | PowerShell: 250 to 1,300 ms | 6 to 14 ms |

The 8 and 9 ms are Windows' own call for the whole process list (5 to 8 ms
here); it is the call Task Manager makes.

One whole pass of the Monitor page, run for real (`real_reads.py --helper`):

| | Before | With the helper |
| --- | --- | --- |
| Programs started | 9 | 0 |
| Time | 3.3 s | 0.32 s |
| GPU temperature | none (the blank tile) | read |
| DPC time | none on this PC | read |

Of the 0.32 seconds, 0.28 is the last of the four temperature sources, which
fails slowly on this PC. Where the first one answers (LibreHardwareMonitor,
inside the app) the other three are not asked.

Adding up the measured parts for an idle minute with the helper (the
temperature ceiling at 0.14 ms a second, the network monitor at 9 ms a second,
the game monitor at 8 ms every three seconds) gives about 1% of one core,
where the same three cost a quarter of a core plus 80 program starts. That sum
is arithmetic, not a measurement: the real figure needs a build with the helper
running for a while, watched with `live_sample.py`.

### The same answers

`native/check_parity.py` asks the real library every question and holds the
answer against PowerShell, `nvidia-smi`, `ping` and psutil on the same PC: 30
of 30 checks agree. The same 11 processes with a window, by id and name, with
the same memory figures; all 316 processes' counters between psutil's reading
before and after; the card's limits, maximum clocks and memory total exactly;
the processor's clock exactly; four WMI classes row for row.

Two differences are known and deliberate:

* **The GPU temperature can read one degree higher.** The driver reports
  whole degrees, rounded, and that is what the helper returns. Current drivers
  make `nvidia-smi` print decimals (`53.64`), which the old code cuts off (53)
  where the driver itself says 54. On drivers that print whole degrees the two
  are the same.
* **A few protected system processes have a name** in the helper's list
  ("Secure System") where psutil had none.

## What the second piece changes

Step 2 of [the order](rust-port.md): the readings a start waits for now go
through the native helper, and two things that needed no Rust were fixed with
it. Measured the same day, the same ways.

### The first screen

Run for real on this PC (`real_reads.py`), the system information the first
screen waits for:

| | Before | With the helper |
| --- | --- | --- |
| Programs started | 8 (7 PowerShell, 1 `nvidia-smi`) | 0 |
| Time | **3.6 s** | **0.1 s** |

(The 4.4 seconds further up was taken while a game was running. This time
the PC was idle: the graphics card at 0% and 34 degrees.)

The data is the same. Read both ways one after the other on this PC, every
field agreed: Windows, the processor, the card and its driver, the memory
sticks, both drives and their type, the adapter and its DNS servers. Only the
two figures that move between two looks (memory in use, free disk space) were
left out of the comparison.

What one of its answers costs (`helper_cost.py`): a WMI query 7 to 20 ms, the
card's name and driver 0.01 ms. The slowest PowerShell call it replaces took
1.25 s.

### A start

Counted in the sandbox. The fake PC begins as one the app has never run on, so
`count_starts.py` now makes the start twice: the first start on a PC, and a
later start, which is every start after that.

Programs started by **a later start**:

| | Before (3.8.1) | Now, without the helper | Now, with the helper |
| --- | --- | --- | --- |
| Before the window opens | 16 | 6 | **3** |
| Background tasks | 33 | 25 | **8** |
| The first screen's data | 8 | 8 | **0** |
| All of it | 57 | 39 | **11** |

And by the **first start on a PC** (one-time repairs, values written for the
first time, the power plan switched):

| | Before (3.8.1) | Now, without the helper | Now, with the helper |
| --- | --- | --- | --- |
| Before the window opens | 28 | 24 | 20 |
| Background tasks | 40 | 40 | 23 |
| The first screen's data | 8 | 8 | 0 |
| All of it | 76 | 72 | 43 |

The middle column is the part that needed no Rust: the app wrote 17 registry
values again at every launch (nine for the Game Bar handlers before the window
opened, eight in the start-up preparation), a `reg` start each. They are read
first now, directly, and written only when one is missing or different.

What a later start still starts, with the helper:

* before the window: one `nvidia-smi` (the overclock page's capability check,
  left where it is until the overclock module moves) and two FFmpeg probes;
* in the background: `ipconfig /flushdns`, and on the sandbox's PC, which has
  nothing installed, four more FFmpeg probes and three `winget` calls to
  install what is missing. On a PC that has FFmpeg, a probe that finds it is
  now remembered for the rest of the run, and those four are none.

### The start-up preparation's two memory steps

Each started PowerShell and compiled a small C# program in it, at every start.
They are the helper's two actions now, kept apart from its questions (see
[native/README.md](../native/README.md)).

| | The old way | The helper |
| --- | --- | --- |
| Trimming one scratch process (`memory_step_cost.py`) | 223 ms: PowerShell, the compile, the call | 0.03 ms |

That is one process, started by the script for the purpose. Neither way was
run over every process here, and the standby list was not cleared: on a PC in
use both would be felt. So what the two steps take in a real start with the
helper is **not measured**. The old way's figure for the whole preparation is
the one from the log further up, a median 3.3 seconds.

### A game starting, and quitting during one

Counted in the sandbox with the helper answering:

| | Before | Now |
| --- | --- | --- |
| A game starting | 57 programs | **33** |
| Quit during a game | 24 | **16** |

The 24 and 8 that went were all reads: the active power plan asked for again
and again, single power settings, three registry values. What is left writes
something (`powercfg` setting a value, `reg add`), and moves with game mode
itself in step 4. One power-setting read costs 0.6 ms through the helper,
against a `powercfg` start of 16 ms.

### The two fixes that needed no Rust

**Efficiency mode after the start-up work.** A start somebody is waiting for
now does the first screen, the start-up preparation and the dependency check
at normal priority, and enters efficiency mode when all three are done (or
after 60 seconds, whichever comes first). A start at sign-in still throttles
itself at once, as before. What this does to the median 14 seconds is **not
measured**: it needs a real start. The log says when it happened: "Start-up
over after N s: now in efficiency mode".

**The network monitor only while its page reads it.** It ran from the first
second of every start: the thread that was 26% of a core on the installed
3.7.0, and 3.5 ms every second (`helper_cost.py`) once the first piece made it
one call. Now it starts when the Network page first asks and stops 30 seconds
after the last request. At idle, with the page closed, it does nothing.

With both pieces, an idle minute adds up to: the temperature ceiling at 0.1 ms
a second and the game monitor at 3.5 ms every three seconds. About a tenth of
a percent of one core, and no program started. As before that is arithmetic
from the parts, not a measurement of the running app.

### The same answers

`native/check_parity.py` now makes 56 checks on this PC, and all 56 agree. The
26 that are new with this piece:

* the card's name, driver version and memory as `nvidia-smi` prints them;
* the first screen's five WMI classes row for row and in the same order, the
  DNS servers per adapter, and the drive type decided by the same disk;
* seven services (running, stopped, and not installed) as `sc query` has them;
* the active power plan and the list of plans as `powercfg` prints them, and
  seven processor settings on each plan as `powercfg /qh` does: values,
  limits, hidden settings, and the ones that do not exist;
* trimming a scratch process through the helper leaves it as the old
  PowerShell leaves another: 310 MB down to under 1 MB, both still running.

One existing answer changed: a WMI property that is an unsigned 32-bit number
(a graphics adapter's memory) came back negative above 2 GB, and is now the
number PowerShell prints.

### What is still not measured

* **A real start.** Everything above is the parts: counted in the sandbox, or
  timed one by one. How long a real start takes now, from the click to a
  screen with data, needs the owner to start a build that has this in it:
  `python tools/measure/log_timeline.py` reads it from the log afterwards.
* **The running app**, as after the first piece: `python
  tools/measure/live_sample.py 120` beside it.
* **Quitting**, still. The steps are at the end of the section on quitting.

## The shell (step 3), so far

The shell's core and its window exist (`shell_rs/core`, `shell_rs/app`).
What can be timed is the shell with a backend that is **not the real one**:
the app on the fake machine of the tests (`python shell_rs/try_it.py` for
the core alone, `python shell_rs/app/try_it.py --time` for the window; five
runs each, the middle value). Which of these numbers say something about a
real PC and which do not:

| | Today | The shell, in the trial | What the trial's number is worth |
| --- | --- | --- | --- |
| The shell's process is up | (does not exist) | 12 ms | Real: it is the shell's own exe. Without the window, which will add to it |
| The page can be handed to a window | after the unpacking (0.3 to 2.9 s) and Python and Flask (0.5 s and more) | **14 ms** after the shell's process started | Real for the shell. It does not wait for the backend: the page is compiled in |
| A window on screen | 1 s (1 to 5) after the first log line, by hand, which itself comes after the unpacking | **36 ms** after the shell's process started | Real: the shell's own exe and window, drawn beyond the edge of the desktop. Not the build that asks for administrator rights, whose prompt comes first |
| The first screen has its data | 14 s (6 to 44) after the first log line, by hand | 0.9 s after the shell's process started | A trial's: it waits for the backend, which here is the app on the fake machine with no start-up work. The window and the page in it are the real ones |
| The backend answers | (the same process) | 0.6 s after the shell started it | A trial's. Python 3.14 from source on the fake machine: no unpacking, no start-up work. A real backend does more |
| A call the page makes | straight to Flask | 0.8 ms through the shell and back (1.0 ms for the slowest in twenty) | Real for the way through the shell; the route asked is the cheapest one |
| A call the shell answers itself | (goes to Flask) | under 0.05 ms | Real |
| Quit, until both processes are gone | not measured (see Quitting) | 0.1 s until the backend has ended. With the window: 0.25 s from closing it until the shell's process has ended too, because the shell takes its window's folder away first and WebView2 has to let go of it. Quit from the tray's menu: 0.3 s | A trial's: on the fake machine a quit has nothing to undo. What is real: the fixed half second before the old exit is not waited behind the shell |

The tray icon costs the start nothing that shows: the window is on screen
after 36 ms with it, as without it. What the icon shows is asked of the
backend every three seconds, as today's tray asks: one call on this PC that
starts no program.

Taking away what an earlier run left behind is not in the window's way any
more. It is done once the window is there, on a thread of its own, and by
name first. In a trial: one earlier run's folder of 129 files, with 5,000
other files beside it, 20 ms on the shell's own clock, and the window on
screen after 36 ms in that run as in every other. (The place the app as it
ships uses, Windows' own folder for temporary files, often holds thousands
of files that are nothing of the app's.)

### Carrying the backend in the exe

The shell's exe carries the app's files and unpacks them before it starts
the backend ("One file" in [shell_rs/README.md](../shell_rs/README.md)).
There are two ways of keeping what was unpacked. The owner chose (b),
once per version, on 2026-11-25: it is what the build that ships does, and
(a) is still built and measured beside it.
Measured with `python shell_rs/app/try_it.py --time --pack <pack>
--carried-by <exe>`: a trial build of the shell that carries the real
backend's pack in itself, three starts each way, the middle value.

What is real in these numbers: the files (the app built as a folder with
PyInstaller 6.22.3 and Python 3.12: 206 files, 57,077,942 bytes), the pack
(26,059,148 bytes), the exe that carries it, the unpacking, the checking
and the taking away. What is a trial's: the place (the trial's own data
folder, closed to its own account, where the app as it ships uses folders
that are the administrators'), and the program that is started afterwards,
which is the stand-in and not the backend that was unpacked. Everything was
already in the disk cache.

| | Today's exe | (a) Unpacked at every start | (b) Once per version: first start | (b) later starts |
| --- | --- | --- | --- | --- |
| The download | 28,208,008 bytes | 32,842,752 bytes | the same | the same |
| Unpacking 57 MB (today: 46 MB), or checking the folder against the exe | 0.3 s replayed, 2.9 s at a sign-in | 94 ms (93 to 97) | 108 ms (unpacking, then the check) | 11 ms |
| The backend can be started, after the window was made | | 109 ms | 121 ms | 23 ms |
| A window on screen | after the unpacking | 36 ms | 36 ms | 36 ms |
| The first screen has its data (stand-in backend) | | 1.01 s | 1.02 s | 0.93 s |
| Quit, until the shell's process has ended (stand-in backend) | | 0.27 s | 0.24 s | 0.24 s |
| On the PC while the app is not running | nothing | nothing | 57.1 MB | 57.1 MB |

"Today's exe" is the one built on the development PC; its unpacking times
are the ones from "Before Python runs" above, for the installed 3.7.0. The
32,842,752 bytes are the build that is meant to ship, built once with the
pack in it and never started: 6.8 MB of shell and 26.1 MB of pack.

Why the shell unpacks faster than today's exe does, with more to unpack:
each file is squeezed by itself, so up to six are brought back at the same
time, and Windows does the work (its Compression API). The pack is squeezed
with LZMS, the smallest of the three ways Windows has. The other two, with
the same files: MSZIP 28,354,984 bytes; XPRESS with Huffman 30,026,295
bytes, which unpacks in 42 ms.

What is not in these numbers, and has not been measured: a start with
nothing in the disk cache (after a restart, at sign-in); and the virus
scanner, which looks at program files when they are written and when they
are loaded. In (a) all 206 files are new at every start, as they are with
today's app. In (b) they are new once per version, and the check at later
starts reads files the scanner has seen. So the 86 ms between the two ways
here is the least the difference can be, not what it is on a slow or busy
disk: that is not known until it is tried on a real PC.

What the shell changes about waiting, whatever the numbers turn out to be:

* The page does not wait for Python any more. Its first calls do: they are
  held until the backend answers, and then go through.
* A quit takes the window away at once. What takes time afterwards (the
  backend putting the system back) happens out of sight, and has a limit
  of 30 seconds, after which the backend is ended and the next start puts
  back what was left.

Still to be measured, and by whom: when the page is first painted in the
window (the trial sees the window appear and the page's first call, not the
paint in between); the real backend's start behind the shell and a real quit
(the owner's PC, once a build exists that they choose to run).

## Step 4: the backend, one module at a time

Each module that moves from the backend into the shell gets a line here:
what it cost in the backend, and what it costs in the shell.

### The performance sampler (the first module)

The Monitor page's live charts and its stability score: the graphics card
read five times a second while that page is open, 300 samples kept
(`core/perf_monitor.py`, now also `shell_rs/core/src/moved/perf.rs`). It is
the coldest of the seven things the plan lists as running all the time: it
has run only while its page is open since 3.8.0. It went first because it
is the smallest, not because it was slow.

Measured with `python shell_rs/try_it.py --moved`: three shells, each with
a backend on the fake machine, whose card gives every reading the same.

* **With its reader**: the shell answers the sampler's calls itself, and
  the card is read by the graphics card's own reader, a small program the
  shell starts for it ([rust-port.md](rust-port.md), "Item 1, cut into
  pieces", says why the shell's own process does not read the card). It
  reads this PC's real card.
* **Asking its backend**: the shell answers the calls itself and has no
  reader, so every reading is a call to its backend. That is how it is
  while a driver is being installed, on a PC whose card gives the reader
  no reading, and in a shell whose pack carries no reader.
* **Passed on**: the backend's own sampler runs.

Each samples for a minute, and then the same call is made 300 times in
each (the middle value, and the slowest one in twenty). Processor time is
counted in the processor's cycles, not in the ticks Windows charges by:
work that comes in short bursts is charged wrongly by those.

| | In the backend, behind the shell | In the shell, asking its backend | In the shell, with its reader |
| --- | --- | --- | --- |
| One reading of the card | 0.14 ms through the helper ("What the first native piece changes"); a started `nvidia-smi` without it | the backend's reading, asked for: 0.87 ms (the slowest of 2,000: 1.7 ms), and 1.9 to 2.7 ms of the backend's processor time | **0.12 ms** (the slowest of 2,000: 1.2 ms) |
| The chart's data, a minute of it (`/api/perf/samples?seconds=60`) | 1.12 ms (1.34) | 0.13 ms (0.16) | **0.13 ms** (0.15) |
| The score (`/api/perf/score?seconds=30`) | 1.10 ms (1.32) | 0.011 ms (0.012) | **0.008 ms** (0.009) |
| `/api/perf/status` | 0.92 ms (1.18) | 0.003 ms (0.004) | **0.003 ms** (0.005) |
| Processor time while the sampler runs, of one core | the backend 0.06%, the shell 0.01%: 0.07% | the backend 0.94%, the shell 0.23%: 1.17% | the shell 0.04%, its reader 0.13%, the backend 0.01%: **0.18%** |
| While the Monitor page is closed | nothing | nothing | nothing (the reader is ended ten seconds after the last reading) |

The reader itself:

| | |
| --- | --- |
| From its program being started to its first reading | 40 ms (33 to 41 in seven starts): the process, and NVIDIA's library loaded and started in it |
| Its process while it runs | 31 MB in memory, 23 MB of them its own: nearly all of it NVIDIA's library |
| Its program | 0.29 MB; 0.14 MB of the download, squeezed in the pack |
| While nobody wants a reading | no process |

What is real in these numbers: the reader and its reading of this PC's
card (an RTX 5070 Ti, driver 621.40), the shell's sampling and its
answers, and a call's way through the shell to a backend and back. What is
a trial's: what the backend does for one reading of the card. On the fake
machine it reads a table, which costs nothing; on a PC it asks the helper
(the 0.14 ms above, five times a second: another 0.07% of one core in the
first two columns).

What it comes to: with its reader, the sampler behind the shell costs
about what the backend's own sampler costs on a PC (0.18% of one core
against about 0.14%), where it cost eight times that while every reading
was a call to the backend (the state of things between the first piece
and this one). The page's two calls every two seconds are answered in a
tenth of a millisecond where they took two. And none of it needs the
backend: with the reader, the backend is not asked once in the minute.

What the reader costs that the backend's sampler did not: a process start
(40 ms) when the Monitor page is opened, 31 MB while it is open, and the
middle column for as long as the reader is away. That is 900 seconds
after a driver's install was started, and at all times on a PC whose card
gives the helper no reading: there the shell pays the 1.17% while the
Monitor page is open.

The 1.9 to 2.7 ms of the backend's processor time for answering one call
(a thread, the request read in Python, Flask) is also what every call of
the page costs the backend, and so what each module that moves saves for
its own calls.

Not measured: anything with the real backend. Its answer to a call may
cost more or less than the stand-in's (the same Flask and routes, on
Python 3.14 here; a release is built with 3.12). And the reader with
administrator rights, started from the folder the pack is unpacked into
on a person's PC.

### The hardware monitor (the second module)

The Monitor page's tiles: the processor, the card, the memory, DPC time
and a ping, once a second (`core/hw_monitor.py`, now also
`shell_rs/core/src/moved/hw.rs`). Until the two fixes below it ran from
the first visit to that page until its Stop button or the quit: the page
does not stop it when it is left. Now it sleeps half a minute after the
page's last snapshot. Before step 1 one pass started nine programs and
took 3.3 s ("The Monitor page", above); since 3.8.1 the helper takes every
reading on this PC, and no program is started.

The numbers in the table just below were taken before the fixes, and count
only the monitors' own processes; "Before and after the two fixes" counts
Windows' WMI service too.

Measured with `python shell_rs/try_it.py --monitor`, on this PC, both at
the same time for a minute, each with its echo requests to 127.0.0.1
(four runs; the snapshot's time was taken in the last two):

* **the shell**: the trial tool with the card's reader beside it, its
  monitor started by the page's call; the backend is the stand-in, asked
  for a part of a pass only where this PC leaves the shell without a
  reading (it was asked none);
* **the backend's own**: `core/hw_monitor.py` in a Python process of its
  own, with the app's native helper as a source checkout builds it.

| | The backend's own | The shell |
| --- | --- | --- |
| Processor time while it runs, of one core | 3.07% to 3.27% | 2.94% to 3.12%, and its card's reader 0.06%, its backend 0.01% |
| Programs started in a minute | 0 | 0 (it cannot start one) |
| One pass, the middle of 20 taken one a second (the slowest) | 269 to 483 ms (460 to 697) | 258 to 508 ms (477 to 715) |
| The page's call for the snapshot, a minute of readings, 300 times (the slowest in twenty) | 0.94 to 0.96 ms (1.09 to 1.12), answered by the backend through the shell | **0.036 to 0.080 ms** (0.039 to 0.081) |
| Before the first visit to the Monitor page, and after its Stop button | nothing | nothing (the reader is ended ten seconds after the last reading) |

The readings themselves agree: the same clock, card temperature and clock,
memory of the card, standby list (to the megabyte), ping; the load and
the DPC shares a few tenths apart (each pass covers its own second).

What it came to: on this PC the monitor costs the shell what it costs
the backend. Nearly all of a pass was WMI: no source on this PC has a
processor temperature, and the four questions for one were asked at every
pass by both, as Python asks them. Most of that work is done in WMI's own
process, which neither number counts. The page's call every three seconds
is answered in under a twentieth of the time, and the monitor no longer needs
the backend, which is what item 1's last step needs.

#### Before and after the two fixes, Windows' WMI service counted

The owner said yes on 2026-11-25 to two fixes, made in the Python module
and the shell together ([rust-port.md](rust-port.md), piece 2): the
monitor sleeps when nobody has asked for its snapshot for half a minute,
and a place in WMI that had nothing is left alone for half a minute.

Measured on this PC from Windows' own list of every process's processor
time (no rights needed): the monitor's own process, every `WmiPrvSE.exe`
(WMI's process that answers the questions), and the `svchost.exe` that
holds Windows' WMI service (Winmgmt). A minute each, in this order, with
the echo requests to 127.0.0.1. The PC was not idle (Roblox Studio used
more than a core, and the installed app a fifth of one), so a difference
of a point or so is noise: with nothing of the measuring running,
`WmiPrvSE.exe` used 25.1% and 24.6% of one core, the WMI service 6.7% and
7.7%.

The Python module, as today's app runs it (in a process of its own, its
data folder redirected, the native helper of the checkout):

| | Before (main, 42213fc) | After, the page on screen (a snapshot every 3 s) | After, nobody looks (from 35 s after the start) |
| --- | --- | --- | --- |
| The monitor's own process, of one core | 3.46% | 0.29% | 0.00% |
| `WmiPrvSE.exe` | 42.5% (+17.6) | 26.3% (+1.4) | 23.5% (-1.4) |
| The WMI service | 9.0% (+1.7) | 7.7% (+0.4) | 8.1% (+0.8) |
| In all, about | **23% of one core** | **2%** | **nothing** |
| Passes in the minute | 60 | 60 | none (30, then it slept) |
| Questions to WMI in the minute | 63 of each of the four | 3 of each | none (1 of each before it slept) |

The shell's monitor (the trial tool, with the card's reader beside it,
reading this PC through its own door; its backend the stand-in), measured
the same way, each build after a minute with nothing running of its own
(`WmiPrvSE.exe` 22.8% and 24.4%, the WMI service 6.8% and 7.2%):

| | Before (main, 42213fc) | After, the page on screen (a snapshot every 3 s) | After, nobody looks (from 45 s after the start) |
| --- | --- | --- | --- |
| The shell's own process, of one core | 2.86% | 0.26% | 0.00% |
| Its card's reader, its backend | 0.08%, 0.03% | 0.03%, 0.05% | none (the reader was ended), 0.03% |
| `WmiPrvSE.exe` | 40.3% (+17.5) | 26.3% (+2.0) | 24.3% (0.0) |
| The WMI service | 9.1% (+2.3) | 7.9% (+0.7) | 6.9% (-0.4) |
| In all, about | **23% of one core** | **3%** | **nothing** |

Before the fixes nobody looking changed nothing: the shell's monitor used
3.36% of a core itself and `WmiPrvSE.exe` 40.8% in the minute from 45
seconds on, the reader still running and a full minute of readings kept.

Which place costs: each question asked by itself through the helper, once
a second for 30 seconds, as a pass asks it on this PC (where none of them
has anything):

| The place | One question (the slowest) | `WmiPrvSE.exe` and the WMI service, beside nothing running |
| --- | --- | --- |
| LibreHardwareMonitor, the processor's temperature | 6.6 ms (7.3) | no more |
| OpenHardwareMonitor | 6.7 ms (7.7) | no more |
| The ACPI zone | 13.5 ms (27.0) | no more |
| **The thermal zone's counter** (`Win32_PerfFormattedData_Counters_ThermalZoneInformation`) | **242 ms (543)** | **+12.8 and +3.5: about 16% of one core** |
| LibreHardwareMonitor, the card's sensors (asked only where the card does not say its temperature and load: not on this PC) | 6.9 ms (7.5) | no more |

So the cost was the thermal zone's counter, a performance counter that WMI
works out afresh for every question: asked once a second it kept about a
sixth of a core busy in WMI (about 0.16 s of a core for each question).
Asked twice a minute, as now, that is about 20 seconds of a core an hour
while the page is on screen, and nothing once the monitor sleeps. The other four namespaces and classes are
not there or have no rows on this PC, and WMI says so in a few
milliseconds.

**One program now and then, in the second pass after a start or a wake.**
In the parent session's run of `python shell_rs/try_it.py --monitor` after
the fixes, the backend's own monitor started one program in its minute
and the shell asked its backend for one part, where both were none
before. Two runs here started the same program each time: PowerShell's
`Get-Counter` for the DPC and interrupt shares, in the second pass. The
first pass after a start or a wake asks all four places and waits twice a
quarter of a second; with two monitors asking WMI at the same moment, as
in that trial (the thermal zone's counter then takes up to half a second),
it runs past one second. The loop then starts the next pass at once, and
that pass, its places quiet now, reads the processor's times 2 ms after
the first pass's last reading. The totals have not moved, which Python
takes for "no reading", so it asks `Get-Counter`; the shell asks its
backend's part for the shares, as it must to give Python's answer.
Before the fixes every pass asked the four places before the shares,
half a second at least. Shown on this PC by making only the first pass
0.3 s slower: the second pass's two readings 2 ms apart, the same totals,
`Get-Counter` started. The window's trial saw it too, once in three runs
of the hardware monitor's checks: the shell asked the stand-in for the
shares' part beside the card's, and "the card is the backend's part of
each pass" failed. Not changed: it is today's app's behaviour, and
changing it (a second reading a moment later, as when there is nothing
to compare with, instead of PowerShell) would be done on both sides
together, with the same-answers test.

How it was measured: a scratch script that starts the module of each
checkout in a process of its own, as `python shell_rs/try_it.py
--monitor` does, and reads the list before and after each minute. Not
measured: with administrator rights (with them the ACPI zone may
answer); on a PC where a source has a temperature (then the first place
that has one is asked every pass, and the others not at all, as before).

Not measured: with administrator rights; on a PC where
LibreHardwareMonitor answers (one question instead of four); the parts the
shell asks of a real backend (what a part costs there is the function's
own cost and a call's, 1.9 to 2.7 ms of the backend's processor time:
"The performance sampler", above).

### The network monitor (the third module)

The Network page's live figures: the network cards' bytes a second, the
programs that move the most and the warning of a burst, sampled once a
second while the page reads it and for half a minute after
(`core/net_monitor.py`, now also `shell_rs/core/src/moved/net.rs`). Before
step 2 it ran from every start until the quit, at 26 to 28% of a core
(above); since then it runs only while its page is read, and since step 1
the list of programs is the helper's.

Measured with `python shell_rs/try_it.py --network --python <a Python
with psutil>` on this PC, three runs. Each side is read as the page reads
it (the live figures and the minute of totals every two seconds) for a
minute, one after the other:

* **the shell**: the trial tool, its monitor reading this PC through its
  own door; its backend is the stand-in, which it asks which game runs
  once a pass;
* **the backend's own**: `core/net_monitor.py` in a Python process of its
  own, with the app's native helper and psutil, as the app runs it.

| | The backend's own | The shell |
| --- | --- | --- |
| Processor time while it runs, of one core | 1.13% to 1.20% | 1.12% to 1.15%, and its backend 0.20% to 0.21% (asked which game runs) |
| What it makes other processes do, at its pace (below) | about 0.3% of a core in Windows' DNS Client service | nothing that stands out; under 0.1% of a core |
| The page's call for the live figures, 300 times (the slowest in twenty) | about 1 ms through the shell to the backend (a call's way and back, as for the other modules) | **0.009 ms** (0.011 to 0.014) |
| While nobody reads it | nothing (it stops half a minute after the last read) | the same |

**Other processes' work.** The readings of a pass were made fifty times a
second in windows of 15 seconds, between windows in which nothing was
read, three rounds each (the same script), and every other process's
processor time was taken from Windows' own list. At fifty times the pace
the effect stands out of the PC's own noise; a fiftieth of it is what the
monitor causes.

* Python's readings (psutil's `net_io_counters()` and the helper's list of
  programs): the DNS Client service (`Dnscache`, in a `svchost.exe` of its
  own) used 14 to 18% of a core more. psutil asks Windows for the list of
  adapters with every detail, and Windows asks that service for each
  adapter's DNS servers and suffix at every call. At the monitor's pace
  that is about 0.3% of a core, in today's app, on top of the 1.2%.
* The shell's readings (the helper's `network_cards` and `process_io`):
  the list of adapters is asked for without those details (the adapters,
  their order and their names are the same: a test holds that), and the
  service did not work for it (its `svchost.exe` used 0 to 7% less than in
  the windows around). What remained was WMI's provider host, 1 to 4% at
  fifty times (under a tenth of a percent at the monitor's pace), and the
  kernel's own work for the list of programs.

What it comes to: the shell's monitor costs what the backend's own does
in its own process, plus a fifth of a percent in the backend for the
question which game runs (until the game monitor moves, piece 7), and it
spares the DNS Client service the third of a percent that today's app
costs it. The page's call is answered in a hundredth of a millisecond.

Not measured: with administrator rights; beside the real backend; on a PC
where psutil's call costs more (many adapters, slow DNS settings).

## Measuring again

After each piece of the port, run the same scripts and put the new numbers
beside these:

```
python tools/measure/count_starts.py            (about 4 minutes)
python tools/measure/count_starts.py --helper
python tools/measure/process_cost.py
python tools/measure/helper_cost.py             (needs the helper built)
python tools/measure/memory_step_cost.py        (needs the helper built)
python tools/measure/real_reads.py
python tools/measure/real_reads.py --helper
python tools/measure/log_timeline.py
python tools/measure/live_sample.py 120         (with the app running)
python native/check_parity.py                   (needs the helper built)
python shell_rs/try_it.py --moved               (what has moved into the shell; needs the shell's core built)
python shell_rs/try_it.py --monitor             (the hardware monitor, the shell's beside the backend's own)
python shell_rs/try_it.py --network --python EXE   (the network monitor, and other processes' work; EXE: a Python with psutil)
```

None of them changes a setting on the PC, and none needs the app to be
closed. Two of them start scratch processes of their own and trim those
(`memory_step_cost.py`, `check_parity.py`); nothing else is touched.
