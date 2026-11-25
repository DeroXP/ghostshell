//! Vispora's native helper.
//!
//! The app (Python today) asks a handful of questions over and over: which
//! processes have a window, how much each process has sent, how warm the
//! graphics card is.  It used to start a program for every one of them
//! (PowerShell, nvidia-smi, ping).  This library answers them inside the app's
//! own process: a fraction of a millisecond for the card's readings, a few
//! milliseconds for the whole process list (docs/performance.md).
//!
//! It uses no crates from outside this folder.  It is loaded into a program
//! that runs as administrator, so everything in it is either written here or
//! part of Windows.
//!
//! One exported function takes a question as JSON and writes the answer as
//! JSON; `core/native.py` is the only caller.  Every question only READS the
//! machine: nothing here changes a setting, a process or the card.
//!
//! Questions (`{"q": "...", ...}`) and their answers are listed in README.md.

#![cfg(windows)]

#[cfg(not(target_arch = "x86_64"))]
compile_error!("vispora-native reads 64-bit Windows structures by offset: build it for x86_64 only");

mod cpu;
mod gpu;
pub mod json;
mod ping;
mod procs;
mod win32;
mod window_scan;
mod wmi_query;

use json::Json;

/// Bumped whenever a question or the shape of an answer changes in a way the
/// Python side must know about.  `core/native.py` refuses a library whose
/// number is not the one it was written for (and then asks the old way).
pub const ABI: u32 = 1;

/// Answer one question.  `request` is the JSON text of `{"q": "<question>", ...}`.
/// Returns the JSON text of `{"ok": true, "data": ...}` or
/// `{"ok": false, "err": "..."}`.  Never panics.
pub fn answer(request: &str) -> String {
    let reply = std::panic::catch_unwind(|| dispatch(request)).unwrap_or_else(|_| {
        Err("the helper hit an internal error (a panic) answering this".to_string())
    });
    match reply {
        Ok(Reply { data, note: None }) => Json::object([("ok", true.into()), ("data", data)]),
        Ok(Reply { data, note: Some(note) }) => {
            Json::object([("ok", true.into()), ("data", data), ("note", note.into())])
        }
        Err(err) => Json::object([("ok", false.into()), ("err", err.into())]),
    }
    .to_string()
}

/// A good answer, and optionally a remark for whoever reads the raw JSON (the
/// command-line tool prints it; the app ignores it).
pub(crate) struct Reply {
    pub(crate) data: Json,
    pub(crate) note: Option<String>,
}

impl From<Json> for Reply {
    fn from(data: Json) -> Self {
        Reply { data, note: None }
    }
}

fn dispatch(request: &str) -> Result<Reply, String> {
    let req = Json::parse(request).map_err(|e| format!("not JSON: {e}"))?;
    let question = req["q"].as_str().ok_or("no question (\"q\") given")?;
    match question {
        "abi" => Ok(Json::object([("abi", ABI.into())]).into()),
        "windowed_processes" => window_scan::windowed_processes().map(Into::into),
        "process_io" => procs::process_io().map(Into::into),
        "gpu_live" => gpu::live().map(Into::into),
        "gpu_release" => Ok(gpu::release(req["hold_s"].as_u64().unwrap_or(0)).into()),
        "cpu_clock" => cpu::clock().map(Into::into),
        "cpu_times" => cpu::times().map(Into::into),
        "ping" => {
            let target = req["target"].as_str().ok_or("ping needs a \"target\"")?;
            let timeout = req["timeout_ms"].as_u64().unwrap_or(500).min(10_000);
            ping::ping(target, timeout as u32).map(Into::into)
        }
        "wmi" => {
            let namespace = req["namespace"].as_str().unwrap_or("root\\cimv2");
            let query = req["query"].as_str().ok_or("wmi needs a \"query\"")?;
            Ok(wmi_query::query(namespace, query))
        }
        other => Err(format!("unknown question: {other}")),
    }
}

// ─────────────────────────────────────────────────────────────────────────
// The two functions the DLL exports
// ─────────────────────────────────────────────────────────────────────────

/// The library's ABI number (see `ABI`).
#[no_mangle]
pub extern "system" fn vispora_native_abi() -> u32 {
    ABI
}

/// Answer the question in `request[..request_len]` (UTF-8 JSON) into
/// `out[..out_cap]` (UTF-8 JSON, not NUL-terminated).
///
/// Returns the number of bytes written; or, when the answer does not fit, the
/// number of bytes it needs as a negative number (nothing is written, ask
/// again with a larger buffer); or 0 when there is no answer at all (a null
/// pointer, a request that is not UTF-8).
///
/// # Safety
/// `request` must point to `request_len` readable bytes and `out` to `out_cap`
/// writable bytes, both valid for the duration of the call.
#[no_mangle]
pub unsafe extern "system" fn vispora_native_call(
    request: *const u8,
    request_len: u32,
    out: *mut u8,
    out_cap: u32,
) -> i32 {
    if request.is_null() || out.is_null() {
        return 0;
    }
    let bytes = std::slice::from_raw_parts(request, request_len as usize);
    let Ok(text) = std::str::from_utf8(bytes) else {
        return 0;
    };
    let reply = answer(text);
    let len = reply.len();
    if len > i32::MAX as usize {
        return 0;
    }
    if len > out_cap as usize {
        return -(len as i32);
    }
    std::ptr::copy_nonoverlapping(reply.as_ptr(), out, len);
    len as i32
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ask(request: &str) -> Json {
        Json::parse(&answer(request)).expect("the answer is JSON")
    }

    #[test]
    fn a_request_that_is_not_a_question_is_refused_politely() {
        for bad in ["", "not json", "[]", "{}", "{\"q\": 5}", "{\"q\": \"no such question\"}"] {
            let a = ask(bad);
            assert_eq!(a["ok"], Json::Bool(false), "{bad}");
            assert!(a["err"].as_str().unwrap().len() > 3, "{bad}");
        }
    }

    #[test]
    fn the_abi_question_answers_the_constant() {
        assert_eq!(ask("{\"q\":\"abi\"}")["data"]["abi"].as_u64(), Some(ABI as u64));
        assert_eq!(vispora_native_abi(), ABI);
    }

    #[test]
    fn the_exported_call_reports_the_size_it_needs() {
        let request = b"{\"q\":\"process_io\"}";
        let mut small = [0u8; 8];
        let need = unsafe {
            vispora_native_call(request.as_ptr(), request.len() as u32, small.as_mut_ptr(), small.len() as u32)
        };
        assert!(need < -8, "a negative size: {need}");
        assert_eq!(small, [0u8; 8], "and nothing written");
        let mut big = vec![0u8; (-need) as usize + 65536];
        let wrote = unsafe {
            vispora_native_call(request.as_ptr(), request.len() as u32, big.as_mut_ptr(), big.len() as u32)
        };
        assert!(wrote > 0);
        let a = Json::parse(std::str::from_utf8(&big[..wrote as usize]).unwrap()).unwrap();
        assert_eq!(a["ok"], Json::Bool(true));
        assert_eq!(unsafe { vispora_native_call(std::ptr::null(), 0, big.as_mut_ptr(), 10) }, 0);
        let not_utf8 = [0xffu8, 0xfe];
        assert_eq!(unsafe { vispora_native_call(not_utf8.as_ptr(), 2, big.as_mut_ptr(), 10) }, 0);
    }
}
