//! Every process on the machine from ONE system call.
//!
//! `NtQuerySystemInformation(SystemProcessInformation)` is what Task Manager
//! and .NET's `Process.GetProcesses()` read: one buffer with every process's
//! id, image name, working set and I/O counters.  No process is opened, so it
//! works for protected processes too and costs about a millisecond, where
//! asking each process in turn (psutil) cost the app a quarter of a core.

use serde_json::{json, Value};

use crate::win32::{self, read};

/// One process, as the snapshot describes it.
#[derive(Debug, Clone, PartialEq)]
pub struct Process {
    pub pid: u32,
    /// The image name as Windows has it ("chrome.exe"); empty for the idle process.
    pub image: String,
    pub working_set: u64,
    /// Bytes of I/O that is neither a read nor a write.  On Windows that
    /// includes network traffic; it is psutil's `io_counters().other_bytes`.
    pub other_bytes: u64,
}

// Offsets inside SYSTEM_PROCESS_INFORMATION on 64-bit Windows.
const NEXT_ENTRY: usize = 0;
const NAME_LENGTH: usize = 56; // UNICODE_STRING.Length, in bytes
const NAME_BUFFER: usize = 64; // UNICODE_STRING.Buffer
const PROCESS_ID: usize = 80;
const WORKING_SET: usize = 144;
const OTHER_TRANSFER: usize = 248;
const ENTRY_MIN: usize = 256;

/// Take the snapshot.
pub fn snapshot() -> Result<Vec<Process>, String> {
    let mut size: usize = 1 << 20;
    for _ in 0..8 {
        // u64 elements keep the buffer 8-byte aligned, as the structures in it are.
        let mut buf = vec![0u64; size / 8 + 1];
        let bytes = buf.len() * 8;
        let mut needed: u32 = 0;
        // SAFETY: the buffer is `bytes` long and writable; `needed` is a valid out pointer.
        let status = unsafe {
            win32::NtQuerySystemInformation(
                win32::SYSTEM_PROCESS_INFORMATION,
                buf.as_mut_ptr().cast(),
                bytes as u32,
                &mut needed,
            )
        };
        if status == win32::STATUS_INFO_LENGTH_MISMATCH {
            // Processes come and go between the two calls: ask for more than was needed.
            size = (needed as usize).max(size) + (256 << 10);
            continue;
        }
        if status != 0 {
            return Err(format!("NtQuerySystemInformation failed (0x{:08X})", status as u32));
        }
        let used = (needed as usize).min(bytes);
        // SAFETY: `buf` owns `bytes` initialised bytes and outlives `raw`.
        let raw = unsafe { std::slice::from_raw_parts(buf.as_ptr() as *const u8, bytes) };
        return Ok(parse(raw, if used == 0 { bytes } else { used }));
    }
    Err("the process list kept growing faster than it could be read".into())
}

/// Walk the entries in `raw[..used]`.  The name pointers inside point into
/// `raw` itself, so they are checked against it before being followed.
fn parse(raw: &[u8], used: usize) -> Vec<Process> {
    let raw = &raw[..used.min(raw.len())];
    let base = raw.as_ptr() as usize;
    let mut out = Vec::with_capacity(512);
    let mut at = 0usize;
    loop {
        if at + ENTRY_MIN > raw.len() {
            break;
        }
        let entry = &raw[at..];
        let next: u32 = read(entry, NEXT_ENTRY).unwrap_or(0);
        let pid: u64 = read(entry, PROCESS_ID).unwrap_or(0);
        let name_len: u16 = read(entry, NAME_LENGTH).unwrap_or(0);
        let name_ptr: u64 = read(entry, NAME_BUFFER).unwrap_or(0);
        let image = name_at(raw, base, name_ptr as usize, name_len as usize);
        out.push(Process {
            pid: pid as u32,
            image,
            working_set: read(entry, WORKING_SET).unwrap_or(0),
            other_bytes: read::<i64>(entry, OTHER_TRANSFER).unwrap_or(0).max(0) as u64,
        });
        if next == 0 {
            break;
        }
        at += next as usize;
    }
    out
}

fn name_at(raw: &[u8], base: usize, ptr: usize, len_bytes: usize) -> String {
    if ptr == 0 || len_bytes == 0 || ptr < base {
        return String::new();
    }
    let start = ptr - base;
    let Some(end) = start.checked_add(len_bytes) else {
        return String::new();
    };
    if end > raw.len() {
        return String::new();
    }
    let units: Vec<u16> = raw[start..end].chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
    String::from_utf16_lossy(&units)
}

/// The name psutil (and Task Manager) show for a process: the image name, and
/// the two names Windows itself has none for.
pub fn display_name(p: &Process) -> String {
    if !p.image.is_empty() {
        return p.image.clone();
    }
    match p.pid {
        0 => "System Idle Process".to_string(),
        4 => "System".to_string(),
        _ => String::new(),
    }
}

/// `q: process_io` - [{"pid", "name", "other_bytes"}] for every process.
pub fn process_io() -> Result<Value, String> {
    let rows: Vec<Value> = snapshot()?
        .iter()
        .map(|p| json!({ "pid": p.pid, "name": display_name(p), "other_bytes": p.other_bytes }))
        .collect();
    Ok(Value::Array(rows))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_snapshot_holds_this_process_and_the_two_special_ones() {
        let all = snapshot().unwrap();
        assert!(all.len() > 20, "a Windows machine runs more than {} processes", all.len());
        let me = all.iter().find(|p| p.pid == std::process::id()).expect("this test's own process");
        assert!(me.image.to_lowercase().ends_with(".exe"), "{}", me.image);
        assert!(me.working_set > 100_000);
        let idle = all.iter().find(|p| p.pid == 0).expect("the idle process");
        assert_eq!(display_name(idle), "System Idle Process");
        let system = all.iter().find(|p| p.pid == 4).expect("the System process");
        assert_eq!(display_name(system), "System");
        let mut pids: Vec<u32> = all.iter().map(|p| p.pid).collect();
        pids.sort_unstable();
        pids.dedup();
        assert_eq!(pids.len(), all.len(), "every process once");
    }

    #[test]
    fn a_truncated_or_empty_buffer_is_read_as_far_as_it_goes() {
        assert!(parse(&[], 0).is_empty());
        assert!(parse(&[0u8; 100], 100).is_empty());
        let one = vec![0u8; ENTRY_MIN];
        let got = parse(&one, one.len());
        assert_eq!(got.len(), 1);
        assert_eq!(got[0], Process { pid: 0, image: String::new(), working_set: 0, other_bytes: 0 });
        // An entry that says the next one starts beyond the buffer ends the walk.
        let mut bad = vec![0u8; ENTRY_MIN];
        bad[0..4].copy_from_slice(&1_000_000u32.to_le_bytes());
        assert_eq!(parse(&bad, bad.len()).len(), 1);
        // A name pointer that points outside the buffer is not followed.
        let mut wild = vec![0u8; ENTRY_MIN];
        wild[NAME_LENGTH..NAME_LENGTH + 2].copy_from_slice(&8u16.to_le_bytes());
        wild[NAME_BUFFER..NAME_BUFFER + 8].copy_from_slice(&0xdead_beef_u64.to_le_bytes());
        assert_eq!(parse(&wild, wild.len())[0].image, "");
    }

    #[test]
    fn process_io_lists_this_process_by_name() {
        let rows = process_io().unwrap();
        let me = rows
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["pid"] == std::process::id())
            .expect("this process");
        assert!(me["name"].as_str().unwrap().ends_with(".exe"));
        assert!(me["other_bytes"].is_u64());
    }
}
