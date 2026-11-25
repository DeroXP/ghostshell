# Where the time goes

Measured on 2026-11-24, before any of the app moved to Rust. This is the
baseline the port is steered by: [rust-port.md](rust-port.md) is the order that
follows from it, and [native/](../native/README.md) is the first piece.

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

These are counts from the sandbox with a stand-in for the helper. How long the
real library takes per answer, and what the running app's processor use drops
to, can only be measured on a build that contains it. The pull request that
added the helper says where that stands.

## Measuring again

After each piece of the port, run the same scripts and put the new numbers
beside these:

```
python tools/measure/count_starts.py            (about 4 minutes)
python tools/measure/count_starts.py --helper
python tools/measure/process_cost.py
python tools/measure/real_reads.py
python tools/measure/log_timeline.py
python tools/measure/live_sample.py 120         (with the app running)
```

None of them changes anything on the PC, and none needs the app to be closed.
