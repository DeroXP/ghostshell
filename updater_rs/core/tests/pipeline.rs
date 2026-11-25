//! The standalone updater: finding the install, closing the app, download and
//! SHA-256 check, the exe swap with backup and rollback, the command line,
//! and what its window is told.
//!
//! These are the cases of tests/test_updater.py, the Python updater's own
//! tests, one for one and under the same names.  That file is the
//! specification; what passes there has to pass here.

#![cfg(windows)]

mod common;

use common::*;
use vispora_updater_core::cli;
use vispora_updater_core::json::Json;
use vispora_updater_core::paths;
use vispora_updater_core::sha256;
use vispora_updater_core::sources::{self, parse_version};
use vispora_updater_core::state::PIPELINE_STEPS;
use vispora_updater_core::UPDATER_VERSION;

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

// ═════════════════════════════════════════════════════════════════════
// Names and folders
// ═════════════════════════════════════════════════════════════════════
#[test]
fn updater_is_sandboxed() {
    let rig = Rig::update();
    let p = &rig.updater.paths;
    assert!(p.appdata_dir.starts_with(&rig.root) && p.install_info.starts_with(&rig.root));
    assert!(p.updater_dir.starts_with(&rig.root) && p.log_path.starts_with(&rig.root));
    assert_eq!(p.appdata_dir, paths::join(&paths::join(&rig.root, "Roaming"), "Vispora"));
    assert_eq!(p.install_info, paths::join(&p.appdata_dir, "install_info.json"));
    assert_eq!(p.log_path, paths::join(&paths::join(&p.appdata_dir, "updater"), "updater.log"));
}

#[test]
fn the_data_folder_from_before_the_rename_is_followed_not_replaced() {
    // %APPDATA%\GhostShell is the only one on disk: the updater must not
    // make an empty %APPDATA%\Vispora beside it.
    let rig = Rig::bare();
    let roaming = paths::join(&rig.root, "Roaming");
    std::fs::remove_dir_all(paths::join(&roaming, "Vispora")).unwrap();
    std::fs::create_dir_all(paths::join(&roaming, "GhostShell")).unwrap();
    let p = paths::Paths::resolve(&rig.updater.env);
    assert_eq!(p.appdata_dir, paths::join(&roaming, "GhostShell"));
    assert!(!paths::is_dir(&paths::join(&roaming, "Vispora")));
    // Both there: the new one.
    std::fs::create_dir_all(paths::join(&roaming, "Vispora")).unwrap();
    assert_eq!(paths::Paths::resolve(&rig.updater.env).appdata_dir, paths::join(&roaming, "Vispora"));
}

#[test]
fn updater_talks_to_the_same_server_as_the_app() {
    assert_eq!(sources::SERVER_URL_DEFAULT, "https://vispora.up.railway.app");
    assert_eq!(cli::Args::default().server, sources::SERVER_URL_DEFAULT);
    assert_eq!(cli::DEAD_SERVER_HOST, "ghostshell-site.up.railway.app");
    assert!(parse_version(UPDATER_VERSION) > parse_version("1.5.0"), "above the last Python updater");
    assert!(parse_version(UPDATER_VERSION) >= parse_version(sources::UPDATER_MIN_FOR_SOURCES), "1.5.0 is the first that knows GitHub");
}

#[test]
fn an_existing_exe_keeps_its_filename() {
    let rig = Rig::bare();
    let folder = paths::join(&rig.root, "pick");
    std::fs::create_dir_all(&folder).unwrap();
    assert_eq!(rig.updater.pick_app_exe(&folder), paths::join(&folder, "Vispora.exe"), "a fresh install gets the new name");
    std::fs::write(paths::join(&folder, "GhostShell.exe"), b"").unwrap();
    assert_eq!(rig.updater.pick_app_exe(&folder), paths::join(&folder, "GhostShell.exe"),
               "shortcuts, taskbar pins and the autostart task all point at the old name");
    std::fs::write(paths::join(&folder, "Vispora.exe"), b"").unwrap();
    assert_eq!(rig.updater.pick_app_exe(&folder), paths::join(&folder, "Vispora.exe"));
}

// ═════════════════════════════════════════════════════════════════════
// Finding the install
// ═════════════════════════════════════════════════════════════════════
#[test]
fn install_is_found_through_the_apps_own_record() {
    let rig = Rig::update();
    rig.write_install_info(&format!("{{\"install_path\": {}, \"channel\": \"beta\", \"version\": \"3.6.0\"}}", quoted(&rig.target)));
    let r = rig.updater.find_existing_install();
    assert_eq!(r["found"], Json::Bool(true));
    assert_eq!((r["path"].as_str(), r["source"].as_str(), r["channel"].as_str(), r["version"].as_str()),
               (Some(rig.target.as_str()), Some("install_info"), Some("beta"), Some("3.6.0")));
    assert_eq!(r["dir"].as_str(), Some(rig.folder().as_str()));
}

#[test]
fn a_stale_record_falls_back_to_the_standard_folders() {
    let rig = Rig::update();
    rig.write_install_info(&format!("{{\"install_path\": {}}}", quoted(&paths::join(&paths::join(&rig.root, "moved-away"), "Vispora.exe"))));
    let old_style = paths::join(&rig.updater.env.program_files, "GhostShell"); // an install from before the rename
    std::fs::create_dir_all(&old_style).unwrap();
    std::fs::write(paths::join(&old_style, "GhostShell.exe"), b"").unwrap();
    let r = rig.updater.find_existing_install();
    assert!(r["found"].truthy() && r["source"].as_str() == Some("standard"));
    assert_eq!(r["path"].as_str(), Some(paths::join(&old_style, "GhostShell.exe").as_str()));
    std::fs::remove_dir_all(&old_style).unwrap();
    let r = rig.updater.find_existing_install();
    assert!(r["found"] == Json::Bool(false) && r["path"].as_str() == Some(""));
    let candidates: Vec<&str> = r["candidates"].as_array().unwrap().iter().filter_map(Json::as_str).collect();
    assert!(candidates.iter().any(|c| c.ends_with("Vispora\\Vispora.exe")));
    assert!(candidates.iter().any(|c| c.ends_with("GhostShell\\GhostShell.exe")), "both names are looked for");
    assert!(candidates.iter().all(|c| c.starts_with(&rig.root) || c.starts_with("install_info.json")),
            "the scan stayed inside the sandbox");
    // The same places, in the same order, as the Python updater looks in: 5
    // folders under each of the two names, the desktop and the downloads
    // folder, two exe names in each.
    assert_eq!(candidates.len(), 1 + (5 * 2 + 2) * 2);
    assert!(candidates[1].ends_with("pf\\Vispora\\Vispora.exe") && candidates[2].ends_with("pf\\Vispora\\GhostShell.exe"));
    assert!(candidates.last().unwrap().ends_with("Profile\\Downloads\\GhostShell.exe"));
}

#[test]
fn a_damaged_record_is_not_fatal() {
    let rig = Rig::update();
    rig.write_install_info("{ not json");
    assert_eq!(rig.updater.find_existing_install()["found"], Json::Bool(false));
    assert_eq!(rig.updater.load_install_info(), Json::Object(vec![]));
    rig.write_install_info("[1, 2]");
    assert_eq!(rig.updater.find_existing_install()["found"], Json::Bool(false));
    assert_eq!(rig.updater.load_install_info(), Json::Object(vec![]));
}

#[test]
fn default_install_folder_is_only_a_suggestion() {
    let rig = Rig::update();
    let suggested = rig.updater.default_install_dir();
    assert_eq!(suggested, paths::join(&rig.updater.env.program_files, "Vispora"));
    assert!(!std::path::Path::new(&suggested).exists(), "nothing is created until the install runs");
    // Where there is no Program Files: the user's own Programs folder.
    std::fs::remove_dir_all(&rig.updater.env.program_files).unwrap();
    assert_eq!(rig.updater.default_install_dir(), paths::join(&paths::join(&rig.updater.env.localappdata, "Programs"), "Vispora"));
}

// ═════════════════════════════════════════════════════════════════════
// Closing the running app
// ═════════════════════════════════════════════════════════════════════
#[test]
fn running_copies_are_found_by_the_name_of_the_exe_being_replaced() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111, 222]);
    assert_eq!(rig.updater.find_app_pids("GhostShell.exe"), [111, 222]);
    assert_eq!(rig.updater.find_app_pids("Vispora.exe"), [0u32; 0]);
    rig.procs.commands.lock().unwrap().clear();
    let r = rig.updater.close_app(&rig.target, 30.0);
    assert!(r.ok && r.msg.contains("gracefully"));
    let asked = rig.procs.ran("tasklist");
    assert!(!asked.is_empty() && asked.iter().all(|c| c.contains(&"IMAGENAME eq GhostShell.exe".to_string())),
            "a process it cannot find is a locked exe the swap would then fail on");
    assert_eq!(asked[0], strings(&["tasklist", "/FI", "IMAGENAME eq GhostShell.exe", "/FO", "CSV", "/NH"]));
    assert!(rig.procs.ran("taskkill").iter().all(|c| !c.contains(&"/F".to_string())), "asked politely first, and that was enough");
    assert_eq!(rig.procs.ran("taskkill"), [strings(&["taskkill", "/PID", "111"]), strings(&["taskkill", "/PID", "222"])]);
}

#[test]
fn app_that_is_not_running_skips_the_close_step() {
    let rig = Rig::update();
    let r = rig.updater.close_app(&rig.target, 30.0);
    assert!(r.ok && r.msg == "not running" && r.skipped);
    assert!(rig.procs.ran("taskkill").is_empty());
}

#[test]
fn app_that_ignores_a_polite_close_is_force_closed() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111]);
    *rig.procs.polite.lock().unwrap() = false;
    let r = rig.updater.close_app(&rig.target, 0.2);
    assert!(r.ok && r.msg == "force-closed" && !r.skipped);
    assert!(rig.procs.commands().contains(&strings(&["taskkill", "/F", "/PID", "111"])));
}

#[test]
fn an_app_that_cannot_be_closed_stops_the_update_before_anything_is_downloaded() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111]);
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    *rig.procs.immortal.lock().unwrap() = true; // neither a polite close nor a forced one gets rid of it
    let r = rig.updater.close_app(&rig.target, 0.2);
    assert!(!r.ok && r.msg == "could not close Vispora (pids still alive: [111])", "{}", r.msg);
    assert!(rig.procs.commands().contains(&strings(&["taskkill", "/F", "/PID", "111"])));
    assert_eq!(rig.run(), 3);
    assert_eq!(rig.error(), "could not close Vispora (pids still alive: [111]). Please close Vispora manually and click Retry.");
    assert_eq!(rig.steps()["close"], "error");
    assert!(!rig.net.calls().contains(&format!("{SERVER}/download/stable")), "nothing is downloaded for an app that stays open");
    assert!(rig.installed() == old() && rig.backups().is_empty());
}

// ═════════════════════════════════════════════════════════════════════
// The whole update
// ═════════════════════════════════════════════════════════════════════
#[test]
fn update_replaces_the_exe_in_place_and_keeps_one_backup() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111]);
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    assert_eq!(rig.run(), 0);
    assert_eq!(rig.installed(), new());
    assert!(paths::basename(&rig.target) == "GhostShell.exe" && !paths::is_file(&paths::join(&rig.folder(), "Vispora.exe")),
            "updated under the name it already had");
    assert_eq!(rig.backups().len(), 1);
    assert_eq!(std::fs::read(paths::join(&rig.folder(), &rig.backups()[0])).unwrap(), old());
    assert_eq!(rig.procs.launched(), [vec![rig.target.clone()]], "the new build is started");
    assert!(rig.all_steps_are("done") && rig.state().phase == "done");
    let info = rig.install_info();
    assert_eq!((info["install_path"].as_str(), info["channel"].as_str(), info["version"].as_str()),
               (Some(rig.target.as_str()), Some("stable"), Some("3.6.1")));
    assert_eq!(info["install_dir"].as_str(), Some(rig.folder().as_str()));
    assert!(matches!(info["updated_at"], Json::Float(t) if t > 1.7e9));
    assert!(rig.part_files().is_empty(), "no download left behind");
    assert_eq!(rig.net.calls(), [GITHUB.to_string(), format!("{SERVER}/version/stable"), format!("{SERVER}/download/stable")],
               "GitHub is asked first; it has nothing here, so the update server is asked");
    assert_eq!(rig.net.header(&format!("{SERVER}/download/stable"), "User-Agent"), Some(format!("VisporaUpdater/{UPDATER_VERSION}")));
    assert_eq!(rig.state().progress, 100);
}

#[test]
fn the_install_record_is_written_the_way_the_python_updater_wrote_it() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    assert_eq!(rig.run(), 0);
    let text = std::fs::read_to_string(&rig.updater.paths.install_info).unwrap();
    // json.dump(d, f, indent=2): these keys in this order, two spaces, no newline at the end.
    let expected = format!(
        "{{\n  \"install_path\": {},\n  \"install_dir\": {},\n  \"channel\": \"stable\",\n  \"version\": \"3.6.1\",\n  \"updated_at\": ",
        quoted(&rig.target),
        quoted(&rig.folder())
    );
    assert!(text.starts_with(&expected), "{text}");
    assert!(text.ends_with("\n}") && text.is_ascii());
}

#[test]
fn beta_channel_is_asked_for_the_beta_build() {
    let rig = Rig::setup("update", "GhostShell.exe", "beta", &[]);
    rig.net.publish("beta", "3.7.0-beta.1", &new(), None, SERVER);
    assert_eq!(rig.run(), 0);
    assert_eq!(rig.net.calls()[..2], [GITHUB.to_string(), format!("{SERVER}/version/beta")]);
    assert_eq!(rig.installed(), new());
    assert_eq!(rig.install_info()["version"].as_str(), Some("3.7.0-beta.1"));
}

#[test]
fn fresh_install_creates_the_folder_and_closes_nothing() {
    let rig = Rig::setup("install", "Vispora.exe", "stable", &[]);
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    assert!(!std::path::Path::new(&rig.folder()).exists());
    assert_eq!(rig.run(), 0);
    assert!(rig.installed() == new() && rig.backups().is_empty());
    assert!(rig.steps()["close"] == "skipped" && rig.procs.commands().is_empty(), "nothing to close: tasklist is not even asked");
    assert_eq!(rig.procs.launched(), [vec![rig.target.clone()]]);
}

#[test]
fn a_download_that_fails_its_hash_check_is_not_installed() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), Some(&"0".repeat(64)), SERVER);
    assert_eq!(rig.run(), 6);
    assert!(rig.installed() == old() && rig.backups().is_empty() && rig.procs.launched().is_empty());
    assert!(rig.error().contains("SHA-256 mismatch") && rig.steps()["verify"] == "error");
    assert_eq!(rig.error(), format!(
        "SHA-256 mismatch: expected 0000000000000000\u{2026}, got {}\u{2026}.  Aborting to avoid installing a corrupted or tampered build.",
        &sha256::hex_of(&new())[..16]));
    assert!(rig.part_files().is_empty(), "the bad download is deleted");
}

#[test]
fn a_hash_in_capitals_is_the_same_hash() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), Some(&sha256::hex_of(&new()).to_uppercase()), SERVER);
    assert_eq!(rig.run(), 0);
    assert_eq!(rig.installed(), new());
}

#[test]
fn a_server_that_sends_no_hash_is_refused() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), Some(""), SERVER);
    assert_eq!(rig.run(), 6, "fail closed: a missing hash looks the same as a stripped one");
    assert!(rig.installed() == old() && rig.procs.launched().is_empty());
    assert!(rig.error().contains("refusing to install an unverified binary"));
}

#[test]
fn an_error_page_is_not_mistaken_for_a_build() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", b"<html>404 Not Found</html>", None, SERVER);
    assert_eq!(rig.run(), 5);
    assert!(rig.installed() == old() && rig.error().contains("server error page"));
    assert_eq!(rig.error(), "Download failed: downloaded file is 26 B, likely a server error page");
}

#[test]
fn unreachable_server_changes_nothing() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111]);
    assert_eq!(rig.run(), 4);
    assert!(rig.installed() == old() && rig.backups().is_empty() && rig.procs.launched().is_empty());
    assert_eq!(rig.steps()["channel"], "error");
    assert_eq!(rig.error(), format!("could not reach GitHub; could not reach {SERVER}/version/stable"));
}

#[test]
fn nothing_published_on_the_channel() {
    let rig = Rig::update();
    rig.net.route(&format!("{SERVER}/version/stable"), "{\"available\": false}");
    assert!(rig.run() == 4 && rig.installed() == old());
    assert!(rig.error().contains("no release published"));
    assert_eq!(rig.error(), "could not reach GitHub; no release published on 'stable'");
}

#[test]
fn failed_swap_puts_the_old_exe_back() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    let target = rig.target.clone();
    *rig.disk.copier.lock().unwrap() = Some(Box::new(move |from, to| {
        // antivirus, say
        (to == target && from.ends_with(".part")).then(|| Err(io_error(13, "file is in use by another process")))
    }));
    assert_eq!(rig.run(), 7);
    assert_eq!(rig.installed(), old(), "rolled back from the backup made a moment earlier");
    assert!(rig.error().contains("rolled_back=True") && rig.procs.launched().is_empty());
    assert_eq!(rig.error(), "install copy failed: file is in use by another process; rolled_back=True");
    assert_eq!(rig.steps()["install"], "error");
}

#[test]
fn old_backups_are_pruned_once_a_new_one_exists() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    for name in ["GhostShell.exe.bak.111", "GhostShell.exe.bak.222", "ghostshell.EXE.bak.333", "GhostShell.exe.bak.txt",
                 "GhostShell.exe.bak", "Other.exe.bak.5", "notes.txt"] {
        std::fs::write(paths::join(&rig.folder(), name), b"x").unwrap();
    }
    assert_eq!(rig.run(), 0);
    let left: Vec<String> = std::fs::read_dir(rig.folder()).unwrap().flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect();
    assert_eq!(rig.backups().len(), 1);
    assert!(!left.contains(&"GhostShell.exe.bak.111".to_string()) && !left.contains(&"GhostShell.exe.bak.222".to_string()),
            "every update used to leave a full copy of the previous exe behind for good");
    assert!(!left.contains(&"ghostshell.EXE.bak.333".to_string()), "whatever the case of the name");
    for kept in ["GhostShell.exe.bak.txt", "GhostShell.exe.bak", "Other.exe.bak.5", "notes.txt"] {
        assert!(left.contains(&kept.to_string()), "only its own backups are removed: {kept}");
    }
}

#[test]
fn cancelled_before_it_starts() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    rig.updater.lock().cancelled = true;
    assert_eq!(rig.run(), 1);
    assert!(rig.installed() == old() && rig.net.calls().is_empty() && rig.procs.launched().is_empty());
}

#[test]
fn missing_target_or_channel_is_reported_not_guessed() {
    let rig = Rig::update();
    rig.updater.lock().channel.clear();
    assert!(rig.run() == 2 && rig.error().contains("release channel"));
    let rig = Rig::update();
    rig.updater.lock().target_exe.clear();
    assert!(rig.run() == 2 && rig.error().contains("target install path"));
    assert!(rig.net.calls().is_empty());
}

// ═════════════════════════════════════════════════════════════════════
// Command line, as the app (or an old install) starts it
// ═════════════════════════════════════════════════════════════════════
#[test]
fn silent_run_with_everything_on_the_command_line() {
    let rig = Rig::update();
    rig.net.publish("beta", "3.6.1", &new(), None, SERVER);
    assert_eq!(rig.main(&["--silent", "--target-exe", &rig.target, "--channel", "beta"]), Some(0));
    assert!(rig.installed() == new() && rig.state().channel == "beta");
    assert_eq!(rig.state().updater_version, UPDATER_VERSION);
    assert!(rig.state().silent && rig.state().mode == "update" && rig.state().target_dir == rig.folder());
    let log = std::fs::read_to_string(&rig.updater.paths.log_path).unwrap();
    assert!(log.contains(&format!("[2026-11-24T12:00:00] VisporaUpdater v{UPDATER_VERSION} invoked")), "{log}");
    assert!(log.contains("running in --silent mode") && log.contains("silent install exit: 0") && log.contains("pipeline complete (exit 0)"));
}

#[test]
fn an_old_install_passing_the_retired_server_is_pointed_at_the_live_one() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    let code = rig.main(&["--silent", "--target-dir", &rig.folder(), "--channel", "stable", "--server",
                          "https://ghostshell-site.up.railway.app/"]);
    assert!(code == Some(0) && rig.installed() == new());
    assert_eq!(rig.net.calls()[..2], [GITHUB.to_string(), format!("{SERVER}/version/stable")], "the old hostname has been dead since July");
    assert_eq!(rig.state().target_exe, rig.target, "the exe in that folder, under the name it has");
}

#[test]
fn without_arguments_the_apps_record_says_what_to_update() {
    let rig = Rig::update();
    rig.net.publish("beta", "3.6.1", &new(), None, SERVER);
    rig.record("beta", "");
    assert_eq!(rig.main(&["--silent"]), Some(0));
    assert_eq!(rig.installed(), new());
    assert_eq!(rig.net.calls()[..2], [GITHUB.to_string(), format!("{SERVER}/version/beta")]);
    assert_eq!(rig.state().current_version, "3.6.0");
}

#[test]
fn without_a_record_the_pc_is_looked_over_and_a_fresh_install_is_offered() {
    let rig = Rig::bare();
    assert_eq!(rig.main(&["--channel", "stable"]), None, "a window would open");
    let st = rig.state();
    assert_eq!(st.mode, "install");
    assert_eq!(st.target_dir, paths::join(&rig.updater.env.program_files, "Vispora"));
    assert_eq!(st.target_exe, paths::join(&st.target_dir, "Vispora.exe"));
    assert_eq!(st.scan_result["found"], Json::Bool(false));
    // An install in a standard place is found and updated, with nothing said.
    let rig = Rig::bare();
    let place = paths::join(&paths::join(&rig.updater.env.localappdata, "Programs"), "Vispora");
    std::fs::create_dir_all(&place).unwrap();
    std::fs::write(paths::join(&place, "Vispora.exe"), old()).unwrap();
    assert_eq!(rig.main(&[]), None);
    let st = rig.state();
    assert_eq!((st.mode.as_str(), st.target_exe.as_str()), ("update", paths::join(&place, "Vispora.exe").as_str()));
    assert_eq!(st.scan_result["source"].as_str(), Some("standard"));
}

#[test]
fn a_command_line_that_is_wrong_starts_nothing() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    assert_eq!(rig.main(&["--silent", "--source", "railway"]), Some(2));
    assert_eq!(rig.main(&["--silent", "--no-such-option"]), Some(2));
    assert!(rig.net.calls().is_empty() && rig.installed() == old());
}

// ═════════════════════════════════════════════════════════════════════
// Its own window
// ═════════════════════════════════════════════════════════════════════
#[test]
fn window_page_and_status() {
    let rig = Rig::update();
    let page = rig.get("/");
    let html = String::from_utf8(page.body.clone()).unwrap();
    assert!(page.status == 200 && html.contains("Vispora") && !html.contains("GhostShell"));
    assert!(html.contains(&format!("v{UPDATER_VERSION}")) && !html.contains("{{"), "the version is filled in");
    assert!(page.content_type.starts_with("text/html"));
    let status = rig.get("/api/status").to_json();
    let keys: Vec<&str> = status["steps"].as_array().unwrap().iter().filter_map(|s| s["key"].as_str()).collect();
    assert_eq!(keys, ["channel", "close", "download", "verify", "install", "launch"], "the build is looked for before anything is closed");
    assert_eq!(keys, PIPELINE_STEPS.map(|(key, _)| key));
    assert!(status["updater_version"].as_str() == Some(UPDATER_VERSION) && status["mode"].as_str() == Some("update"));
    for asset in ["/static/app.js", "/static/style.css"] {
        let r = rig.get(asset);
        assert!(r.status == 200 && r.body.len() > 1000, "{asset}");
    }
    assert!(rig.get("/static/app.js?v=2").status == 200 && rig.get("/api/status?x=1").status == 200);
    assert_eq!(rig.get("/nothing-here").status, 404);
    assert_eq!(rig.get("/static/../../Cargo.toml").status, 404);
    assert_eq!(rig.get("/api/start").status, 405, "starting an install is never a GET");
    assert_eq!(vispora_updater_core::server::handle(&rig.updater, "POST", "/api/status", b"").status, 405);
}

#[test]
fn the_status_has_every_field_the_page_reads() {
    // updater/static/app.js reads these from /api/status.
    let rig = Rig::update();
    let status = rig.get("/api/status").to_json();
    for key in ["updater_version", "target_dir", "target_exe", "channel", "current_version", "target_version",
                "requested_version", "source", "allow_older", "server_url", "auto_confirm", "silent", "mode",
                "scan_result", "phase", "phase_label", "progress", "downloaded_bytes", "total_bytes", "steps", "error",
                "cancelled", "started_at", "ended_at", "channel_check"] {
        assert!(status.get(key).is_some(), "{key}");
    }
    assert_eq!((status["phase"].as_str(), status["progress"].as_int(), &status["error"]), (Some("ready"), Some(0), &Json::Null));
    let step = &status["steps"].as_array().unwrap()[0];
    assert_eq!((step["label"].as_str(), step["status"].as_str(), step["detail"].as_str()),
               (Some("Looking for the newest build"), Some("pending"), Some("")));
}

#[test]
fn window_can_change_channel_and_folder() {
    let rig = Rig::update();
    assert_eq!(rig.post("/api/set-channel", "{\"channel\": \"nightly\"}")["ok"], Json::Bool(false));
    assert_eq!(rig.post("/api/set-channel", "{\"channel\": \"beta\"}"), j("{\"ok\": true, \"channel\": \"beta\"}"));
    assert_eq!(rig.post("/api/set-target-dir", "{\"target_dir\": \"\"}")["ok"], Json::Bool(false));
    let missing = paths::join(&paths::join(&rig.root, "no-such-parent"), "Vispora");
    let refused = rig.post("/api/set-target-dir", &format!("{{\"target_dir\": {}}}", quoted(&missing)));
    assert_eq!(refused["ok"], Json::Bool(false));
    assert_eq!(refused["err"].as_str(), Some(format!("parent directory not found: {}", paths::dirname(&missing)).as_str()));
    assert_eq!(rig.post("/api/set-target-dir", &format!("{{\"target_dir\": {}}}", quoted(&rig.folder())))["ok"], Json::Bool(true));
    assert_eq!(rig.state().target_exe, rig.target);
    assert!(rig.post("/api/cancel", "{}")["ok"].truthy() && rig.updater.is_cancelled());
    assert_eq!(rig.state().phase, "cancelled");
    assert_eq!(rig.post("/api/set-channel", "not json")["ok"], Json::Bool(false));
}

#[test]
fn the_window_starts_the_install_and_shows_it_through() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111]);
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    assert_eq!(rig.post("/api/start", "{}"), j("{\"ok\": true}"));
    assert!(wait(|| rig.state().phase == "done", 10.0), "{:?}", rig.state());
    assert!(rig.installed() == new() && rig.all_steps_are("done"));
    let status = rig.get("/api/status").to_json();
    assert_eq!((status["target_version"].as_str(), status["progress"].as_int()), (Some("3.6.1"), Some(100)));
    assert_eq!(status["channel_check"]["source"].as_str(), Some("server"));
    assert_eq!(status["total_bytes"].as_int(), Some(new().len() as i128));
    // After an error the same button is "Retry": a second start runs again.
    assert!(wait(|| !rig.updater.install_running(), 5.0));
    rig.net.unroute(&format!("{SERVER}/version/stable"));
    assert_eq!(rig.post("/api/start", "{}")["ok"], Json::Bool(true));
    assert!(wait(|| rig.state().phase == "error", 10.0));
    assert_eq!(rig.state().steps[0].status, "error");
}

#[test]
fn the_window_can_look_again_start_the_app_and_pick_a_folder() {
    let rig = Rig::setup("install", "Vispora.exe", "stable", &[]);
    // Nothing installed yet: still a fresh install, into the suggested folder.
    rig.updater.lock().target_dir.clear();
    let r = rig.post("/api/rescan", "{}");
    assert_eq!((r["ok"].truthy(), r["mode"].as_str(), r["found"]["found"].truthy()), (true, Some("install"), false));
    assert_eq!(rig.state().target_dir, paths::join(&rig.updater.env.program_files, "Vispora"));
    assert_eq!(rig.post("/api/launch-app", "{}")["err"].as_str(),
               Some(format!("Vispora not found at {}", rig.state().target_exe).as_str()));
    // The app turns up in a standard place: the next look switches to update mode.
    let place = paths::join(&rig.updater.env.program_files, "Vispora");
    std::fs::create_dir_all(&place).unwrap();
    std::fs::write(paths::join(&place, "Vispora.exe"), old()).unwrap();
    let r = rig.post("/api/rescan", "{}");
    assert_eq!((r["mode"].as_str(), r["found"]["source"].as_str()), (Some("update"), Some("standard")));
    assert_eq!(rig.state().target_exe, paths::join(&place, "Vispora.exe"));
    assert_eq!(rig.post("/api/launch-app", "{}"), j("{\"ok\": true}"));
    assert_eq!(rig.procs.launched(), [vec![paths::join(&place, "Vispora.exe")]]);
    // The folder dialog: none without a window, then cancelled, then a folder.
    assert_eq!(rig.post("/api/browse-folder", "{}"), j("{\"ok\": false, \"err\": \"no webview window\"}"));
    let picked = std::sync::Arc::new(std::sync::Mutex::new(None::<String>));
    let (answer, started_in) = (picked.clone(), std::sync::Arc::new(std::sync::Mutex::new(String::new())));
    let seen = started_in.clone();
    *rig.updater.folder_hook.lock().unwrap() = Some(std::sync::Arc::new(move |start: &str| {
        *seen.lock().unwrap() = start.to_string();
        Ok(answer.lock().unwrap().clone())
    }));
    assert_eq!(rig.post("/api/browse-folder", "{}"), j("{\"ok\": true, \"cancelled\": true, \"path\": \"\"}"));
    assert_eq!(*started_in.lock().unwrap(), place, "the dialog opens where the install is");
    let elsewhere = paths::join(&rig.root, "Games");
    *picked.lock().unwrap() = Some(elsewhere.clone());
    assert_eq!(rig.post("/api/browse-folder", "{}")["path"].as_str(), Some(elsewhere.as_str()));
    assert_eq!(rig.state().target_exe, paths::join(&elsewhere, "Vispora.exe"));
    *rig.updater.folder_hook.lock().unwrap() = Some(std::sync::Arc::new(|_: &str| Err("the dialog failed".to_string())));
    assert_eq!(rig.post("/api/browse-folder", "{}"), j("{\"ok\": false, \"err\": \"the dialog failed\"}"));
}
