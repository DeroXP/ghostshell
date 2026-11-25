//! A request and an answer, as the shell passes them around: what the page
//! asked (handed over by the window, never read from a socket) and what goes
//! back to it.

use crate::json::Json;

/// What the page asked.
#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    pub method: String,
    /// The path and, after a `?`, the query: `/api/logs?since=12`.
    pub target: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Request {
    pub fn new(method: &str, target: &str) -> Request {
        Request { method: method.to_ascii_uppercase(), target: target.to_string(), headers: Vec::new(), body: Vec::new() }
    }

    pub fn with_header(mut self, name: &str, value: &str) -> Request {
        self.headers.push((name.to_string(), value.to_string()));
        self
    }

    pub fn with_body(mut self, body: impl Into<Vec<u8>>) -> Request {
        self.body = body.into();
        self
    }

    /// The path alone, without the query.
    pub fn path(&self) -> &str {
        self.target.split(['?', '#']).next().unwrap_or("")
    }

    /// A header's value, whatever the case of its name (the first, if it is
    /// there more than once).
    pub fn header(&self, name: &str) -> Option<&str> {
        header(&self.headers, name)
    }
}

/// What goes back to the page.
#[derive(Debug, Clone, PartialEq)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    pub fn new(status: u16, content_type: &str, body: impl Into<Vec<u8>>) -> Response {
        Response { status, headers: vec![("Content-Type".to_string(), content_type.to_string())], body: body.into() }
    }

    pub fn json(status: u16, value: &Json) -> Response {
        Response::new(status, "application/json", value.to_string())
    }

    /// `{"ok": false, "err": "..."}`: how the backend says no, so that the
    /// page reads the shell's refusals the way it reads the backend's.
    pub fn refused(status: u16, why: &str) -> Response {
        Response::json(status, &Json::object([("ok", false.into()), ("err", why.into())]))
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        header(&self.headers, name)
    }

    /// The body as JSON; Null when it is none.
    pub fn to_json(&self) -> Json {
        Json::parse_bytes(&self.body).unwrap_or(Json::Null)
    }
}

fn header<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers.iter().find(|(n, _)| n.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_request_knows_its_path_and_its_headers() {
        let r = Request::new("post", "/api/logs?since=12&x=/y").with_header("X-Vispora-Token", "abc").with_body("{}");
        assert_eq!((r.method.as_str(), r.path(), r.body.as_slice()), ("POST", "/api/logs", b"{}".as_slice()));
        assert_eq!((r.header("x-vispora-token"), r.header("Origin")), (Some("abc"), None));
        assert_eq!(Request::new("GET", "/").path(), "/");
    }

    #[test]
    fn a_refusal_reads_like_the_backends() {
        let no = Response::refused(403, "missing or wrong session token");
        assert_eq!(no.status, 403);
        assert_eq!(no.header("content-type"), Some("application/json"));
        assert_eq!(String::from_utf8(no.body.clone()).unwrap(), "{\"ok\":false,\"err\":\"missing or wrong session token\"}");
        assert_eq!(no.to_json()["ok"], Json::Bool(false));
    }
}
