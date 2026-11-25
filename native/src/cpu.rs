//! Processor readings: the clock, and the time spent in DPCs and interrupts.
//!
//! The hardware monitor asked PowerShell for each of these at every pass:
//! `(Get-CimInstance Win32_Processor).CurrentClockSpeed` (over a second on the
//! owner's PC) and `Get-Counter '\Processor(_Total)\% DPC Time', ...` (which
//! fails outright where the performance-counter library is damaged).  Both
//! numbers come straight from the kernel here.

use crate::json::Json;
use crate::win32::{self, read};

/// PROCESSOR_POWER_INFORMATION: six ULONGs per processor.
const POWER_INFO_SIZE: usize = 24;
const POWER_INFO_CURRENT_MHZ: usize = 8;
const POWER_INFO_MAX_MHZ: usize = 4;

/// SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION: five LARGE_INTEGERs and a ULONG.
const PERF_INFO_SIZE: usize = 48;

const MAX_PROCESSORS: usize = 1024;

/// `q: cpu_clock` - {"mhz", "max_mhz", "processors"}: the first processor's
/// current clock, which is what Win32_Processor.CurrentClockSpeed reports.
pub fn clock() -> Result<Json, String> {
    let mut buf = vec![0u32; MAX_PROCESSORS * POWER_INFO_SIZE / 4];
    let bytes = buf.len() * 4;
    // SAFETY: the buffer is `bytes` long and writable; no input buffer is used.
    let status = unsafe {
        win32::CallNtPowerInformation(
            win32::PROCESSOR_INFORMATION,
            std::ptr::null(),
            0,
            buf.as_mut_ptr().cast(),
            bytes as u32,
        )
    };
    if status != 0 {
        return Err(format!("CallNtPowerInformation failed (0x{:08X})", status as u32));
    }
    // SAFETY: `buf` owns `bytes` initialised bytes.
    let raw = unsafe { std::slice::from_raw_parts(buf.as_ptr() as *const u8, bytes) };
    let (mhz, max_mhz, processors) = parse_clock(raw);
    if mhz == 0 {
        return Err("Windows reported no processor clock".into());
    }
    Ok(Json::object([("mhz", mhz.into()), ("max_mhz", max_mhz.into()), ("processors", processors.into())]))
}

/// (current MHz of the first processor, its maximum MHz, how many processors
/// the buffer describes).  Unused entries are all zero.
fn parse_clock(raw: &[u8]) -> (u32, u32, usize) {
    let mhz: u32 = read(raw, POWER_INFO_CURRENT_MHZ).unwrap_or(0);
    let max_mhz: u32 = read(raw, POWER_INFO_MAX_MHZ).unwrap_or(0);
    let processors = raw
        .chunks_exact(POWER_INFO_SIZE)
        .take_while(|entry| read::<u32>(entry, POWER_INFO_MAX_MHZ).unwrap_or(0) != 0)
        .count();
    (mhz, max_mhz, processors)
}

/// Processor time since boot, summed over all processors, in 100 ns units.
#[derive(Debug, Default, PartialEq)]
pub struct Times {
    /// Kernel + user time.  Kernel time includes idle time, so this is simply
    /// the time the processors have existed: it advances by
    /// (processors x 10,000,000) a second.
    pub total: u64,
    pub idle: u64,
    pub dpc: u64,
    pub interrupt: u64,
    pub processors: usize,
}

fn parse_times(raw: &[u8]) -> Times {
    let mut t = Times::default();
    for entry in raw.chunks_exact(PERF_INFO_SIZE) {
        let part = |offset: usize| read::<i64>(entry, offset).unwrap_or(0).max(0) as u64;
        let (idle, kernel, user, dpc, interrupt) = (part(0), part(8), part(16), part(24), part(32));
        t.total += kernel + user;
        t.idle += idle;
        t.dpc += dpc;
        t.interrupt += interrupt;
        t.processors += 1;
    }
    t
}

/// `q: cpu_times` - {"total", "idle", "dpc", "interrupt", "processors"}.
/// Two answers a moment apart give "% DPC Time" and "% Interrupt Time":
/// (dpc2 - dpc1) / (total2 - total1) x 100.
pub fn times() -> Result<Json, String> {
    let mut buf = vec![0u64; MAX_PROCESSORS * PERF_INFO_SIZE / 8];
    let bytes = buf.len() * 8;
    let mut written: u32 = 0;
    // SAFETY: the buffer is `bytes` long and writable; `written` is a valid out pointer.
    let status = unsafe {
        win32::NtQuerySystemInformation(
            win32::SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION,
            buf.as_mut_ptr().cast(),
            bytes as u32,
            &mut written,
        )
    };
    if status != 0 {
        return Err(format!("NtQuerySystemInformation failed (0x{:08X})", status as u32));
    }
    let used = (written as usize).min(bytes);
    // SAFETY: `buf` owns `bytes` initialised bytes, of which `used` were written.
    let raw = unsafe { std::slice::from_raw_parts(buf.as_ptr() as *const u8, used) };
    let t = parse_times(raw);
    if t.processors == 0 || t.total == 0 {
        return Err("Windows reported no processor times".into());
    }
    Ok(Json::object([
        ("total", t.total.into()),
        ("idle", t.idle.into()),
        ("dpc", t.dpc.into()),
        ("interrupt", t.interrupt.into()),
        ("processors", t.processors.into()),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn perf_entry(idle: i64, kernel: i64, user: i64, dpc: i64, interrupt: i64) -> Vec<u8> {
        let mut e = Vec::with_capacity(PERF_INFO_SIZE);
        for v in [idle, kernel, user, dpc, interrupt] {
            e.extend_from_slice(&v.to_le_bytes());
        }
        e.extend_from_slice(&[0u8; 8]); // InterruptCount and padding
        e
    }

    #[test]
    fn times_are_summed_over_the_processors() {
        let mut raw = perf_entry(700, 800, 200, 10, 5);
        raw.extend(perf_entry(300, 600, 400, 30, 15));
        raw.extend_from_slice(&[1, 2, 3]); // a trailing fragment is ignored
        assert_eq!(parse_times(&raw), Times { total: 2000, idle: 1000, dpc: 40, interrupt: 20, processors: 2 });
        assert_eq!(parse_times(&[]), Times::default());
    }

    #[test]
    fn the_clock_is_the_first_processors() {
        let mut raw = Vec::new();
        for (number, max, current) in [(0u32, 3700u32, 3700u32), (1, 3700, 2100), (2, 3700, 3700)] {
            for v in [number, max, current, 3700, 3, 1] {
                raw.extend_from_slice(&v.to_le_bytes());
            }
        }
        raw.extend_from_slice(&[0u8; POWER_INFO_SIZE * 4]); // unused entries
        assert_eq!(parse_clock(&raw), (3700, 3700, 3));
        assert_eq!(parse_clock(&[]), (0, 0, 0));
    }

    #[test]
    fn this_machine_answers_both() {
        let c = clock().unwrap();
        assert!(c["mhz"].as_u64().unwrap() > 100, "{c}");
        let a = times().unwrap();
        std::thread::sleep(std::time::Duration::from_millis(60));
        let b = times().unwrap();
        let processors = a["processors"].as_u64().unwrap();
        assert!(processors >= 1 && processors == b["processors"].as_u64().unwrap());
        let advanced = b["total"].as_u64().unwrap() - a["total"].as_u64().unwrap();
        // 60 ms on every processor, give or take the scheduler.
        assert!(advanced > processors * 300_000 && advanced < processors * 20_000_000, "{advanced}");
        assert!(b["idle"].as_u64().unwrap() <= b["total"].as_u64().unwrap());
        assert!(b["dpc"].as_u64().unwrap() >= a["dpc"].as_u64().unwrap());
    }
}
