//! Passing a call on to the backend: what is sent, what is not, and how the
//! answer is read.  The backend is a stand-in in this test (common/mod.rs);
//! the connection to it is a real one, on 127.0.0.1.

#![cfg(windows)]

mod common;

use std::time::Duration;

use common::*;
use vispora_shell_core::forward::{self, Limits};
use vispora_shell_core::http::Request;
use vispora_shell_core::json::Json;
use vispora_shell_core::token;

#[test]
fn a_call_is_passed_on_as_the_page_made_it() {
    let rig = Rig::ready();
    let request = rig
        .asking("POST", "/api/echo?since=12&name=a%20b")
        .with_header("Content-Type", "application/json")
        .with_header("Accept", "application/json")
        .with_header("Origin", &rig.shell.settings.origin)
        .with_header("Referer", "http://vispora.localhost/")
        .with_header("Cookie", "session=1")
        .with_header("User-Agent", "Mozilla/5.0")
        .with_header("Sec-Fetch-Site", "same-origin")
        .with_header("Accept-Encoding", "gzip, br")
        .with_header("X-Something-Else", "1")
        .with_body("{\"a\": 1}");
    let answer = rig.shell.handle(&request);
    assert_eq!(answer.status, 200, "{}", text(&answer));
    let got = answer.to_json();
    assert_eq!((got["method"].as_str(), got["target"].as_str(), got["body"].as_str()),
               (Some("POST"), Some("/api/echo?since=12&name=a%20b"), Some("{\"a\": 1}")));
    let backend = rig.stage.last();
    let sent = &got["headers"];
    // What the backend reads, and the address it expects to be asked under.
    assert_eq!(sent["host"].as_str(), Some(format!("127.0.0.1:{}", backend.port).as_str()));
    assert_eq!(sent["x-vispora-token"].as_str(), Some(rig.shell.token().as_str()));
    assert_eq!((sent["content-type"].as_str(), sent["accept"].as_str()), (Some("application/json"), Some("application/json")));
    assert_eq!((sent["content-length"].as_str(), sent["connection"].as_str()), (Some("8"), Some("close")));
    // What a browser adds about where a call came from stays with the shell.
    for name in ["origin", "referer", "cookie", "user-agent", "sec-fetch-site", "accept-encoding", "x-something-else"] {
        assert_eq!(sent[name], Json::Null, "{name} was passed on");
    }
    // What is about the connection is not handed back to the page.
    assert_eq!(answer.header("Content-Type"), Some("application/json"));
    for name in ["Server", "Date", "Connection", "Content-Length"] {
        assert_eq!(answer.header(name), None, "{name}");
    }
}

#[test]
fn an_answer_is_read_to_its_end_however_the_end_is_marked() {
    let rig = Rig::ready();
    // By its length.
    let big = rig.call("GET", "/api/big?300000");
    assert_eq!((big.status, big.body.len()), (200, 300_000));
    assert!(big.body.iter().all(|b| *b == b'x'));
    // In pieces, each with its own length.
    let pieces = rig.call("GET", "/api/in-pieces");
    assert_eq!((pieces.status, text(&pieces).as_str()), (200, "hello from pieces!"));
    assert_eq!(pieces.header("Transfer-Encoding"), None);
    // By the end of the connection.
    let rest = rig.call("GET", "/api/to-the-end");
    assert_eq!((rest.status, text(&rest).as_str()), (200, "all there is"));
    // With nothing at all.
    let empty = rig.call("GET", "/api/empty");
    assert_eq!((empty.status, empty.body.len()), (204, 0));
    assert_eq!((empty.header("X-Kept"), empty.header("Keep-Alive")), (Some("yes"), None));
    // A path the backend does not have is the backend's to say.
    let unknown = rig.call("GET", "/api/no/such/thing");
    assert_eq!(unknown.status, 404);
    assert_eq!(unknown.to_json()["path"].as_str(), Some("/api/no/such/thing"));
}

#[test]
fn an_answer_that_is_none_is_said_in_plain_words() {
    let mut settings = settings();
    settings.forward.body = 2000;
    let rig = Rig::with(vec![Behaviour::default()], settings);
    rig.shell.start();
    rig.wait_ready();
    for target in ["/api/cut-short", "/api/not-http", "/api/big?5000"] {
        let answer = rig.call("GET", target);
        assert_eq!(answer.status, 502, "{target}");
        let said = answer.to_json();
        assert_eq!(said["ok"], Json::Bool(false));
        assert_eq!(said["err"].as_str(), Some("Vispora's background program did not answer. Try again."));
    }
    // The log has what really happened.
    let history = rig.shell.history().join("\n");
    assert!(history.contains("GET /api/cut-short was not answered: the backend's answer was cut short"), "{history}");
    assert!(history.contains("does not start like one") && history.contains("far too large"), "{history}");
    // Within the limit, the same call is fine.
    assert_eq!(rig.call("GET", "/api/big?1500").body.len(), 1500);
}

#[test]
fn a_port_nobody_listens_on_is_an_error_and_soon() {
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let limits = Limits { connect: Duration::from_millis(300), ..Limits::default() };
    let began = std::time::Instant::now();
    let refused = forward::send(port, &Request::new("GET", "/api/x").with_header(token::HEADER, "x"), &limits).unwrap_err();
    assert!(refused.contains(&format!("the backend does not answer on port {port}")), "{refused}");
    assert!(began.elapsed() < Duration::from_secs(3));
}

#[test]
fn a_backend_that_goes_silent_is_not_waited_for_longer_than_the_limit() {
    let mut settings = settings();
    settings.forward.silence = Duration::from_millis(300);
    let rig = Rig::with(vec![Behaviour::default()], settings);
    rig.shell.start();
    rig.wait_ready();
    let began = std::time::Instant::now();
    let answer = rig.call("GET", "/api/slow?2500");
    assert_eq!(answer.status, 502);
    assert!(began.elapsed() < Duration::from_millis(2000), "{:?}", began.elapsed());
    assert!(rig.shell.history().join("\n").contains("the backend stopped answering"));
    // A call that takes its time within the limit is simply answered.
    assert_eq!(rig.call("GET", "/api/slow?100").status, 200);
}
