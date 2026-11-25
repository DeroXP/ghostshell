//! Just enough JSON to read a question and write an answer.
//!
//! The helper is loaded into a program that runs as administrator, so it is
//! built from this repository's code and Windows itself and nothing else: no
//! crates from outside.  A JSON library is the one thing that would otherwise
//! have been worth one, and what is needed of it fits on a page.

use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    UInt(u64),
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    /// Keys in the order they were added.
    Object(Vec<(String, Json)>),
}

static NULL: Json = Json::Null;

impl Json {
    /// An object from (key, value) pairs.
    pub fn object<const N: usize>(pairs: [(&str, Json); N]) -> Json {
        Json::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_u64(&self) -> Option<u64> {
        match *self {
            Json::UInt(n) => Some(n),
            Json::Int(n) if n >= 0 => Some(n as u64),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Json]> {
        match self {
            Json::Array(items) => Some(items),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Json::Null)
    }

    /// Read JSON text.  Nesting deeper than 32 levels is refused, so no input
    /// can make the reader use up the stack.
    pub fn parse(text: &str) -> Result<Json, String> {
        let mut p = Parser { bytes: text.as_bytes(), at: 0 };
        let value = p.value(0)?;
        p.space();
        if p.at != p.bytes.len() {
            return Err(format!("unexpected text after the value at byte {}", p.at));
        }
        Ok(value)
    }
}

/// `value["key"]`: the member, or null when there is none (or it is no object).
impl std::ops::Index<&str> for Json {
    type Output = Json;
    fn index(&self, key: &str) -> &Json {
        self.get(key).unwrap_or(&NULL)
    }
}

macro_rules! from_number {
    ($variant:ident as $wide:ty: $($t:ty),*) => {$(
        impl From<$t> for Json {
            fn from(n: $t) -> Json { Json::$variant(n as $wide) }
        }
    )*};
}
from_number!(UInt as u64: u8, u16, u32, u64, usize);
from_number!(Int as i64: i8, i16, i32, i64);

impl From<bool> for Json {
    fn from(b: bool) -> Json {
        Json::Bool(b)
    }
}

impl From<f64> for Json {
    /// A number that JSON cannot write (NaN, infinity) becomes null.
    fn from(f: f64) -> Json {
        if f.is_finite() {
            Json::Float(f)
        } else {
            Json::Null
        }
    }
}

impl From<&str> for Json {
    fn from(s: &str) -> Json {
        Json::Str(s.to_string())
    }
}

impl From<String> for Json {
    fn from(s: String) -> Json {
        Json::Str(s)
    }
}

impl<T: Into<Json>> From<Option<T>> for Json {
    fn from(v: Option<T>) -> Json {
        v.map(Into::into).unwrap_or(Json::Null)
    }
}

impl fmt::Display for Json {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Json::Null => f.write_str("null"),
            Json::Bool(b) => write!(f, "{b}"),
            Json::Int(n) => write!(f, "{n}"),
            Json::UInt(n) => write!(f, "{n}"),
            // Rust prints the shortest text that reads back as the same
            // number, without an exponent, which is valid JSON as it stands.
            Json::Float(n) if n.is_finite() => write!(f, "{n}"),
            Json::Float(_) => f.write_str("null"),
            Json::Str(s) => write_string(f, s),
            Json::Array(items) => {
                f.write_str("[")?;
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        f.write_str(",")?;
                    }
                    write!(f, "{item}")?;
                }
                f.write_str("]")
            }
            Json::Object(pairs) => {
                f.write_str("{")?;
                for (i, (key, value)) in pairs.iter().enumerate() {
                    if i > 0 {
                        f.write_str(",")?;
                    }
                    write_string(f, key)?;
                    write!(f, ":{value}")?;
                }
                f.write_str("}")
            }
        }
    }
}

fn write_string(f: &mut fmt::Formatter<'_>, s: &str) -> fmt::Result {
    f.write_str("\"")?;
    for c in s.chars() {
        match c {
            '"' => f.write_str("\\\"")?,
            '\\' => f.write_str("\\\\")?,
            '\n' => f.write_str("\\n")?,
            '\r' => f.write_str("\\r")?,
            '\t' => f.write_str("\\t")?,
            c if (c as u32) < 0x20 => write!(f, "\\u{:04x}", c as u32)?,
            c => write!(f, "{c}")?,
        }
    }
    f.write_str("\"")
}

const MAX_DEPTH: usize = 32;

struct Parser<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl Parser<'_> {
    fn space(&mut self) {
        while matches!(self.bytes.get(self.at), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn eat(&mut self, word: &str) -> bool {
        if self.bytes[self.at..].starts_with(word.as_bytes()) {
            self.at += word.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self, depth: usize) -> Result<Json, String> {
        if depth > MAX_DEPTH {
            return Err("nested too deeply".into());
        }
        self.space();
        match self.bytes.get(self.at) {
            None => Err("the text ends where a value should be".into()),
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => self.string().map(Json::Str),
            Some(b't') if self.eat("true") => Ok(Json::Bool(true)),
            Some(b'f') if self.eat("false") => Ok(Json::Bool(false)),
            Some(b'n') if self.eat("null") => Ok(Json::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(_) => Err(format!("not a JSON value at byte {}", self.at)),
        }
    }

    fn object(&mut self, depth: usize) -> Result<Json, String> {
        self.at += 1;
        let mut pairs = Vec::new();
        self.space();
        if self.bytes.get(self.at) == Some(&b'}') {
            self.at += 1;
            return Ok(Json::Object(pairs));
        }
        loop {
            self.space();
            if self.bytes.get(self.at) != Some(&b'"') {
                return Err(format!("a key should start at byte {}", self.at));
            }
            let key = self.string()?;
            self.space();
            if self.bytes.get(self.at) != Some(&b':') {
                return Err(format!("a ':' should follow the key at byte {}", self.at));
            }
            self.at += 1;
            pairs.push((key, self.value(depth + 1)?));
            self.space();
            match self.bytes.get(self.at) {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    return Ok(Json::Object(pairs));
                }
                _ => return Err(format!("',' or '}}' expected at byte {}", self.at)),
            }
        }
    }

    fn array(&mut self, depth: usize) -> Result<Json, String> {
        self.at += 1;
        let mut items = Vec::new();
        self.space();
        if self.bytes.get(self.at) == Some(&b']') {
            self.at += 1;
            return Ok(Json::Array(items));
        }
        loop {
            items.push(self.value(depth + 1)?);
            self.space();
            match self.bytes.get(self.at) {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    return Ok(Json::Array(items));
                }
                _ => return Err(format!("',' or ']' expected at byte {}", self.at)),
            }
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.at;
        while matches!(self.bytes.get(self.at), Some(b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9')) {
            self.at += 1;
        }
        // The bytes just walked over are all ASCII, so this cannot fail.
        let text = std::str::from_utf8(&self.bytes[start..self.at]).map_err(|e| e.to_string())?;
        if let Ok(n) = text.parse::<u64>() {
            return Ok(Json::UInt(n));
        }
        if let Ok(n) = text.parse::<i64>() {
            return Ok(Json::Int(n));
        }
        match text.parse::<f64>() {
            Ok(n) if n.is_finite() => Ok(Json::Float(n)),
            _ => Err(format!("not a number: {text}")),
        }
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let digits = self.bytes.get(self.at..self.at + 4).ok_or("a \\u escape is cut short")?;
        let text = std::str::from_utf8(digits).map_err(|_| "a \\u escape is not hexadecimal")?;
        let n = u32::from_str_radix(text, 16).map_err(|_| "a \\u escape is not hexadecimal")?;
        self.at += 4;
        Ok(n)
    }

    fn string(&mut self) -> Result<String, String> {
        self.at += 1; // the opening quote
        let mut out: Vec<u8> = Vec::new();
        loop {
            let Some(&b) = self.bytes.get(self.at) else {
                return Err("a string is not closed".into());
            };
            self.at += 1;
            match b {
                b'"' => break,
                b'\\' => {
                    let Some(&escape) = self.bytes.get(self.at) else {
                        return Err("a string ends in a backslash".into());
                    };
                    self.at += 1;
                    let c = match escape {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'b' => '\u{8}',
                        b'f' => '\u{c}',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        b'u' => {
                            let mut code = self.hex4()?;
                            // Two escapes in a row for one character beyond U+FFFF.
                            if (0xD800..0xDC00).contains(&code) && self.eat("\\u") {
                                let low = self.hex4()?;
                                if (0xDC00..0xE000).contains(&low) {
                                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                                } else {
                                    return Err("half of a character pair in a \\u escape".into());
                                }
                            }
                            char::from_u32(code).ok_or("a \\u escape that is no character")?
                        }
                        _ => return Err(format!("unknown escape at byte {}", self.at - 1)),
                    };
                    out.extend_from_slice(c.encode_utf8(&mut [0u8; 4]).as_bytes());
                }
                b if b < 0x20 => return Err("a control character inside a string".into()),
                b => out.push(b),
            }
        }
        // The input was a &str and only whole escapes were replaced, so the
        // bytes are UTF-8 again; checked all the same.
        String::from_utf8(out).map_err(|_| "a string that is not UTF-8".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn what_is_written_reads_back_the_same() {
        let value = Json::object([
            ("ok", true.into()),
            ("pid", 4242u32.into()),
            ("offset", (-3i32).into()),
            ("big", u64::MAX.into()),
            ("temp", 52.375f64.into()),
            ("name", "C:\\Games\\\"x\"\n\ttab \u{1} é 日本 😀".into()),
            ("nothing", Json::Null),
            ("missing", Option::<u32>::None.into()),
            ("list", Json::Array(vec![1u8.into(), "two".into(), Json::Array(vec![])])),
            ("empty", Json::Object(vec![])),
        ]);
        let text = value.to_string();
        assert_eq!(Json::parse(&text).unwrap(), value, "{text}");
        assert!(text.starts_with("{\"ok\":true,\"pid\":4242,\"offset\":-3,\"big\":18446744073709551615,\"temp\":52.375,"));
        assert!(text.contains("\\u0001") && text.contains("\\\"x\\\"") && text.contains("\\n\\t"));
    }

    #[test]
    fn numbers_json_cannot_write_become_null() {
        assert_eq!(Json::from(f64::NAN), Json::Null);
        assert_eq!(Json::from(f64::INFINITY), Json::Null);
        assert_eq!(Json::Float(f64::NAN).to_string(), "null");
        assert_eq!(Json::from(52.0f64).to_string(), "52");
        assert_eq!(Json::from(1e21f64).to_string(), "1000000000000000000000");
        assert_eq!(Json::from(0.001f64).to_string(), "0.001");
    }

    #[test]
    fn a_request_is_read() {
        let r = Json::parse(" {\"q\": \"wmi\", \"namespace\": \"root\\\\cimv2\", \"timeout_ms\": 500,\n \"x\": [1, -2, 3.5, true, null], \"u\": \"\\u00e9\\ud83d\\ude00\\/\"} ").unwrap();
        assert_eq!(r["q"].as_str(), Some("wmi"));
        assert_eq!(r["namespace"].as_str(), Some("root\\cimv2"));
        assert_eq!(r["timeout_ms"].as_u64(), Some(500));
        assert_eq!(r["x"].as_array().unwrap().len(), 5);
        assert_eq!(r["x"].as_array().unwrap()[1], Json::Int(-2));
        assert_eq!(r["u"].as_str(), Some("é😀/"));
        assert!(r["not there"].is_null() && r["q"]["deeper"].is_null());
        assert_eq!(r["x"].as_u64(), None);
    }

    #[test]
    fn what_is_not_json_is_refused_not_guessed_at() {
        for bad in [
            "", " ", "{", "}", "[1,", "[1 2]", "{\"a\"}", "{\"a\":}", "{a:1}", "\"open", "\"bad \\x escape\"",
            "\"\\u12\"", "\"\\ud800\"", "tru", "nul", "01x", "1.2.3", "--1", "{\"a\":1} extra", "\"line\nbreak\"",
            "NaN", "Infinity", "'single'",
        ] {
            assert!(Json::parse(bad).is_err(), "{bad:?}");
        }
        let deep = "[".repeat(100) + &"]".repeat(100);
        assert!(Json::parse(&deep).unwrap_err().contains("deeply"));
    }
}
