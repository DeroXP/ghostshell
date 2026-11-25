//! The exe swap never leaves an install without an exe: the cases of
//! tests/test_robustness_updates.py that are about the standalone updater,
//! under the same names.

#![cfg(windows)]

mod common;

use std::sync::{Arc, Condvar, Mutex, Weak};
use std::time::{Duration, Instant};

use common::*;
use vispora_updater_core::paths;
use vispora_updater_core::platform::Body;
use vispora_updater_core::Updater;

/// Is this the name of a backup ("<exe>.bak.<digits>")?
fn is_backup(path: &str) -> bool {
    path.rsplit_once(".bak.").is_some_and(|(_, n)| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()))
}

#[test]
fn no_backup_means_the_old_exe_is_left_alone() {
    let rig = Rig::setup("update", "GhostShell.exe", "stable", &[111]);
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    let target = rig.target.clone();
    *rig.disk.copier.lock().unwrap() = Some(Box::new(move |_from, to| {
        if is_backup(to) {
            std::fs::write(to, &old()[..1000]).unwrap(); // part of it, then the disk is full
            return Some(Err(io_error(28, "No space left on device")));
        }
        (to == target).then(|| Err(io_error(13, "The process cannot access the file")))
    }));
    assert_eq!(rig.run(), 7);
    assert!(paths::is_file(&rig.target) && rig.installed() == old(),
            "the old exe was deleted with no backup to roll back to, and the new one never arrived");
    assert!(rig.backups().is_empty(), "the partial backup is not left behind");
    assert!(rig.procs.launched().is_empty() && rig.steps()["install"] == "error");
    assert!(rig.error().contains("back up"));
    assert_eq!(rig.error(), "could not back up the old exe: No space left on device");
}

/// A disk on which the copy of the new exe into place waits until it is let
/// go.  Not for ever: a test that goes wrong before it lets go must end in a
/// failed check, not in a test run that never ends.
const HELD_AT_MOST: Duration = Duration::from_secs(20);

struct SlowSwap {
    in_swap: (Mutex<bool>, Condvar),
    release: (Mutex<bool>, Condvar),
}

impl SlowSwap {
    fn install(rig: &Rig) -> Arc<SlowSwap> {
        let gate = Arc::new(SlowSwap { in_swap: Default::default(), release: Default::default() });
        let (mine, target) = (gate.clone(), rig.target.clone());
        *rig.disk.copier.lock().unwrap() = Some(Box::new(move |from, to| {
            if to == target && from.ends_with(".part") {
                *mine.in_swap.0.lock().unwrap() = true;
                mine.in_swap.1.notify_all();
                let released = mine.release.0.lock().unwrap();
                let _ = mine.release.1.wait_timeout_while(released, HELD_AT_MOST, |let_go| !*let_go).unwrap();
            }
            None
        }));
        gate
    }

    fn wait_until_in_swap(&self, seconds: u64) -> bool {
        let guard = self.in_swap.0.lock().unwrap();
        *self.in_swap.1.wait_timeout_while(guard, Duration::from_secs(seconds), |reached| !*reached).unwrap().0
    }

    fn let_go(&self) {
        *self.release.0.lock().unwrap() = true;
        self.release.1.notify_all();
    }
}

#[test]
fn the_close_button_mid_swap_waits_for_the_swap() {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    let gate = SlowSwap::install(&rig);
    let _at_the_end = LetGoAtTheEnd(gate.clone());
    // (exit code, was the pipeline still running, was there an exe at the target)
    let exits: Arc<Mutex<Vec<(i32, bool, bool)>>> = Arc::default();
    let (seen, updater, target) = (exits.clone(), Arc::downgrade(&rig.updater), rig.target.clone());
    *rig.updater.exit_hook.lock().unwrap() = Some(Arc::new(move |code| {
        let running = updater.upgrade().is_some_and(|u| u.install_running());
        seen.lock().unwrap().push((code, running, paths::is_file(&target)));
    }));
    assert_eq!(rig.post("/api/start", "{}"), j("{\"ok\": true}"));
    assert!(gate.wait_until_in_swap(10), "the pipeline reached the swap");
    assert!(!paths::is_file(&rig.target), "(the old exe is out of the way, the new one not yet in)");
    assert_eq!(rig.post("/api/quit", "{}"), j("{\"ok\": true}"));
    std::thread::sleep(Duration::from_millis(500));
    assert!(exits.lock().unwrap().is_empty(), "the updater exited with neither the old exe nor the new one in place");
    gate.let_go();
    assert!(wait(|| !exits.lock().unwrap().is_empty(), 10.0), "it exits once the swap is done");
    assert_eq!(*exits.lock().unwrap(), [(0, false, true)]);
    assert_eq!(rig.installed(), new());
    assert!(rig.procs.launched().is_empty(), "the user closed it: the new build is not started behind their back");
    assert_eq!(rig.steps()["launch"], "skipped");
    *rig.updater.exit_hook.lock().unwrap() = None;
}

#[test]
fn closing_the_window_mid_swap_waits_for_the_swap() {
    // The window is gone (Alt+F4) while the swap runs.  What the window's
    // owner does then is shut_down(): it must not come back before the
    // swap is done, or the process would end with the exe half in place.
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    let gate = SlowSwap::install(&rig);
    let _at_the_end = LetGoAtTheEnd(gate.clone());
    assert_eq!(rig.updater.start_install(), Ok(()));
    assert!(gate.wait_until_in_swap(10));
    let released_at: Arc<Mutex<Option<Instant>>> = Arc::default();
    let (when, releaser) = (released_at.clone(), gate.clone());
    let timer = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(500));
        *when.lock().unwrap() = Some(Instant::now());
        releaser.let_go();
    });
    rig.updater.shut_down();
    let returned = Instant::now();
    timer.join().unwrap();
    let released = released_at.lock().unwrap().expect("the swap was let go");
    assert!(returned >= released, "the updater exited with the swap half done");
    assert!(!rig.updater.install_running());
    assert_eq!(rig.installed(), new());
}

// ── more than one close, and a start after a close ────────────────────
// Three ways the Python updater could not be made to exit in the middle of
// the swap, or to run two installs at once, because everybody who stops the
// install there waits on the same thread.  Here too, whoever asks and however
// often.

/// Lets the held swap go when a test ends, also when it ends in a failed
/// check: the pipeline would otherwise wait in it for ever.
struct LetGoAtTheEnd(Arc<SlowSwap>);

impl Drop for LetGoAtTheEnd {
    fn drop(&mut self) {
        self.0.let_go();
    }
}

/// What each call of the exit hook saw: was an install still running, and
/// was there an exe at the target.
fn watch_exits(rig: &Rig) -> Arc<Mutex<Vec<(bool, bool)>>> {
    let exits: Arc<Mutex<Vec<(bool, bool)>>> = Arc::default();
    let (seen, updater, target) = (exits.clone(), Arc::downgrade(&rig.updater), rig.target.clone());
    *rig.updater.exit_hook.lock().unwrap() = Some(Arc::new(move |_code| {
        let running = updater.upgrade().is_some_and(|u| u.install_running());
        seen.lock().unwrap().push((running, paths::is_file(&target)));
    }));
    exits
}

/// How often the update server was asked what it has: every pipeline asks.
fn times_looked(rig: &Rig) -> usize {
    rig.net.calls().iter().filter(|url| url.ends_with("/version/stable")).count()
}

/// An update that is being installed and is held inside the swap: the old
/// exe is out of the way and the new one is not in yet.
fn held_in_the_swap() -> (Rig, Arc<SlowSwap>, LetGoAtTheEnd, Arc<Mutex<Vec<(bool, bool)>>>) {
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    let gate = SlowSwap::install(&rig);
    let at_the_end = LetGoAtTheEnd(gate.clone());
    let exits = watch_exits(&rig);
    assert_eq!(rig.post("/api/start", "{}"), j("{\"ok\": true}"));
    assert!(gate.wait_until_in_swap(10), "the pipeline reached the swap");
    assert!(!paths::is_file(&rig.target));
    (rig, gate, at_the_end, exits)
}

#[test]
fn a_second_close_mid_swap_waits_for_the_swap_too() {
    // The X is pressed while the exe is being swapped, and then once more.
    let (rig, gate, _at_the_end, exits) = held_in_the_swap();
    assert_eq!(rig.post("/api/quit", "{}"), j("{\"ok\": true}"));
    std::thread::sleep(Duration::from_millis(200)); // (the first close is waiting for the swap by now)
    assert_eq!(rig.post("/api/quit", "{}"), j("{\"ok\": true}"));
    std::thread::sleep(Duration::from_millis(500));
    assert!(exits.lock().unwrap().is_empty(),
            "the second close ended the updater with neither the old exe nor the new one in place");
    assert!(!paths::is_file(&rig.target), "(the swap is still held)");
    gate.let_go();
    assert!(wait(|| exits.lock().unwrap().len() == 2, 10.0), "both closes go through once the swap is done");
    assert_eq!(*exits.lock().unwrap(), [(false, true), (false, true)]);
    assert_eq!(rig.installed(), new());
    *rig.updater.exit_hook.lock().unwrap() = None;
}

#[test]
fn the_window_closing_after_the_close_button_waits_for_the_swap_too() {
    // The X during the swap, then Alt+F4.  The window is gone, and what its
    // owner does before the process ends is shut_down() (app/src/main.rs).
    let (rig, gate, _at_the_end, exits) = held_in_the_swap();
    assert_eq!(rig.post("/api/quit", "{}"), j("{\"ok\": true}"));
    std::thread::sleep(Duration::from_millis(200));
    // Was the exe in place when the owner was let go on to end the process?
    let let_go_on: Arc<Mutex<Option<bool>>> = Arc::default();
    let (updater, target, seen) = (rig.updater.clone(), rig.target.clone(), let_go_on.clone());
    let owner = std::thread::spawn(move || {
        updater.shut_down();
        *seen.lock().unwrap() = Some(paths::is_file(&target));
    });
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(*let_go_on.lock().unwrap(), None,
               "the process was let go on to its end with neither the old exe nor the new one in place");
    assert!(exits.lock().unwrap().is_empty());
    gate.let_go();
    owner.join().unwrap();
    assert_eq!(*let_go_on.lock().unwrap(), Some(true));
    assert!(wait(|| exits.lock().unwrap().len() == 1, 10.0));
    assert_eq!(*exits.lock().unwrap(), [(false, true)]);
    assert_eq!(rig.installed(), new());
    *rig.updater.exit_hook.lock().unwrap() = None;
}

#[test]
fn install_cannot_be_started_while_the_updater_is_closing() {
    // Cancel while the pipeline is under way closes the updater; the window
    // stays for as long as that waits, and its Install button can be pressed.
    let (rig, gate, _at_the_end, exits) = held_in_the_swap();
    let looked = times_looked(&rig);
    assert_eq!(rig.post("/api/quit", "{}"), j("{\"ok\": true}"));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(rig.post("/api/start", "{}"), j("{\"ok\": false, \"err\": \"install already running\"}"),
               "a second install was started beside the first");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(times_looked(&rig), looked, "a second pipeline is running beside the first");
    assert!(rig.state().cancelled, "the close was taken back");
    assert!(exits.lock().unwrap().is_empty());
    gate.let_go();
    assert!(wait(|| exits.lock().unwrap().len() == 1, 10.0), "it exits once the swap is done");
    assert_eq!(*exits.lock().unwrap(), [(false, true)]);
    assert_eq!(rig.installed(), new());
    // Between the end of the install and the end of the process nothing new
    // is begun either: the process is about to go.
    assert_eq!(rig.post("/api/start", "{}"), j("{\"ok\": false, \"err\": \"the updater is closing\"}"));
    assert!(!rig.updater.install_running());
    assert_eq!(times_looked(&rig), looked);
    *rig.updater.exit_hook.lock().unwrap() = None;
}

#[test]
fn everybody_who_stops_an_install_waits_for_it() {
    // Not two callers but eight, from eight threads, in no particular order.
    let (rig, gate, _at_the_end, _exits) = held_in_the_swap();
    let back: Arc<Mutex<Vec<bool>>> = Arc::default();
    let callers: Vec<_> = (0..8)
        .map(|n| {
            let (updater, target, seen) = (rig.updater.clone(), rig.target.clone(), back.clone());
            std::thread::spawn(move || {
                if n % 2 == 0 {
                    updater.stop_install();
                } else {
                    updater.shut_down();
                }
                seen.lock().unwrap().push(paths::is_file(&target));
            })
        })
        .collect();
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(*back.lock().unwrap(), Vec::<bool>::new(), "somebody came back while the swap was held");
    gate.let_go();
    for caller in callers {
        caller.join().unwrap();
    }
    assert_eq!(*back.lock().unwrap(), [true; 8], "everybody came back to an exe in place");
    assert!(!rig.updater.install_running());
    *rig.updater.exit_hook.lock().unwrap() = None;
}

#[test]
fn a_new_exe_that_will_not_start_is_rolled_back() {
    for case in ["blocked", "quarantined", "restore fails"] {
        let rig = Rig::update();
        rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
        let started: Arc<Mutex<Vec<Option<Vec<u8>>>>> = Arc::default();
        let seen = started.clone();
        *rig.procs.spawner.lock().unwrap() = Some(Box::new(move |args| {
            let content = std::fs::read(&args[0]).ok();
            seen.lock().unwrap().push(content.clone());
            if content == Some(new()) {
                if case == "quarantined" {
                    std::fs::remove_file(&args[0]).unwrap(); // antivirus took the new exe away
                }
                return Err("Operation did not complete successfully because the file contains a virus".to_string());
            }
            Ok(())
        }));
        *rig.disk.copier.lock().unwrap() = Some(Box::new(move |from, _to| {
            (case == "restore fails" && is_backup(from)).then(|| Err(io_error(13, "Access is denied")))
        }));
        assert_eq!(rig.run(), 8, "{case}");
        let err = rig.error();
        assert_eq!(rig.backups().len(), 1, "{case}: the backup is kept");
        let started = started.lock().unwrap().clone();
        if case == "restore fails" {
            assert_eq!(started, [Some(new())], "{case}");
            assert!(err.contains("saved as") && err.contains(&rig.backups()[0]), "{case}: {err}");
            continue;
        }
        assert!(paths::is_file(&rig.target) && rig.installed() == old(), "{case}: the previous version is not back");
        assert_eq!(started, [Some(new()), Some(old())], "{case}: the previous version was not started");
        assert!(err.contains("put back and started"), "{err}");
        assert!(rig.steps()["install"] == "error" && rig.steps()["launch"] == "error");
        assert_eq!(rig.step_detail("install"), "rolled back: the new version would not start");
    }
}

#[test]
fn a_new_exe_that_will_not_start_and_has_no_backup_says_where_it_is() {
    // A fresh install: nothing to go back to.
    let rig = Rig::setup("install", "Vispora.exe", "stable", &[]);
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    *rig.procs.spawner.lock().unwrap() = Some(Box::new(|_| Err("Access is denied".to_string())));
    assert_eq!(rig.run(), 8);
    assert_eq!(rig.error(), format!(
        "Install OK but couldn't start the new Vispora: Access is denied.  Launch it manually from {}.", rig.target));
    assert!(rig.installed() == new() && rig.steps()["install"] == "done" && rig.steps()["launch"] == "error");
}

/// A download that never ends, during which the user clicks Cancel.
struct Endless {
    reads: usize,
    updater: Weak<Updater>,
}

impl Body for Endless {
    fn content_length(&self) -> Option<u64> {
        Some(new().len() as u64)
    }
    fn read(&mut self, buf: &mut [u8]) -> Result<usize, String> {
        self.reads += 1;
        if self.reads == 2 {
            if let Some(updater) = self.updater.upgrade() {
                updater.lock().cancelled = true; // the user clicks Cancel mid-download
            }
        }
        let piece = &new()[..65536];
        buf[..piece.len()].copy_from_slice(piece);
        Ok(piece.len())
    }
}

#[test]
fn a_cancelled_download_leaves_no_part_file() {
    let rig = Rig::update();
    let updater = Arc::downgrade(&rig.updater);
    *rig.net.opener.lock().unwrap() = Some(Box::new(move |_url: &str| {
        Some(Ok(Box::new(Endless { reads: 0, updater: updater.clone() }) as Box<dyn Body>))
    }));
    let dest = paths::join(&rig.updater.paths.updater_dir, "Vispora-3.6.1.exe");
    assert_eq!(rig.updater.download(&format!("{SERVER}/download/stable"), &dest, 0), Err("cancelled".to_string()));
    assert!(!paths::is_file(&format!("{dest}.part")), "a cancelled download used to stay on disk for good");
}

#[test]
fn a_download_that_breaks_off_leaves_no_part_file() {
    struct Broken(usize);
    impl Body for Broken {
        fn content_length(&self) -> Option<u64> {
            None
        }
        fn read(&mut self, buf: &mut [u8]) -> Result<usize, String> {
            self.0 += 1;
            if self.0 > 3 {
                return Err("the download was interrupted: Windows error 12030 (the connection was closed)".into());
            }
            buf[..4096].fill(7);
            Ok(4096)
        }
    }
    let rig = Rig::update();
    rig.net.publish("stable", "3.6.1", &new(), None, SERVER);
    *rig.net.opener.lock().unwrap() =
        Some(Box::new(|url: &str| url.ends_with("/download/stable").then(|| Ok(Box::new(Broken(0)) as Box<dyn Body>))));
    assert_eq!(rig.run(), 5);
    assert!(rig.error().starts_with("Download failed: the download was interrupted"), "{}", rig.error());
    assert!(rig.part_files().is_empty() && rig.installed() == old() && rig.backups().is_empty());
    assert_eq!(rig.steps()["download"], "error");
}

#[test]
fn a_version_from_the_server_cannot_point_the_download_elsewhere() {
    // The staged file is named after the version.  A version with a path in
    // it must not get the download written outside the updater's folder
    // (here it would have landed three folders up, beside the data folder).
    let rig = Rig::update();
    rig.net.publish("stable", "9.0.0\\..\\..\\..\\escaped", &new(), None, SERVER);
    let seen: Arc<Mutex<Vec<String>>> = Arc::default();
    let staged = seen.clone();
    *rig.disk.copier.lock().unwrap() = Some(Box::new(move |from, _to| {
        staged.lock().unwrap().push(from.to_string());
        None
    }));
    assert_eq!(rig.run(), 0);
    let roaming = paths::join(&rig.root, "Roaming");
    let beside: Vec<_> = std::fs::read_dir(&roaming).unwrap().flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect();
    assert_eq!(beside, ["Vispora"], "nothing was written outside the app's data folder");
    let part = seen.lock().unwrap().iter().find(|f| f.ends_with(".part")).cloned().expect("the download was staged");
    assert_eq!(part, paths::join(&rig.updater.paths.updater_dir, "Vispora-9.0.0_.._.._.._escaped.exe.part"));
    assert!(rig.installed() == new() && rig.part_files().is_empty());
}
