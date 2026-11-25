"""Where updates come from: GitHub's releases first, the update server second.

Three layers are checked here:
  * update_sources.py, the shared reading of what GitHub answers (pure);
  * the app's side (core/updater.py): the check, the download, the standalone
    updater's own download, and the hand-off at install time;
  * the standalone updater (updater/), which looks for the build itself.

Nothing here touches the network: GitHub and the update server are a table of
answers, and every request made is recorded.
"""
import sandbox as m

import contextlib
import hashlib
import io
import json
import os
import sys
import types
import urllib.request

import config
import update_sources as SRC
import core.updater as U
import test_updater as TU                           # its rig for the standalone updater: a sandboxed install

P, SV, I, UM = TU.P, TU.SV, TU.I, TU.UM

SERVER = SRC.SERVER_URL_DEFAULT
GITHUB = SRC.GITHUB_RELEASES_URL
OWN_SERVER = "https://updates.example.test"
APP_NEW = b"MZ" + b"new build " * 120000            # over 1 MB, as every real build is
APP_OTHER = b"MZ" + b"another build " * 90000
UPDATER_NEW = b"MZ" + b"updater 1.5 " * 100000
UPDATER_NEWER = b"MZ" + b"updater 1.6 " * 100000
UPDATER_OLD = b"MZ" + b"updater 1.4 " * 100000
NEWER, NEWEST = "9.0.0", "9.1.0"                    # far above whatever config.APP_VERSION is


# ── GitHub and the update server ──────────────────────────────────────
class _Response:
    def __init__(self, body, headers):
        self._body, self.headers = io.BytesIO(body), headers

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def asset(name, body, tag, **over):
    a = {"name": name, "state": "uploaded", "size": len(body),
         "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
         "browser_download_url": f"https://github.com/{SRC.GITHUB_REPO}/releases/download/{tag}/{name}"}
    a.update(over)
    return a


def release(version, files=(("Vispora.exe", APP_NEW),), prerelease=False, draft=False, tag=None, **over):
    tag = tag or "v" + version
    r = {"tag_name": tag, "name": tag, "draft": draft, "prerelease": prerelease, "body": f"What is new in {version}.",
         "published_at": "2026-11-24T10:00:00Z", "assets": [asset(name, body, tag) for name, body in files]}
    r["_bodies"] = {a["browser_download_url"]: body for a, (name, body) in zip(r["assets"], files)}
    r.update(over)
    return r


class Net:
    def __init__(self):
        self.routes, self.calls, self.headers = {}, [], {}

    def clear(self):
        self.routes.clear()
        self.headers.clear()
        del self.calls[:]

    def urlopen(self, req, timeout=0):
        url = getattr(req, "full_url", req)
        self.calls.append(url)
        self.headers[url] = {k.lower(): v for k, v in dict(getattr(req, "headers", {}) or {}).items()}
        if url not in self.routes:
            raise OSError(f"no route to {url}")
        body = self.routes[url]
        return _Response(body, {"Content-Length": str(len(body))})

    def github(self, *releases):
        """GitHub answers with these releases and serves their files."""
        listed = []
        for r in releases:
            r = dict(r)
            self.routes.update(r.pop("_bodies", {}))
            listed.append(r)
        self.routes[GITHUB] = json.dumps(listed).encode()

    def github_says(self, anything):
        self.routes[GITHUB] = json.dumps(anything).encode()

    def server(self, channel="stable", version=NEWER, body=APP_NEW, sha=None, base=SERVER):
        meta = {"available": True, "version": version, "size_bytes": len(body), "notes": f"Server notes for {version}.",
                "sha256": hashlib.sha256(body).hexdigest() if sha is None else sha,
                "download_url": f"{base}/download/{channel}"}
        self.routes[f"{base}/version/{channel}"] = json.dumps(meta).encode()
        self.routes[f"{base}/download/{channel}"] = body

    def server_updater(self, version="1.5.0", body=UPDATER_NEW, base=SERVER):
        self.routes[f"{base}/version/updater"] = json.dumps(
            {"available": True, "version": version, "sha256": hashlib.sha256(body).hexdigest()}).encode()
        self.routes[f"{base}/download/updater"] = body

    def asked(self, url):
        return self.calls.count(url)


NET = Net()
_FAKE_URLLIB = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=NET.urlopen))
_STATE0 = dict(U._update_state)
_INSTALL_STATE0 = dict(U._updater_install_state)


def url_of(version, name="Vispora.exe"):
    return f"https://github.com/{SRC.GITHUB_REPO}/releases/download/v{version}/{name}"


def read(path):
    with open(path, "rb") as f:
        return f.read()


@contextlib.contextmanager
def app(frozen=False, **settings):
    """core.updater on a clean record, with GitHub and the update server
    answering from NET.  `frozen`: as an installed copy; a hand-off to the
    standalone updater is then recorded in the list this yields, not made."""
    m.fresh()
    for folder in (U.UPDATE_DIR, U.UPDATER_DIR):
        os.makedirs(folder, exist_ok=True)
        for name in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
    for path in (U.SETTINGS_PATH, U.INSTALL_INFO_PATH):
        if os.path.exists(path):
            os.remove(path)
    NET.clear()
    U._forget_github_answer()
    U._update_state.clear()
    U._update_state.update(_STATE0)
    U._updater_install_state.clear()
    U._updater_install_state.update(_INSTALL_STATE0)
    spawned = []

    def popen(args, creationflags=0, **kw):
        spawned.append(list(args))
        return types.SimpleNamespace(pid=4321)

    saved = (U.urllib, U.subprocess, U._game_running, U._exit_for_update, U.is_safe_to_install)
    U.urllib = _FAKE_URLLIB
    U.subprocess = types.SimpleNamespace(Popen=popen, CREATE_NO_WINDOW=0x08000000)
    U._game_running = lambda: ""
    U._exit_for_update = lambda: None
    U.is_safe_to_install = lambda: {"safe": True, "reason": "system idle"}
    if frozen:
        sys.frozen = True
    try:
        if settings:
            assert U.set_settings(**settings)["ok"]
        yield spawned
    finally:
        U.urllib, U.subprocess, U._game_running, U._exit_for_update, U.is_safe_to_install = saved
        if frozen:
            del sys.frozen
        U._update_state.clear()
        U._update_state.update(_STATE0)


def updater_on_disk(body=UPDATER_NEW, version="1.5.0"):
    with open(U.UPDATER_EXE, "wb") as f:
        f.write(body)
    if os.path.exists(U.UPDATER_VERSION_PATH):
        os.remove(U.UPDATER_VERSION_PATH)
    if version:
        U._write_installed_updater_version(version)


def standalone(**init):
    """The standalone updater on a sandboxed install (test_updater's rig),
    asking this file's GitHub and update server.  Returns the app-exe path."""
    target = TU.setup(**init)
    NET.clear()
    I.urllib = _FAKE_URLLIB
    return target


def step_detail(key):
    return [s["detail"] for s in SV.get_state_snapshot()["steps"] if s["key"] == key][0]


# ═════════════════════════════════════════════════════════════════════
# update_sources.py: reading what GitHub answers
# ═════════════════════════════════════════════════════════════════════
def test_versions_sort_the_way_the_releases_are_numbered():
    order = ["2.9.9.8", "3.0.0-alpha.5", "3.0.0-beta.1", "3.0.0-beta.99", "3.0.0-rc.1", "3.0.0", "3.0.0.1", "3.7.0",
             "3.8.0-beta.1", "3.8.0", "3.10.0"]
    assert sorted(reversed(order), key=SRC.parse_version) == order
    assert SRC.parse_version("v3.8.0") == SRC.parse_version("3.8.0") == SRC.parse_version("3.8.0.0") == (3, 8, 0, 0, 0)
    assert SRC.parse_version("") == SRC.parse_version(None) == (0, 0, 0, 0, 0)
    assert SRC.parse_version("three.x") == (0, 0, 0, 0, 0), "never raises"
    assert SRC.parse_version("9.0.1+b5") == (9, 0, 1, 0, 0) and SRC.parse_version("9.0.1-rc.2+b5") == SRC.parse_version("9.0.1-rc.2")
    assert U._parse_version is SRC.parse_version, "the app and the updater read a version the same way"


def test_the_order_in_which_the_places_are_asked():
    assert SRC.source_order() == SRC.source_order("auto") == SRC.source_order("auto", SERVER) == ("github", "server")
    assert SRC.source_order("auto", SERVER + "/") == ("github", "server")
    assert SRC.source_order("github", OWN_SERVER) == ("github",) and SRC.source_order("server") == ("server",)
    assert SRC.source_order("auto", OWN_SERVER) == ("server",), "a server of your own is the one you meant, and the only one"
    assert SRC.source_order("auto", OWN_SERVER.upper() + "/") == ("server",)
    for odd in (None, "", "GitHub ", "railway", 7, ["github"]):
        assert SRC.source_setting(odd) in SRC.SOURCE_SETTINGS
    assert SRC.source_setting("GitHub ") == "github" and SRC.source_setting("railway") == "auto"
    assert SRC.source_order("railway") == ("github", "server")
    assert SRC.SERVER_URL_DEFAULT == config.UPDATE_SERVER_URL_DEFAULT == UM.SERVER_URL_DEFAULT
    assert set(SRC.SOURCE_NAMES) == set(SRC.SOURCES)


def test_stable_gets_the_newest_final_build_and_beta_the_newest_of_all():
    listed = [release("9.0.0"), release("9.1.0-beta.2", prerelease=True, files=(("Vispora.exe", APP_OTHER),)),
              release("8.5.0"), release("9.1.0-beta.1", prerelease=True)]
    stable, beta = SRC.github_app_release(listed, "stable"), SRC.github_app_release(listed, "beta")
    assert (stable["available"], stable["version"], stable["source"]) == (True, "9.0.0", "github")
    assert stable["sha256"] == hashlib.sha256(APP_NEW).hexdigest() and stable["size_bytes"] == len(APP_NEW)
    assert stable["download_url"] == url_of("9.0.0") and stable["notes"] == "What is new in 9.0.0."
    assert (beta["version"], beta["sha256"]) == ("9.1.0-beta.2", hashlib.sha256(APP_OTHER).hexdigest())
    # beta also gets a final build when that is the newest, as on the update server
    assert SRC.github_app_release([release("9.0.0"), release("9.0.0-beta.3", prerelease=True)], "beta")["version"] == "9.0.0"
    # a pre-release that was not marked as one still never reaches stable
    assert SRC.github_app_release([release("9.2.0-beta.1"), release("9.0.0")], "stable")["version"] == "9.0.0"
    assert SRC.github_app_release([release("9.2.0-rc.1")], "stable") == {"available": False, "source": "github"}


def test_the_newest_is_told_by_its_version_not_by_its_place_in_the_list():
    listed = [release("8.9.1"), release("9.0.0"), release("8.0.0")]         # a fix for an older line, published last
    assert SRC.github_app_release(listed, "stable")["version"] == "9.0.0"
    assert SRC.github_app_release(list(reversed(listed)), "stable")["version"] == "9.0.0"
    assert SRC.github_app_release([release("9.0.0", tag="9.0.0"), release("8.0.0")], "stable")["version"] == "9.0.0", \
        "a tag with or without the v"


def test_a_release_that_is_not_ready_is_passed_over():
    good = release("8.0.0")
    tag = "v9.0.0"
    not_ready = {
        "a draft": release("9.0.0", draft=True),
        "still uploading": release("9.0.0", assets=[asset("Vispora.exe", APP_NEW, tag, state="new")]),
        "no file at all": release("9.0.0", assets=[]),
        "no SHA-256": release("9.0.0", assets=[asset("Vispora.exe", APP_NEW, tag, digest=None)]),
        "another kind of hash": release("9.0.0", assets=[asset("Vispora.exe", APP_NEW, tag, digest="md5:" + "a" * 32)]),
        "a short hash": release("9.0.0", assets=[asset("Vispora.exe", APP_NEW, tag, digest="sha256:" + "a" * 63)]),
        "too small to be a build": release("9.0.0", assets=[asset("Vispora.exe", b"<html>", tag)]),
        "a size that is not a number": release("9.0.0", assets=[asset("Vispora.exe", APP_NEW, tag, size="big")]),
        "served from elsewhere": release("9.0.0", assets=[asset(
            "Vispora.exe", APP_NEW, tag, browser_download_url="https://example.test/releases/download/v9.0.0/Vispora.exe")]),
        "a look-alike host": release("9.0.0", assets=[asset(
            "Vispora.exe", APP_NEW, tag, browser_download_url="https://github.com@example.test/releases/download/x/Vispora.exe")]),
        "not https": release("9.0.0", assets=[asset(
            "Vispora.exe", APP_NEW, tag, browser_download_url=url_of("9.0.0").replace("https://", "http://"))]),
        "not a release file": release("9.0.0", assets=[asset(
            "Vispora.exe", APP_NEW, tag, browser_download_url="https://github.com/somebody/else/raw/main/Vispora.exe")]),
        "another file name": release("9.0.0", files=(("GhostShell.exe", APP_NEW), ("vispora.exe.zip", APP_NEW))),
        "a tag that is no version": release("9.0.0", tag="nightly"),
        "no tag": release("9.0.0", tag_name=None),
    }
    for why, r in not_ready.items():
        assert SRC.github_app_release([r, good], "beta")["version"] == "8.0.0", why
        assert SRC.github_app_release([r], "beta") == {"available": False, "source": "github"}, why


def test_an_answer_that_is_not_a_list_of_releases_offers_nothing():
    nothing = {"available": False, "source": "github"}
    for answer in (None, {}, "", "<html>", 7, {"message": "API rate limit exceeded for 203.0.113.9."},
                   [], [None, 7, "x", [], {}], [{"tag_name": "v9.0.0", "assets": "Vispora.exe"}],
                   [{"tag_name": "v9.0.0", "assets": [None, "x", {"name": 3}, {"name": "Vispora.exe"}]}]):
        assert SRC.github_app_release(answer, "stable") == nothing, answer
        assert SRC.github_updater(answer, "beta") == nothing, answer


def test_the_old_ghostshell_releases_on_github_offer_nothing():
    """What the repository's releases looked like before this: tags without a
    v, one GhostShell.exe each."""
    old = [{"tag_name": v, "name": "v" + v, "draft": False, "prerelease": False, "body": "# GhostShell v" + v,
            "published_at": "2026-05-07T19:26:50Z",
            "assets": [{"name": "GhostShell.exe", "size": 23966402, "state": "uploaded",
                        "digest": "sha256:22e5764423919235675ec5624893239f4f35dd5bdea4a645e7537c11251ec27c",
                        "browser_download_url": f"https://github.com/DeroXP/ghostshell/releases/download/{v}/GhostShell.exe"}]}
           for v in ("2.9.9.8", "2.9.9.7")]
    assert SRC.github_app_release(old, "stable")["available"] is False
    assert SRC.github_app_release(old + [release("9.0.0")], "stable")["version"] == "9.0.0"


def test_the_updater_is_found_by_the_version_in_its_file_name():
    listed = [release("9.0.0", files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.5.0.exe", UPDATER_NEW))),
              release("9.1.0-beta.1", prerelease=True, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.6.0.exe", UPDATER_NEWER))),
              release("8.0.0", files=(("VisporaUpdater-1.4.5.exe", UPDATER_OLD), ("VisporaUpdater.exe", UPDATER_OLD),
                                      ("VisporaUpdater-1.9.0.exe.part", UPDATER_OLD), ("xVisporaUpdater-2.0.0.exe", UPDATER_OLD)))]
    stable, beta = SRC.github_updater(listed, "stable"), SRC.github_updater(listed, "beta")
    assert (stable["version"], stable["sha256"]) == ("1.5.0", hashlib.sha256(UPDATER_NEW).hexdigest())
    assert stable["download_url"] == url_of("9.0.0", "VisporaUpdater-1.5.0.exe") and stable["source"] == "github"
    assert (beta["version"], beta["sha256"]) == ("1.6.0", hashlib.sha256(UPDATER_NEWER).hexdigest())
    # the newest updater, wherever it is: an app release without one does not hide an older release's
    assert SRC.github_updater([release("9.5.0"), listed[0]], "stable")["version"] == "1.5.0"
    assert SRC.github_updater([release("9.5.0")], "stable") == {"available": False, "source": "github"}
    half = release("9.6.0", assets=[asset("VisporaUpdater-1.7.0.exe", UPDATER_NEWER, "v9.6.0", state="new")])
    assert SRC.github_updater([half, listed[0]], "stable")["version"] == "1.5.0", "one that is still uploading is not there yet"


# ═════════════════════════════════════════════════════════════════════
# The app: the check
# ═════════════════════════════════════════════════════════════════════
def test_the_app_asks_github_first_and_takes_what_it_has():
    with app():
        NET.github(release(NEWER), release("8.0.0"))
        NET.server(version=NEWEST, body=APP_OTHER)          # even a newer one on the server: GitHub answered
        r = U.check_for_update()
        assert (r["ok"], r["update_available"], r["latest"], r["source"]) == (True, True, NEWER, "github")
        assert r["download_url"] == url_of(NEWER) and r["sha256"] == hashlib.sha256(APP_NEW).hexdigest()
        assert r["size_bytes"] == len(APP_NEW) and r["notes"] == f"What is new in {NEWER}."
        assert NET.calls == [GITHUB], "the update server is not asked when GitHub has the release"
        assert NET.headers[GITHUB]["accept"] == "application/vnd.github+json" and NET.headers[GITHUB]["user-agent"]
        st = U.get_update_status()
        assert (st["source"], st["source_order"], st["latest_version"]) == ("github", ["github", "server"], NEWER)
        assert st["valid_sources"] == ["auto", "github", "server"] and st["releases_page"].startswith("https://github.com/")


def test_when_github_does_not_answer_the_update_server_is_asked():
    for why, set_up in (("unreachable", lambda: None),
                        ("its hourly limit", lambda: NET.github_says({"message": "API rate limit exceeded"})),
                        ("not JSON", lambda: NET.routes.update({GITHUB: b"<html>502</html>"}))):
        with app():
            set_up()
            NET.server(version=NEWER)
            r = U.check_for_update()
            assert (r["ok"], r["update_available"], r["latest"], r["source"]) == (True, True, NEWER, "server"), why
            assert NET.calls == [GITHUB, f"{SERVER}/version/stable"], why
            assert U._update_state["latest_url"] == f"{SERVER}/download/stable" and U._update_state["latest_source"] == "server"
            assert U._update_state["error"] is None, why


def test_github_with_nothing_for_this_channel_lets_the_update_server_answer():
    with app():
        NET.github(release("9.5.0-beta.1", prerelease=True))        # nothing for stable
        NET.server(version=NEWER)
        r = U.check_for_update()
        assert (r["update_available"], r["latest"], r["source"]) == (True, NEWER, "server")
    with app():                                                     # both answer, neither has a build
        NET.github()
        NET.routes[f"{SERVER}/version/stable"] = json.dumps({"available": False, "version": None}).encode()
        r = U.check_for_update()
        assert (r["ok"], r["update_available"], r["latest"], r["source"]) == (True, False, None, "github")
        assert "No release published" in r["message"] and U._update_state["error"] is None
        assert NET.calls == [GITHUB, f"{SERVER}/version/stable"]


def test_a_place_that_is_down_is_not_taken_for_nothing_published():
    """GitHub has nothing for the channel and the update server cannot be
    reached: that is not "up to date".  What was staged before stays."""
    with app():
        NET.github(release(NEWER))
        assert U.check_for_update()["update_available"] and U.download_update()["ok"]
        NET.github()                                                # GitHub answers: no releases at all
        r = U.check_for_update()                                    # and the update server is down
        assert r == {"ok": False, "err": f"Could not reach update server ({SERVER})"}
        assert U._update_state["error"] == r["err"] and U._update_state["update_available"] is False
        assert U._update_state["download_complete"] is True and U._update_state["pending_install_path"]
        assert U._update_state["staged"] == {"version": NEWER, "source": "github"}
    with app():                                                     # the other way round
        NET.routes[f"{SERVER}/version/stable"] = json.dumps({"available": False}).encode()
        assert U.check_for_update() == {"ok": False, "err": "Could not reach GitHub"}


def test_when_nobody_answers_the_check_says_who_was_asked():
    with app():
        r = U.check_for_update()
        assert r == {"ok": False, "err": f"Could not reach GitHub or the update server ({SERVER})"}
        assert U._update_state["error"] == r["err"] and U._update_state["checking"] is False
    with app(update_source="github"):
        assert U.check_for_update()["err"] == "Could not reach GitHub" and NET.calls == [GITHUB]
    with app(update_source="server"):
        assert U.check_for_update()["err"] == f"Could not reach update server ({SERVER})"
        assert NET.calls == [f"{SERVER}/version/stable"]


def test_up_to_date_on_github_is_the_answer():
    with app():
        NET.github(release(config.APP_VERSION), release("0.9.0"))
        NET.server(version=NEWER)
        r = U.check_for_update()
        assert (r["ok"], r["update_available"], r["latest"], r["source"]) == (True, False, config.APP_VERSION, "github")
        assert NET.calls == [GITHUB] and U._update_state["update_available"] is False


def test_the_beta_channel_gets_pre_releases_from_github():
    with app(channel="beta"):
        NET.github(release(NEWER), release("9.5.0-beta.1", prerelease=True, files=(("Vispora.exe", APP_OTHER),)))
        r = U.check_for_update()
        assert (r["latest"], r["sha256"], r["channel"]) == ("9.5.0-beta.1", hashlib.sha256(APP_OTHER).hexdigest(), "beta")
    with app(channel="stable"):
        NET.github(release(NEWER), release("9.5.0-beta.1", prerelease=True))
        assert U.check_for_update()["latest"] == NEWER


def test_the_setting_says_which_places_are_asked():
    with app(update_source="github"):
        NET.server(version=NEWER)
        assert U.check_for_update()["ok"] is False and NET.calls == [GITHUB], "GitHub only: the update server is left alone"
        assert U.get_update_status()["source_order"] == ["github"]
    with app(update_source="server"):
        NET.github(release(NEWEST))
        NET.server(version=NEWER)
        r = U.check_for_update()
        assert (r["latest"], r["source"]) == (NEWER, "server") and NET.calls == [f"{SERVER}/version/stable"]
    with app(server_url=OWN_SERVER):                        # a server of one's own is the only place asked
        NET.github(release(NEWEST))
        NET.server(version=NEWER, base=OWN_SERVER)
        r = U.check_for_update()
        assert (r["latest"], r["source"]) == (NEWER, "server") and NET.calls == [f"{OWN_SERVER}/version/stable"]
        assert U.get_update_status()["source_order"] == ["server"]
    with app(server_url=OWN_SERVER):                        # ... also when it is down: it may carry its own build
        NET.github(release(NEWEST))
        r = U.check_for_update()
        assert r == {"ok": False, "err": f"Could not reach update server ({OWN_SERVER})"}
        assert NET.calls == [f"{OWN_SERVER}/version/stable"], "the official build on GitHub is not offered instead"
    with app(server_url=OWN_SERVER, update_source="github"):    # GitHub's build is theirs to pick
        NET.github(release(NEWEST))
        assert U.check_for_update()["latest"] == NEWEST and NET.calls == [GITHUB]
    with app():
        bad = U.set_settings(update_source="railway")
        assert bad["ok"] is False and "update_source" in bad["err"]
        assert U.get_settings()["update_source"] == "auto", "the default, and unchanged by a value it does not know"


def test_changing_where_updates_come_from_forgets_what_was_found():
    with app():
        NET.github(release(NEWER))
        assert U.check_for_update()["update_available"] and U.download_update()["ok"]
        assert U._update_state["download_complete"] and U._update_state["pending_install_path"]
        assert U.set_settings(update_source="server")["ok"]
        st = U._update_state
        assert (st["update_available"], st["latest_version"], st["latest_url"], st["latest_source"],
                st["download_complete"], st["pending_install_path"]) == (False, None, None, None, False, None)
    with app():
        NET.github(release(NEWER))
        assert U.check_for_update()["update_available"] and U.download_update()["ok"]
        assert U.set_settings(update_source="auto", auto_install=True)["ok"]    # the same source again
        assert U._update_state["download_complete"] and U._update_state["latest_version"] == NEWER, \
            "saving the source it already had keeps what was found and staged"
    with app(local_update_dir=m.ROOT):
        assert U.get_update_status()["source_order"] == ["local"], "a local update folder is asked instead of both"
        U.check_for_update()
        assert NET.calls == []


# ═════════════════════════════════════════════════════════════════════
# The app: the download
# ═════════════════════════════════════════════════════════════════════
def test_a_download_from_github_is_checked_against_githubs_sha256():
    with app():
        NET.github(release(NEWER))
        assert U.check_for_update()["update_available"]
        r = U.download_update()
        staged = os.path.join(U.UPDATE_DIR, f"Vispora_{NEWER}.exe")
        assert r["ok"] and r["path"] == staged and read(staged) == APP_NEW
        assert NET.calls == [GITHUB, url_of(NEWER)]
        assert U._update_state["download_complete"] and U._update_state["pending_install_path"] == staged
    with app():
        NET.github(release(NEWER))
        NET.routes[url_of(NEWER)] = APP_OTHER                   # not the file GitHub hashed
        assert U.check_for_update()["update_available"]
        r = U.download_update()
        assert r["ok"] is False and "SHA-256" in r["err"]
        assert not os.listdir(U.UPDATE_DIR), "what failed the check is not kept"
        assert U._update_state["download_complete"] is False and U._update_state["pending_install_path"] is None


# ═════════════════════════════════════════════════════════════════════
# The app: the standalone updater's own download
# ═════════════════════════════════════════════════════════════════════
def test_the_updater_is_downloaded_from_github_and_kept_up_to_date_from_there():
    with_updater = release(NEWER, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.5.0.exe", UPDATER_NEW)))
    with app():
        NET.github(with_updater)
        NET.server_updater(version="1.4.5", body=UPDATER_OLD)
        U._download_updater_worker()                            # the first start: no updater on disk
        assert U.is_updater_installed() and read(U.UPDATER_EXE) == UPDATER_NEW
        assert U._read_installed_updater_version() == "1.5.0" and U._updater_knows_sources()
        assert NET.calls == [GITHUB, url_of(NEWER, "VisporaUpdater-1.5.0.exe")], "the update server is not asked"
        del NET.calls[:]
        U._updater_refresh_worker()                             # the next start: nothing newer
        assert m.wait(lambda: not U._updater_install_state["running"])
        assert NET.calls == [], "GitHub's answer of a moment ago is used again, and nothing is downloaded"

        newer = release(NEWEST, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.6.0.exe", UPDATER_NEWER)))
        NET.github(with_updater, newer)
        U._forget_github_answer()                               # (five minutes later)
        U._updater_refresh_worker()
        assert m.wait(lambda: U._read_installed_updater_version() == "1.6.0")
        assert m.wait(lambda: not U._updater_install_state["running"]) and read(U.UPDATER_EXE) == UPDATER_NEWER


def test_githubs_answer_is_kept_for_a_few_minutes_but_a_check_always_asks():
    with app():
        NET.github(release(NEWER, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.5.0.exe", UPDATER_NEW))))
        assert U._updater_offer()["version"] == "1.5.0" and U._updater_offer()["source"] == "github"
        assert NET.asked(GITHUB) == 1
        assert U.check_for_update()["ok"] and U.check_for_update()["ok"]
        assert NET.asked(GITHUB) == 3, "Check for updates asks GitHub every time"
        saved = U.time
        U.time = types.SimpleNamespace(time=lambda: saved.time() + U.GITHUB_ANSWER_KEEP_S + 1, sleep=saved.sleep)
        try:
            assert U._updater_offer()["version"] == "1.5.0" and NET.asked(GITHUB) == 4, "too old to use again"
        finally:
            U.time = saved


def test_the_updater_comes_from_the_update_server_when_github_has_none():
    for why, set_up in (("GitHub unreachable", lambda: None),
                        ("no updater on GitHub", lambda: NET.github(release(NEWER)))):
        with app():
            set_up()
            NET.server_updater(version="1.5.0", body=UPDATER_NEW)
            offer = U._updater_offer()
            assert (offer["source"], offer["version"], offer["download_url"]) == ("server", "1.5.0", f"{SERVER}/download/updater"), why
            U._download_updater_worker()
            assert read(U.UPDATER_EXE) == UPDATER_NEW and U._read_installed_updater_version() == "1.5.0", why
    with app():                                                 # nobody has one: nothing is installed
        NET.github(release(NEWER))
        assert U._updater_offer() is None
        U._download_updater_worker()
        assert not U.is_updater_installed() and "SHA-256" in U._updater_install_state["err"]
        assert U._updater_install_state["failures"] == 1 and not U._updater_install_state["running"]
    with app():                                                 # a file that is not what GitHub hashed
        NET.github(release(NEWER, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.5.0.exe", UPDATER_NEW))))
        NET.routes[url_of(NEWER, "VisporaUpdater-1.5.0.exe")] = UPDATER_OLD
        U._download_updater_worker()
        assert not U.is_updater_installed() and "SHA-256" in U._updater_install_state["err"]
        assert not [f for f in os.listdir(U.UPDATER_DIR) if f.endswith(".part")]


# ═════════════════════════════════════════════════════════════════════
# The app: handing the install to the standalone updater
# ═════════════════════════════════════════════════════════════════════
def _found(source, version=NEWER):
    """An update of `version` found at `source`, downloaded and checked."""
    staged = os.path.join(U.UPDATE_DIR, f"Vispora_{version}.exe")
    with open(staged, "wb") as f:
        f.write(APP_NEW)
    U._update_state.update(update_available=True, download_complete=True, pending_install_path=staged,
                           latest_version=version, latest_source=source, installing=False,
                           latest_url=url_of(version) if source == "github" else f"{SERVER}/download/stable")


def _argument(args, name):
    return args[args.index(name) + 1] if name in args else None


def test_the_updater_is_told_where_to_look():
    for setting in ("auto", "github"):
        with app(frozen=True, update_source=setting) as spawned:
            updater_on_disk(version="1.5.0")
            _found("github")
            r = U.install_update_now(force=True)
            assert r["ok"] and r["method"] == "standalone-updater" and len(spawned) == 1
            args = spawned[0]
            assert args[0] == U.UPDATER_EXE and _argument(args, "--source") == setting
            assert _argument(args, "--target-version") == NEWER and _argument(args, "--server") == SERVER
            parsed = UM.parse_args(args[1:])                    # every argument is one the updater knows
            assert (parsed["source"], parsed["target_version"], parsed["auto_confirm"]) == (setting, NEWER, True)
            assert U.get_settings()["pending_install_version"] == NEWER
            assert json.load(open(U.INSTALL_INFO_PATH, encoding="utf-8"))["update_source"] == setting


def test_an_updater_from_before_github_is_not_given_an_argument_it_does_not_know():
    """1.4.5 and older stop at an unknown argument.  With an update that was
    found on the update server they work as they always did."""
    for version in ("1.4.5", ""):                               # known to be old, or no record of its version
        with app(frozen=True) as spawned:
            updater_on_disk(UPDATER_OLD, version)
            _found("server")
            assert U.install_update_now(force=True)["ok"] and len(spawned) == 1
            assert "--source" not in spawned[0] and _argument(spawned[0], "--server") == SERVER
            assert NET.calls == [], "nothing is downloaded first: this updater can do it"
            assert read(U.UPDATER_EXE) == UPDATER_OLD


def test_an_old_updater_is_replaced_before_an_update_from_github_is_installed():
    with app(frozen=True) as spawned:
        updater_on_disk(UPDATER_OLD, "1.4.5")
        NET.github(release(NEWER, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.5.0.exe", UPDATER_NEW))))
        _found("github")
        r = U.install_update_now(force=True)
        assert r["ok"] and len(spawned) == 1
        assert read(U.UPDATER_EXE) == UPDATER_NEW and U._read_installed_updater_version() == "1.5.0"
        assert _argument(spawned[0], "--source") == "auto"


def test_an_install_the_old_updater_could_not_finish_is_not_started():
    """Found on GitHub, the updater on disk only knows the update server, and
    no newer updater can be had: the old one would close the app and then
    stop.  So the app stays open and says why."""
    for why, set_up in (("no updater on GitHub", lambda: NET.github(release(NEWER))),
                        ("only the old one anywhere", lambda: NET.server_updater(version="1.4.5", body=UPDATER_OLD)),
                        ("nobody answers", lambda: None)):
        with app(frozen=True) as spawned:
            updater_on_disk(UPDATER_OLD, "1.4.5")
            set_up()
            _found("github")
            r = U.install_update_now(force=True)
            assert r["ok"] is False and "GitHub" in r["err"] and SRC.GITHUB_RELEASES_PAGE in r["err"], why
            assert spawned == [] and U._update_state["installing"] is False, why
            assert U.get_settings()["pending_install_version"] == "", why
            assert U._update_state["download_complete"] is True, "the download is kept for the next try"


def test_the_install_record_says_where_updates_come_from():
    with app(update_source="server"):
        assert U.write_install_info()["update_source"] == "server"
    with app():
        assert U.write_install_info()["update_source"] == "auto"


# ═════════════════════════════════════════════════════════════════════
# The standalone updater
# ═════════════════════════════════════════════════════════════════════
def test_the_updater_installs_from_github():
    target = standalone(running=(111,))
    NET.github(release(NEWER), release("8.0.0", files=(("Vispora.exe", APP_OTHER),)))
    NET.server(version=NEWEST, body=APP_OTHER)
    assert I.run_install_pipeline() == 0
    assert read(target) == APP_NEW and TU.PROC.launched == [[target]]
    assert NET.calls == [GITHUB, url_of(NEWER)], "found on GitHub: the update server is not asked"
    assert NET.headers[GITHUB]["accept"] == "application/vnd.github+json"
    assert step_detail("channel") == f"Latest on 'stable': v{NEWER} (from GitHub)"
    assert set(TU.steps().values()) == {"done"}
    assert json.load(open(P.INSTALL_INFO, encoding="utf-8"))["version"] == NEWER
    assert SV.get_state_snapshot()["channel_check"]["source"] == "github"


def test_the_updater_falls_back_to_the_update_server():
    for why, set_up in (("GitHub unreachable", lambda: None),
                        ("GitHub has nothing for the channel", lambda: NET.github(release("9.5.0-beta.1", prerelease=True))),
                        ("GitHub's limit", lambda: NET.github_says({"message": "API rate limit exceeded"}))):
        target = standalone()
        set_up()
        NET.server(version=NEWER)
        assert I.run_install_pipeline() == 0, why
        assert read(target) == APP_NEW, why
        assert NET.calls == [GITHUB, f"{SERVER}/version/stable", f"{SERVER}/download/stable"], why
        assert step_detail("channel") == f"Latest on 'stable': v{NEWER} (from the update server)", why


def test_the_updater_refuses_a_github_file_that_fails_its_hash():
    target = standalone()
    NET.github(release(NEWER))
    NET.routes[url_of(NEWER)] = APP_OTHER
    assert I.run_install_pipeline() == 6
    assert read(target) == TU.OLD and "SHA-256 mismatch" in SV.get_state_snapshot()["error"]
    assert not [f for f in os.listdir(P.UPDATER_DIR) if f.endswith(".part")]


def test_a_place_that_is_behind_is_not_installed_from():
    """The app found 9.0.0 on GitHub and asked for it.  If GitHub does not
    answer the updater a moment later and the update server still has an
    older build, that build must not be installed over a newer app."""
    target = standalone()
    SV._state["requested_version"] = NEWER
    NET.server(version="8.0.0", body=APP_OTHER)
    assert I.run_install_pipeline() == 4
    assert read(target) == TU.OLD, "nothing was installed"
    err = SV.get_state_snapshot()["error"]
    assert "could not reach GitHub" in err and f"the update server has v8.0.0, older than the v{NEWER} that was asked for" in err
    assert f"{SERVER}/download/stable" not in NET.calls

    target = standalone()                                       # GitHub is behind, the update server has it
    SV._state["requested_version"] = NEWER
    NET.github(release("8.0.0", files=(("Vispora.exe", APP_OTHER),)))
    NET.server(version=NEWER)
    assert I.run_install_pipeline() == 0 and read(target) == APP_NEW
    assert step_detail("channel").endswith("(from the update server)")

    target = standalone()                                       # something newer than was asked for is fine
    SV._state["requested_version"] = NEWER
    NET.github(release(NEWEST))
    assert I.run_install_pipeline() == 0 and read(target) == APP_NEW

    target = standalone()                                       # started by hand: whatever the channel has
    NET.server(version="8.0.0", body=APP_OTHER)
    assert I.run_install_pipeline() == 0 and read(target) == APP_OTHER


def test_the_updater_asks_only_the_place_it_was_told():
    target = standalone()
    SV._state["source"] = "github"
    NET.server(version=NEWER)
    assert I.run_install_pipeline() == 4 and NET.calls == [GITHUB], "GitHub only"
    assert SV.get_state_snapshot()["error"] == "could not reach GitHub" and read(target) == TU.OLD

    target = standalone()
    SV._state["source"] = "server"
    NET.github(release(NEWEST, files=(("Vispora.exe", APP_OTHER),)))
    NET.server(version=NEWER)
    assert I.run_install_pipeline() == 0 and read(target) == APP_NEW
    assert NET.calls == [f"{SERVER}/version/stable", f"{SERVER}/download/stable"]

    target = standalone()                                       # a server of one's own: that one only
    SV._state["server_url"] = OWN_SERVER
    NET.github(release(NEWEST, files=(("Vispora.exe", APP_OTHER),)))
    NET.server(version=NEWER, base=OWN_SERVER)
    assert I.run_install_pipeline() == 0 and read(target) == APP_NEW
    assert NET.calls[0] == f"{OWN_SERVER}/version/stable" and GITHUB not in NET.calls

    target = standalone()                                       # ... also when it is down
    SV._state["server_url"] = OWN_SERVER
    NET.github(release(NEWEST, files=(("Vispora.exe", APP_OTHER),)))
    assert I.run_install_pipeline() == 4 and read(target) == TU.OLD and GITHUB not in NET.calls


def test_the_updaters_command_line_and_the_apps_record_say_where_to_look():
    assert UM.parse_args(["--source", "github"])["source"] == "github"
    try:
        UM.parse_args(["--source", "railway"])
        raise AssertionError("an unknown place was accepted")
    except SystemExit:
        pass

    def run(*argv, record=None):
        target = standalone()
        NET.github(release(NEWER))
        NET.server(version=NEWEST, body=APP_OTHER)
        info = {"install_path": target, "install_dir": os.path.dirname(target), "channel": "stable", "version": "3.6.0"}
        info.update(record or {})
        with open(P.INSTALL_INFO, "w", encoding="utf-8") as f:
            json.dump(info, f)
        assert TU._run_main("--silent", *argv) == 0
        return SV.get_state_snapshot(), read(target)

    st, installed = run()
    assert st["source"] == "auto" and installed == APP_NEW, "nothing said: GitHub first"
    st, installed = run(record={"update_source": "server"})
    assert st["source"] == "server" and installed == APP_OTHER, "started by hand: what the app wrote down"
    st, installed = run("--source", "github", record={"update_source": "server"})
    assert st["source"] == "github" and installed == APP_NEW, "the command line wins"
    st, installed = run(record={"update_source": "nonsense"})
    assert st["source"] == "auto" and installed == APP_NEW
    st, installed = run("--target-version", NEWER)
    assert (st["requested_version"], st["target_version"]) == (NEWER, NEWER)


def test_picking_another_channel_in_the_window_drops_the_version_that_was_asked_for():
    standalone()
    SV._state["requested_version"] = "9.5.0-beta.1"
    SV._state["channel"] = "beta"
    client = SV.app.test_client()
    assert client.post("/api/set-channel", json={"channel": "beta"}).get_json()["ok"]
    assert SV._state["requested_version"] == "9.5.0-beta.1", "the same channel: still what the app asked for"
    assert client.post("/api/set-channel", json={"channel": "stable"}).get_json()["ok"]
    assert SV._state["requested_version"] == "" and SV._state["channel"] == "stable"
    labels = [s["label"] for s in SV.PIPELINE_STEPS]
    assert "Looking for the newest build" in labels and not [x for x in labels if "server" in x.lower()]


# ═════════════════════════════════════════════════════════════════════
# The settings page
# ═════════════════════════════════════════════════════════════════════
def test_the_settings_page_offers_the_three_choices():
    with open(os.path.join(m.REPO, "static", "js", "app.js"), encoding="utf-8") as f:
        js = f.read()
    for needed in ("function setUpdaterSource(", "update_source: source", 'id="updater-source-select"',
                   '<option value="auto" ', '<option value="github" ', '<option value="server" '):
        assert needed in js, needed
    with open(os.path.join(m.REPO, "app.py"), encoding="utf-8") as f:
        assert 'update_source=data.get("update_source")' in f.read(), "the settings route passes it on"


# ═════════════════════════════════════════════════════════════════════
# What the review of this change found
# ═════════════════════════════════════════════════════════════════════
OLD_UPDATER_ARGUMENTS = {"--target-dir", "--target-exe", "--channel", "--current-version", "--target-version",
                         "--auto-confirm", "--server", "--silent"}       # all that 1.4.5 knows


def _flags(args):
    return {a for a in args[1:] if a.startswith("--")}


def test_what_is_staged_is_installed_as_what_it_is_after_a_check_that_failed():
    """A check that reaches nobody clears what the last check found, but the
    file that was downloaded is still that build from that place."""
    with app(frozen=True) as spawned:
        updater_on_disk(version="1.5.0")
        NET.github(release(NEWER))
        assert U.check_for_update()["update_available"] and U.download_update()["ok"]
        NET.clear()
        assert U.check_for_update()["ok"] is False                  # offline now
        assert U._update_state["latest_version"] is None and U._update_state["latest_source"] is None
        assert U.install_update_now(force=True)["ok"] and len(spawned) == 1
        assert _argument(spawned[0], "--target-version") == NEWER and _argument(spawned[0], "--source") == "auto"
        assert U.get_settings()["pending_install_version"] == NEWER
    with app(frozen=True) as spawned:                               # the same with an updater that is too old
        updater_on_disk(UPDATER_OLD, "1.4.5")
        NET.github(release(NEWER))
        assert U.check_for_update()["update_available"] and U.download_update()["ok"]
        NET.clear()
        assert U.check_for_update()["ok"] is False
        r = U.install_update_now(force=True)
        assert r["ok"] is False and "GitHub" in r["err"] and spawned == [], \
            "still known to be from GitHub: the old updater is not started on it"


def test_an_old_updater_is_given_only_the_arguments_it_knows():
    with app(frozen=True) as spawned:
        updater_on_disk(UPDATER_OLD, "1.4.5")
        _found("server")
        assert U.install_update_now(force=True)["ok"]
        assert _flags(spawned[0]) <= OLD_UPDATER_ARGUMENTS, _flags(spawned[0]) - OLD_UPDATER_ARGUMENTS
    with app(frozen=True) as spawned:
        updater_on_disk(version="1.5.0")
        _found("github")
        assert U.install_update_now(force=True)["ok"]
        assert _flags(spawned[0]) - OLD_UPDATER_ARGUMENTS == {"--source"}


def test_the_old_updater_still_installs_what_the_update_server_has_too():
    """Found on GitHub, no newer updater to be had, but the update server
    carries the same build: the updater on disk can install it from there."""
    with app(frozen=True) as spawned:
        updater_on_disk(UPDATER_OLD, "1.4.5")
        NET.github(release(NEWER))
        NET.server(version=NEWER)
        _found("github")
        assert U.install_update_now(force=True)["ok"] and len(spawned) == 1
        assert "--source" not in spawned[0] and _argument(spawned[0], "--target-version") == NEWER
        assert read(U.UPDATER_EXE) == UPDATER_OLD
    with app(frozen=True) as spawned:                               # the server is behind: it would not get there
        updater_on_disk(UPDATER_OLD, "1.4.5")
        NET.github(release(NEWER))
        NET.server(version="8.0.0")
        _found("github")
        assert U.install_update_now(force=True)["ok"] is False and spawned == []
    with app(frozen=True, update_source="github") as spawned:       # GitHub only: the server is not asked
        updater_on_disk(UPDATER_OLD, "1.4.5")
        NET.github(release(NEWER))
        NET.server(version=NEWER)
        _found("github")
        assert U.install_update_now(force=True)["ok"] is False and spawned == []
        assert f"{SERVER}/version/stable" not in NET.calls


def test_an_updater_that_would_not_help_is_not_downloaded_again_and_again():
    """1.4.5 on disk, an update from GitHub waiting, and the only updater on
    offer is 1.4.5 again: it is not fetched, and the automatic install backs
    off instead of asking every two minutes."""
    with app(frozen=True, auto_install=True) as spawned:
        updater_on_disk(UPDATER_OLD, "1.4.5")
        NET.github(release(NEWER))
        NET.server_updater(version="1.4.5", body=UPDATER_OLD)
        _found("github")
        U._update_state["staged"] = {"version": NEWER, "source": "github"}
        assert U._refresh_updater_now() is False
        assert NET.asked(f"{SERVER}/download/updater") == 0, "the old updater is not downloaded again"
        assert U._updater_install_state["failures"] == 1 and not U._updater_install_state["running"]
        del NET.calls[:]
        r = U._try_auto_install(quiet=True)
        assert r.get("deferred") and "next try" in r["reason"] and NET.calls == [], "inside the wait: nobody is asked"
        assert spawned == []
        clock = U.time
        U.time = types.SimpleNamespace(time=lambda: clock.time() + U.SAFETY_GATE_RECHECK_SEC + 1, sleep=clock.sleep)
        try:
            assert U._try_auto_install(quiet=True)["ok"] is False   # the wait is over: one more look, still nothing
            assert U._updater_install_state["failures"] == 2 and NET.asked(f"{SERVER}/download/updater") == 0
        finally:
            U.time = clock


def test_a_second_install_click_does_not_start_a_second_updater():
    import threading
    with app(frozen=True) as spawned:
        updater_on_disk(UPDATER_OLD, "1.4.5")
        NET.github(release(NEWER, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.5.0.exe", UPDATER_NEW))))
        _found("github")
        real, entered, go, first = U._refresh_updater_now, threading.Event(), threading.Event(), []

        def slow_refresh(*a, **kw):
            entered.set()
            assert go.wait(10)
            return real(*a, **kw)

        U._refresh_updater_now = slow_refresh
        try:
            worker = threading.Thread(target=lambda: first.append(U.install_update_now(force=True)))
            worker.start()
            assert entered.wait(10)
            second = U.install_update_now(force=True)               # clicked again while the updater is fetched
            assert second["ok"] is False and "already" in second["err"] and spawned == []
            go.set()
            worker.join(10)
        finally:
            U._refresh_updater_now = real
            go.set()
        assert first and first[0]["ok"] and len(spawned) == 1
        third = U.install_update_now(force=True)                    # and once it is under way
        assert third["ok"] is False and "already being installed" in third["err"] and len(spawned) == 1


def test_a_file_github_cannot_deliver_is_taken_from_the_update_server():
    with app():
        NET.github(release(NEWER))
        del NET.routes[url_of(NEWER)]                               # GitHub lists it, its file host does not answer
        NET.server(version=NEWER)
        assert U.check_for_update()["source"] == "github"
        r = U.download_update()
        staged = os.path.join(U.UPDATE_DIR, f"Vispora_{NEWER}.exe")
        assert r["ok"] and read(staged) == APP_NEW
        assert U._update_state["staged"] == {"version": NEWER, "source": "server"}
        assert U._update_state["latest_source"] == "server" and U._update_state["latest_url"] == f"{SERVER}/download/stable"
        assert NET.calls == [GITHUB, url_of(NEWER), GITHUB, f"{SERVER}/version/stable", f"{SERVER}/download/stable"] or \
            NET.calls == [GITHUB, url_of(NEWER), f"{SERVER}/version/stable", f"{SERVER}/download/stable"]
    with app():                                                     # a file that fails GitHub's hash: the same
        NET.github(release(NEWER))
        NET.routes[url_of(NEWER)] = APP_OTHER
        NET.server(version=NEWER)
        assert U.check_for_update()["update_available"] and U.download_update()["ok"]
        assert read(os.path.join(U.UPDATE_DIR, f"Vispora_{NEWER}.exe")) == APP_NEW
    with app():                                                     # the server has another version: not that one
        NET.github(release(NEWER))
        del NET.routes[url_of(NEWER)]
        NET.server(version=NEWEST, body=APP_OTHER)
        assert U.check_for_update()["update_available"]
        r = U.download_update()
        assert r == {"ok": False, "err": "Download failed"} and not os.listdir(U.UPDATE_DIR)
        assert U._update_state["staged"] is None and U._update_state["latest_source"] == "github"
    with app():                                                     # and its own file must pass its own hash
        NET.github(release(NEWER))
        del NET.routes[url_of(NEWER)]
        NET.server(version=NEWER, sha="0" * 64)
        assert U.check_for_update()["update_available"]
        r = U.download_update()
        assert r["ok"] is False and "SHA-256" in r["err"] and not os.listdir(U.UPDATE_DIR)


def test_an_updater_file_github_cannot_deliver_is_taken_from_the_update_server():
    with app():
        NET.github(release(NEWER, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.5.0.exe", UPDATER_NEW))))
        del NET.routes[url_of(NEWER, "VisporaUpdater-1.5.0.exe")]
        NET.server_updater(version="1.5.0", body=UPDATER_NEW)
        U._download_updater_worker()
        assert read(U.UPDATER_EXE) == UPDATER_NEW and U._read_installed_updater_version() == "1.5.0"
        assert U._updater_install_state["failures"] == 0 and not U._updater_install_state["running"]
    with app():                                                     # a fallback never puts an older one over a newer
        updater_on_disk(UPDATER_NEW, "1.5.0")
        NET.github(release(NEWER, files=(("Vispora.exe", APP_NEW), ("VisporaUpdater-1.6.0.exe", UPDATER_NEWER))))
        del NET.routes[url_of(NEWER, "VisporaUpdater-1.6.0.exe")]
        NET.server_updater(version="1.4.5", body=UPDATER_OLD)
        U._updater_refresh_worker()
        assert m.wait(lambda: U._updater_install_state["failures"] == 1 and not U._updater_install_state["running"])
        assert read(U.UPDATER_EXE) == UPDATER_NEW and U._read_installed_updater_version() == "1.5.0"
        assert NET.asked(f"{SERVER}/download/updater") == 0
    with app():                                                     # a server that says it has none is believed
        NET.routes[f"{SERVER}/version/updater"] = json.dumps(
            {"available": False, "version": "1.4.5", "sha256": hashlib.sha256(UPDATER_OLD).hexdigest()}).encode()
        assert U._updater_offer() is None
        U._download_updater_worker()
        assert not U.is_updater_installed()
        assert "GitHub or the update server gave no updater" in U._updater_install_state["err"]
    with app(update_source="server"):                               # the error names who was asked
        U._download_updater_worker()
        assert U._updater_install_state["err"].startswith("the update server gave no updater")


def test_a_setting_changed_during_a_check_is_not_written_over():
    with app():
        NET.github(release(NEWER))
        real = U._ask_github

        def slow(channel):
            answer = real(channel)
            assert U.set_settings(auto_install=True)["ok"]          # the user flips a switch while GitHub is asked
            return answer

        U._ask_github = slow
        try:
            assert U.check_for_update()["update_available"]
        finally:
            U._ask_github = real
        s = U.get_settings()
        assert s["auto_install"] is True and s["last_check_ts"] > 0, "the check's own save kept the switch"
    with app():
        NET.github(release(NEWER))
        NET.server(version=NEWEST)
        real = U._ask_github

        def slow(channel):
            answer = real(channel)
            assert U.set_settings(update_source="server")["ok"]     # "Update server only", picked mid-check
            return answer

        U._ask_github = slow
        try:
            r = U.check_for_update()
        finally:
            U._ask_github = real
        assert r["ok"] is False and "changed during the check" in r["err"]
        assert U.get_settings()["update_source"] == "server"
        assert U._update_state["latest_version"] is None and U._update_state["update_available"] is False
        assert U.check_for_update()["source"] == "server", "the next check asks the place that was picked"


def test_nothing_is_closed_before_a_build_is_found():
    target = standalone(running=(111,))
    SV._state["source"] = "github"                                  # and GitHub is at its hourly limit
    NET.github_says({"message": "API rate limit exceeded"})
    assert I.run_install_pipeline() == 4
    assert TU.steps()["channel"] == "error" and TU.steps()["close"] == "pending"
    assert TU.PROC.commands == [] and TU.PROC.running["ghostshell.exe"] == [111], "the running app was left alone"
    assert read(target) == TU.OLD


def test_the_updater_takes_the_file_from_the_next_place_when_one_fails():
    target = standalone()
    NET.github(release(NEWER))
    del NET.routes[url_of(NEWER)]                                   # listed on GitHub, but the file does not come
    NET.server(version=NEWER)
    assert I.run_install_pipeline() == 0 and read(target) == APP_NEW
    assert NET.calls == [GITHUB, url_of(NEWER), f"{SERVER}/version/stable", f"{SERVER}/download/stable"]
    assert step_detail("channel") == f"Latest on 'stable': v{NEWER} (from the update server; the file from GitHub could not be used)"
    assert set(TU.steps().values()) == {"done"}

    target = standalone()                                           # a file that fails GitHub's hash: the same
    NET.github(release(NEWER))
    NET.routes[url_of(NEWER)] = APP_OTHER
    NET.server(version=NEWER)
    assert I.run_install_pipeline() == 0 and read(target) == APP_NEW

    target = standalone()                                           # nobody can deliver it: the first error stands
    NET.github(release(NEWER))
    del NET.routes[url_of(NEWER)]
    assert I.run_install_pipeline() == 5 and read(target) == TU.OLD
    assert SV.get_state_snapshot()["error"].startswith("Download failed") and TU.steps()["download"] == "error"
    assert not [f for f in os.listdir(P.UPDATER_DIR) if f.endswith(".part")]

    target = standalone()                                           # the other place is behind what was asked for
    SV._state["requested_version"] = NEWER
    NET.github(release(NEWER))
    del NET.routes[url_of(NEWER)]
    NET.server(version="8.0.0", body=APP_OTHER)
    assert I.run_install_pipeline() == 5 and read(target) == TU.OLD


def test_an_odd_answer_from_the_update_server_is_an_error_not_a_crash():
    for size in ("44 MB", ["x"], {"a": 1}, 1e400, None, -5):
        target = standalone()
        NET.server(version=NEWER)
        meta = json.loads(NET.routes[f"{SERVER}/version/stable"])
        meta["size_bytes"] = size
        NET.routes[f"{SERVER}/version/stable"] = json.dumps(meta).encode()
        assert I.run_install_pipeline() == 0 and read(target) == APP_NEW, f"size_bytes {size!r} is only a hint"
    for sha in (12345, ["a"], {"x": 1}):
        target = standalone()
        NET.server(version=NEWER)
        meta = json.loads(NET.routes[f"{SERVER}/version/stable"])
        meta["sha256"] = sha
        NET.routes[f"{SERVER}/version/stable"] = json.dumps(meta).encode()
        assert I.run_install_pipeline() == 6 and read(target) == TU.OLD, sha
        assert SV.get_state_snapshot()["phase"] == "error"
    target = standalone()                                           # whatever else goes wrong is said, not left spinning
    NET.server(version=NEWER)
    real = I._download

    def broken(*a, **kw):
        raise ZeroDivisionError("unexpected")

    I._download = broken
    try:
        assert I.run_install_pipeline() == 9
    finally:
        I._download = real
    st = SV.get_state_snapshot()
    assert st["phase"] == "error" and "unexpected error" in st["error"] and "active" not in TU.steps().values()
    assert read(target) == TU.OLD


def test_started_by_hand_the_updater_does_not_go_below_what_is_installed():
    """GitHub is behind the update server, and the install already has the
    server's build: GitHub's older one is not installed over it."""
    target = standalone()
    SV._state["current_version"] = NEWEST
    NET.github(release(NEWER, files=(("Vispora.exe", APP_OTHER),)))
    NET.server(version=NEWEST)
    assert I.run_install_pipeline() == 0 and read(target) == APP_NEW, "the update server's, which is not older"
    assert step_detail("channel").endswith("(from the update server)")

    target = standalone()                                           # nobody reaches what is installed: nothing happens
    SV._state["current_version"] = NEWEST
    NET.github(release(NEWER, files=(("Vispora.exe", APP_OTHER),)))
    assert I.run_install_pipeline() == 4 and read(target) == TU.OLD
    assert f"GitHub has v{NEWER}, older than the v{NEWEST} that is installed" in SV.get_state_snapshot()["error"]

    target = standalone()                                           # another channel picked in the window: asked for
    SV._state["current_version"] = "9.5.0-beta.1"
    SV._state["channel"] = "beta"
    assert SV.app.test_client().post("/api/set-channel", json={"channel": "stable"}).get_json()["ok"]
    NET.github(release(NEWER, files=(("Vispora.exe", APP_OTHER),)))
    assert I.run_install_pipeline() == 0 and read(target) == APP_OTHER

    target = standalone(mode="install")                             # a fresh install has nothing to go below
    SV._state["current_version"] = NEWEST
    NET.github(release(NEWER))
    assert I.run_install_pipeline() == 0 and read(target) == APP_NEW


def test_the_updater_keeps_the_apps_note_of_where_updates_come_from():
    target = standalone()
    with open(P.INSTALL_INFO, "w", encoding="utf-8") as f:
        json.dump({"install_path": target, "install_dir": os.path.dirname(target), "channel": "stable",
                   "version": "3.6.0", "update_source": "server"}, f)
    NET.server(version=NEWER)
    assert TU._run_main("--silent") == 0 and read(target) == APP_NEW
    info = json.load(open(P.INSTALL_INFO, encoding="utf-8"))
    assert (info["version"], info["update_source"]) == (NEWER, "server"), "a second run by hand asks the same place"
    assert GITHUB not in NET.calls
