//! The real network client (win.rs: Windows' own HTTP client), talking to a
//! listener on this PC.  Nothing leaves the machine: the "server" is a thread
//! in this test on 127.0.0.1.
//!
//! The pipeline tests fake the network.  This is the one place where the
//! code that really fetches is run: its request, how it reads an answer of
//! known and of unknown length, redirects, errors and time-outs, and a whole
//! install through it into the sandbox.

#![cfg(windows)]

mod common;

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};

use common::*;
use vispora_updater_core::platform::Net;
use vispora_updater_core::sha256;
use vispora_updater_core::state::State;
use vispora_updater_core::win;

/// A small web server for one test: it answers by path and writes down what
/// each request sent.
struct Listener {
    base: String,
    /// path -> the request's headers, lower-case names
    seen: Arc<Mutex<HashMap<String, HashMap<String, String>>>>,
    /// path -> body, for the paths a test adds
    files: Arc<Mutex<HashMap<String, Vec<u8>>>>,
}

fn big() -> Vec<u8> {
    (0..300_000u32).map(|i| (i % 251) as u8).collect()
}

fn respond(mut stream: TcpStream, seen: &Mutex<HashMap<String, HashMap<String, String>>>, files: &Mutex<HashMap<String, Vec<u8>>>) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut first = String::new();
    if reader.read_line(&mut first).is_err() {
        return;
    }
    let path = first.split_whitespace().nth(1).unwrap_or("/").to_string();
    let mut headers = HashMap::new();
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).unwrap_or(0) == 0 || line.trim().is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.insert(name.trim().to_lowercase(), value.trim().to_string());
        }
    }
    seen.lock().unwrap().insert(path.clone(), headers);
    let plain = |status: &str, kind: &str, body: &[u8]| {
        let mut out = format!("HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).into_bytes();
        out.extend_from_slice(body);
        out
    };
    let added = files.lock().unwrap().get(&path).cloned();
    let answer = match (path.as_str(), added) {
        (_, Some(body)) => plain("200 OK", "application/octet-stream", &body),
        ("/json", _) => plain("200 OK", "application/json", b"{\"available\": true, \"version\": \"9.0.0\"}"),
        ("/big", _) => plain("200 OK", "application/octet-stream", &big()),
        ("/chunked", _) => {
            // No length said: the body comes in pieces, each with its size.
            let mut out = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n".to_vec();
            for piece in big().chunks(70_000) {
                out.extend_from_slice(format!("{:x}\r\n", piece.len()).as_bytes());
                out.extend_from_slice(piece);
                out.extend_from_slice(b"\r\n");
            }
            out.extend_from_slice(b"0\r\n\r\n");
            out
        }
        ("/moved", _) => b"HTTP/1.1 302 Found\r\nLocation: /json\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec(),
        ("/moved-twice", _) => b"HTTP/1.1 301 Moved\r\nLocation: /moved\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec(),
        ("/slow", _) => {
            std::thread::sleep(std::time::Duration::from_secs(12));
            plain("200 OK", "text/plain", b"late")
        }
        ("/cut-short", _) => {
            // Says 300000 bytes, sends 1000 and hangs up.
            let mut out = b"HTTP/1.1 200 OK\r\nContent-Length: 300000\r\nConnection: close\r\n\r\n".to_vec();
            out.extend_from_slice(&big()[..1000]);
            out
        }
        ("/error", _) => plain("500 Internal Server Error", "text/html", b"<html>oops</html>"),
        _ => plain("404 Not Found", "text/html", b"<html>404 Not Found</html>"),
    };
    let _ = stream.write_all(&answer);
    let _ = stream.flush();
}

impl Listener {
    fn start() -> Listener {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        let seen: Arc<Mutex<HashMap<String, HashMap<String, String>>>> = Arc::default();
        let files: Arc<Mutex<HashMap<String, Vec<u8>>>> = Arc::default();
        let (seen_by_server, served) = (seen.clone(), files.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let (seen, files) = (seen_by_server.clone(), served.clone());
                std::thread::spawn(move || respond(stream, &seen, &files));
            }
        });
        Listener { base, seen, files }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base)
    }

    fn header(&self, path: &str, name: &str) -> Option<String> {
        self.seen.lock().unwrap().get(path)?.get(name).cloned()
    }
}

fn read_all(net: &dyn Net, url: &str, timeout_s: u32) -> Result<(Option<u64>, Vec<u8>), String> {
    let mut body = net.open(url, &[("User-Agent", "VisporaUpdater/test")], timeout_s)?;
    let length = body.content_length();
    let mut out = Vec::new();
    let mut buf = vec![0u8; 65536];
    loop {
        let n = body.read(&mut buf)?;
        if n == 0 {
            return Ok((length, out));
        }
        out.extend_from_slice(&buf[..n]);
    }
}

#[test]
fn a_request_carries_its_headers_and_the_answer_is_read() {
    let server = Listener::start();
    let net = win::platform().net;
    let body = net
        .get(&server.url("/json"), &[("User-Agent", "VisporaUpdater/2.0.0"), ("Accept", "application/vnd.github+json"),
                                    ("X-GitHub-Api-Version", "2022-11-28")], 10)
        .unwrap();
    assert_eq!(body, b"{\"available\": true, \"version\": \"9.0.0\"}");
    assert_eq!(server.header("/json", "user-agent").as_deref(), Some("VisporaUpdater/2.0.0"));
    assert_eq!(server.header("/json", "accept").as_deref(), Some("application/vnd.github+json"));
    assert_eq!(server.header("/json", "x-github-api-version").as_deref(), Some("2022-11-28"));
    assert!(server.header("/json", "host").unwrap().starts_with("127.0.0.1:"));
    // A query is sent on as it is.
    let _ = net.get(&server.url("/releases?per_page=30"), &[], 10);
    assert!(server.seen.lock().unwrap().contains_key("/releases?per_page=30"));
}

#[test]
fn a_download_is_read_whole_whether_or_not_its_length_is_said() {
    let server = Listener::start();
    let net = win::platform().net;
    let (length, body) = read_all(net.as_ref(), &server.url("/big"), 10).unwrap();
    assert_eq!(length, Some(300_000));
    assert!(body == big(), "{} bytes", body.len());
    let (length, body) = read_all(net.as_ref(), &server.url("/chunked"), 10).unwrap();
    assert_eq!(length, None, "no length was said");
    assert!(body == big(), "{} bytes", body.len());
}

#[test]
fn a_redirect_is_followed_and_an_error_is_an_error() {
    let server = Listener::start();
    let net = win::platform().net;
    assert_eq!(read_all(net.as_ref(), &server.url("/moved"), 10).unwrap().1, b"{\"available\": true, \"version\": \"9.0.0\"}");
    assert_eq!(read_all(net.as_ref(), &server.url("/moved-twice"), 10).unwrap().1.len(), 39, "through two redirects");
    assert_eq!(read_all(net.as_ref(), &server.url("/missing"), 10).unwrap_err(), "HTTP Error 404");
    assert_eq!(read_all(net.as_ref(), &server.url("/error"), 10).unwrap_err(), "HTTP Error 500");
    // Nobody listening, a name that does not exist, and what is no address at all.
    let closed = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    assert!(read_all(net.as_ref(), &format!("http://127.0.0.1:{closed}/x"), 5).is_err());
    for bad in ["ftp://127.0.0.1/x", "file:///C:/Windows/notepad.exe", "127.0.0.1/x", "12345", ""] {
        let err = read_all(net.as_ref(), bad, 5).unwrap_err();
        assert!(err.contains("not an http(s) address"), "{bad}: {err}");
    }
    assert!(net.open(&server.url("/json"), &[("X-Bad", "a\r\nInjected: yes")], 5).is_err(), "a header cannot carry a line break");
}

#[test]
fn an_answer_that_does_not_come_is_given_up_on() {
    let server = Listener::start();
    let net = win::platform().net;
    let started = std::time::Instant::now();
    // (The server answers after 12 seconds.  Windows' client does not time
    // anything under about four seconds, so the limit tried here is five;
    // the updater's own limits are 15 and 60.)
    let err = read_all(net.as_ref(), &server.url("/slow"), 5).unwrap_err();
    let waited = started.elapsed().as_secs_f64();
    assert!((4.0..9.0).contains(&waited), "waited {waited} s for a 5 second limit");
    assert!(err.contains("timed out"), "{err}");
    // A download that is cut short is not taken for a whole one.
    match read_all(net.as_ref(), &server.url("/cut-short"), 5) {
        Err(why) => assert!(why.contains("interrupted"), "{why}"),
        Ok((length, body)) => panic!("{} of {length:?} bytes taken for the whole file", body.len()),
    }
}

#[test]
fn a_whole_install_through_the_real_client() {
    // The update server is the listener; everything else is the sandbox.
    let server = Listener::start();
    let build = app_new();
    let meta = format!(
        "{{\"available\": true, \"version\": \"9.0.0\", \"sha256\": \"{}\", \"size_bytes\": {}, \"download_url\": \"{}\"}}",
        sha256::hex_of(&build),
        build.len(),
        server.url("/download/stable")
    );
    server.files.lock().unwrap().insert("/version/stable".into(), meta.into_bytes());
    server.files.lock().unwrap().insert("/download/stable".into(), build.clone());

    let mut rig = Rig::with_net(Some(win::platform().net));
    let folder = vispora_updater_core::paths::join(&rig.root, "install");
    rig.target = vispora_updater_core::paths::join(&folder, "Vispora.exe");
    std::fs::create_dir_all(&folder).unwrap();
    std::fs::write(&rig.target, old()).unwrap();
    rig.updater.init_state(State {
        target_dir: folder,
        target_exe: rig.target.clone(),
        channel: "stable".into(),
        current_version: "3.6.0".into(),
        server_url: server.base.clone(),
        source: "server".into(), // (GitHub is not asked: nothing leaves this PC)
        silent: true,
        updater_version: vispora_updater_core::UPDATER_VERSION.into(),
        ..State::default()
    });
    assert_eq!(rig.run(), 0, "{}", rig.error());
    assert!(rig.installed() == build && rig.backups().len() == 1 && rig.part_files().is_empty());
    assert_eq!(rig.procs.launched(), [vec![rig.target.clone()]]);
    let st = rig.state();
    assert_eq!((st.total_bytes, st.downloaded_bytes, st.progress), (build.len() as u64, build.len() as u64, 100));
    assert_eq!(server.header("/download/stable", "user-agent"), Some(format!("VisporaUpdater/{}", vispora_updater_core::UPDATER_VERSION)));
    assert_eq!(rig.install_info()["version"].as_str(), Some("9.0.0"));

    // The same server sending a file that is not what it said: refused.
    server.files.lock().unwrap().insert("/download/stable".into(), app_other());
    std::fs::write(&rig.target, old()).unwrap();
    assert_eq!(rig.run(), 6);
    assert!(rig.installed() == old() && rig.error().contains("SHA-256 mismatch"));
}
