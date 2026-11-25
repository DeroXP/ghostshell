"""Where Vispora's releases are published, and how to read what each place
answers.

Two places:
  * GitHub: the releases of the app's repository.  A release carries
    Vispora.exe and the standalone updater as VisporaUpdater-<version>.exe.
    GitHub's API gives every uploaded file's size and SHA-256 ("digest"),
    which is what a download is checked against.
  * the update server (the release site): /version/<channel> and
    /download/<channel>, as it always was.

GitHub is asked first and the update server is the fallback, unless the user
picked one of them.  An app that was pointed at a server of somebody's own
asks only that server.

Shared by the app (core/updater.py) and the standalone updater
(updater/installer.py), which is built and shipped on its own.  So nothing in
here imports the app, reads a file or opens a connection: the caller fetches,
this module only decides.  Standard library only.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

SERVER_URL_DEFAULT = "https://vispora.up.railway.app"

GITHUB_REPO = "DeroXP/ghostshell"
# One request answers both questions (the app and the updater), for both
# channels.  Without a sign-in GitHub allows 60 of these an hour per address.
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"

APP_ASSET = "Vispora.exe"
# The updater has a version of its own, and a release has no field for it:
# the file's name carries it.
UPDATER_ASSET_RE = re.compile(r"VisporaUpdater-([0-9][0-9A-Za-z.+-]{0,39})[.]exe")
VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+-]{0,39}")
_DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")
_MIN_EXE_BYTES = 1024 * 1024            # anything smaller is not one of our builds

SOURCES = ("github", "server")
SOURCE_SETTINGS = ("auto",) + SOURCES
SOURCE_NAMES = {"github": "GitHub", "server": "the update server"}
# The first standalone updater that knows GitHub (and the --source argument).
UPDATER_MIN_FOR_SOURCES = "1.5.0"


def parse_version(v) -> tuple:
    """Parse a version string into a comparable 5-tuple.

    Supported forms:
        '2.9.8'              -> (2, 9, 8, 0, 0)
        '2.9.8.0'            -> (2, 9, 8, 0, 0)       equal to 2.9.8
        '2.9.8.1'            -> (2, 9, 8, 1, 0)       newer than 2.9.8.0
        '3.0.0'              -> (3, 0, 0, 0, 0)
        '3.0.0-beta.1'       -> (3, 0, 0, 0, -1999)   sorts BEFORE 3.0.0
        '3.0.0-beta.5'       -> (3, 0, 0, 0, -1995)   newer than -beta.1
        '3.0.0-rc.1'         -> (3, 0, 0, 0, -999)
        'v3.0.0-beta.1'      -> (3, 0, 0, 0, -1999)   (strips leading v)

    The 5th component (prerelease ordinal) is NEGATIVE for prereleases
    so the natural tuple comparison correctly sorts '3.0.0-beta.1' as
    older than '3.0.0' and as older than '3.0.0-beta.5'.
    """
    v = str(v or "").strip().lstrip("vV").split("+", 1)[0]     # "+build" says nothing about the order
    if not v:
        return (0, 0, 0, 0, 0)
    # Split into core + prerelease tag
    if "-" in v:
        core, pre = v.split("-", 1)
    else:
        core, pre = v, ""
    parts = []
    for p in core.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    parts = parts[:4]
    # Prerelease ordinal: alpha < beta < rc < final.  The final release
    # gets pre_ord=0; any prerelease is negative.
    pre_ord = 0
    if pre:
        # base + n: higher N within a category sorts NEWER.
        # Bands are 1000 wide so no realistic prerelease counter (rc.999
        # etc.) can overflow into a higher category's band.  Ordering:
        # alpha (-3000) < beta (-2000) < rc (-1000) < final (0).
        # alpha.5 = -2995 < beta.1 = -1999 < beta.99 = -1901 < rc.1 = -999 < final = 0.
        kind_map = {"alpha": -3000, "beta": -2000, "rc": -1000}
        pre_parts = pre.split(".")
        base = kind_map.get(pre_parts[0].lower(), -500)
        n = 0
        if len(pre_parts) > 1:
            try:
                n = int(pre_parts[1])
            except ValueError:
                n = 0
        pre_ord = base + n
    return tuple(parts + [pre_ord])


def source_setting(value) -> str:
    """A saved "where do updates come from" setting, made safe: "auto",
    "github" or "server" ("auto" for anything else)."""
    s = str(value or "").strip().lower()
    return s if s in SOURCE_SETTINGS else "auto"


def source_order(setting="auto", server_url="") -> tuple:
    """The places to ask, in order.

    "github" / "server": that one only.  "auto": GitHub, then the update
    server.  Somebody who pointed the app at a server of their own meant that
    server (it may carry a build of their own), so with a `server_url` other
    than ours "auto" asks that server and nothing else; GitHub's build is
    theirs to pick with "github"."""
    s = source_setting(setting)
    if s in SOURCES:
        return (s,)
    own = str(server_url or "").strip().rstrip("/").lower()
    if own and own != SERVER_URL_DEFAULT:
        return ("server",)
    return ("github", "server")


def _is_github_download(url) -> bool:
    """A release file's own address on github.com, over https.  (The download
    is checked against its SHA-256 afterwards whatever the address is; this
    only keeps an answer that is not GitHub's from sending us elsewhere.)"""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.netloc.lower() == "github.com"
            and "/releases/download/" in parts.path)


def _files(release: dict, match) -> list:
    """The finished uploads of `release` whose name `match` accepts, as
    [(what match answered, {"name", "sha256", "size_bytes", "download_url"})].
    A file that is still uploading, has no SHA-256, is too small to be a
    build or is not served from github.com is left out."""
    out = []
    assets = release.get("assets")
    for a in assets if isinstance(assets, list) else ():
        if not isinstance(a, dict) or not isinstance(a.get("name"), str):
            continue
        hit = match(a["name"])
        if not hit:
            continue
        digest = _DIGEST_RE.fullmatch(str(a.get("digest") or "").strip().lower())
        size = a.get("size")
        if (a.get("state") != "uploaded" or digest is None
                or not isinstance(size, int) or isinstance(size, bool) or size < _MIN_EXE_BYTES
                or not _is_github_download(a.get("browser_download_url"))):
            continue
        out.append((hit, {"name": a["name"], "sha256": digest.group(1), "size_bytes": size,
                          "download_url": a["browser_download_url"]}))
    return out


def _channel_releases(releases, channel) -> list:
    """The published releases a channel may get, newest version first:
    [(version as a tuple, version as text, release)].

    "stable" gets final builds only.  "beta" gets everything, final builds
    included, as on the update server.  A draft, a release whose tag is not a
    version, and (on stable) anything marked as a pre-release or carrying a
    pre-release suffix in its version is left out.  Sorted by version, not by
    date: a fix for an older line must not pass for the newest build."""
    out = []
    for r in releases if isinstance(releases, list) else ():
        if not isinstance(r, dict) or r.get("draft"):
            continue
        tag = r.get("tag_name")
        text = tag.strip().lstrip("vV") if isinstance(tag, str) else ""
        if not VERSION_RE.fullmatch(text):
            continue
        version = parse_version(text)
        if channel != "beta" and (r.get("prerelease") or version[4] != 0):
            continue
        out.append((version, text, r))
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def github_app_release(releases, channel) -> dict:
    """What GitHub offers `channel`, read from the answer of
    GITHUB_RELEASES_URL (`releases`, whatever came back).

    {"available": True, "version", "sha256", "size_bytes", "download_url",
     "notes", "released_at", "source": "github"} for the newest release that
    carries a finished Vispora.exe, or {"available": False, "source":
    "github"}.  Never raises: an answer that is not a list of releases offers
    nothing."""
    for _version, text, r in _channel_releases(releases, channel):
        files = _files(r, lambda name: name == APP_ASSET)
        if not files:
            continue                    # still uploading, or a release without the app
        f = files[0][1]
        return {"available": True, "version": text, "sha256": f["sha256"],
                "size_bytes": f["size_bytes"], "download_url": f["download_url"],
                "notes": str(r.get("body") or ""), "released_at": str(r.get("published_at") or ""),
                "source": "github"}
    return {"available": False, "source": "github"}


def github_updater(releases, channel) -> dict:
    """The newest standalone updater among the releases `channel` may get:
    {"available": True, "version", "sha256", "size_bytes", "download_url",
     "source": "github"}, or {"available": False, "source": "github"}.
    Never raises."""
    best = None
    for _version, _text, r in _channel_releases(releases, channel):
        for hit, f in _files(r, UPDATER_ASSET_RE.fullmatch):
            version = parse_version(hit.group(1))
            if best is None or version > best[0]:
                best = (version, hit.group(1), f)
    if best is None:
        return {"available": False, "source": "github"}
    return {"available": True, "version": best[1], "sha256": best[2]["sha256"],
            "size_bytes": best[2]["size_bytes"], "download_url": best[2]["download_url"],
            "source": "github"}
