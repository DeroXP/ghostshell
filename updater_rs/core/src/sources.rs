//! Where Vispora's releases are published, and how to read what each place
//! answers.  This is update_sources.py, decision for decision: the app (still
//! Python) and this updater must agree on what counts as a release.
//!
//! Two places:
//!   * GitHub: the releases of the app's repository.  A release carries
//!     Vispora.exe and the standalone updater as VisporaUpdater-<version>.exe.
//!     GitHub's API gives every uploaded file's size and SHA-256 ("digest"),
//!     which is what a download is checked against.
//!   * the update server: /version/<channel> and /download/<channel>.
//!
//! GitHub is asked first and the update server is the fallback, unless one of
//! them was picked.  An app that was pointed at a server of somebody's own
//! asks only that server.
//!
//! Nothing in here opens a connection or reads a file: the caller fetches,
//! this module only decides.

use crate::json::Json;

pub const SERVER_URL_DEFAULT: &str = "https://vispora.up.railway.app";
pub const GITHUB_REPO: &str = "DeroXP/ghostshell";
/// One request answers for both channels.  Without a sign-in GitHub allows 60
/// of these an hour per address.
pub const GITHUB_RELEASES_URL: &str = "https://api.github.com/repos/DeroXP/ghostshell/releases?per_page=30";
pub const GITHUB_HEADERS: [(&str, &str); 2] =
    [("Accept", "application/vnd.github+json"), ("X-GitHub-Api-Version", "2022-11-28")];
pub const GITHUB_RELEASES_PAGE: &str = "https://github.com/DeroXP/ghostshell/releases";

pub const APP_ASSET: &str = "Vispora.exe";
const UPDATER_ASSET_PREFIX: &str = "VisporaUpdater-";
const MIN_EXE_BYTES: i128 = 1024 * 1024; // anything smaller is not one of our builds

pub const SOURCES: [&str; 2] = ["github", "server"];
pub const SOURCE_SETTINGS: [&str; 3] = ["auto", "github", "server"];
/// The first standalone updater that knows GitHub (and the --source argument).
pub const UPDATER_MIN_FOR_SOURCES: &str = "1.5.0";

/// "GitHub" / "the update server".
pub fn source_name(source: &str) -> &'static str {
    match source {
        "github" => "GitHub",
        _ => "the update server",
    }
}

/// A version as five numbers that compare the way the releases are numbered.
pub type Version = [i64; 5];

/// What Python's `int(text)` accepts: optional sign, digits, single
/// underscores between digits, blanks around it.  None for anything else.
pub(crate) fn py_int(text: &str) -> Option<i64> {
    let t = text.trim();
    let (negative, digits) = match t.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    let mut n: i64 = 0;
    for c in digits.chars() {
        if c == '_' {
            continue;
        }
        let d = c.to_digit(10)? as i64;
        n = n.saturating_mul(10).saturating_add(d); // (Python has no largest number; this one stops at the top)
    }
    Some(if negative { -n } else { n })
}

/// Parse a version into a comparable 5-tuple.
///
///     '2.9.8'          -> [2, 9, 8, 0, 0]
///     '2.9.8.1'        -> [2, 9, 8, 1, 0]       newer than 2.9.8
///     '3.0.0-beta.1'   -> [3, 0, 0, 0, -1999]   sorts BEFORE 3.0.0
///     '3.0.0-rc.1'     -> [3, 0, 0, 0, -999]
///     'v3.0.0-beta.1'  -> the same without the v
///
/// The fifth number is negative for a pre-release (alpha -3000, beta -2000,
/// rc -1000, anything else -500, plus its counter), so that plain comparison
/// puts 3.0.0-beta.1 before 3.0.0-beta.5 before 3.0.0.  "+build" says nothing
/// about the order.  Never fails: what is not a number counts as 0.
pub fn parse_version(v: &str) -> Version {
    let v = v.trim().trim_start_matches(['v', 'V']);
    let v = v.split('+').next().unwrap_or("");
    if v.is_empty() {
        return [0; 5];
    }
    let (core, pre) = v.split_once('-').unwrap_or((v, ""));
    let mut out = [0i64; 5];
    for (slot, part) in out.iter_mut().take(4).zip(core.split('.')) {
        *slot = py_int(part).unwrap_or(0);
    }
    if !pre.is_empty() {
        let mut parts = pre.split('.');
        let base = match parts.next().unwrap_or("").to_lowercase().as_str() {
            "alpha" => -3000,
            "beta" => -2000,
            "rc" => -1000,
            _ => -500,
        };
        let n = parts.next().and_then(py_int).unwrap_or(0);
        out[4] = base + n;
    }
    out
}

/// `[0-9][0-9A-Za-z.+-]{0,39}`, the whole text.
fn is_version_text(text: &str) -> bool {
    let b = text.as_bytes();
    (1..=40).contains(&b.len())
        && b[0].is_ascii_digit()
        && b.iter().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'+' | b'-'))
}

/// The version in "VisporaUpdater-<version>.exe", or None for any other name.
pub fn updater_asset_version(name: &str) -> Option<&str> {
    let version = name.strip_prefix(UPDATER_ASSET_PREFIX)?.strip_suffix(".exe")?;
    is_version_text(version).then_some(version)
}

/// A saved "where do updates come from" setting, made safe: "auto", "github"
/// or "server" ("auto" for anything else).
pub fn source_setting(value: &str) -> &'static str {
    let s = value.trim().to_lowercase();
    SOURCE_SETTINGS.iter().copied().find(|known| *known == s).unwrap_or("auto")
}

/// The places to ask, in order.
///
/// "github" / "server": that one only.  "auto": GitHub, then the update
/// server.  Somebody who pointed the app at a server of their own meant that
/// server (it may carry a build of their own), so with a `server_url` other
/// than ours "auto" asks that server and nothing else.
pub fn source_order(setting: &str, server_url: &str) -> Vec<&'static str> {
    let s = source_setting(setting);
    if s != "auto" {
        return vec![s];
    }
    let own = server_url.trim().trim_end_matches('/').to_lowercase();
    if !own.is_empty() && own != SERVER_URL_DEFAULT {
        return vec!["server"];
    }
    vec!["github", "server"]
}

/// A release file's own address on github.com, over https.  (The download is
/// checked against its SHA-256 afterwards whatever the address is; this only
/// keeps an answer that is not GitHub's from sending us elsewhere.)
pub fn is_github_download(url: &str) -> bool {
    if url.chars().any(char::is_whitespace) {
        return false;
    }
    let Some((scheme, rest)) = url.split_once("://") else {
        return false;
    };
    if !scheme.eq_ignore_ascii_case("https") {
        return false;
    }
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (host, after) = rest.split_at(end);
    let path = &after[..after.find(['?', '#']).unwrap_or(after.len())];
    host.eq_ignore_ascii_case("github.com") && path.contains("/releases/download/")
}

/// One finished upload of a release.
#[derive(Debug, Clone, PartialEq)]
pub struct File {
    pub name: String,
    pub sha256: String,
    pub size_bytes: i128,
    pub download_url: String,
}

/// The finished uploads of `release` whose name `matches` accepts, with what
/// it answered.  A file that is still uploading, has no SHA-256, is too small
/// to be a build or is not served from github.com is left out.
fn files<'a, T>(release: &'a Json, matches: impl Fn(&'a str) -> Option<T>) -> Vec<(T, File)> {
    let mut out = Vec::new();
    for a in release["assets"].as_array().unwrap_or(&[]) {
        let Some(name) = a["name"].as_str() else {
            continue;
        };
        if !a.is_object() {
            continue;
        }
        let Some(hit) = matches(name) else {
            continue;
        };
        let digest = a["digest"].py_str_or_empty().trim().to_lowercase();
        let sha256 = digest.strip_prefix("sha256:").filter(|hex| {
            hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        });
        let (Some(sha256), Some(size), Some(url)) =
            (sha256, a["size"].as_int(), a["browser_download_url"].as_str())
        else {
            continue;
        };
        if a["state"].as_str() != Some("uploaded") || size < MIN_EXE_BYTES || !is_github_download(url) {
            continue;
        }
        out.push((
            hit,
            File { name: name.to_string(), sha256: sha256.to_string(), size_bytes: size, download_url: url.to_string() },
        ));
    }
    out
}

/// The published releases a channel may get, newest version first.
///
/// "stable" gets final builds only.  "beta" gets everything, final builds
/// included, as on the update server.  A draft, a release whose tag is not a
/// version, and (on stable) anything marked as a pre-release or carrying a
/// pre-release suffix in its version is left out.  Sorted by version, not by
/// date: a fix for an older line must not pass for the newest build.
fn channel_releases<'a>(releases: &'a Json, channel: &str) -> Vec<(Version, &'a str, &'a Json)> {
    let mut out = Vec::new();
    for r in releases.as_array().unwrap_or(&[]) {
        if !r.is_object() || r["draft"].truthy() {
            continue;
        }
        let Some(tag) = r["tag_name"].as_str() else {
            continue;
        };
        let text = tag.trim().trim_start_matches(['v', 'V']);
        if !is_version_text(text) {
            continue;
        }
        let version = parse_version(text);
        if channel != "beta" && (r["prerelease"].truthy() || version[4] != 0) {
            continue;
        }
        out.push((version, text, r));
    }
    out.sort_by(|a, b| b.0.cmp(&a.0)); // (stable: equal versions keep the order they were listed in)
    out
}

/// What a place offers: a build, or nothing.
#[derive(Debug, Clone, PartialEq)]
pub struct Offer {
    pub version: String,
    pub sha256: String,
    pub size_bytes: i128,
    pub download_url: String,
    pub notes: String,
    pub released_at: String,
}

/// What GitHub offers `channel`, read from the answer of GITHUB_RELEASES_URL
/// (`releases`, whatever came back): the newest release that carries a
/// finished Vispora.exe, or None.  Never fails: an answer that is not a list
/// of releases offers nothing.
pub fn github_app_release(releases: &Json, channel: &str) -> Option<Offer> {
    for (_, text, r) in channel_releases(releases, channel) {
        let found = files(r, |name| (name == APP_ASSET).then_some(()));
        let Some((_, f)) = found.into_iter().next() else {
            continue; // still uploading, or a release without the app
        };
        return Some(Offer {
            version: text.to_string(),
            sha256: f.sha256,
            size_bytes: f.size_bytes,
            download_url: f.download_url,
            notes: r["body"].py_str_or_empty(),
            released_at: r["published_at"].py_str_or_empty(),
        });
    }
    None
}

/// The newest standalone updater among the releases `channel` may get, by
/// the version in its file name, or None.  Never fails.
pub fn github_updater(releases: &Json, channel: &str) -> Option<Offer> {
    let mut best: Option<(Version, &str, File)> = None;
    for (_, _, r) in channel_releases(releases, channel) {
        for (text, f) in files(r, updater_asset_version) {
            let version = parse_version(text);
            if best.as_ref().map_or(true, |b| version > b.0) {
                best = Some((version, text, f));
            }
        }
    }
    best.map(|(_, text, f)| Offer {
        version: text.to_string(),
        sha256: f.sha256,
        size_bytes: f.size_bytes,
        download_url: f.download_url,
        notes: String::new(),
        released_at: String::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_sort_the_way_the_releases_are_numbered() {
        let order = ["2.9.9.8", "3.0.0-alpha.5", "3.0.0-beta.1", "3.0.0-beta.99", "3.0.0-rc.1", "3.0.0", "3.0.0.1",
                     "3.7.0", "3.8.0-beta.1", "3.8.0", "3.10.0"];
        let mut shuffled: Vec<&str> = order.iter().rev().copied().collect();
        shuffled.sort_by_key(|v| parse_version(v));
        assert_eq!(shuffled, order);
        assert_eq!(parse_version("v3.8.0"), [3, 8, 0, 0, 0]);
        assert_eq!(parse_version("3.8.0"), parse_version("3.8.0.0"));
        assert_eq!(parse_version(""), [0; 5]);
        assert_eq!(parse_version("three.x"), [0; 5], "never fails");
        assert_eq!(parse_version("9.0.1+b5"), [9, 0, 1, 0, 0]);
        assert_eq!(parse_version("9.0.1-rc.2+b5"), parse_version("9.0.1-rc.2"));
        assert_eq!(parse_version("3.0.0-beta.1"), [3, 0, 0, 0, -1999]);
        assert_eq!(parse_version("3.0.0-RC.1"), [3, 0, 0, 0, -999]);
        assert_eq!(parse_version("3.0.0-nightly"), [3, 0, 0, 0, -500]);
        assert_eq!(parse_version("3.0.0-beta.x"), [3, 0, 0, 0, -2000]);
        assert_eq!(parse_version(" V1.2.3.4.5 "), [1, 2, 3, 4, 0], "a fifth part is dropped");
        assert_eq!(parse_version("1. 2 .1_0"), [1, 2, 10, 0, 0], "what Python's int() takes");
        assert_eq!(parse_version("1.2.x_1"), [1, 2, 0, 0, 0]);
    }

    #[test]
    fn the_order_in_which_the_places_are_asked() {
        let own = "https://updates.example.test";
        assert_eq!(source_order("auto", ""), ["github", "server"]);
        assert_eq!(source_order("", SERVER_URL_DEFAULT), ["github", "server"]);
        assert_eq!(source_order("auto", &format!("{SERVER_URL_DEFAULT}/")), ["github", "server"]);
        assert_eq!(source_order("github", own), ["github"]);
        assert_eq!(source_order("server", ""), ["server"]);
        assert_eq!(source_order("auto", own), ["server"], "a server of your own is the one you meant, and the only one");
        assert_eq!(source_order("auto", &format!("{}/", own.to_uppercase())), ["server"]);
        assert_eq!(source_setting("GitHub "), "github");
        assert_eq!(source_setting("railway"), "auto");
        assert_eq!(source_order("railway", ""), ["github", "server"]);
        assert_eq!((source_name("github"), source_name("server")), ("GitHub", "the update server"));
    }

    #[test]
    fn only_a_release_file_on_github_is_a_github_download() {
        let good = "https://github.com/DeroXP/ghostshell/releases/download/v9.0.0/Vispora.exe";
        assert!(is_github_download(good));
        assert!(is_github_download(&good.replace("https://github.com", "HTTPS://GitHub.com")));
        assert!(is_github_download(&format!("{good}?x=1#y")));
        for bad in [
            "https://example.test/releases/download/v9.0.0/Vispora.exe",
            "https://github.com@example.test/releases/download/x/Vispora.exe",
            "https://github.com.example.test/releases/download/x/Vispora.exe",
            "https://github.com:443/DeroXP/ghostshell/releases/download/v9/Vispora.exe",
            "http://github.com/DeroXP/ghostshell/releases/download/v9.0.0/Vispora.exe",
            "https://github.com/somebody/else/raw/main/Vispora.exe",
            "https://github.com/x?next=/releases/download/",
            "https://github.com/DeroXP/ghostshell/releases/download/v9/Vispora.exe ",
            "https://github.com/DeroXP/ghostshell/releases/down\tload/v9/Vispora.exe",
            "github.com/DeroXP/ghostshell/releases/download/v9/Vispora.exe",
            "",
        ] {
            assert!(!is_github_download(bad), "{bad}");
        }
    }

    #[test]
    fn the_updaters_file_name_carries_its_version() {
        assert_eq!(updater_asset_version("VisporaUpdater-1.5.0.exe"), Some("1.5.0"));
        assert_eq!(updater_asset_version("VisporaUpdater-2.0.0-beta.1.exe"), Some("2.0.0-beta.1"));
        for bad in ["VisporaUpdater.exe", "VisporaUpdater-1.9.0.exe.part", "xVisporaUpdater-2.0.0.exe",
                    "VisporaUpdater-.exe", "VisporaUpdater-v2.exe", "visporaupdater-2.0.0.exe", "VisporaUpdater-2.0.0.EXE",
                    "VisporaUpdater-2 0.exe"] {
            assert_eq!(updater_asset_version(bad), None, "{bad}");
        }
        assert_eq!(updater_asset_version(&format!("VisporaUpdater-1{}.exe", "0".repeat(39))).map(str::len), Some(40));
        assert_eq!(updater_asset_version(&format!("VisporaUpdater-1{}.exe", "0".repeat(40))), None);
    }
}
