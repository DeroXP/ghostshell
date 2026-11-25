# The standalone updater, in Rust

`VisporaUpdater.exe` is the small program that replaces `Vispora.exe` with a
newer build: the app starts it and exits, or somebody starts it by hand. This
folder is that program rewritten in Rust, as the first part of step 3 of
[docs/rust-port.md](../docs/rust-port.md).

The Python updater (`updater/`, version 1.5.0) stays in the repository and
stays buildable until this one has shipped and worked. Until then there are
two, and they do the same thing: the same command line, the same places asked
in the same order, the same checks before a download is trusted, the same
backup, swap and rollback, the same `install_info.json` and the same log file.
The app can start either.

## Two crates

| Folder | What it is | Outside crates |
| --- | --- | --- |
| `core/` | Everything the updater does: where to look for a build, the download and its SHA-256 check, closing the app, backup, swap, rollback, the command line, and what its window is told | none |
| `app/` | The window (Tauri on WebView2) around `core/`, and the exe that is shipped | Tauri and what it needs |

The split is on purpose. `core/` is the code that downloads a program,
decides whether to trust it and puts it in the place of the app's exe, and it
runs as administrator. It is built from that folder and from Windows itself:
its HTTP client is Windows' own, its JSON reader and its SHA-256 are in the
folder, and a build downloads nothing. The window only shows what `core/`
does.

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

The window's page is the Python updater's own (`updater/templates`,
`updater/static`), compiled into the exe: one copy for both while both exist.

## Its version

`2.0.0`, in `core/Cargo.toml` and nowhere else. The app tells updaters apart
by version (1.5.0 was the first that knew GitHub) and keeps the one with the
higher number, and a release carries the file as
`VisporaUpdater-<version>.exe`. So the first Rust updater is above every
Python one.

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
on 127.0.0.1 that the test starts itself.

`tests/test_updater_rs.py` (part of `python tests/run_all.py`) checks what
only the Python side can: that the two updaters and the app agree on every
name, address, argument and step, and, where the Rust side has been built,
that it decides the same as `update_sources.py` for the same answers from
GitHub. It does that through `vispora-updater-decide.exe`, a small tool built
with `core/` that reads JSON, prints JSON, and opens no file and no
connection.

## Three things done differently from the Python updater

All three are stricter, not looser, and none changes what a normal update
does.

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

## Never run it against a real install by hand

The exe asks for administrator rights and replaces `Vispora.exe`. To try it,
use a folder of your own and `--target-dir`, or run the tests.
