# The shell, in Rust

The shell is to become `Vispora.exe`: a Rust program that opens the window at
once, starts the Python app as its backend, answers the page's calls itself
where a Rust handler exists and passes every other call on, and owns
quitting. It is the second part of step 3 of
[docs/rust-port.md](../docs/rust-port.md).

**Nothing in a release uses it yet.** `build.spec` and what a release carries
are as they were. What is here is the first of three pieces:

| Piece | What | State |
| --- | --- | --- |
| `core/` | The shell without a window: which request is answered where, the backend's start and stop, quitting with a time limit | written, tested, tried against a stand-in backend |
| the window | The window (Tauri, as for the updater), the tray, telling a second copy from the first, autostart | not started |
| one file | `Vispora.exe` carrying the backend and unpacking it once per version | not started |

## How the two sides work together

The page is the app's own (`templates/index.html`, `static/`), not changed.
Its requests never leave the process on their way to the shell. The shell
decides (`core/src/router.rs`):

* the page and its two files are answered from what is compiled into the
  shell, so the window has something to show before the backend is up;
* the window's own calls (`/api/window/...`) are answered by the shell,
  because the window is the shell's;
* every other `/api/...` call is passed on to the backend, unchanged. As
  modules move from Python to Rust, their calls get a line of their own.

Before any `/api/...` call is answered or passed on, the shell asks what the
backend has always asked: does it come from the app's own page (not another
page's), and does it carry the secret of this run. The backend asks again
when the call reaches it.

The backend is `app.py`, started as a program of its own:

```
<the backend> --backend
VISPORA_SHELL={"port": 49213, "token": "...", "shell_pid": 1234}
```

* **The hand-over** is one environment variable, not a command line (which
  other programs can read). The backend takes it out of its environment at
  once, so that the programs it starts do not inherit the secret.
* **Where it listens:** on a port of 127.0.0.1 that the shell picked. It is
  no longer the fixed port 5987, so a port that is taken or reserved (which
  used to leave the window blank) is not a problem any more. The shell
  connects to 127.0.0.1 and to nothing else; the address is written in
  `core/src/forward.rs`, not taken from a request.
* **The secret** is made by the shell, 32 random bytes from Windows, and
  given to both sides: to the page in the fragment of the window's address
  (as before), to the backend in the hand-over. It is in no log line.
* **Up:** the backend is up when it answers `/api/shell/hello` with the
  version the shell was built with (read from `config.py` at build time).
  Calls that have to be passed on wait for that. A backend of another
  version is not this shell's backend, and the shell says so.
* **Stopped:** a backend that ends on its own with exit code 0 meant to go
  (an update is being installed), and the shell goes too. One that ends any
  other way is started again, with the same secret, up to three times in
  five minutes; after that the shell says so in the page's calls and stops
  trying.
* **Quit**, however it is asked for and however often: the window goes out
  of sight at once; the backend is told (`/api/window/quit`, the call the
  page always made), puts the system back and ends; the shell waits for its
  process to end. After 30 seconds a backend that has not ended is ended
  (5 seconds for one that never came up). Only then does the shell exit.
  Everybody who asks for the quit, or waits for it, waits for the same one.
* **The shell gone without a quit** (crashed, ended from Task Manager): the
  backend watches the shell's process, puts the system back and ends too.

What the backend does not do behind the shell: tell a second copy from the
first, ask for administrator rights, open a window, show a tray icon. Those
are the shell's (the next piece). Everything else a start does, it does.

## What is where

| File | What it does |
| --- | --- |
| `core/src/router.rs` | Which request is answered where, and which is refused |
| `core/src/shell.rs` | Starting the backend, noticing that it stopped, starting it again, quitting |
| `core/src/forward.rs` | Passing a call on to the backend and reading its answer |
| `core/src/page.rs` | The page and its files, as the backend serves them |
| `core/src/token.rs` | The run's secret |
| `core/src/platform.rs`, `core/src/win.rs` | The doors to the outside (starting a program, a free port, random numbers), and the real ones |
| `core/src/bin/try.rs` | `vispora-shell-try`: the shell without a window, driven by lines of JSON. For trying and for the tests; never shipped |
| `core/build.rs` | Reads the app's version from `config.py`, the one place it is written |
| `try_it.py` | Runs the trial tool against a stand-in backend and takes the times |

In the app: `app.py`, "Running behind the shell" (the hand-over, whom the
backend answers, leaving with the shell), and `tests/shell_backend_stand_in.py`
(the app on the fake machine, as a backend for trials).

## No outside crates

`core/` is the part of `Vispora.exe` that starts a program as administrator
and decides which calls reach it. It uses no crates from outside this
repository: its one dependency is `updater_rs/core`, for its JSON reader, and
that has none either. A build downloads nothing. The window will bring Tauri
with it, as the updater's window did, in a crate of its own.

## Build

```
cargo build --release --manifest-path shell_rs/core/Cargo.toml
```

Keep the build output out of the repository if the repository is in a synced
folder: see "Build" in the [README](../README.md).

## Tests

```
cargo test --release --manifest-path shell_rs/core/Cargo.toml
```

Nothing is started for real in them. The backend is a stand-in that exists
only in the test: a thread that listens on the port the shell picked and
answers the way the real backend would, and a "process" that is alive until
it is told to quit, is ended, or decides to die. The limits are short there,
so that a backend that never comes up or will not go is over in a fraction
of a second, and a mistake in the waiting is a failed test, not a test run
that never ends.

`tests/test_backend_mode.py` and `tests/test_shell_rs.py` (part of
`python tests/run_all.py`) check the app's side and the two together: that
the shell and the app agree on the hand-over, on the calls and on the words
of a refusal; that the shell's page is the app's page, to the letter; and,
where the trial tool has been built, a real run: the tool starts a real
process, the app on the fake machine, and the page, the calls, the quit, a
backend that will not go and a shell that dies are tried for real.

## Trying it, and timing it

```
python shell_rs/try_it.py
```

It prints how soon the page is there, how soon the backend answers, what a
call costs on its way through the shell, and how long a quit takes. The
numbers and what they mean are in
[docs/performance.md](../docs/performance.md).

**Never start the real backend with the trial tool.** The real backend
changes the PC when it starts and would meet a Vispora that is already
running. The trial tool has no backend of its own on purpose: it starts what
it is told to start, and the only thing to tell it is the stand-in.

## What has not been done yet

* No window, no tray, no single-instance check, no autostart: the next
  piece.
* The real backend has never been started by the shell. Its side of the
  arrangement is tested on the fake machine only, where the start-up work
  and a real quit's undoing do not happen.
* Where the app's exe is, for the updater and for autostart: behind the
  shell the backend's own exe is not `Vispora.exe` any more. The hand-over
  will have to say it; nothing needs it before the one-file piece.
* The graphics settings for the window (today the backend picks WebView2's
  switches for the PC's graphics card before it opens its window) are the
  window's piece.
