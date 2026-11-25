//! What the window asks, and what it is told.  A port of updater/server.py:
//! the same paths, the same answers, read by the same script
//! (updater/static/app.js), which is used as it is.
//!
//!   GET  /                       the page
//!   GET  /static/app.js, style.css
//!   GET  /api/status             the state and progress
//!   POST /api/start              start the install pipeline
//!   POST /api/cancel             cancel an install that is running
//!   POST /api/set-channel        the user picked a channel
//!   POST /api/set-target-dir     the user picked an install folder
//!   POST /api/launch-app         start the (just installed) app
//!   POST /api/quit               close the updater
//!   POST /api/rescan             look for an existing install again
//!   POST /api/browse-folder      the "pick a folder" dialog
//!
//! There is no web server here.  `handle` is a function; the window
//! (updater_rs/app) calls it for every request its page makes, and the tests
//! call it directly.

use std::sync::Arc;

use crate::installer::Updater;
use crate::json::Json;
use crate::paths;

// The page is the Python updater's own, one copy for both while both exist.
const INDEX_HTML: &str = include_str!("../../../updater/templates/index.html");
const APP_JS: &str = include_str!("../../../updater/static/app.js");
const STYLE_CSS: &str = include_str!("../../../updater/static/style.css");

pub struct Response {
    pub status: u16,
    pub content_type: &'static str,
    pub body: Vec<u8>,
}

impl Response {
    fn new(status: u16, content_type: &'static str, body: impl Into<Vec<u8>>) -> Response {
        Response { status, content_type, body: body.into() }
    }

    fn json(value: Json) -> Response {
        Response::new(200, "application/json", value.to_string())
    }

    /// The body as JSON (for the tests).
    pub fn to_json(&self) -> Json {
        Json::parse_bytes(&self.body).unwrap_or(Json::Null)
    }
}

fn ok() -> Json {
    Json::object([("ok", true.into())])
}

fn refused(why: impl Into<String>) -> Json {
    Json::object([("ok", false.into()), ("err", why.into().into())])
}

fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&#34;").replace('\'', "&#39;")
}

/// Answer one request of the page.
pub fn handle(updater: &Arc<Updater>, method: &str, path: &str, body: &[u8]) -> Response {
    let path = path.split(['?', '#']).next().unwrap_or("");
    let get = method.eq_ignore_ascii_case("GET");
    let post = method.eq_ignore_ascii_case("POST");
    let known_get = ["/", "/static/app.js", "/static/style.css", "/api/status"];
    let known_post = ["/api/start", "/api/cancel", "/api/set-channel", "/api/set-target-dir", "/api/launch-app",
                      "/api/quit", "/api/rescan", "/api/browse-folder"];
    if (known_get.contains(&path) && !get) || (known_post.contains(&path) && !post) {
        return Response::new(405, "text/plain; charset=utf-8", "Method Not Allowed");
    }
    match path {
        "/" => {
            let version = html_escape(&updater.lock().updater_version);
            Response::new(200, "text/html; charset=utf-8", INDEX_HTML.replace("{{ updater_version }}", &version))
        }
        "/static/app.js" => Response::new(200, "text/javascript; charset=utf-8", APP_JS),
        "/static/style.css" => Response::new(200, "text/css; charset=utf-8", STYLE_CSS),
        "/api/status" => Response::json(updater.lock().to_json()),
        "/api/start" => Response::json(match updater.start_install() {
            Ok(()) => ok(),
            Err(why) => refused(why),
        }),
        "/api/cancel" => {
            let mut st = updater.lock();
            st.cancelled = true;
            st.phase = "cancelled".into();
            st.phase_label = "Cancelled".into();
            Response::json(ok())
        }
        "/api/set-channel" | "/api/set-target-dir" => {
            let data = match Json::parse_bytes(body) {
                Ok(data) => data,
                Err(_) => return Response::new(400, "application/json", refused("the request is not JSON").to_string()),
            };
            Response::json(if path == "/api/set-channel" { set_channel(updater, &data) } else { set_target_dir(updater, &data) })
        }
        "/api/launch-app" => Response::json(launch_app(updater)),
        "/api/quit" => {
            quit(updater);
            Response::json(ok())
        }
        "/api/rescan" => Response::json(rescan(updater)),
        "/api/browse-folder" => Response::json(browse_folder(updater)),
        _ => Response::new(404, "text/plain; charset=utf-8", "Not Found"),
    }
}

fn set_channel(updater: &Updater, data: &Json) -> Json {
    let channel = match data["channel"].as_str() {
        Some(ch @ ("stable" | "beta")) => ch,
        _ => return refused("channel must be 'stable' or 'beta'"),
    };
    let mut st = updater.lock();
    if channel != st.channel {
        // The version the app asked for was this install's channel's.
        // Picking another channel here means "install what that one has".
        st.requested_version.clear();
        st.allow_older = true;
    }
    st.channel = channel.to_string();
    Json::object([("ok", true.into()), ("channel", channel.into())])
}

fn set_target_dir(updater: &Updater, data: &Json) -> Json {
    let dir = data["target_dir"].as_str().unwrap_or("").trim().to_string();
    if dir.is_empty() {
        return refused("target_dir required");
    }
    // It has to exist, or its parent has to (the folder itself is made later).
    let parent = match paths::dirname(&dir) {
        p if p.is_empty() => dir.clone(),
        p => p,
    };
    if !paths::is_dir(&parent) {
        return refused(format!("parent directory not found: {parent}"));
    }
    let exe = updater.pick_app_exe(&dir);
    let mut st = updater.lock();
    st.target_dir = dir.clone();
    st.target_exe = exe;
    Json::object([("ok", true.into()), ("target_dir", dir.into())])
}

/// "Launch Vispora" after an install (the page no longer shows a button for
/// it: the pipeline starts the app itself).
fn launch_app(updater: &Updater) -> Json {
    let target = updater.lock().target_exe.clone();
    if !paths::is_file(&target) {
        return refused(format!("Vispora not found at {target}"));
    }
    match updater.launch_new(&target) {
        Ok(()) => ok(),
        Err(e) => refused(e),
    }
}

/// Close the updater: a moment later, and only once an install that is
/// running has ended (the swap is never cut off half-way).  That holds for
/// every close, the second and the third as much as the first.  From the
/// first one on, no install is started any more: the window stays while
/// this waits, and its Install button with it.
fn quit(updater: &Arc<Updater>) {
    updater.begin_closing();
    let me = Arc::clone(updater);
    let _ = std::thread::Builder::new().name("updater-quit".into()).spawn(move || {
        me.platform.clock.sleep(0.3);
        me.shut_down();
        let exit = me.exit_hook.lock().unwrap_or_else(|p| p.into_inner()).clone();
        if let Some(exit) = exit {
            exit(0);
        }
    });
}

/// Look for an existing install again: for when the app was just moved or
/// installed, to get from install mode to update mode without restarting.
fn rescan(updater: &Updater) -> Json {
    let found = updater.find_existing_install();
    let default_dir = updater.default_install_dir();
    let default_exe = updater.pick_app_exe(&default_dir);
    let mut st = updater.lock();
    st.scan_result = found.clone();
    if found["found"].truthy() {
        st.mode = "update".into();
        st.target_exe = found["path"].py_str();
        st.target_dir = found["dir"].py_str();
        if found["channel"].truthy() && st.channel.is_empty() {
            st.channel = found["channel"].py_str();
        }
        if found["version"].truthy() && st.current_version.is_empty() {
            st.current_version = found["version"].py_str();
        }
    } else {
        st.mode = "install".into();
        // A default folder, if the user has not picked one.
        if st.target_dir.is_empty() {
            st.target_dir = default_dir;
            st.target_exe = default_exe;
        }
    }
    Json::object([("ok", true.into()), ("mode", st.mode.as_str().into()), ("found", found)])
}

fn browse_folder(updater: &Updater) -> Json {
    let hook = updater.folder_hook.lock().unwrap_or_else(|p| p.into_inner()).clone();
    let Some(pick) = hook else {
        return refused("no webview window");
    };
    let start_in = updater.lock().target_dir.clone();
    match pick(&start_in) {
        Err(e) => refused(e),
        Ok(None) => Json::object([("ok", true.into()), ("cancelled", true.into()), ("path", "".into())]),
        Ok(Some(picked)) => {
            let exe = updater.pick_app_exe(&picked);
            let mut st = updater.lock();
            st.target_dir = picked.clone();
            st.target_exe = exe;
            Json::object([("ok", true.into()), ("path", picked.into())])
        }
    }
}
