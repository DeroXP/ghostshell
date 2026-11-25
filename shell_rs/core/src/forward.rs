//! Passing a call on to the backend.
//!
//! The backend listens on a port of 127.0.0.1, and 127.0.0.1 is the only
//! place this file ever connects to: the address is written here, not taken
//! from the request.  A call is passed on as the page made it (method, path
//! and query, body) with the few headers the backend reads, and with the
//! Host the backend expects.  What a browser adds about where a request came
//! from (Origin, Referer, cookies) is not passed on: the shell has looked at
//! it (router.rs), and to the backend the caller is the shell.
//!
//! One connection per call, closed when the answer is in.  On 127.0.0.1 that
//! costs a fraction of a millisecond, and nothing has to be kept in step.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::{Duration, Instant};

use crate::http::{Request, Response};

/// How long things may take, and how much an answer may be.
#[derive(Debug, Clone)]
pub struct Limits {
    pub connect: Duration,
    /// The longest silence while the backend works on an answer.  Long: some
    /// calls do real work before they answer, and the page has limits of
    /// its own where it wants them.
    pub silence: Duration,
    /// The longest the whole exchange may take, where there is such a
    /// limit.  Silence alone is no limit: an answer that arrives a byte at
    /// a time is never silent.
    pub whole: Option<Duration>,
    pub body: usize,
}

impl Default for Limits {
    fn default() -> Limits {
        Limits { connect: Duration::from_secs(2), silence: Duration::from_secs(30 * 60), whole: None, body: 256 * 1024 * 1024 }
    }
}

const METHODS: [&str; 7] = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];
/// The headers of a call that the backend reads.  The secret is among them.
const PASSED_ON: [&str; 4] = ["content-type", "accept", "accept-language", "x-vispora-token"];
/// What is about the connection and not about the answer, and is not handed
/// back to the page.
const NOT_HANDED_BACK: [&str; 11] = ["connection", "keep-alive", "transfer-encoding", "content-length", "proxy-authenticate",
                                     "proxy-authorization", "te", "trailer", "upgrade", "server", "date"];
const MOST_HEADERS: usize = 64 * 1024;

/// Why a call cannot be passed on as it is.
fn mistake_in(request: &Request) -> Option<&'static str> {
    if !METHODS.contains(&request.method.as_str()) {
        return Some("a method the backend does not know");
    }
    // One line of text is made of these, so a line break or a blank in one
    // of them would be another line, or another request.
    if !request.target.starts_with('/') || request.target.bytes().any(|b| b <= b' ' || b == 0x7f) {
        return Some("an address that is not a path");
    }
    for (name, value) in &request.headers {
        let plain_name = !name.is_empty() && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_');
        if !plain_name || value.bytes().any(|b| b == b'\r' || b == b'\n' || b == 0) {
            return Some("a header that is not one line");
        }
    }
    None
}

/// How often a wait for the backend's answer looks up to ask whether it is
/// still worth waiting.
const LOOK_UP_EVERY: Duration = Duration::from_millis(250);

/// Send `request` to the backend on 127.0.0.1:`port` and read its answer.
pub fn send(port: u16, request: &Request, limits: &Limits) -> Result<Response, String> {
    send_while(port, request, limits, &|| true)
}

/// The same, for as long as `worth_waiting` says so.  The port is the
/// shell's own listening socket: a call to it is taken in whether or not a
/// backend is there to answer, so "nobody is there" does not show as a
/// refused connection.  Whoever knows that the backend is gone for good
/// says so here, and the wait ends.
pub fn send_while(port: u16, request: &Request, limits: &Limits, worth_waiting: &dyn Fn() -> bool) -> Result<Response, String> {
    if let Some(why) = mistake_in(request) {
        return Err(format!("not passed on: {why}"));
    }
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream =
        TcpStream::connect_timeout(&address, limits.connect).map_err(|e| format!("the backend does not answer on port {port}: {e}"))?;
    let _ = stream.set_nodelay(true);
    stream.set_write_timeout(Some(limits.silence)).map_err(|e| e.to_string())?;
    let began = Instant::now();

    let mut head = format!("{} {} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n", request.method, request.target);
    for (name, value) in &request.headers {
        if PASSED_ON.contains(&name.to_ascii_lowercase().as_str()) {
            head.push_str(&format!("{name}: {value}\r\n"));
        }
    }
    if !request.body.is_empty() || matches!(request.method.as_str(), "POST" | "PUT" | "PATCH" | "DELETE") {
        head.push_str(&format!("Content-Length: {}\r\n", request.body.len()));
    }
    head.push_str("\r\n");
    let sent = stream.write_all(head.as_bytes()).and_then(|()| stream.write_all(&request.body)).and_then(|()| stream.flush());
    sent.map_err(|e| format!("the call could not be handed to the backend: {e}"))?;
    let answer = Answer {
        stream,
        read: Vec::new(),
        at: 0,
        most: limits.body,
        silence: limits.silence,
        over_at: limits.whole.map(|whole| began + whole),
        heard_at: Instant::now(),
        worth_waiting,
    };
    answer.read(request.method == "HEAD")
}

/// The backend's answer, as it comes in.
struct Answer<'a> {
    stream: TcpStream,
    read: Vec<u8>,
    at: usize,
    most: usize,
    silence: Duration,
    /// When the whole exchange is over, if it has a limit.
    over_at: Option<Instant>,
    /// When the backend was last heard from.
    heard_at: Instant,
    worth_waiting: &'a dyn Fn() -> bool,
}

impl Answer<'_> {
    /// More from the backend.  False at the end.
    fn more(&mut self) -> Result<bool, String> {
        let mut piece = [0u8; 16 * 1024];
        loop {
            // Wait a short while at a time, and look up in between.
            let now = Instant::now();
            let mut wait = LOOK_UP_EVERY.min(self.silence.saturating_sub(now - self.heard_at));
            if let Some(over_at) = self.over_at {
                wait = wait.min(over_at.saturating_duration_since(now));
            }
            // (a wait of no length would be read by Windows as "for ever")
            let _ = self.stream.set_read_timeout(Some(wait.max(Duration::from_millis(1))));
            match self.stream.read(&mut piece) {
                Ok(0) => return Ok(false),
                Ok(n) => {
                    self.heard_at = Instant::now();
                    self.read.extend_from_slice(&piece[..n]);
                    if self.read.len() > self.most + MOST_HEADERS {
                        return Err("the backend's answer is far too large".into());
                    }
                    if self.over_at.is_some_and(|over_at| Instant::now() >= over_at) {
                        return Err("the backend took too long over its answer".into());
                    }
                    return Ok(true);
                }
                Err(e) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => {
                    let now = Instant::now();
                    if self.over_at.is_some_and(|over_at| now >= over_at) {
                        return Err("the backend took too long over its answer".into());
                    }
                    if now - self.heard_at >= self.silence {
                        return Err("the backend stopped answering".into());
                    }
                    if !(self.worth_waiting)() {
                        return Err("the backend is gone".into());
                    }
                }
                Err(e) => return Err(format!("the backend stopped answering: {e}")),
            }
        }
    }

    /// The next line, without its line break.
    fn line(&mut self, most: usize) -> Result<String, String> {
        loop {
            if let Some(end) = self.read[self.at..].windows(2).position(|pair| pair == b"\r\n") {
                let line = String::from_utf8_lossy(&self.read[self.at..self.at + end]).into_owned();
                self.at += end + 2;
                return Ok(line);
            }
            if self.read.len() - self.at > most {
                return Err("the backend's answer does not start like one".into());
            }
            if !self.more()? {
                return Err("the backend's answer was cut short".into());
            }
        }
    }

    /// The next `n` bytes.
    fn exactly(&mut self, n: usize) -> Result<Vec<u8>, String> {
        if n > self.most {
            return Err("the backend's answer is far too large".into());
        }
        while self.read.len() - self.at < n {
            if !self.more()? {
                return Err("the backend's answer was cut short".into());
            }
        }
        let bytes = self.read[self.at..self.at + n].to_vec();
        self.at += n;
        Ok(bytes)
    }

    fn read(mut self, head_only: bool) -> Result<Response, String> {
        let (status, all) = loop {
            // "HTTP/1.1 200 OK"
            let first = self.line(MOST_HEADERS)?;
            let mut words = first.splitn(3, ' ');
            let status = match (words.next(), words.next().and_then(|code| code.parse::<u16>().ok())) {
                (Some(version), Some(code)) if version.starts_with("HTTP/1.") && (100..=599).contains(&code) => code,
                _ => return Err("the backend's answer does not start like one".into()),
            };
            let mut all: Vec<(String, String)> = Vec::new();
            loop {
                let line = self.line(MOST_HEADERS)?;
                if line.is_empty() {
                    break;
                }
                if let Some((name, value)) = line.split_once(':') {
                    all.push((name.trim().to_string(), value.trim().to_string()));
                }
                if self.at > 4 * MOST_HEADERS {
                    return Err("the backend's answer has far too many headers".into());
                }
            }
            // "100 Continue" and its like say that the answer is still to
            // come: it is the next one that counts.
            if !(100..200).contains(&status) || status == 101 {
                break (status, all);
            }
        };
        let of = |wanted: &str| all.iter().find(|(name, _)| name.eq_ignore_ascii_case(wanted)).map(|(_, value)| value.as_str());
        let body = if head_only || status < 200 || status == 204 || status == 304 {
            Vec::new()
        } else if of("transfer-encoding").is_some_and(|coding| coding.to_ascii_lowercase().contains("chunked")) {
            self.in_pieces()?
        } else if let Some(length) = of("content-length") {
            let length: usize = length.parse().map_err(|_| "the backend's answer gives no usable length".to_string())?;
            self.exactly(length)?
        } else {
            // (neither: the answer is everything up to the end of the connection)
            while self.more()? {}
            if self.read.len() - self.at > self.most {
                return Err("the backend's answer is far too large".into());
            }
            self.read.split_off(self.at)
        };
        let headers = all.into_iter().filter(|(name, _)| !NOT_HANDED_BACK.contains(&name.to_ascii_lowercase().as_str())).collect();
        Ok(Response { status, headers, body })
    }

    /// A body sent in pieces, each with its length in front of it.
    fn in_pieces(&mut self) -> Result<Vec<u8>, String> {
        let mut body = Vec::new();
        loop {
            let line = self.line(1024)?;
            let size = line.split(';').next().unwrap_or("").trim();
            let size = usize::from_str_radix(size, 16).map_err(|_| "the backend's answer is in pieces that cannot be read".to_string())?;
            if size == 0 {
                // (what may follow the last piece, up to an empty line)
                while !self.line(MOST_HEADERS)?.is_empty() {}
                return Ok(body);
            }
            if body.len().checked_add(size).map_or(true, |all| all > self.most) {
                return Err("the backend's answer is far too large".into());
            }
            body.extend(self.exactly(size)?);
            if !self.line(2)?.is_empty() {
                return Err("the backend's answer is in pieces that cannot be read".into());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_call_that_is_not_one_line_each_is_not_passed_on() {
        let limits = Limits::default();
        let port = 9; // (never connected to: each of these is refused before)
        for (request, why) in [
            (Request::new("BREW", "/api/x"), "method"),
            (Request::new("GET", "api/x"), "path"),
            (Request::new("GET", "http://evil.example/api/x"), "path"),
            (Request::new("GET", "/api/x y"), "path"),
            (Request::new("GET", "/api/x\r\nHost: evil.example"), "path"),
            (Request::new("GET", "/api/x").with_header("X-Vispora-Token", "a\r\nHost: evil.example"), "header"),
            (Request::new("GET", "/api/x").with_header("Bad Name", "x"), "header"),
            (Request::new("GET", "/api/x").with_header("", "x"), "header"),
        ] {
            let refused = send(port, &request, &limits).unwrap_err();
            assert!(refused.starts_with("not passed on: ") && refused.contains(why), "{request:?}: {refused}");
        }
    }
}
