//! The actions: the ONLY code in this library that changes anything.
//!
//! Everything else here reads the machine.  These two free memory, and they
//! are what the app's start-up preparation has always done, each by starting
//! PowerShell and compiling a small C# program inside it at every start:
//!
//! * `trim_working_sets`: ask Windows to trim every process's working set
//!   (the pages it keeps in memory), as "empty working set" tools do.  The
//!   pages are not lost: Windows hands them back when the process touches
//!   them again.
//! * `purge_standby_list`: drop the standby list, the cache of file pages
//!   nobody is using right now.
//!
//! They are kept apart on purpose.  They have their own exported function
//! (`vispora_native_act`), so no question asked through `vispora_native_call`
//! can ever reach them, and the command-line tool does not offer them.
//!
//! Neither changes a setting: nothing here survives a restart, and there is
//! nothing to put back.

use std::ffi::c_void;

use crate::json::Json;
use crate::procs;

const PROCESS_SET_QUOTA: u32 = 0x0100;
const SE_PROF_SINGLE_PROCESS_PRIVILEGE: u32 = 13;
const SE_DEBUG_PRIVILEGE: u32 = 20;
const SYSTEM_MEMORY_LIST_INFORMATION: u32 = 80;
const MEMORY_PURGE_STANDBY_LIST: i32 = 4;
const SECURITY_IMPERSONATION: u32 = 2;
const EMPTY: usize = usize::MAX; // (SIZE_T)-1 for both sizes means "trim as far as possible"

#[link(name = "ntdll")]
extern "system" {
    fn RtlAdjustPrivilege(privilege: u32, enable: u8, current_thread: u8, was_enabled: *mut u8) -> i32;
    fn NtSetSystemInformation(class: u32, info: *const c_void, length: u32) -> i32;
}

#[link(name = "kernel32")]
extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> isize;
    fn CloseHandle(handle: isize) -> i32;
    fn SetProcessWorkingSetSize(process: isize, minimum: usize, maximum: usize) -> i32;
}

#[link(name = "advapi32")]
extern "system" {
    fn ImpersonateSelf(level: u32) -> i32;
    fn RevertToSelf() -> i32;
}

/// A privilege switched on for THIS THREAD only, for as long as the value
/// lives.
///
/// The thread is given a copy of the program's own security token and the
/// privilege is switched on in that copy.  The program's token is never
/// touched, so no other thread gains the privilege and none that is using it
/// (the app switches the debug privilege on and off itself) loses it.
struct ThreadPrivilege {
    impersonating: bool,
    held: bool,
}

impl ThreadPrivilege {
    fn enable(privilege: u32) -> ThreadPrivilege {
        // SAFETY: no pointers; gives the calling thread a copy of the process token.
        let impersonating = unsafe { ImpersonateSelf(SECURITY_IMPERSONATION) } != 0;
        let mut held = false;
        if impersonating {
            let mut was = 0u8;
            // SAFETY: `was` is a valid out pointer; current_thread = 1 adjusts
            // the thread's token made above, not the process's.
            held = unsafe { RtlAdjustPrivilege(privilege, 1, 1, &mut was) } == 0;
        }
        ThreadPrivilege { impersonating, held }
    }
}

impl Drop for ThreadPrivilege {
    fn drop(&mut self) {
        if self.impersonating {
            // SAFETY: drops the thread's token copy; the thread runs as the program again.
            unsafe { RevertToSelf() };
        }
    }
}

/// Trim the working sets of `pids`.  Returns how many were trimmed.
fn trim(pids: &[u32]) -> u32 {
    let mut trimmed = 0;
    for &pid in pids {
        if pid == 0 || pid == 4 {
            continue; // the idle process and the kernel itself
        }
        // SAFETY: a plain call; the handle, if any, is closed below.  The
        // handle may set quotas and nothing else: it cannot read or write the
        // process's memory, and a protected process refuses it.
        let handle = unsafe { OpenProcess(PROCESS_SET_QUOTA, 0, pid) };
        if handle == 0 {
            continue;
        }
        // SAFETY: `handle` is open with the right this call needs.
        if unsafe { SetProcessWorkingSetSize(handle, EMPTY, EMPTY) } != 0 {
            trimmed += 1;
        }
        // SAFETY: opened above, closed once.
        unsafe { CloseHandle(handle) };
    }
    trimmed
}

/// `do: trim_working_sets` - trim every process's working set, or only those
/// of `pids` when given.  `{"processes": n, "trimmed": m}`.
///
/// With administrator rights the debug privilege is switched on for the
/// calling thread while the processes are opened, so that services are
/// reached as well; that is what the PowerShell this replaces did (PowerShell
/// runs with it on).  Protected processes refuse the handle either way.
pub fn trim_working_sets(only: Option<&[u32]>) -> Result<Json, String> {
    let pids: Vec<u32> = match only {
        Some(pids) => pids.to_vec(),
        None => procs::snapshot()?.iter().map(|p| p.pid).collect(),
    };
    let privilege = ThreadPrivilege::enable(SE_DEBUG_PRIVILEGE);
    let trimmed = trim(&pids);
    let with_debug = privilege.held;
    drop(privilege);
    Ok(Json::object([
        ("processes", (pids.len() as u64).into()),
        ("trimmed", trimmed.into()),
        ("debug_privilege", with_debug.into()),
    ]))
}

/// `do: purge_standby_list` - drop the standby memory list.  Needs
/// administrator rights (the "profile single process" privilege, which an
/// elevated program holds switched off); an error says so otherwise.
pub fn purge_standby_list() -> Result<Json, String> {
    let privilege = ThreadPrivilege::enable(SE_PROF_SINGLE_PROCESS_PRIVILEGE);
    if !privilege.held {
        return Err("the standby list can only be cleared with administrator rights".into());
    }
    let command: i32 = MEMORY_PURGE_STANDBY_LIST;
    // SAFETY: `command` is the 4-byte value this information class takes.
    let status = unsafe {
        NtSetSystemInformation(SYSTEM_MEMORY_LIST_INFORMATION, (&command as *const i32).cast(), 4)
    };
    drop(privilege);
    if status != 0 {
        return Err(format!("Windows refused to clear the standby list (0x{:08X})", status as u32));
    }
    Ok(Json::object([("purged", true.into())]))
}

/// The tests that trim the test program itself take this first: they all run
/// in one process, and one trimming it while another measures would mislead.
#[cfg(test)]
pub(crate) static TRIMMING_THIS_PROCESS: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[cfg(test)]
mod tests {
    use super::*;

    #[link(name = "shell32")]
    extern "system" {
        fn IsUserAnAdmin() -> i32;
    }

    fn working_set_of(pid: u32) -> u64 {
        procs::snapshot().unwrap().iter().find(|p| p.pid == pid).map(|p| p.working_set).unwrap()
    }

    // The tests below only ever trim THIS test program.  Trimming every
    // process, or clearing the standby list, is never done by a test: on a
    // PC somebody is using that would be felt.

    #[test]
    fn trimming_this_process_shrinks_its_working_set_and_nothing_is_lost() {
        let _alone = TRIMMING_THIS_PROCESS.lock().unwrap_or_else(|p| p.into_inner());
        let me = std::process::id();
        let mut block = vec![0u8; 64 << 20];
        for i in (0..block.len()).step_by(4096) {
            block[i] = (i / 4096) as u8;
        }
        let before = working_set_of(me);
        assert!(before > 64 << 20, "{before}");
        let done = trim_working_sets(Some(&[me])).unwrap();
        assert_eq!(done["processes"].as_u64(), Some(1));
        assert_eq!(done["trimmed"].as_u64(), Some(1), "{done}");
        let after = working_set_of(me);
        assert!(after + (48 << 20) < before, "the working set went from {before} to {after}");
        // The pages come back when they are touched, with what was in them.
        for i in (0..block.len()).step_by(4096) {
            assert_eq!(block[i], (i / 4096) as u8);
        }
    }

    #[test]
    fn processes_that_cannot_be_opened_are_passed_over() {
        // The idle process, the kernel, and a process id nothing has.
        let done = trim_working_sets(Some(&[0, 4, 0xFFFF_FFF0])).unwrap();
        assert_eq!(done["processes"].as_u64(), Some(3));
        assert_eq!(done["trimmed"].as_u64(), Some(0));
        assert_eq!(trim(&[]), 0);
    }

    #[test]
    fn the_privilege_is_for_one_thread_and_gone_afterwards() {
        // After a trim, this thread is the program again: it can still do
        // what the program can, and a second trim works the same.
        let _alone = TRIMMING_THIS_PROCESS.lock().unwrap_or_else(|p| p.into_inner());
        let me = std::process::id();
        for _ in 0..3 {
            assert_eq!(trim_working_sets(Some(&[me])).unwrap()["trimmed"].as_u64(), Some(1));
        }
        // Without administrator rights the privilege is not there to switch on.
        // SAFETY: no arguments, no side effects.
        if unsafe { IsUserAnAdmin() } == 0 {
            assert!(!ThreadPrivilege::enable(SE_PROF_SINGLE_PROCESS_PRIVILEGE).held);
        }
    }

    #[test]
    fn clearing_the_standby_list_without_administrator_rights_says_so() {
        // SAFETY: as above.
        if unsafe { IsUserAnAdmin() } != 0 {
            return; // (as administrator this would really clear it: not from a test)
        }
        let err = purge_standby_list().unwrap_err();
        assert!(err.contains("administrator"), "{err}");
    }
}
