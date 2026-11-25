//! Where the backend runs from.
//!
//! The exe carries its backend as a pack (pack.rs) and unpacks it before it
//! starts it.  Where it is unpacked decides whether a program without
//! administrator rights can get code of its own run with them, so the
//! rules are the window's folder's, and stricter:
//!
//! * never into a place such a program can write to or prepare (not the
//!   person's temporary folder, not their AppData);
//! * the shell runs a backend only from a folder it made itself in this
//!   run, or from one it has checked: its own making by owner and access
//!   list, and holding exactly what the exe carries, file by file;
//! * a folder that was checked is held from before the check until the
//!   run is over, so that the folder under the name cannot be exchanged
//!   between the check and the start (or a later start, after a backend
//!   that died);
//! * a folder with the right name that does not pass is never used, and
//!   never put right in place;
//! * one that is not this program's own making is never touched at all:
//!   it is left as it is, and the backend is unpacked somewhere else for
//!   this run;
//! * one that IS this program's own making (by owner and access list) and
//!   does not hold what the exe carries is taken away whole and made
//!   again, if no copy of the app is using it.  That is the same act, on
//!   the same evidence, as taking away another version's folder.  It is
//!   not left: nothing but the log would ever say that this version now
//!   unpacks at every start, and a virus scanner that took one file away
//!   would be enough to cause it;
//! * only folders of this program's own making are ever taken away.
//!
//! There are two ways, and which one a build uses is one switch:
//!
//! * `EveryStart`: unpacked at every start into a folder for that run,
//!   which is taken away at the quit.  Nothing stays on the PC.
//! * `OncePerVersion`: unpacked once into a folder that stays, and checked
//!   and used again at later starts.  Folders of other versions are taken
//!   away.  Files stay on the PC when the exe is deleted.
//!
//! Both are measured in shell_rs/app/try_it.py; the choice is the owner's.

use std::time::Instant;

use crate::pack::Pack;
use crate::win::{self, OpenTo, OwnFolder};

/// How the folders that stay begin.
pub const KEPT_FOLDER_PREFIX: &str = "Vispora-backend-kept-";
/// How the name of one ends while it is being made.
const BEING_MADE: &str = ".making";

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Way {
    EveryStart,
    OncePerVersion,
}

/// Where the backend may be unpacked.
pub struct Places {
    /// For one run (Windows' own folder for temporary files, in the app as
    /// it ships): a folder is made here, marked as in use, and taken away.
    pub for_one_run: String,
    /// For a folder that stays.  None where there is no such place.
    pub kept: Option<String>,
    /// Whose the folders are: administrators' in the app as it ships.
    pub open_to: OpenTo,
}

/// A backend that can be started.
#[derive(Debug)]
pub struct Ready {
    /// The program to start.
    pub program: String,
    /// The folder it is in.
    pub folder: String,
    /// The folder, where it is this run's own: to be taken away at the quit.
    pub own: Option<OwnFolder>,
    /// The name of the folder that stays, where one is used.
    pub kept: Option<String>,
    /// The folder that stays, held under its name (win::hold_own_folder):
    /// to be kept for as long as a backend may be started from it.  The
    /// folder that was checked is then the folder that is run from.
    pub held: Option<std::fs::File>,
    /// What there is to say about it, for the log.
    pub said: Vec<String>,
}

/// What became of the folders that stay and are not this start's
/// (remove_other_kept).
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct Others {
    /// Taken away.
    pub removed: usize,
    /// Left as they are, for now: held open (as by a copy of the app that
    /// runs from one), being made at this moment, or with something in
    /// them that Windows would not let go of.  Which program it is that
    /// holds a folder or a file in it is not known here.
    pub left: usize,
    /// Left as they are: of the name, but not of this program's making.
    pub not_ours: usize,
}

/// The name of the folder that stays for this pack: the version it is for,
/// and the beginning of the pack's own mark, so that two packs of one
/// version are never taken for each other.
pub fn kept_name(pack: &Pack) -> String {
    let version: String = pack.version.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '.').take(24).collect();
    format!("{KEPT_FOLDER_PREFIX}{version}-{}", &pack.id[..16])
}

/// Make the backend ready to be started.
pub fn make_ready(pack: &Pack, way: Way, places: &Places) -> Result<Ready, String> {
    let mut said = Vec::new();
    if way == Way::OncePerVersion {
        match &places.kept {
            Some(kept_in) => match kept(pack, kept_in, places.open_to, &mut said) {
                Ok((folder, held)) => {
                    return Ok(Ready { program: format!("{folder}\\{}", pack.start), folder, own: None, kept: Some(kept_name(pack)), held: Some(held), said });
                }
                Err(why) => said.push(format!("the folder that stays is not used: {why}. Unpacked for this run instead.")),
            },
            None => said.push("there is no place for a folder that stays on this PC. Unpacked for this run instead.".to_string()),
        }
    }
    let began = Instant::now();
    let own = win::private_folder_named(&places.for_one_run, win::BACKEND_FOLDER_PREFIX, places.open_to)?;
    if let Err(why) = pack.unpack_into(&own.path) {
        own.remove();
        return Err(why);
    }
    said.push(format!("the background program was unpacked for this run: {} files, {} bytes, {} ms, into {}",
                      pack.files.len(), pack.size(), began.elapsed().as_millis(), own.path));
    Ok(Ready { program: format!("{}\\{}", own.path, pack.start), folder: own.path.clone(), own: Some(own), kept: None, held: None, said })
}

/// The folder that stays, checked or made, and held under its name.
fn kept(pack: &Pack, kept_in: &str, open_to: OpenTo, said: &mut Vec<String>) -> Result<(String, std::fs::File), String> {
    let folder = format!("{kept_in}\\{}", kept_name(pack));
    let began = Instant::now();
    let check = |folder: &str| -> Result<std::fs::File, String> {
        // Whose it is first, and held from then on: only in a folder of
        // this program's own making does what the files hold now say
        // anything about what they will hold when the program in it is
        // started, and only while it is held is the folder under the name
        // the folder that was looked at.
        let held = win::hold_own_folder(folder, open_to)?;
        pack.same_as(folder, &[])?;
        Ok(held)
    };
    if std::fs::symlink_metadata(&folder).is_ok() {
        // Whose it is.  One that is not this program's own making is the
        // end of it: not used, not touched.
        let held = win::hold_own_folder(&folder, open_to)?;
        match pack.same_as(&folder, &[]) {
            Ok(()) => {
                said.push(format!("the background program is in {folder} from an earlier start, and is what the exe carries: {} files, {} bytes, checked in {} ms",
                                  pack.files.len(), pack.size(), began.elapsed().as_millis()));
                return Ok((folder, held));
            }
            // This program's own making, and not what the exe carries.  It
            // is never put right in place.  It is taken away whole, if no
            // copy is using it, and made again below like one that was
            // never there.
            Err(why) => {
                drop(held);
                take_away(&folder, open_to).map_err(|kept| format!("{why}, and it could not be taken away to be made again ({kept})"))?;
                said.push(format!("the folder that stays did not hold what the exe carries ({why}): it was this program's own, nobody was using it, and it was taken away to be made again"));
            }
        }
    }
    // Made under another name and given its own when it is whole: a start
    // that is cut short never leaves half a folder under the name.
    let mut random = [0u8; 8];
    crate::platform::Platform::random(&*win::platform(), &mut random)?;
    let making = format!("{folder}.{}{BEING_MADE}", random.iter().map(|byte| format!("{byte:02x}")).collect::<String>());
    win::make_closed_folder(&making, open_to)?;
    if let Err(why) = pack.unpack_into(&making).and_then(|()| win::move_without_replacing(&making, &folder)) {
        let _ = std::fs::remove_dir_all(&making);
        // (another start, in another session, may have made it meanwhile:
        // then that one is held against the exe like any other)
        if std::fs::symlink_metadata(&folder).is_ok() {
            let held = check(&folder)?;
            return Ok((folder, held));
        }
        return Err(why);
    }
    // It has its name, and nothing was there under it.  Held, and held
    // against the exe, like one that was found: no backend is started from
    // a folder that stays without both, however the folder came to be.
    let held = check(&folder)?;
    said.push(format!("the background program was unpacked into {folder}, to stay: {} files, {} bytes, {} ms",
                      pack.files.len(), pack.size(), began.elapsed().as_millis()));
    Ok((folder, held))
}

/// Take away the folders that stay, all but the one named `but` (None: all
/// of them, for a build that keeps none).
///
/// Only a folder of this program's own making is taken away, and only one
/// that no copy of the app is using.  Both are asked of Windows in ways it
/// answers for certain (win::open_to_remove): a copy that uses a folder
/// holds it, and a folder that is held cannot be asked for to be removed;
/// and while this has asked for one, no copy can come to hold it.  A
/// folder with the name that somebody else made is left exactly as it is:
/// what is in it, and what its name leads to by now, is theirs to decide.
///
/// Two more cases.  A backend whose shell is gone, and that has not ended
/// yet: nobody holds its folder, but a program that is running cannot be
/// deleted, so the folder's programs are tried first, and a folder whose
/// program Windows will not delete is left as it is.  And a folder that a
/// copy in another session is unpacking into at this very moment: it looks
/// like one that a start was cut short in, and is left alone until it is
/// old enough to be the second (as a window's folder without its mark is).
pub fn remove_other_kept(kept_in: &str, but: Option<&str>, open_to: OpenTo) -> Others {
    let mut others = Others::default();
    let Ok(entries) = std::fs::read_dir(kept_in) else { return others };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with(KEPT_FOLDER_PREFIX) || but.is_some_and(|but| name.eq_ignore_ascii_case(but)) {
            continue;
        }
        if name.ends_with(BEING_MADE) && !made_long_ago(&entry) {
            others.left += 1;
            continue;
        }
        match take_away(&entry.path().to_string_lossy(), open_to) {
            Ok(()) => others.removed += 1,
            Err(NotTaken::NotOurs) => others.not_ours += 1,
            Err(NotTaken::Held | NotTaken::WouldNotGo) => others.left += 1,
        }
    }
    others
}

/// Why a folder was not taken away: what is known, and no more.  (Who
/// holds a folder, or a file in it, Windows does not say: a copy of the
/// app that runs from it, a backend that has not ended yet, or a virus
/// scanner that is reading a file.)
#[derive(Debug, PartialEq)]
enum NotTaken {
    /// Not of this program's making: never touched.
    NotOurs,
    /// Some program holds the folder open.  Nothing in it was touched.
    Held,
    /// Windows would not let go of something in it: a program in it that
    /// is running, or a file that some program holds.
    WouldNotGo,
}

impl std::fmt::Display for NotTaken {
    fn fmt(&self, to: &mut std::fmt::Formatter) -> std::fmt::Result {
        to.write_str(match self {
            NotTaken::NotOurs => "it is not this program's folder",
            NotTaken::Held => "a program holds the folder open",
            NotTaken::WouldNotGo => "Windows would not let go of something in it",
        })
    }
}

/// Take the folder `path` away, whole: if it is of this program's own
/// making, no copy of the app holds it, and no program in it is running.
/// Whose it is, is asked of the folder that was opened, which stays open
/// until it is gone (win::open_to_remove).
fn take_away(path: &str, open_to: OpenTo) -> Result<(), NotTaken> {
    match win::open_to_remove(path, open_to) {
        win::ToRemove::NotOurs => Err(NotTaken::NotOurs),
        win::ToRemove::InUse => Err(NotTaken::Held),
        win::ToRemove::Ours(going) => {
            let gone = win::programs_let_go(std::path::Path::new(path)) && std::fs::remove_dir_all(path).is_ok();
            drop(going);
            if gone {
                Ok(())
            } else {
                Err(NotTaken::WouldNotGo)
            }
        }
    }
}

/// Was this made so long ago that no start can still be making it?
fn made_long_ago(entry: &std::fs::DirEntry) -> bool {
    let made = entry.metadata().ok().and_then(|found| found.created().ok());
    made.and_then(|made| made.elapsed().ok()).is_some_and(|age| age >= win::MARK_EXPECTED_WITHIN)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pack::tests::{a_backend, a_place};
    use crate::pack::{make, Way as Squeezed};

    fn places(place: &str) -> Places {
        // (the place for folders that stay is there already, as it is on a
        // PC: the program never makes that place itself)
        std::fs::create_dir_all(format!("{place}\\kept")).unwrap();
        Places { for_one_run: format!("{place}\\runs"), kept: Some(format!("{place}\\kept")), open_to: OpenTo::ThisAccount }
    }

    fn there(path: &str) -> bool {
        std::path::Path::new(path).exists()
    }

    fn kept_folders(place: &str) -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(format!("{place}\\kept")).map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect()).unwrap_or_default();
        names.sort();
        names
    }

    /// Whoever it was closed to, away with a test's folder.
    fn tidy(path: &str) {
        let _ = std::process::Command::new("icacls.exe").args([path, "/reset", "/t", "/c", "/q"]).output();
        let _ = std::fs::remove_dir_all(path);
        assert!(!there(path), "a test left {path} behind");
    }

    #[test]
    fn unpacked_at_every_start_into_a_folder_of_that_run_and_gone_after_it() {
        let place = a_place("every-start");
        let bytes = make(&a_backend(&place), "3.8.1", "VisporaBackend.exe", Squeezed::Lzms).unwrap();
        let pack = Pack::read(&bytes).unwrap();
        let (first, second) = (make_ready(&pack, Way::EveryStart, &places(&place)).unwrap(), make_ready(&pack, Way::EveryStart, &places(&place)).unwrap());
        assert_ne!(first.folder, second.folder, "two runs in one folder");
        for ready in [&first, &second] {
            assert!(ready.folder.starts_with(&format!("{place}\\runs\\{}", win::BACKEND_FOLDER_PREFIX)) && ready.kept.is_none());
            assert_eq!(ready.program, format!("{}\\VisporaBackend.exe", ready.folder));
            assert_eq!(std::fs::read(&ready.program).unwrap(), b"MZ not really a program");
            // The run's own making: closed, marked as in use, and holding
            // what the exe carries and its own mark, nothing else.
            assert_eq!(win::is_closed_to_all_but(&ready.folder, OpenTo::ThisAccount), Ok(()));
            assert_eq!(pack.same_as(&ready.folder, &[win::IN_USE_MARK]), Ok(()));
            assert!(pack.same_as(&ready.folder, &[]).is_err());
            assert!(ready.said.iter().any(|line| line.contains("unpacked for this run: 5 files")), "{:?}", ready.said);
        }
        // A start does not take a running copy's backend away (the mark),
        // and takes away what a run that was ended left behind.
        let left = second.folder.clone();
        drop(second);
        let tidied = win::remove_private_folders(&format!("{place}\\runs"), OpenTo::ThisAccount);
        assert_eq!((tidied.removed, tidied.left), (1, 1));
        assert!(!there(&left) && there(&first.program));
        // At the quit it goes.
        let was = first.folder.clone();
        assert!(first.own.unwrap().remove() && !there(&was));
        assert!(kept_folders(&place).is_empty(), "nothing stays");
        // What cannot be unpacked (a byte in the exe is another byte) is
        // not started from, and no folder is left behind for it, either way.
        let mut spoiled = bytes.clone();
        let last = spoiled.len() - 3;
        spoiled[last] ^= 0x55;
        let spoiled = Pack::read(&spoiled).unwrap();
        for way in [Way::EveryStart, Way::OncePerVersion] {
            let why = make_ready(&spoiled, way, &places(&place)).unwrap_err();
            assert!(why.contains("_internal\\python312.dll"), "{why}");
            for left in ["runs", "kept"] {
                assert!(std::fs::read_dir(format!("{place}\\{left}")).unwrap().next().is_none(), "{way:?}: something was left in {left}");
            }
        }
        tidy(&place);
    }

    #[test]
    fn unpacked_once_per_version_and_checked_before_it_is_used_again() {
        let place = a_place("once");
        let bytes = make(&a_backend(&place), "3.8.1", "VisporaBackend.exe", Squeezed::XpressHuff).unwrap();
        let pack = Pack::read(&bytes).unwrap();
        let name = kept_name(&pack);
        assert_eq!(name, format!("Vispora-backend-kept-3.8.1-{}", &pack.id[..16]));
        let first = make_ready(&pack, Way::OncePerVersion, &places(&place)).unwrap();
        assert_eq!((first.folder.as_str(), first.kept.as_deref(), first.own.is_none()), (format!("{place}\\kept\\{name}").as_str(), Some(name.as_str()), true));
        assert!(first.said.iter().any(|line| line.contains("to stay: 5 files")), "{:?}", first.said);
        assert_eq!(win::is_closed_to_all_but(&first.folder, OpenTo::ThisAccount), Ok(()));
        assert_eq!(pack.same_as(&first.folder, &[]), Ok(()), "exactly what the exe carries: no mark, nothing half made");
        assert_eq!(kept_folders(&place), [name.clone()]);
        // The next start (a second copy, here: the first is still there)
        // finds it, checks it and uses it: nothing is unpacked.
        let again = make_ready(&pack, Way::OncePerVersion, &places(&place)).unwrap();
        assert_eq!(again.folder, first.folder);
        assert!(again.said.iter().any(|line| line.contains("from an earlier start, and is what the exe carries")), "{:?}", again.said);
        assert!(std::fs::read_dir(format!("{place}\\runs")).map_or(true, |mut d| d.next().is_none()), "it was unpacked a second time");
        // Whoever uses it holds it: from the check on, the folder under the
        // name is the folder that was checked, until the last copy is gone.
        let elsewhere = format!("{}-elsewhere", first.folder);
        assert!(first.held.is_some() && again.held.is_some() && first.own.is_none());
        assert!(std::fs::rename(&first.folder, &elsewhere).is_err(), "it could be given another name while it was in use");
        drop(first);
        assert!(std::fs::rename(&again.folder, &elsewhere).is_err(), "it could be given another name while the second copy used it");
        assert_eq!(remove_other_kept(&format!("{place}\\kept"), None, OpenTo::ThisAccount), Others { removed: 0, left: 1, not_ours: 0 });
        assert_eq!(pack.same_as(&again.folder, &[]), Ok(()), "a folder in use was touched");
        drop(again);
        tidy(&place);
    }

    /// A start that does not use the folder that stays: it unpacks for the
    /// run, says why, and runs what the exe carries.
    fn used_instead(pack: &Pack, place: &str, why: &str) {
        let ready = make_ready(pack, Way::OncePerVersion, &places(place)).unwrap();
        assert!(ready.folder.starts_with(&format!("{place}\\runs\\")) && ready.kept.is_none() && ready.held.is_none(), "{why}: {} was used", ready.folder);
        assert!(ready.said.iter().any(|line| line.starts_with("the folder that stays is not used: ") && line.contains(why)), "{why}: {:?}", ready.said);
        assert_eq!(std::fs::read(&ready.program).unwrap(), b"MZ not really a program", "what is run is the exe's");
        assert!(ready.own.unwrap().remove());
    }

    #[test]
    fn a_folder_that_somebody_else_made_is_never_used_and_never_touched() {
        let place = a_place("somebody-elses");
        let bytes = make(&a_backend(&place), "3.8.1", "VisporaBackend.exe", Squeezed::Mszip).unwrap();
        let pack = Pack::read(&bytes).unwrap();
        let folder = format!("{place}\\kept\\{}", kept_name(&pack));
        // Somebody made a folder of that name first: with the right files
        // in it, even.  It is not this program's making, at this start and
        // at every later one.
        std::fs::create_dir_all(&folder).unwrap();
        pack.unpack_into(&folder).unwrap();
        for _ in 0..2 {
            used_instead(&pack, &place, "is not this program's folder");
            assert_eq!(pack.same_as(&folder, &[]), Ok(()), "it was touched");
            assert!(win::is_closed_to_all_but(&folder, OpenTo::ThisAccount).is_err(), "it was made the program's own in place");
        }
        // With the wrong files in it: the same.  It is not taken away to be
        // made again, as one of this program's own would be.
        let program = format!("{folder}\\VisporaBackend.exe");
        std::fs::write(&program, b"MZ somebody else's program").unwrap();
        std::fs::write(format!("{folder}\\_internal\\version.dll"), b"planted").unwrap();
        used_instead(&pack, &place, "is not this program's folder");
        assert_eq!(std::fs::read(&program).unwrap(), b"MZ somebody else's program", "somebody else's folder was touched");
        assert!(there(&format!("{folder}\\_internal\\version.dll")));
        // No place for a folder that stays: for this run, then.
        let nowhere = Places { kept: None, ..places(&place) };
        let ready = make_ready(&pack, Way::OncePerVersion, &nowhere).unwrap();
        assert!(ready.own.is_some() && ready.said[0].starts_with("there is no place for a folder that stays"));
        ready.own.unwrap().remove();
        tidy(&place);
    }

    #[test]
    fn a_folder_of_this_programs_own_that_is_not_what_the_exe_carries_is_made_again() {
        use std::os::windows::fs::OpenOptionsExt as _;
        let place = a_place("made-again");
        let bytes = make(&a_backend(&place), "3.8.1", "VisporaBackend.exe", Squeezed::Mszip).unwrap();
        let pack = Pack::read(&bytes).unwrap();
        let folder = format!("{place}\\kept\\{}", kept_name(&pack));
        let program = format!("{folder}\\VisporaBackend.exe");
        let nothing_for_the_run = || std::fs::read_dir(format!("{place}\\runs")).map_or(true, |mut d| d.next().is_none());
        // (a virus scanner taking one file away is the likeliest of these)
        let spoiled = [
            ("another program in place of the backend's", "VisporaBackend.exe is not as long as the exe's"),
            ("a file too many", "_internal\\version.dll is in the folder and is not the exe's"),
            ("a file gone", "_internal\\empty.txt is missing from the folder"),
        ];
        for (what, why) in spoiled {
            // This program's own making, by a start whose copy has ended.
            drop(make_ready(&pack, Way::OncePerVersion, &places(&place)).unwrap());
            match what {
                "a file too many" => std::fs::write(format!("{folder}\\_internal\\version.dll"), b"planted").unwrap(),
                "a file gone" => std::fs::remove_file(format!("{folder}\\_internal\\empty.txt")).unwrap(),
                _ => std::fs::write(&program, b"MZ somebody else's program").unwrap(),
            }
            // The next start: it is taken away whole and made again, and
            // that is what the backend is run from.  Never put right in
            // place: what was in it is gone with it.
            let ready = make_ready(&pack, Way::OncePerVersion, &places(&place)).unwrap();
            assert_eq!((ready.folder.as_str(), ready.held.is_some(), ready.own.is_none()), (folder.as_str(), true, true), "{what}");
            let said = ready.said.join("\n");
            assert!(said.contains("did not hold what the exe carries") && said.contains(why) && said.contains("taken away to be made again"), "{what}: {said}");
            assert!(said.contains("to stay: 5 files"), "{what}: {said}");
            assert_eq!(pack.same_as(&folder, &[]), Ok(()), "{what}");
            assert_eq!(win::is_closed_to_all_but(&folder, OpenTo::ThisAccount), Ok(()));
            assert!(nothing_for_the_run(), "{what}: it was unpacked for the run as well");
            // ... and the start after that finds it as it should be.
            drop(ready);
            let again = make_ready(&pack, Way::OncePerVersion, &places(&place)).unwrap();
            assert!(again.said.iter().any(|line| line.contains("from an earlier start, and is what the exe carries")), "{:?}", again.said);
        }

        // A copy is running from it (the folder was right when that copy
        // started): the folder is left exactly as it is, and this start
        // unpacks for the run.
        let a_copy = make_ready(&pack, Way::OncePerVersion, &places(&place)).unwrap();
        std::fs::write(&program, b"MZ somebody else's program").unwrap();
        used_instead(&pack, &place, "could not be taken away to be made again (a program holds the folder open)");
        assert_eq!(std::fs::read(&program).unwrap(), b"MZ somebody else's program", "a folder that a copy holds was touched");
        drop(a_copy);
        // A backend whose shell is gone still runs in it (as Windows holds
        // a program that is running): left as it is, too, with what comes
        // before the program when the folder is read.
        std::fs::write(format!("{folder}\\0-comes-first.txt"), b"still needed").unwrap();
        let running = std::fs::OpenOptions::new().read(true).share_mode(1).open(&program).unwrap();
        used_instead(&pack, &place, "could not be taken away to be made again (Windows would not let go of something in it)");
        assert!(there(&format!("{folder}\\0-comes-first.txt")) && there(&format!("{folder}\\_internal\\python312.dll")), "a folder whose program runs was taken apart");
        // Once it has ended, the next start makes the folder again.
        drop(running);
        let ready = make_ready(&pack, Way::OncePerVersion, &places(&place)).unwrap();
        assert_eq!((ready.folder.as_str(), pack.same_as(&folder, &[])), (folder.as_str(), Ok(())));
        drop(ready);
        tidy(&place);
    }

    #[test]
    fn another_versions_folder_is_taken_away_unless_a_copy_is_using_it() {
        use std::os::windows::fs::OpenOptionsExt as _;
        let place = a_place("versions");
        let folder = a_backend(&place);
        let packed = |version: &str| make(&folder, version, "VisporaBackend.exe", Squeezed::Lzms).unwrap();
        let (old, older, oldest) = (packed("3.8.0"), packed("3.7.0"), packed("3.6.0"));
        std::fs::write(format!("{folder}\\_internal\\new.txt"), b"new in this version").unwrap();
        let new = packed("3.8.1");
        let (old, older, oldest, new) = (Pack::read(&old).unwrap(), Pack::read(&older).unwrap(), Pack::read(&oldest).unwrap(), Pack::read(&new).unwrap());
        let kept_in = format!("{place}\\kept");
        let others = |but: Option<&str>| remove_other_kept(&kept_in, but, OpenTo::ThisAccount);
        // Three versions before this one.  One whose copy has ended.  One
        // whose copy is running: it holds its folder.  And one whose shell
        // is gone and whose backend has not ended yet: nobody holds the
        // folder, and Windows holds the program (as it does one that runs).
        let was = make_ready(&old, Way::OncePerVersion, &places(&place)).unwrap().folder;
        let in_use = make_ready(&older, Way::OncePerVersion, &places(&place)).unwrap();
        let orphan = make_ready(&oldest, Way::OncePerVersion, &places(&place)).unwrap();
        let (orphans, its_program) = (orphan.folder.clone(), orphan.program.clone());
        drop(orphan);
        let running = std::fs::OpenOptions::new().read(true).share_mode(1).open(&its_program).unwrap();
        // (something in that folder that Windows would let go of, and that
        // comes before the program when the folder is read)
        std::fs::write(format!("{orphans}\\0-comes-first.txt"), b"still needed").unwrap();
        // One that a start was cut short in, long ago; one that looks the
        // same and is new (a copy in another session may be unpacking into
        // it at this moment); one of the name that somebody else made; and
        // something that is not of the name at all.
        let half_made = format!("{kept_in}\\{}.0123456789abcdef.making", kept_name(&new));
        let being_made = format!("{}.fedcba9876543210.making", kept_name(&new));
        for folder in [half_made.clone(), format!("{kept_in}\\{being_made}")] {
            win::make_closed_folder(&folder, OpenTo::ThisAccount).unwrap();
            std::fs::write(format!("{folder}\\VisporaBackend.exe"), b"MZ half of it").unwrap();
        }
        {
            use std::os::windows::fs::FileTimesExt as _;
            let long_ago = std::time::SystemTime::now() - win::MARK_EXPECTED_WITHIN - std::time::Duration::from_secs(60);
            let backup_semantics = 0x0200_0000;     // (what opening a folder takes)
            let folder = std::fs::OpenOptions::new().write(true).custom_flags(backup_semantics).open(&half_made).unwrap();
            folder.set_times(std::fs::FileTimes::new().set_created(long_ago)).unwrap();
        }
        let theirs = format!("{KEPT_FOLDER_PREFIX}9.9.9-somebody-elses");
        std::fs::create_dir(format!("{kept_in}\\{theirs}")).unwrap();
        std::fs::write(format!("{kept_in}\\{theirs}\\VisporaBackend.exe"), b"theirs").unwrap();
        std::fs::create_dir(format!("{kept_in}\\Something-else")).unwrap();

        let now = make_ready(&new, Way::OncePerVersion, &places(&place)).unwrap();
        assert_eq!(others(now.kept.as_deref()), Others { removed: 2, left: 3, not_ours: 1 });
        assert!(!there(&was) && !there(&half_made), "an earlier version's folder, and one a start was cut short in");
        assert!(there(&format!("{kept_in}\\{being_made}\\VisporaBackend.exe")), "a folder that may be being made was taken away");
        assert_eq!(older.same_as(&in_use.folder, &[]), Ok(()), "a folder that a copy holds was touched");
        assert_eq!(oldest.same_as(&orphans, &["0-comes-first.txt"]), Ok(()), "a folder whose program is running was touched");
        assert!(there(&format!("{orphans}\\0-comes-first.txt")), "a folder whose program is running was taken apart as far as Windows let it");
        assert_eq!(std::fs::read(format!("{kept_in}\\{theirs}\\VisporaBackend.exe")).unwrap(), b"theirs", "somebody else's folder was touched");
        assert_eq!(new.same_as(&now.folder, &[]), Ok(()));
        // Once those have ended, the next start takes their folders too.
        drop((in_use, running));
        assert_eq!(others(now.kept.as_deref()), Others { removed: 2, left: 1, not_ours: 1 });
        assert_eq!(kept_folders(&place), ["Something-else".to_string(), kept_name(&new), being_made.clone(), theirs.clone()]);
        // A build that keeps none takes away what a build that kept one
        // left: not under a copy that is using it, and then.
        assert_eq!(others(None), Others { removed: 0, left: 2, not_ours: 1 });
        assert_eq!(new.same_as(&now.folder, &[]), Ok(()));
        drop(now);
        assert_eq!(others(None), Others { removed: 1, left: 1, not_ours: 1 });
        assert_eq!(kept_folders(&place), ["Something-else".to_string(), being_made, theirs]);
        tidy(&place);
    }
}
