//! Where updates come from: GitHub's releases first, the update server
//! second.
//!
//! The cases of tests/test_update_sources.py that are about update_sources.py
//! itself and about the standalone updater, one for one and under the same
//! names.  (The cases about the app's own side stay with the app, in Python.)
//! Nothing here touches the network: GitHub and the update server are a
//! table of answers, and every request made is recorded.

#![cfg(windows)]

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use common::*;
use vispora_updater_core::json::Json;
use vispora_updater_core::platform::{Body, ANSWER_LIMIT};
use vispora_updater_core::sha256;
use vispora_updater_core::sources::{github_app_release, github_updater};
use vispora_updater_core::state::PIPELINE_STEPS;

fn listed(releases: &[Release]) -> Json {
    Json::Array(releases.iter().map(|r| r.json.clone()).collect())
}

fn updater_bytes(tag: &str) -> Vec<u8> {
    repeated(b"MZ", format!("updater {tag} ").as_bytes(), 100000)
}

// ═════════════════════════════════════════════════════════════════════
// Reading what GitHub answers
// ═════════════════════════════════════════════════════════════════════
#[test]
fn stable_gets_the_newest_final_build_and_beta_the_newest_of_all() {
    let all = listed(&[
        release("9.0.0"),
        release_with("9.1.0-beta.2", &[("Vispora.exe", &app_other())], true),
        release("8.5.0"),
        release_with("9.1.0-beta.1", &[("Vispora.exe", &app_new())], true),
    ]);
    let (stable, beta) = (github_app_release(&all, "stable").unwrap(), github_app_release(&all, "beta").unwrap());
    assert_eq!(stable.version, "9.0.0");
    assert!(stable.sha256 == sha256::hex_of(&app_new()) && stable.size_bytes == app_new().len() as i128);
    assert!(stable.download_url == url_of("9.0.0", "Vispora.exe") && stable.notes == "What is new in 9.0.0.");
    assert_eq!(stable.released_at, "2026-11-24T10:00:00Z");
    assert_eq!((beta.version.as_str(), beta.sha256), ("9.1.0-beta.2", sha256::hex_of(&app_other())));
    // beta also gets a final build when that is the newest, as on the update server
    let both = listed(&[release("9.0.0"), release_with("9.0.0-beta.3", &[("Vispora.exe", &app_new())], true)]);
    assert_eq!(github_app_release(&both, "beta").unwrap().version, "9.0.0");
    // a pre-release that was not marked as one still never reaches stable
    assert_eq!(github_app_release(&listed(&[release("9.2.0-beta.1"), release("9.0.0")]), "stable").unwrap().version, "9.0.0");
    assert_eq!(github_app_release(&listed(&[release("9.2.0-rc.1")]), "stable"), None);
}

#[test]
fn the_newest_is_told_by_its_version_not_by_its_place_in_the_list() {
    let all = [release("8.9.1"), release("9.0.0"), release("8.0.0")]; // a fix for an older line, published last
    assert_eq!(github_app_release(&listed(&all), "stable").unwrap().version, "9.0.0");
    let reversed: Vec<Release> = all.iter().rev().cloned().collect();
    assert_eq!(github_app_release(&listed(&reversed), "stable").unwrap().version, "9.0.0");
    let no_v = release("9.0.0").with("tag_name", "9.0.0".into());
    assert_eq!(github_app_release(&listed(&[no_v, release("8.0.0")]), "stable").unwrap().version, "9.0.0", "a tag with or without the v");
}

#[test]
fn a_release_that_is_not_ready_is_passed_over() {
    let good = release("8.0.0");
    let tag = "v9.0.0";
    let with_asset = |change: &dyn Fn(&mut Json)| {
        let mut a = asset("Vispora.exe", &app_new(), tag);
        change(&mut a);
        release("9.0.0").with("assets", Json::Array(vec![a]))
    };
    let elsewhere = |url: &str| {
        let url = url.to_string();
        with_asset(&move |a| a.set("browser_download_url", url.as_str().into()))
    };
    let not_ready: Vec<(&str, Release)> = vec![
        ("a draft", release("9.0.0").with("draft", true.into())),
        ("still uploading", with_asset(&|a| a.set("state", "new".into()))),
        ("no file at all", release("9.0.0").with("assets", Json::Array(vec![]))),
        ("no SHA-256", with_asset(&|a| a.set("digest", Json::Null))),
        ("another kind of hash", with_asset(&|a| a.set("digest", format!("md5:{}", "a".repeat(32)).into()))),
        ("a short hash", with_asset(&|a| a.set("digest", format!("sha256:{}", "a".repeat(63)).into()))),
        ("too small to be a build", release("9.0.0").with("assets", Json::Array(vec![asset("Vispora.exe", b"<html>", tag)]))),
        ("a size that is not a number", with_asset(&|a| a.set("size", "big".into()))),
        ("a size that is true", with_asset(&|a| a.set("size", true.into()))),
        ("a size with a fraction", with_asset(&|a| a.set("size", 2097152.0f64.into()))),
        ("served from elsewhere", elsewhere("https://example.test/releases/download/v9.0.0/Vispora.exe")),
        ("a look-alike host", elsewhere("https://github.com@example.test/releases/download/x/Vispora.exe")),
        ("not https", elsewhere(&url_of("9.0.0", "Vispora.exe").replace("https://", "http://"))),
        ("not a release file", elsewhere("https://github.com/somebody/else/raw/main/Vispora.exe")),
        ("an address that is not text", with_asset(&|a| a.set("browser_download_url", 7u8.into()))),
        ("another file name", release_with("9.0.0", &[("GhostShell.exe", &app_new()), ("vispora.exe.zip", &app_new())], false)),
        ("a tag that is no version", release("9.0.0").with("tag_name", "nightly".into())),
        ("no tag", release("9.0.0").with("tag_name", Json::Null)),
    ];
    for (why, r) in not_ready {
        assert_eq!(github_app_release(&listed(&[r.clone(), good.clone()]), "beta").unwrap().version, "8.0.0", "{why}");
        assert_eq!(github_app_release(&listed(&[r]), "beta"), None, "{why}");
    }
    // What is fine: a hash in capitals with blanks around it.
    let shouting = with_asset(&|a| {
        let digest = format!("  SHA256:{}  ", sha256::hex_of(&app_new()).to_uppercase());
        a.set("digest", digest.into())
    });
    assert_eq!(github_app_release(&listed(&[shouting]), "stable").unwrap().sha256, sha256::hex_of(&app_new()));
}

#[test]
fn an_answer_that_is_not_a_list_of_releases_offers_nothing() {
    for answer in [
        "null", "{}", "\"\"", "\"<html>\"", "7", "{\"message\": \"API rate limit exceeded for 203.0.113.9.\"}", "[]",
        "[null, 7, \"x\", [], {}]", "[{\"tag_name\": \"v9.0.0\", \"assets\": \"Vispora.exe\"}]",
        "[{\"tag_name\": \"v9.0.0\", \"assets\": [null, \"x\", {\"name\": 3}, {\"name\": \"Vispora.exe\"}]}]",
    ] {
        assert_eq!(github_app_release(&j(answer), "stable"), None, "{answer}");
        assert_eq!(github_updater(&j(answer), "beta"), None, "{answer}");
    }
}

#[test]
fn the_old_ghostshell_releases_on_github_offer_nothing() {
    // What the repository's releases looked like before: tags without a v,
    // one GhostShell.exe each.
    let old_release = |v: &str| {
        j(&format!(
            "{{\"tag_name\": \"{v}\", \"name\": \"v{v}\", \"draft\": false, \"prerelease\": false, \"body\": \"# GhostShell v{v}\", \
             \"published_at\": \"2026-05-07T19:26:50Z\", \"assets\": [{{\"name\": \"GhostShell.exe\", \"size\": 23966402, \
             \"state\": \"uploaded\", \"digest\": \"sha256:22e5764423919235675ec5624893239f4f35dd5bdea4a645e7537c11251ec27c\", \
             \"browser_download_url\": \"https://github.com/DeroXP/ghostshell/releases/download/{v}/GhostShell.exe\"}}]}}"
        ))
    };
    let old = vec![old_release("2.9.9.8"), old_release("2.9.9.7")];
    assert_eq!(github_app_release(&Json::Array(old.clone()), "stable"), None);
    let mut with_new = old;
    with_new.push(release("9.0.0").json);
    assert_eq!(github_app_release(&Json::Array(with_new), "stable").unwrap().version, "9.0.0");
}

#[test]
fn the_updater_is_found_by_the_version_in_its_file_name() {
    let (u15, u16, u14) = (updater_bytes("1.5"), updater_bytes("1.6"), updater_bytes("1.4"));
    let all = [
        release_with("9.0.0", &[("Vispora.exe", &app_new()), ("VisporaUpdater-1.5.0.exe", &u15)], false),
        release_with("9.1.0-beta.1", &[("Vispora.exe", &app_new()), ("VisporaUpdater-1.6.0.exe", &u16)], true),
        release_with("8.0.0", &[("VisporaUpdater-1.4.5.exe", &u14), ("VisporaUpdater.exe", &u14),
                                ("VisporaUpdater-1.9.0.exe.part", &u14), ("xVisporaUpdater-2.0.0.exe", &u14)], false),
    ];
    let (stable, beta) = (github_updater(&listed(&all), "stable").unwrap(), github_updater(&listed(&all), "beta").unwrap());
    assert_eq!((stable.version.as_str(), stable.sha256.as_str()), ("1.5.0", sha256::hex_of(&u15).as_str()));
    assert_eq!(stable.download_url, url_of("9.0.0", "VisporaUpdater-1.5.0.exe"));
    assert_eq!((beta.version.as_str(), beta.sha256.as_str()), ("1.6.0", sha256::hex_of(&u16).as_str()));
    // the newest updater, wherever it is: an app release without one does not hide an older release's
    assert_eq!(github_updater(&listed(&[release("9.5.0"), all[0].clone()]), "stable").unwrap().version, "1.5.0");
    assert_eq!(github_updater(&listed(&[release("9.5.0")]), "stable"), None);
    let mut uploading = asset("VisporaUpdater-1.7.0.exe", &u16, "v9.6.0");
    uploading.set("state", "new".into());
    let half = release("9.6.0").with("assets", Json::Array(vec![uploading]));
    assert_eq!(github_updater(&listed(&[half, all[0].clone()]), "stable").unwrap().version, "1.5.0",
               "one that is still uploading is not there yet");
    // This updater's own file is found the same way by the app.
    let own = format!("VisporaUpdater-{}.exe", vispora_updater_core::UPDATER_VERSION);
    let with_own = release_with("9.7.0", &[("Vispora.exe", &app_new()), (&own, &u16)], false);
    assert_eq!(github_updater(&listed(&[with_own, all[0].clone()]), "stable").unwrap().version, vispora_updater_core::UPDATER_VERSION);
}

// ═════════════════════════════════════════════════════════════════════
// The standalone updater
// ═════════════════════════════════════════════════════════════════════
#[test]
fn the_updater_installs_from_github() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111]);
    rig.net.github(&[release(NEWER), release_with("8.0.0", &[("Vispora.exe", &app_other())], false)]);
    rig.net.publish("stable", NEWEST, &app_other(), None, SERVER);
    assert_eq!(rig.run(), 0);
    assert!(rig.installed() == app_new() && rig.procs.launched() == [vec![rig.target.clone()]]);
    assert_eq!(rig.net.calls(), [GITHUB.to_string(), url_of(NEWER, "Vispora.exe")], "found on GitHub: the update server is not asked");
    assert_eq!(rig.net.header(GITHUB, "Accept").as_deref(), Some("application/vnd.github+json"));
    assert_eq!(rig.net.header(GITHUB, "X-GitHub-Api-Version").as_deref(), Some("2022-11-28"));
    assert!(rig.net.header(GITHUB, "User-Agent").unwrap().starts_with("VisporaUpdater/"));
    assert_eq!(rig.step_detail("channel"), format!("Latest on 'stable': v{NEWER} (from GitHub)"));
    assert!(rig.all_steps_are("done"));
    assert_eq!(rig.install_info()["version"].as_str(), Some(NEWER));
    let found = rig.state().channel_check;
    assert_eq!((found["source"].as_str(), found["ok"].truthy()), (Some("github"), true));
    assert_eq!(found["notes"].as_str(), Some("What is new in 9.0.0."));
}

#[test]
fn the_updater_falls_back_to_the_update_server() {
    let cases: [(&str, &dyn Fn(&Rig)); 3] = [
        ("GitHub unreachable", &|_| {}),
        ("GitHub has nothing for the channel", &|rig| rig.net.github(&[release_with("9.5.0-beta.1", &[("Vispora.exe", &app_new())], true)])),
        ("GitHub's limit", &|rig| rig.net.github_says("{\"message\": \"API rate limit exceeded\"}")),
    ];
    for (why, set_up) in cases {
        let rig = Rig::update();
        set_up(&rig);
        rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
        assert_eq!(rig.run(), 0, "{why}");
        assert_eq!(rig.installed(), app_new(), "{why}");
        assert_eq!(rig.net.calls(), [GITHUB.to_string(), format!("{SERVER}/version/stable"), format!("{SERVER}/download/stable")], "{why}");
        assert_eq!(rig.step_detail("channel"), format!("Latest on 'stable': v{NEWER} (from the update server)"), "{why}");
    }
}

#[test]
fn the_updater_refuses_a_github_file_that_fails_its_hash() {
    let rig = Rig::update();
    rig.net.github(&[release(NEWER)]);
    rig.net.route(&url_of(NEWER, "Vispora.exe"), app_other());
    assert_eq!(rig.run(), 6);
    assert!(rig.installed() == old() && rig.error().contains("SHA-256 mismatch"));
    assert!(rig.part_files().is_empty());
}

#[test]
fn a_place_that_is_behind_is_not_installed_from() {
    // The app found 9.0.0 on GitHub and asked for it.  If GitHub does not
    // answer the updater a moment later and the update server still has an
    // older build, that build must not be installed over a newer app.
    let rig = Rig::update();
    rig.updater.lock().requested_version = NEWER.into();
    rig.net.publish("stable", "8.0.0", &app_other(), None, SERVER);
    assert_eq!(rig.run(), 4);
    assert_eq!(rig.installed(), old(), "nothing was installed");
    let err = rig.error();
    assert!(err.contains("could not reach GitHub"));
    assert!(err.contains(&format!("the update server has v8.0.0, older than the v{NEWER} that was asked for")), "{err}");
    assert!(!rig.net.calls().contains(&format!("{SERVER}/download/stable")));

    let rig = Rig::update(); // GitHub is behind, the update server has it
    rig.updater.lock().requested_version = NEWER.into();
    rig.net.github(&[release_with("8.0.0", &[("Vispora.exe", &app_other())], false)]);
    rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
    assert!(rig.run() == 0 && rig.installed() == app_new());
    assert!(rig.step_detail("channel").ends_with("(from the update server)"));

    let rig = Rig::update(); // something newer than was asked for is fine
    rig.updater.lock().requested_version = NEWER.into();
    rig.net.github(&[release(NEWEST)]);
    assert!(rig.run() == 0 && rig.installed() == app_new());

    let rig = Rig::update(); // started by hand: whatever the channel has
    rig.net.publish("stable", "8.0.0", &app_other(), None, SERVER);
    assert!(rig.run() == 0 && rig.installed() == app_other());
}

#[test]
fn the_updater_asks_only_the_place_it_was_told() {
    let rig = Rig::update();
    rig.updater.lock().source = "github".into();
    rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
    assert!(rig.run() == 4 && rig.net.calls() == [GITHUB.to_string()], "GitHub only");
    assert!(rig.error() == "could not reach GitHub" && rig.installed() == old());

    let rig = Rig::update();
    rig.updater.lock().source = "server".into();
    rig.net.github(&[release_with(NEWEST, &[("Vispora.exe", &app_other())], false)]);
    rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
    assert!(rig.run() == 0 && rig.installed() == app_new());
    assert_eq!(rig.net.calls(), [format!("{SERVER}/version/stable"), format!("{SERVER}/download/stable")]);

    let rig = Rig::update(); // a server of one's own: that one only
    rig.updater.lock().server_url = OWN_SERVER.into();
    rig.net.github(&[release_with(NEWEST, &[("Vispora.exe", &app_other())], false)]);
    rig.net.publish("stable", NEWER, &app_new(), None, OWN_SERVER);
    assert!(rig.run() == 0 && rig.installed() == app_new());
    assert!(rig.net.calls()[0] == format!("{OWN_SERVER}/version/stable") && !rig.net.calls().contains(&GITHUB.to_string()));

    let rig = Rig::update(); // ... also when it is down
    rig.updater.lock().server_url = OWN_SERVER.into();
    rig.net.github(&[release_with(NEWEST, &[("Vispora.exe", &app_other())], false)]);
    assert!(rig.run() == 4 && rig.installed() == old() && !rig.net.calls().contains(&GITHUB.to_string()));
}

#[test]
fn the_updaters_command_line_and_the_apps_record_say_where_to_look() {
    let run = |argv: &[&str], record: &str| {
        let rig = Rig::update();
        rig.net.github(&[release(NEWER)]);
        rig.net.publish("stable", NEWEST, &app_other(), None, SERVER);
        rig.record("stable", record);
        let mut all = vec!["--silent"];
        all.extend_from_slice(argv);
        assert_eq!(rig.main(&all), Some(0));
        (rig.state(), rig.installed())
    };
    let (st, installed) = run(&[], "");
    assert!(st.source == "auto" && installed == app_new(), "nothing said: GitHub first");
    let (st, installed) = run(&[], "\"update_source\": \"server\"");
    assert!(st.source == "server" && installed == app_other(), "started by hand: what the app wrote down");
    let (st, installed) = run(&["--source", "github"], "\"update_source\": \"server\"");
    assert!(st.source == "github" && installed == app_new(), "the command line wins");
    let (st, installed) = run(&[], "\"update_source\": \"nonsense\"");
    assert!(st.source == "auto" && installed == app_new());
    let (st, _) = run(&["--target-version", NEWER], "");
    assert_eq!((st.requested_version.as_str(), st.target_version.as_str()), (NEWER, NEWER));
    // A channel on the command line that is not the install's own: an older
    // build than the installed one is then what was asked for.
    let (st, _) = run(&["--channel", "beta"], "");
    assert!(st.allow_older && st.channel == "beta");
    let (st, _) = run(&["--channel", "stable"], "");
    assert!(!st.allow_older);
}

#[test]
fn picking_another_channel_in_the_window_drops_the_version_that_was_asked_for() {
    let rig = Rig::update();
    {
        let mut st = rig.updater.lock();
        st.requested_version = "9.5.0-beta.1".into();
        st.channel = "beta".into();
    }
    assert!(rig.post("/api/set-channel", "{\"channel\": \"beta\"}")["ok"].truthy());
    assert_eq!(rig.state().requested_version, "9.5.0-beta.1", "the same channel: still what the app asked for");
    assert!(!rig.state().allow_older);
    assert!(rig.post("/api/set-channel", "{\"channel\": \"stable\"}")["ok"].truthy());
    assert!(rig.state().requested_version.is_empty() && rig.state().channel == "stable" && rig.state().allow_older);
    let labels = PIPELINE_STEPS.map(|(_, label)| label);
    assert!(labels.contains(&"Looking for the newest build") && !labels.iter().any(|l| l.to_lowercase().contains("server")));
}

#[test]
fn nothing_is_closed_before_a_build_is_found() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111]);
    rig.updater.lock().source = "github".into(); // and GitHub is at its hourly limit
    rig.net.github_says("{\"message\": \"API rate limit exceeded\"}");
    assert_eq!(rig.run(), 4);
    assert!(rig.steps()["channel"] == "error" && rig.steps()["close"] == "pending");
    assert!(rig.procs.commands().is_empty() && rig.procs.pids("GhostShell.exe") == [111], "the running app was left alone");
    assert_eq!(rig.installed(), old());
}

#[test]
fn the_updater_takes_the_file_from_the_next_place_when_one_fails() {
    let rig = Rig::update();
    rig.net.github(&[release(NEWER)]);
    rig.net.unroute(&url_of(NEWER, "Vispora.exe")); // listed on GitHub, but the file does not come
    rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
    assert!(rig.run() == 0 && rig.installed() == app_new());
    assert_eq!(rig.net.calls(), [GITHUB.to_string(), url_of(NEWER, "Vispora.exe"), format!("{SERVER}/version/stable"),
                                format!("{SERVER}/download/stable")]);
    assert_eq!(rig.step_detail("channel"),
               format!("Latest on 'stable': v{NEWER} (from the update server; the file from GitHub could not be used)"));
    assert!(rig.all_steps_are("done"));

    let rig = Rig::update(); // a file that fails GitHub's hash: the same
    rig.net.github(&[release(NEWER)]);
    rig.net.route(&url_of(NEWER, "Vispora.exe"), app_other());
    rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
    assert!(rig.run() == 0 && rig.installed() == app_new());

    let rig = Rig::update(); // nobody can deliver it: the first error stands
    rig.net.github(&[release(NEWER)]);
    rig.net.unroute(&url_of(NEWER, "Vispora.exe"));
    assert!(rig.run() == 5 && rig.installed() == old());
    assert!(rig.error().starts_with("Download failed") && rig.steps()["download"] == "error");
    assert!(rig.part_files().is_empty());

    let rig = Rig::update(); // the other place is behind what was asked for
    rig.updater.lock().requested_version = NEWER.into();
    rig.net.github(&[release(NEWER)]);
    rig.net.unroute(&url_of(NEWER, "Vispora.exe"));
    rig.net.publish("stable", "8.0.0", &app_other(), None, SERVER);
    assert!(rig.run() == 5 && rig.installed() == old());

    let rig = Rig::update(); // both places fail: each is asked once, never the same one twice
    rig.net.github(&[release(NEWER)]);
    rig.net.route(&url_of(NEWER, "Vispora.exe"), app_other());
    rig.net.publish("stable", NEWER, &app_new(), Some(&"1".repeat(64)), SERVER);
    assert!(rig.run() == 6 && rig.installed() == old());
    assert_eq!(rig.net.calls().iter().filter(|c| c.as_str() == GITHUB).count(), 1);
    assert_eq!(rig.net.calls().iter().filter(|c| c.ends_with("/download/stable")).count(), 1);
}

#[test]
fn an_odd_answer_from_the_update_server_is_an_error_not_a_crash() {
    let with_field = |key: &str, value: &str| {
        let rig = Rig::update();
        rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
        let url = format!("{SERVER}/version/stable");
        let meta = String::from_utf8(rig.net.routes.lock().unwrap()[&url].clone()).unwrap();
        // The field again at the end of the object: the last one counts, as
        // in Python.  (Infinity and NaN are what Python's json.dumps writes.)
        rig.net.route(&url, format!("{}, \"{key}\": {value}}}", meta.trim_end().trim_end_matches('}')));
        rig
    };
    for size in ["\"44 MB\"", "[\"x\"]", "{\"a\": 1}", "Infinity", "1e400", "null", "-5", "NaN"] {
        let rig = with_field("size_bytes", size);
        assert!(rig.run() == 0 && rig.installed() == app_new(), "size_bytes {size} is only a hint");
    }
    for sha in ["12345", "[\"a\"]", "{\"x\": 1}"] {
        let rig = with_field("sha256", sha);
        assert!(rig.run() == 6 && rig.installed() == old(), "{sha}");
        assert_eq!(rig.state().phase, "error");
    }
    // Whatever else goes wrong is said, not left spinning.
    let rig = Rig::update();
    rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
    *rig.net.opener.lock().unwrap() = Some(Box::new(|url: &str| {
        if url.ends_with("/download/stable") {
            panic!("unexpected");
        }
        None
    }));
    assert_eq!(rig.run(), 9);
    let st = rig.state();
    assert!(st.phase == "error" && st.error.as_deref().unwrap().contains("unexpected error"), "{:?}", st.error);
    assert!(!rig.steps().values().any(|s| s == "active"));
    assert_eq!(rig.step_detail("download"), "stopped unexpectedly");
    assert_eq!(rig.installed(), old());
    // An answer that is not JSON at all, or not an object: as if the server were down.
    for answer in ["<html>502 Bad Gateway</html>", "[1, 2]", "\"up\"", "", "{}"] {
        let rig = Rig::update();
        rig.net.route(&format!("{SERVER}/version/stable"), answer);
        assert!(rig.run() == 4 && rig.error().contains(&format!("could not reach {SERVER}/version/stable")), "{answer}");
    }
}

#[test]
fn started_by_hand_the_updater_does_not_go_below_what_is_installed() {
    // GitHub is behind the update server, and the install already has the
    // server's build: GitHub's older one is not installed over it.
    let rig = Rig::update();
    rig.updater.lock().current_version = NEWEST.into();
    rig.net.github(&[release_with(NEWER, &[("Vispora.exe", &app_other())], false)]);
    rig.net.publish("stable", NEWEST, &app_new(), None, SERVER);
    assert!(rig.run() == 0 && rig.installed() == app_new(), "the update server's, which is not older");
    assert!(rig.step_detail("channel").ends_with("(from the update server)"));

    let rig = Rig::update(); // nobody reaches what is installed: nothing happens
    rig.updater.lock().current_version = NEWEST.into();
    rig.net.github(&[release_with(NEWER, &[("Vispora.exe", &app_other())], false)]);
    assert!(rig.run() == 4 && rig.installed() == old());
    assert!(rig.error().contains(&format!("GitHub has v{NEWER}, older than the v{NEWEST} that is installed")), "{}", rig.error());

    let rig = Rig::update(); // another channel picked in the window: asked for
    {
        let mut st = rig.updater.lock();
        st.current_version = "9.5.0-beta.1".into();
        st.channel = "beta".into();
    }
    assert!(rig.post("/api/set-channel", "{\"channel\": \"stable\"}")["ok"].truthy());
    rig.net.github(&[release_with(NEWER, &[("Vispora.exe", &app_other())], false)]);
    assert!(rig.run() == 0 && rig.installed() == app_other());

    let rig = Rig::setup("install", "Vispora.exe", "stable", &[]); // a fresh install has nothing to go below
    rig.updater.lock().current_version = NEWEST.into();
    rig.net.github(&[release(NEWER)]);
    assert!(rig.run() == 0 && rig.installed() == app_new());
}

#[test]
fn the_updater_keeps_the_apps_note_of_where_updates_come_from() {
    let rig = Rig::update();
    rig.record("stable", "\"update_source\": \"server\"");
    rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
    assert!(rig.main(&["--silent"]) == Some(0) && rig.installed() == app_new());
    let info = rig.install_info();
    assert_eq!((info["version"].as_str(), info["update_source"].as_str()), (Some(NEWER), Some("server")),
               "a second run by hand asks the same place");
    assert!(!rig.net.calls().contains(&GITHUB.to_string()));
}

// ═════════════════════════════════════════════════════════════════════
// Not among the Python cases: what a wild answer does to the window
// ═════════════════════════════════════════════════════════════════════

#[test]
fn a_number_json_cannot_say_does_not_reach_the_page() {
    // Python's json takes NaN and Infinity from a server, and the reader
    // here does too.  The status the page reads must still be something a
    // browser can read: one NaN in it, and the page could read no status
    // again ("Cannot reach updater backend", and buttons that do nothing).
    let rig = Rig::update();
    rig.net.publish("stable", NEWER, &app_new(), None, SERVER);
    let url = format!("{SERVER}/version/stable");
    let meta = String::from_utf8(rig.net.routes.lock().unwrap()[&url].clone()).unwrap();
    let wild = "\"notes\": NaN, \"released_at\": 1e999, \"size_bytes\": -Infinity";
    rig.net.route(&url, format!("{}, {wild}}}", meta.trim_end().trim_end_matches('}')));
    assert_eq!(rig.run(), 0);
    let status = rig.get("/api/status");
    let text = String::from_utf8(status.body.clone()).unwrap();
    assert!(!text.contains("NaN") && !text.contains("Infinity"), "{text}");
    let read = status.to_json();
    assert_eq!((&read["channel_check"]["notes"], &read["channel_check"]["released_at"]), (&Json::Null, &Json::Null));
    assert_eq!(read["channel_check"]["version"], Json::from(NEWER));
    assert_eq!(read["phase"], Json::from("done"));
}

#[test]
fn an_answer_without_an_end_is_given_up_on() {
    /// Blanks for as long as anybody reads.
    struct Endless(Arc<AtomicUsize>);
    impl Body for Endless {
        fn content_length(&self) -> Option<u64> {
            None
        }
        fn read(&mut self, buf: &mut [u8]) -> Result<usize, String> {
            buf.fill(b' ');
            self.0.fetch_add(buf.len(), Ordering::SeqCst);
            Ok(buf.len())
        }
    }
    let rig = Rig::update();
    let handed_out = Arc::new(AtomicUsize::new(0));
    let counter = handed_out.clone();
    *rig.net.opener.lock().unwrap() = Some(Box::new(move |url: &str| {
        url.contains("api.github.com").then(|| Ok(Box::new(Endless(counter.clone())) as Box<dyn Body>))
    }));
    assert_eq!(rig.updater.check_github("stable").unwrap_err(), "could not reach GitHub");
    let read = handed_out.load(Ordering::SeqCst);
    assert!(read > ANSWER_LIMIT && read <= ANSWER_LIMIT + 2 * 65536,
            "{read} bytes were read of an answer that never ends");
}
