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
| the tray | The tray icon and its menu, and what the entry for starting at sign-in names | written, tried for real against a stand-in backend |
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
are the shell's. Everything else a start does, it does.

## The window

`app/` is the window around `core/`: Tauri on WebView2, the versions the
updater's window pinned, with Tauri's own tray feature switched on and
nothing else added. Its lock file lists 422 outside crates: the 418 of the
updater's window, in the same versions, and the 4 that the tray feature
brings. Of those 4 one is compiled for Windows (`tray-icon`); the other
three are for Linux and are only named. No Tauri plugin. A test compares the
two lock files and knows the 4 by name. Plain `cargo build`, as there.

* **The page is the app's own**, not changed. `static/js/app.js` was written
  for pywebview's window and asks it three things through
  `window.pywebview.api` (the run's secret, minimize, hide). The window
  gives its page a few lines of script before the page's own
  (`core/src/page.rs`), which turn those into calls to the shell. The same
  lines make the parts of the page marked `pywebview-drag-region` move the
  window, and only those parts themselves, as pywebview did it.
* **The page cannot reach Tauri's commands**: none are registered and it is
  given no permission for the built-in ones.
* **The window stays on the app's own page**, and "the app's own" is the
  whole of where a page is from (`http://vispora.localhost`), not its host
  name alone: another scheme or another port under that name is another
  place (`core/src/page.rs`, `is_the_apps_page`).
* **What the window gives its page, it gives to nothing else.** WebView2
  runs the window's few lines of script in every frame the window ever
  shows, so the script looks where it is before it does anything: anywhere
  but in the app's own page, at the top of the window, it does nothing and
  hands the run's secret to nobody. The page shows no frames today; this is
  for the day it does, or something gets one in.
* **Is a copy running already?** That is looked at first, before anything
  asks for administrator rights (`core/src/instance.rs`). A second start
  asks the first copy for its window and leaves. Today's exe carries a
  manifest that asks for administrator rights, so Windows' prompt comes
  before the app can look; the shell's exe asks for nothing in its manifest
  and asks itself, after it has looked. "Is a copy running" is the name the
  Python app has always held (a mutex), so an old copy and a new one see
  each other; "show your window" is a second name (an event) that the
  running copy listens on. Windows' own calls for both.
* **Asking for rights cannot go round in a circle.** The copy that is
  started to get administrator rights is given a word of its own,
  `--asked-for-rights`, which goes no further (the backend never sees it).
  A copy that carries it and still has no rights does not start a third: it
  says in a plain message that it did not get them, and stops. (Where
  Windows' prompt is switched off for an account that is no
  administrator's, the new copy is started without rights all the same.)
* **WebView2 takes no settings from the environment.** It reads several from
  variables named `WEBVIEW2_...`: where to take the browser's programs
  from, what to start them with (a way in for a debugger among it), where
  its profile is. A program without administrator rights can set those for
  the person and wait for the next start of an app that has rights. The
  first thing the shell does, before anything of WebView2 is touched, is to
  take every such variable out of its own environment; the log names the
  ones it found. The shell sets none itself. (As far as its documentation
  goes, WebView2 reads the same settings from the policy part of the
  registry as well, `Software\Policies\Microsoft\Edge\WebView2`. Only
  administrators can write there, in the person's own half too: looked at
  on one PC, not tried.)
* **The window's folder is new at every start, closed, and gone
  afterwards.** WebView2 keeps a profile folder. Today's window (pywebview)
  is a private one with a throwaway folder, and so is this one: the window
  is private (`.incognito(true)`), so nothing of a run is written down for a
  later one, and what WebView2 does write goes into a folder made for this
  run alone, under a name nobody can guess (`Vispora-window-` and 32 random
  hex digits), never one that is already there, removed when the run is
  over. The app as it ships makes it in Windows' own folder for temporary
  files (`C:\Windows\Temp`), owned by administrators and open to them and
  to Windows itself and to nobody else. Where that folder is, is asked of
  Windows by the one call that does not look at the environment. (The
  folder for all users' data, `C:\ProgramData`, was the first choice and is
  not used: Windows works out where it is from the `SystemDrive` variable
  of the program that asks. Tried here: with that variable pointing at a
  folder with a `ProgramData` of its own, both of Windows' calls answer
  with that folder; and a variable is for the person's programs to set.)
  A fixed folder in the person's own AppData, which is what this window
  had at first, would be readable by any of their programs, and an
  ordinary window writes the run's secret into it twice (seen here, with
  the private mode taken out: after 4.4 to 4.7 seconds in
  `EBWebView\Default\Session Storage`, where the page keeps it, and after
  9.4 seconds in `EBWebView\Default\History`, with the address the window
  was opened at). It would also be writable by them before the next start,
  and what is planted there (a service worker for the app's own address,
  for one) runs inside the app's page.
* **A start takes away what earlier runs left behind, and never a folder
  that is in use.** A run that is ended from outside cannot remove its
  folder, so a start looks for such folders. It has to tell them from one
  that is in use: "is a copy running" is asked per session of Windows, so
  two people signed in at one PC (fast user switching, remote desktop) each
  run a copy, and each copy's start looks at the same place. A run
  therefore keeps one file in its folder open (`in-use`) for as long as it
  runs, in a way that lets nobody else open it; a start removes a folder
  only if that file is there and nobody holds it. A folder without the file
  is being made at this moment (the folder comes first, the file a moment
  later) and is left alone until it is ten minutes old. Only folders that
  begin as the window's folders begin are looked at, never a link to
  somewhere else. All of this is done once the window is there, on a
  thread of its own, and by name first: the place holds thousands of files
  that are nothing of the app's, and nothing is asked of Windows about
  those. (In a trial: one earlier run's folder of 129 files taken away with
  5,000 other files beside it, 20 ms, with the window on screen after 36 ms
  as in every other run.)
* **WebView2 is asked for before a window is made**, and a missing one is
  said in a plain Windows message. What else can keep the app from
  starting has words of its own (`core/src/trouble.rs`): a folder for the
  window that cannot be made is said as that, with where it was to be
  made, and the person is not sent to install WebView2 for it.
* **Closing the window from outside** (Alt+F4, the taskbar) quits the app:
  the process does not end before the backend has put the system back and
  ended.
* **Signing out, shutting down, Windows replacing the app's files**: Windows
  tells the window, and the window library ends the process as soon as that
  call returns; nothing written after the window's loop is ever reached. The
  quit is therefore done in the loop's own last word, which that call still
  delivers: the backend is told, puts the system back and ends, and then the
  process goes. Windows gives a program about five seconds for this. The
  window's folder cannot be removed at that moment (WebView2 is still in
  it); the next start removes it.
* **A link the page opens is handed to Explorer.** The shell runs with
  administrator rights. Opened by the shell itself, a browser that was not
  running yet would start with those rights, and which program opens links
  is looked up in the person's own part of the registry, which any of their
  programs may change. So the link goes to `explorer.exe` (by its full path
  in Windows' folder, asked of Windows), and Explorer, which is the desktop
  the person is signed in to, opens it as it opens anything clicked there.
  Only a web address is handed over (`http://` or `https://`, no blank, no
  quotation mark), always in quotation marks.
* **What the shell starts besides its backend** (Explorer, for a link) is
  started with none of the shell's handles, and so not the listening
  socket, which only the backend is meant to get.
* **`--from-autostart`** starts the window out of sight and is passed on to
  the backend, as today.
* **How the window's loop ends.** When the backend has ended, the shell
  takes its window away, and the loop ends with its last window. It does
  not tell the loop to end: the window would still be there, WebView2 would
  keep the window's folder, and the folder could not be removed (that is
  how it was until a trial quit the app from the tray: the folder stayed,
  and the quit waited three seconds for it).

## The tray

Today's tray (`app.py`, `_create_tray_icon`) is the measure, and
`tests/test_tray_calls.py` holds the shell's against it: today's tray is
built there for real, the shell's is made from what the app says, and the
two are compared entry by entry.

* **The same three pictures**, dot for dot: the app's mark in grey (Game
  Mode is off), in colour (on), and brighter with a dot in the corner (a
  game is running). The app draws them when it starts; the shell carries
  what the app draws, written down once by `core/pictures/make.py`. A test
  has the app draw them again at every run and compares.
* **The same menu**, in the same order, with the same words and ticks:
  Open Vispora, a status line that can only be read, Game Mode, Force
  Normal Mode (only while a game runs), Crosshair, Refresh PC now, Clean
  RAM now, Quit.
* **The same clicks.** The left button, once or twice, brings the window
  back; the right button opens the menu.
* **The same actions.** "Open Vispora" and "Quit" are the shell's own: the
  window is the shell's, and a quit from the tray goes the way every quit
  goes (the backend puts the system back, then the shell ends). Every
  other entry is carried out by the backend, as it always was: the shell
  asks for it with a call (`/api/tray/...` in `app.py`), which does what
  the entry of the same name does today and tells the person the same
  thing afterwards. The calls need the run's secret like every other call.
  Started as it always was, the app has its own tray and none of these
  calls: they answer as a path that does not exist.
* **Asked every three seconds**, as today's tray asks: one call on this PC
  that starts no program. While there is no backend to ask (it is
  starting), nothing is sent.

Two things are different, and on purpose:

* **The tooltip says what is so.** Today's tray writes its tooltip only
  when the game changes, so "Vispora: game mode armed" shows in it only
  after a game has ended; switch Game Mode on and it still says "Vispora".
  The shell's tooltip follows the state. The words are today's.
* **The icon is there before the backend is.** Until the backend has said
  anything, the tray shows the grey mark and the menu of "Game Mode is
  off"; "Open Vispora" and "Quit" can be clicked, the other entries can be
  read and not clicked.

And one that is not on purpose: in today's menu "Open Vispora" is in bold
(it is what a click on the icon stands for). The menu Tauri makes has no
bold entry.

**Starting at sign-in.** The entry that starts the app at sign-in (a
scheduled task) names the exe a person starts. Behind the shell that is the
shell, not the backend, so the shell says where it is when it starts the
backend, and `core/autostart.py` names that. Started as it always was, the
entry names what it always named. This is written and tested as far as
"what the entry would be"; no task is made by a test or a trial, and the
task today's app makes is not touched.

The exe is `VisporaShell.exe` for now. It gets the app's name in the
one-file piece; until then no exe named after the app is ever started in a
trial, because the app itself is running on the PC the trials are made on.

### Trying the window

```
cargo build --release --locked --no-default-features --manifest-path shell_rs/app/Cargo.toml
python shell_rs/app/try_it.py                  # 71 checks; the window is never shown
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

Besides the page, the calls, the second start and the quit, the checks look
at what another program on the PC could get at, with the real window:

* the window's folder must be closed to everybody but the account the
  trial runs under and Windows itself; it is searched for the run's secret
  twelve seconds after the page had it (a window that is not private has
  written it down by then); and it must be gone after the run, with nothing
  of WebView2 left anywhere else;
* settings for WebView2 are planted in the trial's environment (a
  debugger's port, a profile folder) and must not be used;
* a folder as a run that was ended from outside leaves it must be removed
  and not used; one without the in-use file, made a moment ago, must be
  left alone and not used;
* a second copy is started that shares the place for the folders, as a
  second person's copy does: its start must leave the first copy's folder
  alone, and the first copy's page must go on working;
* an empty frame, a frame that shows one of the app's own files, and a page
  from somewhere else in a frame are put into the page and must find
  nothing of what the window gives the app's page;
* the page is sent to another port and another scheme under its own host
  name and must stay where it is;
* the windows of the program are told that the session ends, as Windows
  tells them, while the window is hidden in the tray, and the backend must
  have put the system back and ended before the shell's process is gone;
  the folder that run has to leave behind is then searched for the secret
  as well;
* where the window's folder cannot be made, the shell must not start and
  must say so in words of its own.

The tray is tried without a mouse, and for real. Its menu is read as
Windows has it (the words, the ticks, what is greyed) and compared with
today's; a click and a double click on the icon are sent as the messages
Windows sends for them; an entry is chosen by the message Windows sends
when one is chosen; a game is made to look as if it were running on the
fake machine, to see the icon change. The menu itself is never put on the
screen. One thing of a trial does show: for the seconds a run lasts there
is a second "Vispora" icon in the notification area, the trial's.

"Somewhere else" is this PC every time (127.0.0.1): nothing is asked of the
internet, and no link is opened in a browser. A trial build takes scripts to
run in its page from a folder the trial names; the build that ships has no
such thing; what the shell would say in a message box goes into a file
in a trial, because a box would sit on the screen of whoever is at the PC.
The trial's folder for the window is closed to everybody but
the account the trial runs under, which keeps WebView2's own helper programs
out as the shipping build's folder does: that WebView2 works in a closed
folder is tried, that it works in one for administrators only is not.

## What is where

| File | What it does |
| --- | --- |
| `app/src/main.rs` | The window: first copy, WebView2, the page, the requests, the quit |
| `app/src/tray.rs` | The tray icon: Tauri's, showing what `core/src/tray.rs` decides |
| `app/build.rs`, `app/app.manifest`, `app/tauri.conf.json` | What the exe carries besides its code: icon, version (from `config.py`), a manifest that asks for no rights |
| `app/try_it.py` | Runs the real window exe against a stand-in backend, out of the way |
| `core/src/instance.rs` | Is this the one copy, and does it have the rights it needs: the order of a start |
| `core/src/router.rs` | Which request is answered where, and which is refused |
| `core/src/shell.rs` | Starting the backend, noticing that it stopped, starting it again, quitting |
| `core/src/forward.rs` | Passing a call on to the backend and reading its answer |
| `core/src/page.rs` | The page and its files, as the backend serves them |
| `core/src/token.rs` | The run's secret |
| `core/src/tray.rs`, `core/pictures/` | Which picture the tray shows, its tooltip and its menu, decided from what the backend says; the three pictures, and the script that has the app draw them |
| `core/src/trouble.rs` | What the person is told when the app cannot start |
| `core/src/platform.rs`, `core/src/win.rs` | The doors to the outside (starting a program, a free port, random numbers), and the real ones |
| `core/src/bin/try.rs` | `vispora-shell-try`: the shell without a window, driven by lines of JSON. For trying and for the tests; never shipped |
| `core/build.rs` | Reads the app's version from `config.py`, the one place it is written |
| `try_it.py` | Runs the trial tool against a stand-in backend and takes the times |

In the app: `app.py`, "Running behind the shell" (the hand-over, whom the
backend answers, leaving with the shell) and "The tray, behind the shell"
(its calls); `core/autostart.py` (which exe the entry at sign-in names); and
`tests/shell_backend_stand_in.py` (the app on the fake machine, as a backend
for trials).

## No outside crates

`core/` is the part of `Vispora.exe` that starts a program as administrator
and decides which calls reach it. It uses no crates from outside this
repository: its one dependency is `updater_rs/core`, for its JSON reader, and
that has none either. A build of it downloads nothing. The window brings
Tauri with it, as the updater's window did, in a crate of its own (`app/`).

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

* **The tray's menu has not been opened with a mouse, and no message of
  the app's was seen.** The trial reads the menu as Windows has it and
  chooses entries by message; it never puts the menu on the screen of the
  PC it runs on. What an entry tells the person afterwards (a toast) is
  the backend's, and on the fake machine it is caught, not shown. The
  icon's picture and tooltip are not read from Windows either (Windows
  does not say what an icon of another program shows): the trial reads the
  shell's own account of them, and a test compares the three pictures'
  files with what the app draws.
* **The entry for starting at sign-in has never been made behind the
  shell.** What it would be is tested. An entry that today's app made
  names today's exe; once the shell is the app under the same name in the
  same place, that is the shell. If the shell ever lives somewhere else,
  an entry that is already there has to be made again: nothing does that
  yet.
* **The administrator prompt and a second start beside a copy that has
  administrator rights** have not been seen: the trials run without them.
  The event a second start rings is made so that a program without those
  rights may ring it; that part is untried.
* **Nothing that needs administrator rights has been run**, and so three
  things of the window are written and tested in their parts but not seen
  whole: the window's folder in Windows' Temp folder, for administrators only
  (the trial's is closed to its own account instead; a test makes the
  administrators' kind and sees that a program without rights cannot get
  in); a link opened from the app (nothing is handed to Explorer in any
  test or trial: it would open the browser of whoever is at the PC, and
  whether the browser then runs without the app's rights only shows from
  an app that has them; what is tested is which addresses are handed over
  and how the line for Explorer is written, and that what the shell starts
  this way gets none of its handles); and the copy that is started for
  rights. For the first real trial: open "Release notes" on the Drivers
  page and look in Task Manager whether the browser runs elevated (it must
  not), with a link that has a question mark and a comma in it.
* **Handing a link to Explorer is the common way, not a documented one.**
  It rests on Explorer passing what it is given to the desktop that is
  already running. If a link ever opens a folder window instead of the
  browser, the documented way is the desktop's own shell object
  (`IShellDispatch2::ShellExecute`), which is a good deal more code.
* **Five seconds at sign-out.** The quit begins at once and the stand-in
  backend is gone in a tenth of a second. The real backend's undoing has
  never been timed at sign-out. If it ever needs longer than Windows gives,
  Windows ends both processes where they are, as it does with today's app;
  what the backend had not yet put back is then put back at the next
  start. Telling Windows to wait (`ShutdownBlockReasonCreate`) would be the
  next step.
* **Something else under the app's two names.** A program that makes an
  object of another kind under the one-copy name (an event, say) before the
  app starts makes every start run without the one-copy check, as the
  Python app's would: "a second copy is a nuisance, an app that does not
  start is worse". One that makes an event of its own under the show name
  makes every start leave as if a first copy had been asked for its window,
  so the app does not come up while that program runs. Both need a program
  already running in the person's session; neither gives it anything.
* **An address Tauri cannot read is let through.** The window's rule sees
  every address the window is sent to after Tauri has read it; where Tauri
  cannot read one at all, it lets the window go (`tauri-runtime-wry`,
  `unwrap_or(true)`). No such address is known that WebView2 would go to.
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
* Where the app's exe is, for the updater: behind the shell the backend's
  own exe is not `Vispora.exe` any more. The hand-over says where the shell
  is, and the entry at sign-in uses it; the updater does not yet, and
  nothing needs it before the one-file piece.
