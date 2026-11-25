//! The real HTTP client against the real internet.
//!
//! Every other test stays on this PC: tests/http.rs runs the same client
//! against a listener on 127.0.0.1, over plain http.  What that cannot show
//! is the part an update depends on: https, Windows' certificate checks, and
//! GitHub itself, which answers the list of releases from one host and
//! serves a release's file from another, by a redirect.
//!
//! So these are ignored by default.  A test run should not need the
//! internet, should not use up GitHub's hourly allowance for questions
//! without a login (60), and should not download the app.  To run them:
//!
//!     cargo test --release --offline --manifest-path updater_rs/core/Cargo.toml --test live -- --ignored --test-threads 1
//!
//! Nothing is installed and nothing is started.  The second test downloads
//! the newest stable `Vispora.exe` into the test's temporary folder, compares
//! its hash with the one GitHub gives for it, and deletes the folder.  The
//! third asks badssl.com, a public site kept for exactly this, for two pages
//! whose certificates are wrong on purpose.

#![cfg(windows)]

mod common;

use common::*;
use vispora_updater_core::paths;
use vispora_updater_core::win;

/// An updater in a sandbox whose network is the real one.
fn on_the_real_network() -> Rig {
    let rig = Rig::with_net(Some(win::platform().net));
    rig.updater.lock().updater_version = vispora_updater_core::UPDATER_VERSION.to_string();
    rig
}

fn is_a_hash(text: &str) -> bool {
    text.len() == 64 && text.bytes().all(|b| b.is_ascii_hexdigit())
}

#[test]
#[ignore = "asks GitHub over the internet"]
fn github_answers_over_https_and_its_newest_build_is_found() {
    let rig = on_the_real_network();
    let found = rig.updater.check_github("stable").expect("GitHub was asked and has a stable release");
    assert_eq!(found.source, "github");
    assert!(found.version.split('.').count() >= 3 && found.version.chars().all(|c| c.is_ascii_digit() || c == '.'),
            "a stable version: {}", found.version);
    assert!(is_a_hash(&found.sha256), "GitHub's own hash of the file: {}", found.sha256);
    assert!(found.download_url.starts_with("https://github.com/DeroXP/ghostshell/releases/download/")
            && found.download_url.ends_with("/Vispora.exe"), "{}", found.download_url);
    assert!(found.size_bytes > 5_000_000, "the app is not that small: {} bytes", found.size_bytes);
    // The beta channel is offered the newest of all, which is never older.
    let beta = rig.updater.check_github("beta").expect("the beta channel is offered a build too");
    assert!(vispora_updater_core::sources::parse_version(&beta.version)
            >= vispora_updater_core::sources::parse_version(&found.version));
}

#[test]
#[ignore = "downloads the app's exe from GitHub (not installed, not started, deleted afterwards)"]
fn the_apps_file_comes_through_the_redirect_and_matches_githubs_hash() {
    let rig = on_the_real_network();
    let found = rig.updater.check_github("stable").expect("GitHub was asked and has a stable release");
    let staged = paths::join(&paths::join(&rig.root, "staged"), "Vispora-download.exe");
    let got = rig.updater.download(&found.download_url, &staged, found.size_bytes).expect("the file was downloaded");
    assert_eq!(got.size_bytes, found.size_bytes, "all of it arrived");
    assert_eq!(got.sha256, found.sha256, "what arrived is what GitHub says it published");
    let head = std::fs::read(&got.path).unwrap();
    assert_eq!(&head[..2], b"MZ", "a Windows program");
    // (the folder goes with the rig; the file was never moved into an install)
    assert!(rig.target.is_empty() && rig.procs.launched().is_empty() && rig.procs.commands().is_empty());
}

#[test]
#[ignore = "asks badssl.com over the internet"]
fn a_certificate_that_is_wrong_is_refused() {
    let net = win::platform().net;
    // One that is out of date and one that is for another name.  (A site
    // that cannot be reached at all fails this test too: it would show
    // nothing.  badssl.com has more kinds, but its self-signed and
    // unknown-authority pages did not answer when this was written.)
    for host in ["expired", "wrong.host"] {
        let answer = net.get(&format!("https://{host}.badssl.com/"), &[("User-Agent", "certificate-check")], 20);
        let why = answer.expect_err(&format!("{host}.badssl.com was read although its certificate is wrong"));
        assert!(why.contains("12175") || why.contains("secure"), "{host}: refused, but not for its certificate: {why}");
    }
}
