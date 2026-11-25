# The standalone updater, in Rust

`VisporaUpdater.exe` is the small program that replaces `Vispora.exe` with a
newer build: the app starts it and exits, or somebody starts it by hand. This
folder is that program rewritten in Rust, as the first part of step 3 of
[docs/rust-port.md](../docs/rust-port.md).

It has not been released. The Python updater (`updater/`, version 1.5.0)
stays in the repository, stays buildable and stays the one releases carry
until this one has shipped and worked. Until then there are two, and they do
the same thing: the same command line, the same places asked in the same
order, the same checks before a download is trusted, the same backup, swap
and rollback, the same `install_info.json`, the same log file and the same
window. The app can start either.

## Two crates

| Folder | What it is | Outside crates |
| --- | --- | --- |
| `core/` | Everything the updater does: where to look for a build, the download and its SHA-256 check, closing the app, backup, swap, rollback, the command line, and what its window is told | none |
| `app/` | The window (Tauri on WebView2) around `core/`, and the exe that is shipped | Tauri and what it needs |

The split is on purpose. `core/` is the code that downloads a program,
decides whether to trust it and puts it in the place of the app's exe, and it
runs as administrator. It is built from that folder and from Windows itself:
its HTTP client is Windows' own, its JSON reader and its SHA-256 are in the
folder, and a build of it downloads nothing. The window only shows what
`core/` does.

## Build

```
cargo build --release --locked --manifest-path updater_rs/app/Cargo.toml
```

The exe is `updater_rs/app/target/release/VisporaUpdater.exe`: one file of
5.6 MB (5,558,784 bytes), where the Python updater's is 14.5 MB. Nothing else
has to be next to it. It needs Microsoft Edge WebView2 for its window, which
is part of Windows 11 and of most Windows 10 PCs, and which the app's own
window needs too.

A release carries it as `VisporaUpdater-<version>.exe`, the name the app
looks for. Publishing is not done from here.

The build needs Rust and the Windows SDK's resource compiler, both of which
the Rust toolchain for Windows already asks for. It does not need Tauri's
command line tool, Node, or an installer builder: the exe is the download.

## What Tauri brings

`app/Cargo.toml` names two outside crates with an exact version each:
`tauri` 2.12.7 and `tauri-build` 2.7.6. `app/Cargo.lock` is committed and
fixes every crate below them, each with the hash cargo checks its download
against; `--locked` makes the build keep to it. All of them come from
crates.io.

| | |
| --- | --- |
| Crates listed in the lock file (it also covers Linux, macOS and phones) | 418 |
| Crates actually compiled into the Windows exe | 219 |
| ... of which run a program of their own while building (a build script) | 31 |
| ... of which are extensions to the compiler (procedural macros) | 19 |
| Downloaded the first time, into cargo's folder in the user's profile | about 350 MB |
| Build output in `app/target/` | about 1.2 GB |
| A build from nothing on the PC this was written on | a little over a minute |

Of Tauri's features only the window is switched on (`wry` and
`custom-protocol`): no tray, no developer tools, no compression, no plugins.

## The window

The page is the Python updater's own (`updater/templates`, `updater/static`),
compiled into the exe and not changed by a character: one copy for both
updaters while both exist.

The Python updater ran a small web server on a port of 127.0.0.1 and pointed
its window at it. Here the page's requests (`/api/status`, `/api/start` and
so on) are handed straight to `core/` inside the process: there is no server
and no open port. The page cannot reach any of Tauri's own commands: none
are registered and it is given no permission for the built-in ones.

Same size (720 by 580), no frame of Windows' own, same background. The
page's title bar moves the window, which WebView2 reads from the page's own
style sheet. "Change..." opens Windows' folder dialog. Closing the window
while an install is running waits for the exe swap to finish, as before.

With `--silent` no window is made and Tauri is never started.

## What is where in `core/`

| File | What it does | The Python it comes from |
| --- | --- | --- |
| `src/sources.rs` | Where releases are published and what counts as one: GitHub first and the update server second, a server of one's own asked alone, stable against beta, versions and how they compare | `update_sources.py` |
| `src/installer.rs` | The pipeline: find the build, close the app, download, verify, back up, swap, roll back, launch | `updater/installer.py` |
| `src/server.rs` | What the window asks and is told | `updater/server.py` |
| `src/cli.rs` | The command line, and what is made of it and of the app's install record | `updater/main.py` |
| `src/state.rs`, `src/paths.rs` | What the window is shown; names and folders | `updater/server.py`, `updater/paths.py` |
| `src/platform.rs`, `src/win.rs` | The four doors to the outside (network, processes, disk, clock), and the real ones | |
| `src/json.rs`, `src/sha256.rs` | Reading and writing JSON; the hash a download is checked against | |

## Its version

`2.0.0`. The app tells updaters apart by version (1.5.0 was the first that
knew GitHub) and keeps the one with the higher number, and a release carries
the file as `VisporaUpdater-<version>.exe`. So the first Rust updater is
above every Python one. The number is written in `core/Cargo.toml`,
`app/Cargo.toml` and `app/tauri.conf.json` (what Windows shows in the exe's
properties); a test holds the three together.

## Tests

The Python updater's tests are the specification. Every case of
`tests/test_updater.py`, of the standalone-updater parts of
`tests/test_update_sources.py` and of the updater parts of
`tests/test_robustness_updates.py` is ported, under the same name, in
`core/tests/`:

```
cargo test --release --offline --manifest-path updater_rs/core/Cargo.toml
```

Nothing in them downloads anything, closes anything or touches a real
install. The network is a table of answers, `tasklist` and `taskkill` are
recorded, the clock does not wait, and the "install" is a folder in a
temporary directory; Program Files and the user's folders point into it too.
One file, `core/tests/http.rs`, runs the real HTTP client, against a listener
on 127.0.0.1 that the test starts itself. Another, `core/tests/dialog.rs`,
opens the real folder dialog and presses its buttons; it is left out of a
normal run because it puts a window on the screen (how to run it is at its
top).

`tests/test_updater_rs.py` (part of `python tests/run_all.py`) checks what
only the Python side can: that the two updaters and the app agree on every
name, address, argument and step, that the window is the Python one's, that
Tauri is pinned and the exe asks for administrator rights, and, where the
Rust side has been built, that it decides the same as `update_sources.py`
for the same answers from GitHub. It does that through
`vispora-updater-decide.exe`, a small tool built with `core/` that reads
JSON, prints JSON, and opens no file and no connection.

### The exe itself

`app/try_it.py` starts the real exe, with its real window, its real HTTP
client and the real file system, and watches it replace an exe. What it is
pointed at is all made up: a temporary folder as the install, an app named
`SandboxApp.exe`, a server on 127.0.0.1 that the script starts, and the
user's folders pointing into the temporary folder.

```
cargo build --release --locked --no-default-features --manifest-path updater_rs/app/Cargo.toml
python updater_rs/app/try_it.py                  # 35 checks; no window is shown
python updater_rs/app/try_it.py --show           # the window, to look at and click
python updater_rs/app/try_it.py --picture x.png  # a picture of the window
```

`--no-default-features` makes the one difference between that build and the
exe that ships: it does not ask for administrator rights, so a script can
start it. The script refuses the exe that ships, and an elevated terminal.
Build without that flag again before anything is published.

With `--show`, the made-up server has nothing to offer, so "Install" ends
with "no release published" before anything is closed or replaced, whichever
folder was picked in the window.

## Things done differently from the Python updater

None changes what a normal update does.

* **A download that ends early is called that.** When the connection closes
  before the announced length has arrived, the Rust updater says "the
  download was interrupted" (exit code 5). The Python one read the shorter
  file to its end and then failed its hash check (exit code 6, "corrupted or
  tampered"). Either way nothing is installed and the next place is tried.
* **A version cannot choose where a download is written.** The download is
  staged as `Vispora-<version>.exe` in the updater's folder. A version from
  the update server with a path in it is made harmless first.
* **Digits other than 0 to 9 in a version are not read as numbers.** Python's
  `int()` takes Arabic-Indic digits and the like; no release is numbered so.
* **No local server.** See "The window" above.
* **Without WebView2 the update still happens.** If the window cannot be
  opened and the app asked for the update (`--auto-confirm`), the update is
  installed without a window. Started by hand, the updater says in a plain
  Windows message why it has no window, and changes nothing. The Python
  updater opened the page in the default browser in a similar spot.
* **WebView2's profile folder** is `updater\webview` in the app's data folder,
  beside the updater's log.

## What has not been done yet

Nobody has run the exe that ships: it asks for administrator rights, and
everything above was run without them. So these are still to be seen on a
real PC, by a person: the administrator prompt, an update into Program Files,
and the app starting this updater in place of the Python one. Dragging the
window and pressing its buttons with a mouse have not been tried either; the
checks drive the page, not the mouse.

## Never run it against a real install by hand

The exe asks for administrator rights and replaces `Vispora.exe`. It also
closes the app it is about to replace by file name, wherever that app runs
from: pointed at a `Vispora.exe` in any folder, it closes the Vispora that is
running. To try it, use `app/try_it.py` or the tests.
