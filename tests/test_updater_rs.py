"""The standalone updater in Rust (updater_rs/), held against the Python one
it is to replace (updater/, update_sources.py) and against the app that
starts it (core/updater.py).

The Rust updater has its own tests, ported from this folder's
(updater_rs/core/tests/, run by `cargo test`).  What is checked here is what
only this side can check:

  * that the two agree on every name, address and step they share;
  * that the Rust one takes every argument the app passes;
  * that its file is found on GitHub the way the app looks for an updater;
  * and, on a PC where it has been built, that its decisions are the same as
    update_sources.py's for the same answers from GitHub: the cases of
    test_update_sources.py and a few thousand made-up ones.

The last part runs a small tool built from the Rust code,
vispora-updater-decide.exe.  It reads JSON, prints JSON, and opens no file and
no connection.  Nothing else of the Rust updater is ever started here.
"""
import sandbox as m

import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys

import config
import update_sources as SRC
import test_update_sources as TS                    # its releases, as GitHub lists them

UM, SV, P = TS.UM, TS.SV, TS.P                      # the Python updater's modules
from core import updater as U                       # noqa: E402

RS = os.path.join(m.REPO, "updater_rs")
CORE = os.path.join(RS, "core")
# (where cargo put it: the folder CARGO_TARGET_DIR names, when the build output
# is kept out of the repository as README.md says, else the crate's own)
DECIDE = next((path for path in (
    os.path.join(os.environ.get("CARGO_TARGET_DIR") or CORE, "release", "vispora-updater-decide.exe"),
    os.path.join(CORE, "target", "release", "vispora-updater-decide.exe"),
) if os.path.isfile(path)), os.path.join(CORE, "target", "release", "vispora-updater-decide.exe"))


def rust(*path):
    with open(os.path.join(CORE, *path), encoding="utf-8") as f:
        return f.read()


def const(source, name):
    """The text of `pub const NAME: &str = "..."` in a Rust file."""
    found = re.search(r"pub const %s: &str =\s*\"([^\"]*)\";" % re.escape(name), source)
    assert found, name
    return found.group(1)


def rust_version():
    found = re.search(r'^version = "([^"]+)"', rust("Cargo.toml"), re.M)
    assert found
    return found.group(1)


# ═════════════════════════════════════════════════════════════════════
# What the two updaters and the app have to agree on
# ═════════════════════════════════════════════════════════════════════
def test_the_rust_updater_is_newer_than_the_last_python_one():
    version = rust_version()
    assert SRC.parse_version(version) > SRC.parse_version(UM.UPDATER_VERSION), \
        "the app keeps the updater with the higher version: the Rust one has to be above the Python one"
    assert SRC.parse_version(version) >= SRC.parse_version(SRC.UPDATER_MIN_FOR_SOURCES), \
        "below this the app would not pass --source"
    assert "pub const UPDATER_VERSION: &str = env!(\"CARGO_PKG_VERSION\");" in rust("src", "lib.rs"), \
        "one place says the version: Cargo.toml"


def test_its_release_file_is_found_the_way_the_app_looks_for_an_updater():
    name = f"VisporaUpdater-{rust_version()}.exe"
    assert SRC.UPDATER_ASSET_RE.fullmatch(name)
    listed = [TS.release("9.0.0", files=(("Vispora.exe", TS.APP_NEW), ("VisporaUpdater-1.5.0.exe", TS.UPDATER_NEW))),
              TS.release("9.1.0", files=(("Vispora.exe", TS.APP_NEW), (name, TS.UPDATER_NEWER)))]
    for r in listed:
        r.pop("_bodies")
    assert SRC.github_updater(listed, "stable")["version"] == rust_version(), "the newer one, by the version in its name"
    # ... and the app would then tell it where to look, as it does a 1.5.0.
    with TS.app():
        TS.updater_on_disk(version=rust_version())
        assert U._updater_knows_sources()


def test_it_takes_every_argument_the_app_passes():
    cli = rust("src", "cli.rs")
    values = set(re.findall(r'"(--[a-z-]+)"', cli[cli.index("const VALUE_OPTIONS"):cli.index("const FLAG_OPTIONS")]))
    flags = set(re.findall(r'"(--[a-z-]+)"', cli[cli.index("const FLAG_OPTIONS"):cli.index("const HELP_OPTION")]))
    # What the Python updater declares (updater/main.py) ...
    main = open(os.path.join(m.REPO, "updater", "main.py"), encoding="utf-8").read()
    declared = dict(re.findall(r'p\.add_argument\("(--[a-z-]+)",\s*([^)]*)\)', main))
    assert set(declared) == values | flags, "the same command line, so that the app can start either"
    assert {name for name, rest in declared.items() if "store_true" in rest} == flags
    # ... and what the app passes at install time (core/updater.py).
    app = open(os.path.join(m.REPO, "core", "updater.py"), encoding="utf-8").read()
    hand_off = app[app.index("args = [\n        UPDATER_EXE,"):app.index('log.info(f"  Spawning:')]
    passed = set(re.findall(r'"(--[a-z-]+)"', hand_off))
    assert passed >= {"--target-exe", "--target-dir", "--channel", "--current-version", "--target-version", "--server",
                      "--auto-confirm", "--source"}
    assert passed <= values | flags, passed - values - flags
    # The choices are the same too.
    assert '"--channel" => &["", "stable", "beta"]' in cli
    assert '"--source" => &["", "auto", "github", "server"]' in cli
    assert ("",) + SRC.SOURCE_SETTINGS == ("", "auto", "github", "server")


def test_the_names_and_addresses_are_the_same():
    sources, paths, cli = rust("src", "sources.rs"), rust("src", "paths.rs"), rust("src", "cli.rs")
    assert const(sources, "SERVER_URL_DEFAULT") == SRC.SERVER_URL_DEFAULT == config.UPDATE_SERVER_URL_DEFAULT
    assert const(sources, "GITHUB_REPO") == SRC.GITHUB_REPO
    assert const(sources, "GITHUB_RELEASES_URL") == SRC.GITHUB_RELEASES_URL
    assert const(sources, "GITHUB_RELEASES_PAGE") == SRC.GITHUB_RELEASES_PAGE
    assert const(sources, "APP_ASSET") == SRC.APP_ASSET == config.APP_EXE_NAME
    assert const(sources, "UPDATER_MIN_FOR_SOURCES") == SRC.UPDATER_MIN_FOR_SOURCES
    for name, value in SRC.GITHUB_HEADERS.items():
        assert f'("{name}", "{value}")' in sources, name
    assert 'const UPDATER_ASSET_PREFIX: &str = "VisporaUpdater-";' in sources
    assert f'"VisporaUpdater-{rust_version()}.exe"'.startswith('"' + config.UPDATER_EXE_NAME[:-4] + "-")
    assert "const MIN_EXE_BYTES: i128 = 1024 * 1024;" in sources and SRC._MIN_EXE_BYTES == 1024 * 1024
    assert const(paths, "APP_NAME") == P.APP_NAME == config.APP_NAME
    assert const(paths, "LEGACY_APP_NAME") == P.LEGACY_APP_NAME == config.LEGACY_APP_NAME
    assert 'pub const APP_EXE_NAMES: [&str; 2] = ["Vispora.exe", "GhostShell.exe"];' in paths
    assert P.APP_EXE_NAMES == ("Vispora.exe", "GhostShell.exe")
    assert const(cli, "DEAD_SERVER_HOST") == UM._DEAD_SERVER_HOST
    assert config.is_dead_update_server("https://" + const(cli, "DEAD_SERVER_HOST"))
    # The same files in the same places: the log, the record, where a download is staged.
    assert 'join(&updater_dir, "updater.log")' in paths and P.LOG_PATH.endswith(os.path.join("updater", "updater.log"))
    assert 'join(&appdata_dir, "install_info.json")' in paths and P.INSTALL_INFO.endswith("install_info.json")
    assert os.path.basename(U.INSTALL_INFO_PATH) == "install_info.json", "the record the app writes"


def test_the_steps_and_the_page_are_the_same():
    state = rust("src", "state.rs")
    steps = re.findall(r'\("(\w+)", "([^"]+)"\),', state[state.index("pub const PIPELINE_STEPS"):state.index("];")])
    assert steps == [(s["key"], s["label"]) for s in SV.PIPELINE_STEPS]
    # One copy of the page for both updaters, while both exist.
    server = rust("src", "server.rs")
    for name in ("templates/index.html", "static/app.js", "static/style.css"):
        assert f'include_str!("../../../updater/{name}")' in server, name
        assert os.path.isfile(os.path.join(m.REPO, "updater", *name.split("/")))
    # Every path the page asks for is answered.
    page = open(os.path.join(m.REPO, "updater", "static", "app.js"), encoding="utf-8").read()
    asked = set(re.findall(r"'(/api/[a-z-]+)'", page))
    assert asked and all(f'"{path}"' in server for path in asked), asked
    # ... and the status has every field the Python one has.
    for key in SV._state:
        assert f'("{key}", ' in state, key


def test_the_same_exit_codes_and_messages():
    """What the app, a script or a person sees when the updater stops: the
    Python updater's own words, which its tests look for."""
    installer = rust("src", "installer.rs")
    for said in ("could not reach GitHub", "no release published on '{channel}' on GitHub",
                 "no release published on '{channel}'", "nowhere else to look for a release",
                 "older than the v{floor} {floor_is}", "that was asked for", "that is installed",
                 "downloaded file is {size} B, likely a server error page", "SHA-256 mismatch: expected ",
                 "refusing to install an unverified binary", "could not back up the old exe: {e}",
                 "could not delete old exe: {e}", "install copy failed: {last_err}; rolled_back={}",
                 "Download failed: {e}", "The update stopped on an unexpected error: {why}",
                 "No release channel: please pick stable or beta.", "Please close {APP_NAME} manually and click Retry.",
                 "so the previous version was put back", "It is saved as {}", "Latest on '{}': v{} (from {place})",
                 "; the file from {instead_of} could not be used"):
        assert said in installer, said
    # Every exit code the Python updater can end with, the Rust one can too:
    # 0 done, 1 cancelled, 2 nothing to go on, 3 close, 4 no build, 5
    # download, 6 verify, 7 install, 8 launch, 9 the unexpected.
    python = open(os.path.join(m.REPO, "updater", "installer.py"), encoding="utf-8").read()
    in_python = set(re.findall(r"return (\d)\b", python)) | set(re.findall(r'\("(?:download|verify)", (\d),', python))
    in_rust = set(re.findall(r"\bOk\((\d)\)", installer)) | set(re.findall(r'\("(?:download|verify)", (\d),', installer))
    if re.search(r'unexpected error: \{why\}"\)\);\n        9\n', installer):
        in_rust.add("9")
    assert in_python == in_rust == set("0123456789"), (sorted(in_python), sorted(in_rust))


def test_the_core_uses_no_outside_crates():
    manifest = rust("Cargo.toml")
    dependencies = manifest[manifest.index("[dependencies]"):manifest.index("[profile.release]")]
    assert [line for line in dependencies.splitlines()[1:] if line.strip() and not line.startswith("#")] == []
    assert "[build-dependencies]" not in manifest and "[dev-dependencies]" not in manifest
    lock = rust("Cargo.lock")
    assert lock.count("[[package]]") == 1 and 'name = "vispora-updater-core"' in lock, \
        "a build of the part that downloads, checks and swaps the exe downloads nothing"


def test_the_python_updater_is_still_here_and_buildable():
    for name in ("main.py", "server.py", "installer.py", "paths.py"):
        assert os.path.isfile(os.path.join(m.REPO, "updater", name)), name
    assert os.path.isfile(os.path.join(m.REPO, "updater_helper.spec")), "kept until the Rust one has shipped and worked"


# ═════════════════════════════════════════════════════════════════════
# The window around it (updater_rs/app), which is the exe that ships
# ═════════════════════════════════════════════════════════════════════
APP = os.path.join(RS, "app")


def app(*path):
    with open(os.path.join(APP, *path), encoding="utf-8") as f:
        return f.read()


def repo(*path):
    with open(os.path.join(m.REPO, *path), encoding="utf-8") as f:
        return f.read()


def test_the_exe_carries_the_cores_version():
    manifest = app("Cargo.toml")
    assert re.search(r'^version = "([^"]+)"', manifest, re.M).group(1) == rust_version()
    assert json.loads(app("tauri.conf.json"))["version"] == rust_version(), "what Windows shows in the exe's properties"
    assert 'vispora-updater-core = { path = "../core" }' in manifest
    assert '[[bin]]\nname = "VisporaUpdater"\n' in manifest, "the file name the app starts"
    assert "cli::prepare(&updater, &args, UPDATER_VERSION)" in app("src", "main.rs"), "and what it says of itself"


def test_tauri_is_pinned_and_the_lock_file_is_whole():
    manifest = app("Cargo.toml")
    pinned = dict(re.findall(r'^(tauri(?:-build)?) = \{ version = "=(\d+\.\d+\.\d+)"', manifest, re.M))
    assert set(pinned) == {"tauri", "tauri-build"}, "one exact version each"
    wanted = manifest[manifest.index("[dependencies]"):manifest.index("[profile.release]")]
    assert sorted(re.findall(r"^([\w-]+) = ", wanted, re.M)) == ["tauri", "tauri-build", "vispora-updater-core"], \
        "nothing else is asked for by name"
    lock = app("Cargo.lock")
    packages = re.split(r"\n\[\[package\]\]\n", lock)[1:]
    assert len(packages) > 100
    for name, version in pinned.items():
        assert f'name = "{name}"\nversion = "{version}"\n' in lock, name
    for package in packages:
        name = re.match(r'name = "([^"]+)"', package).group(1)
        if name in ("vispora-updater", "vispora-updater-core"):
            assert "source = " not in package, name
        else:
            # From crates.io and nowhere else, each with the hash cargo checks
            # the download against.
            assert 'source = "registry+https://github.com/rust-lang/crates.io-index"' in package, name
            assert re.search(r'^checksum = "[0-9a-f]{64}"$', package, re.M), name
    # ... and the build is told to keep to it.
    assert "cargo build --release --locked --manifest-path updater_rs/app/Cargo.toml" in repo("updater_rs", "README.md")


def test_the_exe_asks_for_administrator_rights_like_the_python_one():
    assert re.search(r"uac_admin\s*=\s*True", repo("updater_helper.spec"))
    assert 'level="requireAdministrator"' in app("app.manifest")
    assert re.search(r'^default = \["elevated"\]$', app("Cargo.toml"), re.M), \
        "what a plain cargo build makes is the exe that ships"
    build = app("build.rs")
    assert 'include_str!("app.manifest")' in build and ".app_manifest(manifest)" in build
    assert "CARGO_FEATURE_ELEVATED" in build
    # What keeps a trial's window off the screen is not in the exe that ships.
    assert 'if cfg!(feature = "elevated") {\n        return None;\n    }' in app("src", "main.rs")


def test_the_window_is_the_python_updaters_window():
    python, main = repo("updater", "main.py"), app("src", "main.rs")
    width, height = re.search(r"width=(\d+),\s*height=(\d+)", python).groups()
    least = re.search(r"min_size=\((\d+), (\d+)\)", python).groups()
    assert f".inner_size({width}.0, {height}.0)" in main and f".min_inner_size({least[0]}.0, {least[1]}.0)" in main
    colour = re.search(r'background_color="#([0-9a-f]{6})"', python).group(1)
    red, green, blue = (int(colour[i:i + 2], 16) for i in (0, 2, 4))
    assert f".background_color(Color({red}, {green}, {blue}, 255))" in main
    assert "frameless=True" in python and ".decorations(false)" in main
    assert "resizable=False" in python and ".resizable(false)" in main
    assert '"Vispora Updater",' in python and 'const TITLE: &str = "Vispora Updater";' in main
    # The page's own title bar moves the window: WebView2 reads that from the
    # page's style sheet, which is why the page needed no change.
    style = repo("updater", "static", "style.css")
    assert "-webkit-app-region: drag;" in style and "-webkit-app-region: no-drag;" in style
    # What the page asks goes to the function the cargo tests call.
    assert "server::handle(&updater, request.method().as_str(), request.uri().path(), request.body())" in main
    # A closed window does not cut a running install off.
    assert "stop_install()" in python and "updater.shut_down();" in main
    # --silent: no window, and the pipeline's exit code.
    assert "if args.silent { cli::run_silent(&updater) } else { with_window(&updater) }" in main


def test_the_page_cannot_reach_tauris_commands():
    main = app("src", "main.rs")
    for word in ("invoke_handler", "generate_handler", "tauri::command", ".plugin("):
        assert word not in main, word
    conf = json.loads(app("tauri.conf.json"))
    assert conf["app"]["security"]["capabilities"] == [] and conf["app"]["windows"] == []
    assert not conf["app"].get("withGlobalTauri")
    assert not os.path.isdir(os.path.join(APP, "capabilities")), "a capabilities folder hands the page permissions"
    assert conf["bundle"]["active"] is False, "no installer is made: the exe is the download"
    assert ".on_navigation(" in main, "the window stays on the updater's own page"
    assert 'default-features = false, features = ["wry", "custom-protocol"]' in app("Cargo.toml"), \
        "no developer tools, no tray, no extra protocols"


def test_the_trial_cannot_be_pointed_at_the_real_app():
    """updater_rs/app/try_it.py starts the real exe, and that exe ends the app
    it is about to replace by file name, wherever the app runs from."""
    trial = app("try_it.py")
    name = re.search(r'^APP_NAME = "([^"]+)"$', trial, re.M).group(1)
    assert name.lower() not in {n.lower() for n in P.APP_EXE_NAMES}
    assert '"--target-exe", self.app' in trial and '"--source", "server", "--server", self.server.base' in trial
    for folder in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)"):
        assert f'("{folder}", ' in trial, folder
    assert 'refuse("this terminal runs as administrator' in trial
    assert "b'level=\"requireAdministrator\"' in exe" in trial, "the exe that ships is never started by it"
    # With its window shown, the trial's server has nothing to install.
    assert 'Trial("by hand", root, old_build, new_build, offers=False)' in trial


# ═════════════════════════════════════════════════════════════════════
# The same decisions, on a PC where the Rust side has been built
# ═════════════════════════════════════════════════════════════════════
def decide(requests):
    """Ask the tool `requests` in one go; its answers in the same order."""
    p = m.REAL_POPEN([DECIDE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out, err = p.communicate(json.dumps({"ask": "batch", "requests": requests}).encode("utf-8"), timeout=120)
    assert p.returncode == 0, err.decode("utf-8", "replace")
    return json.loads(out.decode("utf-8"))


def built():
    """Is the tool there?  On a PC without Rust it is not, and the tests
    below then have nothing to compare: cargo test covers the same ground."""
    return os.path.isfile(DECIDE)


def _plain(release):
    r = dict(release)
    r.pop("_bodies", None)
    return r


VERSIONS = ["2.9.9.8", "3.0.0-alpha.5", "3.0.0-beta.1", "3.0.0-beta.99", "3.0.0-rc.1", "3.0.0", "3.0.0.1", "3.7.0",
            "3.8.0-beta.1", "3.8.0", "3.10.0", "v3.8.0", "V3.8.0", "3.8.0.0", "", None, "three.x", "9.0.1+b5",
            "9.0.1-rc.2+b5", " 3.8.1 ", "3.8", "3", "3.8.1.2.3", "3..1", "3.x.1", "3.08.1", "3.8.1-", "3.8.1-beta",
            "3.8.1-beta.", "3.8.1-beta.x", "3.8.1-BETA.2", "3.8.1-Rc.3", "3.8.1-nightly.4", "3.8.1-beta.2.9",
            "1_0.2", "1.2_3.4", "_1.2", "1__0.2", "+3.8", "-3.8", "3.-8", "vv3.8.1", "3.8.1-alpha.-5", "3.8.1+", 7, 0,
            3.8, True, False, "99999999999999999999.1", "٣.٨"]


def test_versions_are_read_the_same():
    if not built():
        return
    rust_side = decide([{"ask": "version", "versions": VERSIONS}])[0]
    for version, got in zip(VERSIONS, rust_side):
        expected = list(SRC.parse_version(version))
        if version == "99999999999999999999.1":
            assert got[0] == 2 ** 63 - 1 and got[1:] == expected[1:], "larger than any number Rust keeps: the largest it has"
            continue
        if version == "٣.٨":
            assert got == [0, 0, 0, 0, 0] and expected == [3, 8, 0, 0, 0], \
                "digits that are not 0-9: Python reads them, the Rust side does not (no release is numbered so)"
            continue
        assert got == expected, (version, got, expected)
    order = sorted(range(len(VERSIONS)), key=lambda i: SRC.parse_version(VERSIONS[i]))
    assert order == sorted(range(len(VERSIONS)), key=lambda i: (rust_side[i] if VERSIONS[i] not in (
        "99999999999999999999.1", "٣.٨") else list(SRC.parse_version(VERSIONS[i])))), "and they sort the same"


def test_the_places_are_asked_in_the_same_order():
    if not built():
        return
    settings = [None, "", "auto", "github", "server", "GitHub ", " SERVER", "railway", 7, True]
    servers = [None, "", SRC.SERVER_URL_DEFAULT, SRC.SERVER_URL_DEFAULT + "/", SRC.SERVER_URL_DEFAULT.upper() + "//",
               "https://updates.example.test", " https://updates.example.test/ ", "http://127.0.0.1:8080", 7]
    cases = [(s, u) for s in settings for u in servers]
    answers = decide([{"ask": "source_order", "setting": s, "server_url": u} for s, u in cases])
    for (setting, server), got in zip(cases, answers):
        assert got == {"setting": SRC.source_setting(setting), "order": list(SRC.source_order(setting, server))}, \
            (setting, server, got)


def _same_offers(lists):
    """Each list of releases, for both channels: the Rust side's answer must
    be update_sources.py's."""
    cases = [(listed, channel) for listed in lists for channel in ("stable", "beta")]
    answers = decide([{"ask": "github", "releases": listed, "channel": channel} for listed, channel in cases])
    for (listed, channel), got in zip(cases, answers):
        expected = {"app": SRC.github_app_release(listed, channel), "updater": SRC.github_updater(listed, channel)}
        assert got == expected, (channel, json.dumps(listed)[:400], got, expected)
    return len(cases)


def test_the_cases_of_the_python_tests_are_decided_the_same():
    if not built():
        return
    R, A, tag = TS.release, TS.asset, "v9.0.0"
    good = R("8.0.0")
    not_ready = [
        R("9.0.0", draft=True),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag, state="new")]),
        R("9.0.0", assets=[]),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag, digest=None)]),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag, digest="md5:" + "a" * 32)]),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag, digest="sha256:" + "a" * 63)]),
        R("9.0.0", assets=[A("Vispora.exe", b"<html>", tag)]),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag, size="big")]),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag,
                             browser_download_url="https://example.test/releases/download/v9.0.0/Vispora.exe")]),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag,
                             browser_download_url="https://github.com@example.test/releases/download/x/Vispora.exe")]),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag,
                             browser_download_url=TS.url_of("9.0.0").replace("https://", "http://"))]),
        R("9.0.0", assets=[A("Vispora.exe", TS.APP_NEW, tag,
                             browser_download_url="https://github.com/somebody/else/raw/main/Vispora.exe")]),
        R("9.0.0", files=(("GhostShell.exe", TS.APP_NEW), ("vispora.exe.zip", TS.APP_NEW))),
        R("9.0.0", tag="nightly"),
        R("9.0.0", tag_name=None),
    ]
    with_updaters = [
        R("9.0.0", files=(("Vispora.exe", TS.APP_NEW), ("VisporaUpdater-1.5.0.exe", TS.UPDATER_NEW))),
        R("9.1.0-beta.1", prerelease=True, files=(("Vispora.exe", TS.APP_NEW), ("VisporaUpdater-1.6.0.exe", TS.UPDATER_NEWER))),
        R("8.0.0", files=(("VisporaUpdater-1.4.5.exe", TS.UPDATER_OLD), ("VisporaUpdater.exe", TS.UPDATER_OLD),
                          ("VisporaUpdater-1.9.0.exe.part", TS.UPDATER_OLD), ("xVisporaUpdater-2.0.0.exe", TS.UPDATER_OLD))),
        R("9.7.0", files=(("Vispora.exe", TS.APP_NEW), (f"VisporaUpdater-{rust_version()}.exe", TS.UPDATER_NEWER))),
    ]
    old = [{"tag_name": v, "name": "v" + v, "draft": False, "prerelease": False, "body": "# GhostShell v" + v,
            "published_at": "2026-05-07T19:26:50Z",
            "assets": [{"name": "GhostShell.exe", "size": 23966402, "state": "uploaded",
                        "digest": "sha256:22e5764423919235675ec5624893239f4f35dd5bdea4a645e7537c11251ec27c",
                        "browser_download_url": f"https://github.com/DeroXP/ghostshell/releases/download/{v}/GhostShell.exe"}]}
           for v in ("2.9.9.8", "2.9.9.7")]
    lists = [
        [R("9.0.0"), R("9.1.0-beta.2", prerelease=True, files=(("Vispora.exe", TS.APP_OTHER),)), R("8.5.0"),
         R("9.1.0-beta.1", prerelease=True)],
        [R("9.0.0"), R("9.0.0-beta.3", prerelease=True)],
        [R("9.2.0-beta.1"), R("9.0.0")],
        [R("9.2.0-rc.1")],
        [R("8.9.1"), R("9.0.0"), R("8.0.0")],
        [R("8.0.0"), R("9.0.0"), R("8.9.1")],
        [R("9.0.0", tag="9.0.0"), R("8.0.0")],
        with_updaters, with_updaters[:1], with_updaters[::-1], [R("9.5.0"), with_updaters[0]], [R("9.5.0")],
        old, old + [R("9.0.0")],
    ]
    lists += [[r, good] for r in not_ready] + [[r] for r in not_ready]
    lists = [[_plain(r) for r in listed] for listed in lists]
    lists += [None, {}, "", "<html>", 7, {"message": "API rate limit exceeded for 203.0.113.9."}, [],
              [None, 7, "x", [], {}], [{"tag_name": "v9.0.0", "assets": "Vispora.exe"}],
              [{"tag_name": "v9.0.0", "assets": [None, "x", {"name": 3}, {"name": "Vispora.exe"}]}]]
    assert _same_offers(lists) == 2 * len(lists) > 100


def _made_up_release(rng):
    """A release as GitHub might list it, or might never: mostly right, and
    every field now and then missing, of the wrong kind or slightly off."""
    def mostly(right, *wrong, p=0.9):
        return right if rng.random() < p else rng.choice(wrong)

    sha = "%064x" % rng.getrandbits(256)
    version = mostly(rng.choice(["9.0.0", "9.1.0", "9.1.0-beta.1", "9.1.0-rc.2", "8.5.3", "10.0.0", "9.0.0.1", "9.0",
                                 "9.1.0-beta.10", "9.1.0+b7", "0.0.1", "9.1.0-alpha.3"]),
                     "nightly", "", "9.1.0-", "v9", "9.1.0 ", "9", p=0.92)
    tag = mostly(rng.choice(["v" + version, "v" + version, version, "V" + version, " v" + version + " "]),
                 None, 9, "vv" + version, "release-" + version, p=0.93)

    def file(name):
        good = f"https://github.com/DeroXP/ghostshell/releases/download/{tag}/{name}"
        a = {"name": name,
             "state": mostly("uploaded", "new", "starter", None, "UPLOADED", True),
             "size": mostly(rng.choice([27_000_000, 1024 * 1024, 2 ** 40]), 1024 * 1024 - 1, 0, -5, "27000000",
                            27_000_000.0, True, None),
             "digest": mostly(rng.choice(["sha256:" + sha, "sha256:" + sha, "SHA256:" + sha.upper(), "  sha256:" + sha + "\n"]),
                              "sha256:" + sha[:63], "sha256:" + sha + "0", "sha512:" + sha, "sha256:" + "g" * 64, sha,
                              None, "", 7),
             "browser_download_url": mostly(
                 rng.choice([good, good, good.replace("https://github.com", "HTTPS://GitHub.com"), good + "?x=1#frag"]),
                 good.replace("https://", "http://"), good.replace("github.com", "github.com.example.test"),
                 good.replace("github.com", "github.com@example.test"), good.replace("github.com", "github.com:443"),
                 f"https://github.com/DeroXP/ghostshell/archive/{name}", f"https://github.com/x?to=/releases/download/{name}",
                 good + " ", good.replace("https://", ""), None, 7, "")}
        for key in list(a):
            if rng.random() < 0.02:
                del a[key]
        return a

    names = rng.sample(["Vispora.exe", "Vispora.exe", "Vispora.exe", "vispora.exe", "GhostShell.exe",
                        "VisporaUpdater-1.5.0.exe", "VisporaUpdater-2.0.0.exe", "VisporaUpdater-2.0.0-beta.1.exe",
                        "VisporaUpdater-1.10.0.exe", "VisporaUpdater.exe", "VisporaUpdater-2.0.0.exe.part",
                        "VisporaUpdater-v2.exe", "xVisporaUpdater-3.0.0.exe", "VisporaUpdater-9.exe", "notes.txt", 3, None],
                       k=rng.randint(0, 5))
    assets = [mostly(file(n), None, "x", 7, [], p=0.95) for n in names]
    r = {"tag_name": tag,
         "draft": mostly(False, True, None, 0, 1, "", "yes"),
         "prerelease": mostly(rng.choice([False, False, True]), None, 0, 1, "", "no"),
         "body": mostly("What is new.", "", None, "Zeile\nzwei äöü \U0001f600", 5, 2.5, True, False, 0, p=0.7),
         "published_at": mostly("2026-11-24T10:00:00Z", None, "", 20261124, False, p=0.8),
         "assets": mostly(assets, None, "Vispora.exe", {}, 7, p=0.95)}
    for key in list(r):
        if rng.random() < 0.02:
            del r[key]
    return mostly(r, None, 7, "release", [], {}, p=0.97)


def test_three_thousand_made_up_answers_are_decided_the_same():
    """Not only the cases somebody thought of: releases put together at
    random (from a fixed seed), fields missing or of the wrong kind, both
    channels.  Whatever update_sources.py makes of one, the Rust side makes
    the same of it."""
    if not built():
        return
    rng = random.Random(20261124)
    lists = [[_made_up_release(rng) for _ in range(rng.randint(0, 6))] for _ in range(1500)]
    assert _same_offers(lists) == 3000
    found = [SRC.github_app_release(listed, "beta")["available"] for listed in lists]
    updaters = [SRC.github_updater(listed, "beta")["available"] for listed in lists]
    assert 400 < sum(found) < 1400 and sum(updaters) > 150, \
        f"the made-up answers are a real mix: {sum(found)} with a build, {sum(updaters)} with an updater"


# Words a command line can be made of: the options, written out and by their
# beginnings, with a value attached, and values of every odd kind.
ARGUMENT_WORDS = [
    "--target-dir", "--target-exe", "--channel", "--current-version", "--target-version", "--auto-confirm",
    "--server", "--source", "--silent", "--help", "-h",
    "--t", "--target", "--target-d", "--target-e", "--target-v", "--c", "--ch", "--cu", "--a", "--s", "--se", "--so",
    "--si", "--h", "--he",
    "--target-dir=x", "--channel=beta", "--channel=", "--source=github", "--silent=1", "--help=1", "--ch=stable",
    "--target-d=a=b", "--target-dir=--silent", "--bogus=1", "--=x", "--h=1",
    "x", "", "stable", "beta", "auto", "github", "server", "nope", "a b", "-", "--", "-5", "-5.5", "-.5", "-0",
    "-my folder", "--my folder", "--silent x", "--target-exe x", "--target-exe=x y", "--sil x", "--help x",
    "-x", "-hx", "-hh", "-h=1", "-h-x", "-h x", "-silent", "--bogus", "--bogus x", "=", "-5 6", "--x =1", "--x=1",
    "-x=1", "- 5", "C:/x", "3.9.0", r"C:\Program Files\Vispora\Vispora.exe",
]

# Taken as a value by the argparse of Python 3.13 and later, and refused by
# 3.12's.  3.12 is the Python the Python updater's exe is built with, so the
# Rust updater does as 3.12 does.
ONLY_NEWER_PYTHONS_TAKE = ("-5.", "-1e5", "-1_000", "-5a")


def _python_reads(argv):
    """What updater/main.py makes of a command line: the arguments, or the
    exit code it stops with."""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return UM.parse_args(argv)
    except SystemExit as stopped:
        return {"exit": stopped.code}


def test_a_command_line_is_read_the_same():
    """The app passes plain words, and those are covered above.  This is the
    rest: whatever somebody types, the Rust updater takes the words the
    Python one took, refuses the ones it refused, and shows the help where
    it did."""
    if not built():
        return
    assert "args" in decide([{"ask": "info"}])[0].get("asks", ()), \
        "the tool on disk is older than the source: rebuild it (cargo build --release in updater_rs/core)"
    rng = random.Random(20261125)
    lines = [[word] for word in ARGUMENT_WORDS]
    lines += [[option, word] for option in ("--target-dir", "--current-version", "--channel", "--silent", "--help")
              for word in ARGUMENT_WORDS]
    lines += [[rng.choice(ARGUMENT_WORDS) for _ in range(rng.randint(0, 5))] for _ in range(3000)]
    # (what the app passes, core/updater.py)
    lines.append(["--target-exe", r"C:\Program Files\Vispora\Vispora.exe", "--target-dir", r"C:\Program Files\Vispora",
                  "--channel", "stable", "--current-version", "3.8.1", "--target-version", "3.9.0",
                  "--server", SRC.SERVER_URL_DEFAULT, "--auto-confirm", "--source", "github"])
    rust = decide([{"ask": "args", "argv": argv} for argv in lines])
    python = [_python_reads(argv) for argv in lines]
    for argv, ours, theirs in zip(lines, rust, python):
        assert ours == theirs, (argv, ours, theirs)
    taken = sum("exit" not in read for read in python)
    shown = sum(read.get("exit") == 0 for read in python)
    refused = sum(read.get("exit") == 2 for read in python)
    assert taken + shown + refused == len(lines) and taken > 500 and shown > 300 and refused > 1000, \
        f"the made-up command lines are a real mix: {taken} taken, {shown} shown the help, {refused} refused"
    # Where the Pythons themselves differ.
    odd = [["--current-version", value] for value in ONLY_NEWER_PYTHONS_TAKE]
    assert decide([{"ask": "args", "argv": argv} for argv in odd]) == [{"exit": 2}] * len(odd)
    if sys.version_info < (3, 13):
        assert [_python_reads(argv) for argv in odd] == [{"exit": 2}] * len(odd)


def test_the_tool_says_who_it_is():
    if not built():
        return
    info = decide([{"ask": "info"}])[0]
    assert info["updater_version"] == rust_version(), "the tool on disk is from another version of the source: rebuild it"
    assert "args" in info.get("asks", ()), "the tool on disk is older than the source: rebuild it"
    assert info["server_url_default"] == SRC.SERVER_URL_DEFAULT and info["github_releases_url"] == SRC.GITHUB_RELEASES_URL
    assert info["github_headers"] == SRC.GITHUB_HEADERS
    assert info["app_exe_names"] == list(P.APP_EXE_NAMES)
    assert info["steps"] == [s["key"] for s in SV.PIPELINE_STEPS]
    assert info["dead_server_host"] == UM._DEAD_SERVER_HOST
