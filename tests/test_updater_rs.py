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

import json
import os
import random
import re
import subprocess

import config
import update_sources as SRC
import test_update_sources as TS                    # its releases, as GitHub lists them

UM, SV, P = TS.UM, TS.SV, TS.P                      # the Python updater's modules
from core import updater as U                       # noqa: E402

RS = os.path.join(m.REPO, "updater_rs")
CORE = os.path.join(RS, "core")
DECIDE = os.path.join(CORE, "target", "release", "vispora-updater-decide.exe")


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
    flags = set(re.findall(r'"(--[a-z-]+)"', cli[cli.index("const FLAG_OPTIONS"):cli.index("const USAGE")]))
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


def test_the_tool_says_who_it_is():
    if not built():
        return
    info = decide([{"ask": "info"}])[0]
    assert info["updater_version"] == rust_version(), "the tool on disk is from another version of the source: rebuild it"
    assert info["server_url_default"] == SRC.SERVER_URL_DEFAULT and info["github_releases_url"] == SRC.GITHUB_RELEASES_URL
    assert info["github_headers"] == SRC.GITHUB_HEADERS
    assert info["app_exe_names"] == list(P.APP_EXE_NAMES)
    assert info["steps"] == [s["key"] for s in SV.PIPELINE_STEPS]
    assert info["dead_server_host"] == UM._DEAD_SERVER_HOST
