//! Which request is answered where.
//!
//! Every request of the page comes here (the window hands them over; none
//! of them ever crosses a network port on its way to the shell):
//!
//! * the page and its two files are answered from what is compiled in;
//! * the window's own calls (`/api/window/...`) are answered by the shell,
//!   because the window is the shell's;
//! * the calls of what has moved from the backend into the shell are
//!   answered by the shell (moved/), where the shell takes them;
//! * every other `/api/...` call is passed on to the backend, unchanged.
//!   As modules move from Python to Rust, their calls move from the last
//!   line of this list to the one above it.
//!
//! Before any `/api/...` call is answered or passed on, it is asked the three
//! things the backend has always asked (app.py, _reject_foreign_requests):
//! does it come from the app's own page, and does it carry the run's secret.
//! The backend asks again when the call reaches it.

use std::sync::Arc;

use crate::forward;
use crate::http::{Request, Response};
use crate::json::Json;
use crate::page;
use crate::shell::{Phase, Shell};
use crate::token;

/// The calls that are the shell's own, all of them made with POST.
const WINDOW_CALLS: [&str; 5] = ["/api/window/minimize", "/api/window/close", "/api/window/show", "/api/window/quit",
                                 "/api/window/close-and-game-mode"];

fn done(ok: bool) -> Response {
    Response::json(200, &Json::object([("ok", ok.into())]))
}

/// As the backend answers a path it does not have, and a method a path does
/// not take.
fn not_found(path: &str) -> Response {
    Response::json(404, &Json::object([("ok", false.into()), ("err", "Endpoint not found".into()), ("path", path.into())]))
}

fn not_allowed(method: &str, path: &str) -> Response {
    Response::refused(405, &format!("Method {method} not allowed for {path}"))
}

/// The page's files are never kept by the window from one build to the next.
fn fresh(mut answer: Response) -> Response {
    answer.headers.push(("Cache-Control".to_string(), "no-store".to_string()));
    answer
}

impl Shell {
    /// Answer one request of the page.
    pub fn handle(self: &Arc<Self>, request: &Request) -> Response {
        let (method, path) = (request.method.as_str(), request.path());
        if !path.starts_with("/api/") {
            let file = if path == "/" {
                Some(("text/html; charset=utf-8", page::index(&self.settings.version, &self.settings.appdata_dir).into_bytes()))
            } else {
                page::file(path).map(|(kind, bytes)| (kind, bytes.to_vec()))
            };
            return match file {
                Some((kind, bytes)) if method == "GET" => fresh(Response::new(200, kind, bytes)),
                Some(_) => not_allowed(method, path),
                None => not_found(path),
            };
        }
        if let Some(refusal) = self.stranger(request) {
            return refusal;
        }
        if WINDOW_CALLS.contains(&path) && method != "POST" {
            return not_allowed(method, path);
        }
        let window = self.window();
        match path {
            "/api/window/minimize" => done(window.is_some_and(|w| w.minimize())),
            "/api/window/close" => done(window.is_some_and(|w| w.hide())),
            "/api/window/show" => done(window.is_some_and(|w| w.show())),
            "/api/window/quit" => {
                self.quit("the page asked for it");
                Response::json(200, &Json::object([("ok", true.into()), ("msg", "Goodbye".into())]))
            }
            "/api/window/close-and-game-mode" => {
                // Two things: the game monitor is switched on, which is
                // the backend's, and the window goes out of sight, which is
                // the shell's.  In that order, as app.py does it: a window
                // that is gone while the monitor was not switched on (the
                // backend still starting, or stopped) would look like it had
                // worked.  The backend's answer says whether the window was
                // hidden; it has none, so that part is set right here.
                let mut answer = self.pass_on(request);
                let mut said = answer.to_json();
                if answer.status == 200 && said["ok"].truthy() {
                    let hidden = window.is_some_and(|w| w.hide());
                    said.set("hidden", hidden.into());
                    answer.body = said.to_string().into_bytes();
                }
                answer
            }
            _ => {
                // What has moved is answered here, without the backend and
                // without waiting for it.  What the shell does not take of
                // a moved path (see moved/) is the backend's like any other.
                let moved = self.moved.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).clone();
                if let Some(answer) = moved.and_then(|moved| moved.answer(request)) {
                    return answer;
                }
                self.pass_on(request)
            }
        }
    }

    /// Why a call is not from the app's own page, if it is not.
    fn stranger(&self, request: &Request) -> Option<Response> {
        // A browser names the page a call was made from whenever it could be
        // another one.  It has to be the shell's own.
        if request.header("Origin").is_some_and(|origin| origin != self.settings.origin) {
            return Some(Response::refused(403, "cross-origin request refused"));
        }
        if request.header("Sec-Fetch-Site").is_some_and(|site| site != "same-origin" && site != "none") {
            return Some(Response::refused(403, "cross-origin request refused"));
        }
        if !self.token().is(request.header(token::HEADER).unwrap_or("")) {
            return Some(Response::refused(403, "missing or wrong session token"));
        }
        None
    }

    /// Hand a call to the backend and its answer back.  A backend that is
    /// still starting is waited for.
    fn pass_on(&self, request: &Request) -> Response {
        let port = match self.port_when_ready() {
            Ok(port) => port,
            Err((status, why)) => return Response::refused(status, &why),
        };
        // (not waited for once the backend is gone for good, or the shell is closing)
        let worth_waiting = || matches!(self.phase(), Phase::Ready | Phase::Starting);
        match forward::send_while(port, request, &self.settings.forward, &worth_waiting) {
            Ok(answer) => answer,
            Err(why) => {
                self.say(&format!("{} {} was not answered: {why}", request.method, request.path()));
                Response::refused(502, "Vispora's background program did not answer. Try again.")
            }
        }
    }
}
