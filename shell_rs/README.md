# The shell, in Rust

The shell is to become `Vispora.exe`: a Rust program that opens the window at
once, starts the Python app as its backend, answers the page's calls itself
where a Rust handler exists and passes every other call on, and owns
quitting. It is the second part of step 3 of
[docs/rust-port.md](../docs/rust-port.md).

**Nothing in a release uses it yet.** `build.spec` and what a release carries
are as they were. The pieces:

| Piece | What | State |
| --- | --- | --- |
| `core/` | The shell without a window: which request is answered where, the backend's start and stop, quitting with a time limit, and whether this is the one copy | written, tested, tried against a stand-in backend |
| `app/` | The window (Tauri, as for the updater), on the app's own page | written, tried for real against a stand-in backend |
| the tray | The tray icon and its menu, and autostart pointing at the shell | not started |
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
VISPORA_SHELL={"port": 49213, "listen": 812, "token": "...",
               "shell_pid": 1234, "shell_started": 134088...}
```

* **The hand-over** is one environment variable, not a command line (which
  other programs can read). The backend takes it out of its environment at
  once, so that the programs it starts do not inherit the secret.
* **The socket is the shell's.** The shell makes one listening socket on
  127.0.0.1 when it starts, on a port Windows picks, marked so that no other
  program can listen on that port as well, and keeps it until it ends. Every
  backend inherits that socket (`"listen"` is its number) and serves on it;
  behind the shell the backend opens no port of its own. So the port is
  never free and never anybody else's: not before a backend is up, not while
  one is started again, not after one has died. See "Why the socket is the
  shell's" below. It is also no longer the fixed port 5987, so a port that
  is taken or reserved (which used to leave the window blank) is not a
  problem any more. The shell connects to 127.0.0.1 and to nothing else; the
  address is written in `core/src/forward.rs`, not taken from a request.
* **The secret** is made by the shell, 32 random bytes from Windows, and
  given to both sides: to the page in the fragment of the window's address
  (as before), to the backend in the hand-over. It is in no log line.
* **Up:** the backend is up when it answers `/api/shell/hello` with the
  version the shell was built with (read from `config.py` at build time).
  Calls that have to be passed on wait for that. A backend of another
  version is not this shell's backend, and the shell says so.
* **Stopped:** a backend that ends on its own with exit code 0 meant to go
  (an update is being installed), and the shell goes too. One that ends any
  other way is started again, with the same secret, on the same socket: up
  to three times in five minutes for one that had been up, up to three
  times in a row for one that never came up, however long each try took.
  After that the shell says so in the page's calls and stops trying.
* **Quit**, however it is asked for and however often: the window goes out
  of sight at once; the backend is told (`/api/window/quit`, the call the
  page always made), puts the system back and ends; the shell waits for its
  process to end. After 30 seconds a backend that has not ended is ended
  (5 seconds for one that never came up). A backend that is being started
  at that very moment is waited for and dealt with like any other. Only
  then does the shell exit. Everybody who asks for the quit, or waits for
  it, waits for the same one.
* **The shell gone without a quit** (crashed, ended from Task Manager): the
  backend watches the shell's process, puts the system back and ends too.
  It knows the shell by its process id and the time it was started, both
  handed over: an id alone is given out again once its process has ended.

### Why the socket is the shell's

Every call the shell passes on carries the run's secret, and a call goes to
whoever listens on the port. The first version of this code asked Windows
for a free port, let go of it, and handed the number to the backend, which
took the port a second or two later. Until then (and again whenever a
backend had died) anybody could listen there, and the shell's "are you up",
sent every 50 ms with the secret, went to them. A program without
administrator rights could learn the secret that way and then call the real,
elevated backend with it: the step up the secret is there to prevent.

Tried on Windows 11 (`core/src/win.rs` has both as tests):

* a listener that asks for a shareable address, which is what a web
  server's own listener does (Werkzeug sets address reuse), **can be joined**
  by a second listener of the same user on the same port;
* a listener that asks for nothing, or that asks to be alone, cannot.

So it would not have been enough for the backend to make its own listener
more carefully: the port would still have been free before and after it.
With one socket made by the shell and kept for its whole life there is no
"before" and no "after". A call made while no backend is accepting waits in
that socket's queue, where nobody else can get at it, and is answered by the
next backend or given up on when the shell stops trying.

What the backend does not do behind the shell: tell a second copy from the
first, ask for administrator rights, open a window, show a tray icon. Those
are the shell's (the next piece). Everything else a start does, it does.

## The window

`app/` is the window around `core/`: Tauri on WebView2, the versions the
updater's window pinned, and no crate the updater's window did not already
bring (a test compares the two lock files). Plain `cargo build`, as there.

* **The page is the app's own**, not changed. `static/js/app.js` was written
  for pywebview's window and asks it three things through
  `window.pywebview.api` (the run's secret, minimize, hide). The window
  gives its page a few lines of script before the page's own
  (`core/src/page.rs`), which turn those into calls to the shell. The same
  lines make the parts of the page marked `pywebview-drag-region` move the
  window, and only those parts themselves, as pywebview did it.
* **The page cannot reach Tauri's commands**: none are registered and it is
  given no permission for the built-in ones. The window stays on the app's
  own page; a link the page opens goes to the browser.
* **Is a copy running already?** That is looked at first, before anything
  asks for administrator rights (`core/src/instance.rs`). A second start
  asks the first copy for its window and leaves. Today's exe carries a
  manifest that asks for administrator rights, so Windows' prompt comes
  before the app can look; the shell's exe asks for nothing in its manifest
  and asks itself, after it has looked. "Is a copy running" is the name the
  Python app has always held (a mutex), so an old copy and a new one see
  each other; "show your window" is a second name (an event) that the
  running copy listens on. Windows' own calls for both.
* **WebView2 is asked for before a window is made**, and a missing one is
  said in a plain Windows message.
* **Closing the window from outside** (Alt+F4, the taskbar, signing out)
  quits the app: the process does not end before the backend has put the
  system back and ended.
* **What the shell starts besides its backend** (a browser for a link) is
  started the way Explorer would start it, not as a child of the shell: it
  inherits none of the shell's handles, and so not the listening socket,
  which only the backend is meant to get.
* **`--from-autostart`** starts the window out of sight and is passed on to
  the backend, as today.

The exe is `VisporaShell.exe` for now. It gets the app's name in the
one-file piece; until then no exe named after the app is ever started in a
trial, because the app itself is running on the PC the trials are made on.

### Trying the window

```
cargo build --release --locked --no-default-features --manifest-path shell_rs/app/Cargo.toml
python shell_rs/app/try_it.py                  # 13 checks; the window is never shown
python shell_rs/app/try_it.py --time           # the times
python shell_rs/app/try_it.py --picture x.png  # a picture of the window
python shell_rs/app/try_it.py --show           # the window, to look at and click
```

`--no-default-features` makes the trial build: it never asks for
administrator rights, takes its backend from the trial (the app on the fake
machine, as a real process) and knows itself by names of the trial's own, so
that it never takes the installed Vispora for its first copy. Without a
trial to tell it these things it says so and ends. The build that is meant
to ship is made without that flag:

```
cargo build --release --locked --manifest-path shell_rs/app/Cargo.toml
```

It has nothing to start yet (the backend is packed in the one-file piece)
and says that its files are incomplete.

## What is where

| File | What it does |
| --- | --- |
| `app/src/main.rs` | The window: first copy, WebView2, the page, the requests, the quit |
| `app/build.rs`, `app/app.manifest`, `app/tauri.conf.json` | What the exe carries besides its code: icon, version (from `config.py`), a manifest that asks for no rights |
| `app/try_it.py` | Runs the real window exe against a stand-in backend, out of the way |
| `core/src/instance.rs` | Is this the one copy, and does it have the rights it needs: the order of a start |
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

* **No tray icon.** A window that was hidden (Hide to Tray, or a start at
  sign-in) is brought back by starting the app again. The tray needs calls
  the backend does not have yet (its menu acts on the app directly today)
  and its three icons; it is the next piece, with autostart pointing at the
  shell's exe.
* **The administrator prompt and a second start beside a copy that has
  administrator rights** have not been seen: the trials run without them.
  The event a second start rings is made so that a program without those
  rights may ring it; that part is untried.
* **Old network filter software.** On a PC where such software (a layered
  service provider) sits between programs and Windows' sockets, a socket
  that a program inherits may not be usable in it. The backend then refuses
  the hand-over, the shell gives up after three tries, and the page is told
  that the background program does not start and where the reason is
  written down (`vispora.log`). If that ever shows up for real, handing the
  socket over with `WSADuplicateSocket` and `socket.fromshare` is the
  documented way.
* **The graphics switches for the window.** Today the backend picks
  WebView2's switches for the PC's graphics card before it opens its
  window. The shell's window opens before the backend is up, and uses
  WebView2's defaults for now.
* **Ending a backend ends one process.** That is right for a backend that
  is one process. If the backend is ever packed so that the program the
  shell starts is only an unpacker for the real one, a forced end would
  leave the real one running as administrator with a valid secret, and a
  restart would make two. Whatever the packaging piece does, a forced end
  has to end all of it, and a test needs a stand-in with a child of its own.
* **A backend that is alive but stuck is not noticed.** Once it is up, only
  its process is looked at. One that hangs is not started again; its calls
  wait until the page gives up on them.
* The real backend has never been started by the shell. Its side of the
  arrangement is tested on the fake machine only, where the start-up work
  and a real quit's undoing do not happen.
* Where the app's exe is, for the updater and for autostart: behind the
  shell the backend's own exe is not `Vispora.exe` any more. The hand-over
  will have to say it; nothing needs it before the one-file piece.
* The graphics settings for the window (today the backend picks WebView2's
  switches for the PC's graphics card before it opens its window) are the
  window's piece.
