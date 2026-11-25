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
//! Two exported functions take a request as JSON and write the answer as
//! JSON; `core/native.py` is the only caller.
//!
//! * `vispora_native_call` answers QUESTIONS (`{"q": "...", ...}`).  Every
//!   question only reads the machine: nothing reached through it changes a
//!   setting, a process or the card.
//! * `vispora_native_act` carries out ACTIONS (`{"do": "...", ...}`): the two
//!   things in `actions.rs` that free memory.  They are the only code here
//!   that changes anything, and no question can reach them.
//!
//! Questions, actions and their answers are listed in README.md.

#![cfg(windows)]

#[cfg(not(target_arch = "x86_64"))]
compile_error!("vispora-native reads 64-bit Windows structures by offset: build it for x86_64 only");

mod actions;
mod cpu;
mod gpu;
pub mod json;
mod ping;
mod power;
mod procs;
mod services;
mod win32;
mod window_scan;
mod wmi_query;

use json::Json;

/// Bumped whenever a question or the shape of an answer changes in a way the
/// Python side must know about.  `core/native.py` refuses a library whose
/// number is not the one it was written for (and then asks the old way).
pub const ABI: u32 = 2;

/// Answer one question.  `request` is the JSON text of `{"q": "<question>", ...}`.
/// Returns the JSON text of `{"ok": true, "data": ...}` or
/// `{"ok": false, "err": "..."}`.  Never panics.
pub fn answer(request: &str) -> String {
    reply_text(std::panic::catch_unwind(|| dispatch(request)))
}

/// Carry out one action.  `request` is the JSON text of `{"do": "<action>", ...}`.
/// Answers like `answer`.  Never panics.
pub fn act(request: &str) -> String {
    reply_text(std::panic::catch_unwind(|| dispatch_action(request)))
}

fn reply_text(outcome: std::thread::Result<Result<Reply, String>>) -> String {
    let reply = outcome.unwrap_or_else(|_| {
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
        "gpu_identity" => gpu::identity().map(Into::into),
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
        "service_state" => {
            let name = req["name"].as_str().ok_or("service_state needs a \"name\"")?;
            services::state(name).map(Into::into)
        }
        "power_scheme" => power::scheme().map(Into::into),
        "power_setting" => {
            let scheme = req["scheme"].as_str().unwrap_or("current");
            let sub = req["sub"].as_str().ok_or("power_setting needs a \"sub\"")?;
            let setting = req["setting"].as_str().ok_or("power_setting needs a \"setting\"")?;
            power::setting(scheme, sub, setting).map(Into::into)
        }
        other => Err(format!("unknown question: {other}")),
    }
}

/// The actions.  Deliberately a separate list from the questions above: a
/// request that reaches `dispatch` can never end up here.
fn dispatch_action(request: &str) -> Result<Reply, String> {
    let req = Json::parse(request).map_err(|e| format!("not JSON: {e}"))?;
    let action = req["do"].as_str().ok_or("no action (\"do\") given")?;
    match action {
        "trim_working_sets" => {
            let only: Option<Vec<u32>> = match req["pids"].as_array() {
                None => None,
                Some(items) => Some(
                    items
                        .iter()
                        .map(|v| v.as_u64().and_then(|n| u32::try_from(n).ok()))
                        .collect::<Option<Vec<u32>>>()
                        .ok_or("\"pids\" must be a list of process ids")?,
                ),
            };
            actions::trim_working_sets(only.as_deref()).map(Into::into)
        }
        "purge_standby_list" => actions::purge_standby_list().map(Into::into),
        other => Err(format!("unknown action: {other}")),
    }
}

// ─────────────────────────────────────────────────────────────────────────
// The three functions the DLL exports
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
    exported(request, request_len, out, out_cap, answer)
}

/// An action is refused outright when the caller's buffer is smaller than
/// this, so "the answer did not fit, ask again" can never repeat an action.
/// (Every action's answer is far smaller.)
const ACTION_REPLY_ROOM: u32 = 4096;

/// Carry out the action in `request[..request_len]` (UTF-8 JSON, `{"do":
/// "..."}`) and write what it did into `out[..out_cap]`.  The buffer and the
/// return value work as for `vispora_native_call`, with one difference: a
/// buffer of fewer than 4096 bytes is refused before anything is done (the
/// return value is -4096), so an action is never carried out twice because
/// its answer did not fit.
///
/// # Safety
/// As for `vispora_native_call`.
#[no_mangle]
pub unsafe extern "system" fn vispora_native_act(
    request: *const u8,
    request_len: u32,
    out: *mut u8,
    out_cap: u32,
) -> i32 {
    if out_cap < ACTION_REPLY_ROOM {
        return -(ACTION_REPLY_ROOM as i32);
    }
    exported(request, request_len, out, out_cap, act)
}

unsafe fn exported(
    request: *const u8,
    request_len: u32,
    out: *mut u8,
    out_cap: u32,
    handler: fn(&str) -> String,
) -> i32 {
    if request.is_null() || out.is_null() {
        return 0;
    }
    let bytes = std::slice::from_raw_parts(request, request_len as usize);
    let Ok(text) = std::str::from_utf8(bytes) else {
        return 0;
    };
    let reply = handler(text);
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

    #[test]
    fn questions_and_actions_cannot_reach_each_other() {
        // An action's name asked as a question is an unknown question...
        for name in ["trim_working_sets", "purge_standby_list"] {
            let a = ask(&format!("{{\"q\":\"{name}\"}}"));
            assert_eq!(a["ok"], Json::Bool(false), "{name}");
            assert!(a["err"].as_str().unwrap().contains("unknown question"), "{name}");
        }
        // ...and a question sent as an action is an unknown action, as is a
        // request that only has a "q".
        let do_ = |request: &str| Json::parse(&act(request)).unwrap();
        assert!(do_("{\"do\":\"process_io\"}")["err"].as_str().unwrap().contains("unknown action"));
        assert!(do_("{\"q\":\"trim_working_sets\"}")["err"].as_str().unwrap().contains("no action"));
        assert_eq!(do_("{\"do\":\"trim_working_sets\",\"pids\":[\"x\"]}")["ok"], Json::Bool(false));
        assert_eq!(do_("{\"do\":\"trim_working_sets\",\"pids\":[99999999999]}")["ok"], Json::Bool(false));
    }

    #[test]
    fn an_action_goes_through_the_exported_function_once() {
        // Only this test program is trimmed.
        let _alone = actions::TRIMMING_THIS_PROCESS.lock().unwrap_or_else(|p| p.into_inner());
        let request = format!("{{\"do\":\"trim_working_sets\",\"pids\":[{}]}}", std::process::id());
        let mut out = vec![0u8; 8192];
        let wrote = unsafe {
            vispora_native_act(request.as_ptr(), request.len() as u32, out.as_mut_ptr(), out.len() as u32)
        };
        assert!(wrote > 0);
        let a = Json::parse(std::str::from_utf8(&out[..wrote as usize]).unwrap()).unwrap();
        assert_eq!(a["ok"], Json::Bool(true), "{a}");
        assert_eq!(a["data"]["trimmed"].as_u64(), Some(1), "{a}");
        // Too small a buffer is refused before anything is done, with the room it wants.
        let mut small = [0u8; 64];
        let refused = unsafe {
            vispora_native_act(request.as_ptr(), request.len() as u32, small.as_mut_ptr(), small.len() as u32)
        };
        assert_eq!(refused, -(ACTION_REPLY_ROOM as i32));
        assert_eq!(small, [0u8; 64]);
    }

    #[test]
    fn the_new_questions_answer_through_the_front_door() {
        let events = ask("{\"q\":\"service_state\",\"name\":\"EventLog\"}");
        assert_eq!(events["data"]["installed"], Json::Bool(true), "{events}");
        assert_eq!(ask("{\"q\":\"service_state\"}")["ok"], Json::Bool(false));
        let plan = ask("{\"q\":\"power_scheme\"}");
        assert_eq!(plan["data"]["guid"].as_str().unwrap().len(), 36, "{plan}");
        let max = ask(concat!(
            "{\"q\":\"power_setting\",\"sub\":\"54533251-82be-4824-96c1-47b60b740d00\",",
            "\"setting\":\"bc5038f7-23e0-4960-96da-33abaf5935ec\"}"
        ));
        assert_eq!(max["ok"], Json::Bool(true), "{max}");
        assert_eq!(ask("{\"q\":\"power_setting\"}")["ok"], Json::Bool(false));
        // gpu_identity answers or says why not, depending on the PC.
        let card = ask("{\"q\":\"gpu_identity\"}");
        assert!(card["ok"] == Json::Bool(true) || card["err"].as_str().unwrap().len() > 5, "{card}");
    }
}
