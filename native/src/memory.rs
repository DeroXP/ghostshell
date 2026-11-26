//! Memory, and how busy the kernel is with deferred work: two readings the
//! hardware monitor takes once a second.
//!
//! * `memory`: how much of the memory is in use (a percentage, as Task
//!   Manager shows it), and how large the standby list is (pages Windows
//!   keeps of files it has read, and gives back the moment a program wants
//!   memory).
//! * `dpc_rate`: deferred procedure calls a second, over all processors.
//!
//! Both only read.  The standby list is read here and never emptied: that is
//! an action (actions.rs), behind the library's other door.

use crate::json::Json;
use crate::win32::{self, read};

/// MEMORYSTATUSEX: its length, the load, and seven 64-bit counts (the
/// first two: the memory there is, and how much of it is free).
const MEMORY_STATUS_SIZE: usize = 64;
const MEMORY_STATUS_LOAD: usize = 4;
const MEMORY_STATUS_TOTAL: usize = 8;
const MEMORY_STATUS_FREE: usize = 16;

/// SYSTEM_MEMORY_LIST_INFORMATION is counts of pages, each as wide as a
/// pointer: five single ones, the standby list by priority (eight), the
/// repurposed pages by priority (eight), and one more.
const WORD: usize = std::mem::size_of::<usize>();
const MEMORY_LIST_SIZE: usize = 22 * WORD;
const MEMORY_LIST_STANDBY: usize = 5 * WORD;
const PRIORITIES: usize = 8;

/// SYSTEM_INFO: the page size is its second field.
const SYSTEM_INFO_SIZE: usize = 64;
const SYSTEM_INFO_PAGE_SIZE: usize = 4;
const USUAL_PAGE_SIZE: u32 = 4096;

/// SYSTEM_INTERRUPT_INFORMATION, one per processor: six 32-bit counts, the
/// DPC rate the third.
const INTERRUPT_INFO_SIZE: usize = 24;
const INTERRUPT_INFO_DPC_RATE: usize = 8;

const MAX_PROCESSORS: usize = 1024;

/// What MEMORYSTATUSEX says: the load in percent, the memory there is, and
/// how much of it is free (in bytes).
fn status_of(raw: &[u8]) -> Option<(u32, u64, u64)> {
    Some((read(raw, MEMORY_STATUS_LOAD)?, read(raw, MEMORY_STATUS_TOTAL)?, read(raw, MEMORY_STATUS_FREE)?))
}

/// The pages of the standby list, summed over its eight priorities.
fn standby_pages(raw: &[u8]) -> Option<u64> {
    let mut pages: u64 = 0;
    for priority in 0..PRIORITIES {
        pages = pages.checked_add(read::<usize>(raw, MEMORY_LIST_STANDBY + priority * WORD)? as u64)?;
    }
    Some(pages)
}

/// The DPC rates of every processor in `raw`, summed, and how many
/// processors that was.
fn dpc_rates(raw: &[u8]) -> (u64, usize) {
    let entries = raw.chunks_exact(INTERRUPT_INFO_SIZE);
    let processors = entries.len();
    (entries.map(|entry| read::<u32>(entry, INTERRUPT_INFO_DPC_RATE).unwrap_or(0) as u64).sum(), processors)
}

/// `q: memory` - `{"load_pct", "total_bytes", "free_bytes", "standby_pages",
/// "page_size"}`.  All but the page size are null where Windows would not say.
pub fn memory() -> Result<Json, String> {
    let mut status = [0u64; MEMORY_STATUS_SIZE / 8];
    status[0] = MEMORY_STATUS_SIZE as u64; // (dwLength, and a load of nought)
    let told = unsafe { win32::GlobalMemoryStatusEx(status.as_mut_ptr().cast()) } != 0;
    let raw = unsafe { std::slice::from_raw_parts(status.as_ptr() as *const u8, MEMORY_STATUS_SIZE) };
    let told = if told { status_of(raw) } else { None };
    let load = told.map(|(load, _, _)| load);

    let mut list = [0u64; MEMORY_LIST_SIZE / 8 + 1];
    let mut written: u32 = 0;
    let status = unsafe {
        win32::NtQuerySystemInformation(win32::SYSTEM_MEMORY_LIST_INFORMATION, list.as_mut_ptr().cast(), MEMORY_LIST_SIZE as u32, &mut written)
    };
    let raw = unsafe { std::slice::from_raw_parts(list.as_ptr() as *const u8, MEMORY_LIST_SIZE) };
    let standby = if status == 0 { standby_pages(raw) } else { None };

    let mut info = [0u64; SYSTEM_INFO_SIZE / 8];
    unsafe { win32::GetSystemInfo(info.as_mut_ptr().cast()) };
    let raw = unsafe { std::slice::from_raw_parts(info.as_ptr() as *const u8, SYSTEM_INFO_SIZE) };
    let page_size = read::<u32>(raw, SYSTEM_INFO_PAGE_SIZE).filter(|size| *size != 0).unwrap_or(USUAL_PAGE_SIZE);

    if load.is_none() && standby.is_none() {
        return Err(format!("Windows told neither the memory in use nor the standby list (0x{:08X})", status as u32));
    }
    Ok(Json::object([
        ("load_pct", load.map_or(Json::Null, Into::into)),
        ("total_bytes", told.map_or(Json::Null, |(_, total, _)| total.into())),
        ("free_bytes", told.map_or(Json::Null, |(_, _, free)| free.into())),
        ("standby_pages", standby.map_or(Json::Null, Into::into)),
        ("page_size", page_size.into()),
    ]))
}

/// `q: dpc_rate` - `{"per_second", "processors"}`.
pub fn dpc_rate() -> Result<Json, String> {
    let mut buf = vec![0u32; MAX_PROCESSORS * INTERRUPT_INFO_SIZE / 4];
    let bytes = buf.len() * 4;
    let mut written: u32 = 0;
    let status = unsafe { win32::NtQuerySystemInformation(win32::SYSTEM_INTERRUPT_INFORMATION, buf.as_mut_ptr().cast(), bytes as u32, &mut written) };
    if status != 0 {
        return Err(format!("NtQuerySystemInformation failed (0x{:08X})", status as u32));
    }
    let used = (written as usize).min(bytes);
    let raw = unsafe { std::slice::from_raw_parts(buf.as_ptr() as *const u8, used) };
    let (per_second, processors) = dpc_rates(raw);
    if processors == 0 {
        return Err("Windows reported no processors".into());
    }
    Ok(Json::object([("per_second", per_second.into()), ("processors", processors.into())]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_load_is_read_where_windows_writes_it() {
        // MEMORYSTATUSEX as Windows fills it: its own length (64), the load,
        // then the counts in bytes.  Each a value of its own, so that one
        // read from another place is not mistaken for it.
        let mut raw = Vec::new();
        raw.extend_from_slice(&64u32.to_le_bytes());
        raw.extend_from_slice(&37u32.to_le_bytes());
        for count in [32_000_000_000u64, 20_000_000_000, 40_000_000_000, 30_000_000_000, 140_000_000_000_000, 139_000_000_000_000, 0] {
            raw.extend_from_slice(&count.to_le_bytes());
        }
        assert_eq!(raw.len(), MEMORY_STATUS_SIZE);
        assert_eq!(status_of(&raw), Some((37, 32_000_000_000, 20_000_000_000)));
        assert_eq!(status_of(&raw[..20]), None);
    }

    #[test]
    fn the_standby_list_is_its_eight_priorities_and_nothing_else() {
        let mut raw = Vec::new();
        for count in 0..22usize {
            // (the five counts before the list and the nine after it are large: none of them may be counted)
            let pages: usize = if (5..13).contains(&count) { count - 4 } else { 1_000_000 };
            raw.extend_from_slice(&pages.to_le_bytes());
        }
        assert_eq!(raw.len(), MEMORY_LIST_SIZE);
        assert_eq!(standby_pages(&raw), Some(1 + 2 + 3 + 4 + 5 + 6 + 7 + 8));
        assert_eq!(standby_pages(&raw[..MEMORY_LIST_STANDBY + 7 * WORD]), None, "a list that is cut short is no list");
        assert_eq!(standby_pages(&[]), None);
    }

    #[test]
    fn the_dpc_rate_is_summed_over_the_processors() {
        let mut raw = Vec::new();
        for (switches, count, rate) in [(7u32, 70u32, 1200u32), (9, 90, 300)] {
            for value in [switches, count, rate, 156_250, 0, 0] {
                raw.extend_from_slice(&value.to_le_bytes());
            }
        }
        raw.extend_from_slice(&[1, 2, 3]); // a trailing fragment is ignored
        assert_eq!(dpc_rates(&raw), (1500, 2));
        assert_eq!(dpc_rates(&[]), (0, 0));
    }

    #[test]
    fn this_machine_answers_both() {
        let said = memory().unwrap();
        let load = said["load_pct"].as_u64().unwrap();
        assert!((1..=100).contains(&load), "{said}");
        // The load is the share of the memory that is not free, as Windows
        // counts it from the same two numbers (rounded its own way).
        let (total, free) = (said["total_bytes"].as_u64().unwrap() as f64, said["free_bytes"].as_u64().unwrap() as f64);
        assert!(total > 1e9 && free < total, "{said}");
        let in_use = (total - free) / total * 100.0;
        assert!((in_use - load as f64).abs() <= 2.0, "{said}: {in_use:.1}% in use by the totals");
        assert!(matches!(said["page_size"].as_u64(), Some(4096 | 16384 | 65536)), "{said}");
        // (told without administrator rights too: measured on Windows 11)
        assert!(said["standby_pages"].as_u64().is_some_and(|pages| pages < (1 << 40)), "{said}");
        let rate = dpc_rate().unwrap();
        assert!(rate["processors"].as_u64().unwrap() >= 1 && rate["per_second"].as_u64().is_some(), "{rate}");
    }
}
