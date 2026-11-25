//! Which processes have a main window.
//!
//! Game mode looks for a game by asking "which processes own a visible
//! window?".  It used to ask PowerShell:
//!
//! ```text
//! Get-Process | Where-Object { $_.MainWindowHandle -ne 0 }
//!             | Select-Object Id, ProcessName, @{N='WS';E={[int]($_.WorkingSet64/1MB)}}
//! ```
//!
//! This answers the same thing the same way .NET does underneath: a process's
//! main window is the first top-level window (in EnumWindows order) that
//! belongs to it, has no owner window and is visible.  So "MainWindowHandle is
//! not 0" means "owns at least one visible top-level window without an owner".

use std::collections::HashSet;

use crate::json::Json;
use crate::procs;
use crate::win32;

unsafe extern "system" fn collect(hwnd: win32::Hwnd, lparam: isize) -> i32 {
    // SAFETY: `lparam` is the &mut HashSet passed to EnumWindows below, which
    // outlives the enumeration; the callback runs on the calling thread.
    let pids = &mut *(lparam as *mut HashSet<u32>);
    if win32::GetWindow(hwnd, win32::GW_OWNER) == 0 && win32::IsWindowVisible(hwnd) != 0 {
        let mut pid: u32 = 0;
        win32::GetWindowThreadProcessId(hwnd, &mut pid);
        if pid != 0 {
            pids.insert(pid);
        }
    }
    1 // keep enumerating
}

/// The ids of the processes that own a main window.
pub fn pids_with_a_main_window() -> Result<HashSet<u32>, String> {
    let mut pids: HashSet<u32> = HashSet::new();
    // SAFETY: the callback only touches the set behind `lparam`, which lives
    // until EnumWindows returns.
    let ok = unsafe { win32::EnumWindows(collect, &mut pids as *mut HashSet<u32> as isize) };
    if ok == 0 && pids.is_empty() {
        // SAFETY: plain call.
        let code = unsafe { win32::GetLastError() };
        return Err(format!("EnumWindows failed (error {code})"));
    }
    Ok(pids)
}

/// The name Get-Process prints for an image name: the file name without a
/// ".exe" ending (any case), and with any other ending kept.  This is .NET's
/// own rule (ProcessManager.GetProcessShortName).
pub fn short_name(image: &str) -> String {
    let file = image.rsplit('\\').next().unwrap_or(image);
    match file.rfind('.') {
        Some(dot) if file[dot..].eq_ignore_ascii_case(".exe") => file[..dot].to_string(),
        _ => file.to_string(),
    }
}

/// `q: windowed_processes` - [{"pid", "name", "ws"}]: every process with a
/// main window, its name as Get-Process prints it and its working set in bytes.
pub fn windowed_processes() -> Result<Json, String> {
    let with_window = pids_with_a_main_window()?;
    let rows = procs::snapshot()?
        .iter()
        .filter(|p| with_window.contains(&p.pid))
        .map(|p| {
            let name = if p.image.is_empty() {
                match p.pid {
                    0 => "Idle".to_string(), // (what Get-Process calls them)
                    4 => "System".to_string(),
                    _ => String::new(),
                }
            } else {
                short_name(&p.image)
            };
            Json::object([("pid", p.pid.into()), ("name", name.into()), ("ws", p.working_set.into())])
        })
        .collect();
    Ok(Json::Array(rows))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_shortened_the_way_get_process_shortens_them() {
        for (image, want) in [
            ("cs2.exe", "cs2"),
            ("VALORANT-Win64-Shipping.exe", "VALORANT-Win64-Shipping"),
            ("Game.EXE", "Game"),
            ("r5apex_dx12.exe", "r5apex_dx12"),
            ("some.tool.exe", "some.tool"),
            ("setup.tmp", "setup.tmp"),
            ("Registry", "Registry"),
            ("Memory Compression", "Memory Compression"),
            ("weird.exe.bak", "weird.exe.bak"),
            (".exe", ""),
            ("", ""),
            (r"C:\Games\x\game.exe", "game"),
        ] {
            assert_eq!(short_name(image), want, "{image}");
        }
    }

    #[test]
    fn the_scan_answers_rows_of_the_right_shape() {
        // A build machine without a desktop has no windows; the scan must
        // still answer (an empty list), not fail.
        let rows = windowed_processes().unwrap();
        for r in rows.as_array().unwrap() {
            assert!(r["pid"].as_u64().unwrap() > 0);
            assert!(!r["name"].as_str().unwrap().to_lowercase().ends_with(".exe"));
            assert!(r["ws"].as_u64().unwrap() > 0);
        }
    }
}
