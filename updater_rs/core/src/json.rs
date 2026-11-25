//! JSON as the updater needs it: reading what GitHub and the update server
//! answer, and writing install_info.json and the window's status.
//!
//! The reader takes what Python's `json.loads` takes, because the Python
//! updater this replaces read the same answers with it: `NaN`, `Infinity`
//! and numbers too large for any machine number are values, not errors (the
//! update server's `size_bytes` is only a hint, and a wild one must not stop
//! an install).  A key that appears twice keeps its last value, as a Python
//! dict does.
//!
//! No outside crates (see Cargo.toml).

use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    /// A number written without a fraction or an exponent.
    Int(i128),
    /// Any other number, `NaN` and the infinities included.
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    /// Keys in the order they were added.
    Object(Vec<(String, Json)>),
}

static NULL: Json = Json::Null;
const MAX_DEPTH: usize = 64;

impl Json {
    pub fn object<const N: usize>(pairs: [(&str, Json); N]) -> Json {
        Json::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    /// The member `key` of an object: the last one of that name, as in a
    /// Python dict.  None for anything that is not an object.
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(pairs) => pairs.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// Set a member of an object, replacing one of the same name.
    pub fn set(&mut self, key: &str, value: Json) {
        if let Json::Object(pairs) = self {
            match pairs.iter_mut().find(|(k, _)| k == key) {
                Some(pair) => pair.1 = value,
                None => pairs.push((key.to_string(), value)),
            }
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Json]> {
        match self {
            Json::Array(items) => Some(items),
            _ => None,
        }
    }

    pub fn is_object(&self) -> bool {
        matches!(self, Json::Object(_))
    }

    /// A whole number, and only that: `true`, `2.0` and `"2"` are not.
    /// (Python: `isinstance(x, int) and not isinstance(x, bool)`.)
    pub fn as_int(&self) -> Option<i128> {
        match *self {
            Json::Int(n) => Some(n),
            _ => None,
        }
    }

    /// What Python's `if value:` decides.
    pub fn truthy(&self) -> bool {
        match self {
            Json::Null => false,
            Json::Bool(b) => *b,
            Json::Int(n) => *n != 0,
            Json::Float(f) => *f != 0.0,
            Json::Str(s) => !s.is_empty(),
            Json::Array(items) => !items.is_empty(),
            Json::Object(pairs) => !pairs.is_empty(),
        }
    }

    /// What Python's `str(value)` gives: text as it is, numbers in decimal,
    /// `True` / `False` / `None`.  A list or an object comes out as JSON
    /// text, which is close to Python's and never what a caller hoped for
    /// (a version or a hash that is a list is wrong either way).
    pub fn py_str(&self) -> String {
        match self {
            Json::Null => "None".into(),
            Json::Bool(true) => "True".into(),
            Json::Bool(false) => "False".into(),
            Json::Int(n) => n.to_string(),
            Json::Float(f) => float_text(*f, "nan", "inf"),
            Json::Str(s) => s.clone(),
            other => other.to_string(),
        }
    }

    /// `str(value or "")`.
    pub fn py_str_or_empty(&self) -> String {
        if self.truthy() {
            self.py_str()
        } else {
            String::new()
        }
    }

    /// Read JSON text (leniently, see the top of this file).
    pub fn parse(text: &str) -> Result<Json, String> {
        let mut p = Parser { bytes: text.as_bytes(), at: 0 };
        let value = p.value(0)?;
        p.space();
        if p.at != p.bytes.len() {
            return Err(format!("unexpected text after the value at byte {}", p.at));
        }
        Ok(value)
    }

    /// Read the body of an HTTP answer: UTF-8, with or without a byte-order
    /// mark.  Anything else is not an answer.
    pub fn parse_bytes(body: &[u8]) -> Result<Json, String> {
        let body = body.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(body);
        let text = std::str::from_utf8(body).map_err(|_| "the answer is not UTF-8 text".to_string())?;
        Json::parse(text)
    }

    /// Compact JSON, non-ASCII characters escaped (what the window's status
    /// is sent as).
    #[allow(clippy::inherent_to_string_shadow_display)]
    pub fn to_string(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, None, 0);
        out
    }

    /// JSON as Python's `json.dump(value, f, indent=2)` writes it, byte for
    /// byte for the values install_info.json holds.
    pub fn to_pretty(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, Some(2), 0);
        out
    }

    fn write(&self, out: &mut String, indent: Option<usize>, level: usize) {
        let newline = |out: &mut String, level: usize| {
            if let Some(n) = indent {
                out.push('\n');
                out.extend(std::iter::repeat(' ').take(n * level));
            }
        };
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Int(n) => {
                let _ = write!(out, "{n}");
            }
            Json::Float(f) => out.push_str(&float_text(*f, "NaN", "Infinity")),
            Json::Str(s) => write_string(out, s),
            Json::Array(items) if items.is_empty() => out.push_str("[]"),
            Json::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    newline(out, level + 1);
                    item.write(out, indent, level + 1);
                }
                newline(out, level);
                out.push(']');
            }
            Json::Object(pairs) if pairs.is_empty() => out.push_str("{}"),
            Json::Object(pairs) => {
                out.push('{');
                for (i, (key, value)) in pairs.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    newline(out, level + 1);
                    write_string(out, key);
                    out.push_str(if indent.is_some() { ": " } else { ":" });
                    value.write(out, indent, level + 1);
                }
                newline(out, level);
                out.push('}');
            }
        }
    }
}

/// `value["key"]`: the member, or null when there is none.
impl std::ops::Index<&str> for Json {
    type Output = Json;
    fn index(&self, key: &str) -> &Json {
        self.get(key).unwrap_or(&NULL)
    }
}

impl std::fmt::Display for Json {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&Json::to_string(self))
    }
}

macro_rules! from_int {
    ($($t:ty),*) => {$(
        impl From<$t> for Json {
            fn from(n: $t) -> Json { Json::Int(n as i128) }
        }
    )*};
}
from_int!(u8, u16, u32, u64, usize, i8, i16, i32, i64);

impl From<bool> for Json {
    fn from(b: bool) -> Json {
        Json::Bool(b)
    }
}

impl From<f64> for Json {
    fn from(f: f64) -> Json {
        Json::Float(f)
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

impl<T: Into<Json>> From<Vec<T>> for Json {
    fn from(items: Vec<T>) -> Json {
        Json::Array(items.into_iter().map(Into::into).collect())
    }
}

/// A float the way Python prints it: always with a fraction ("3.0"), the
/// shortest digits that read back as the same number.
fn float_text(f: f64, nan: &str, infinity: &str) -> String {
    if f.is_nan() {
        nan.to_string()
    } else if f.is_infinite() {
        format!("{}{infinity}", if f < 0.0 { "-" } else { "" })
    } else {
        format!("{f:?}")
    }
}

/// A string as Python's json writes it with ensure_ascii: everything outside
/// ASCII as \uXXXX (a pair of them beyond U+FFFF).
fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e && (c as u32) != 0x7f => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

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
            Some(b'N') if self.eat("NaN") => Ok(Json::Float(f64::NAN)),
            Some(b'I') if self.eat("Infinity") => Ok(Json::Float(f64::INFINITY)),
            Some(b'-') if self.eat("-Infinity") => Ok(Json::Float(f64::NEG_INFINITY)),
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

    /// `-?digits(.digits)?([eE][+-]?digits)?`, as JSON has it.  Without a
    /// fraction or an exponent it is a whole number (of any size Python
    /// would take that fits 128 bits; beyond that, a float).
    fn number(&mut self) -> Result<Json, String> {
        let start = self.at;
        let digits = |p: &mut Self| {
            let from = p.at;
            while matches!(p.bytes.get(p.at), Some(b'0'..=b'9')) {
                p.at += 1;
            }
            p.at > from
        };
        if self.bytes.get(self.at) == Some(&b'-') {
            self.at += 1;
        }
        if !digits(self) {
            return Err(format!("not a number at byte {start}"));
        }
        let mut whole = true;
        if self.bytes.get(self.at) == Some(&b'.') {
            self.at += 1;
            whole = false;
            if !digits(self) {
                return Err(format!("not a number at byte {start}"));
            }
        }
        if matches!(self.bytes.get(self.at), Some(b'e' | b'E')) {
            self.at += 1;
            whole = false;
            if matches!(self.bytes.get(self.at), Some(b'+' | b'-')) {
                self.at += 1;
            }
            if !digits(self) {
                return Err(format!("not a number at byte {start}"));
            }
        }
        // The bytes just walked over are all ASCII, so this cannot fail.
        let text = std::str::from_utf8(&self.bytes[start..self.at]).map_err(|e| e.to_string())?;
        if whole {
            if let Ok(n) = text.parse::<i128>() {
                return Ok(Json::Int(n));
            }
        }
        // (Too large for a float reads as infinity, as in Python.)
        text.parse::<f64>().map(Json::Float).map_err(|_| format!("not a number: {text}"))
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let digits = self.bytes.get(self.at..self.at + 4).ok_or("a \\u escape is cut short")?;
        let text = std::str::from_utf8(digits).map_err(|_| "a \\u escape is not hexadecimal")?;
        if !text.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("a \\u escape is not hexadecimal".into());
        }
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
                            if (0xD800..0xDC00).contains(&code) && self.bytes[self.at..].starts_with(b"\\u") {
                                let back = self.at;
                                self.at += 2;
                                let low = self.hex4()?;
                                if (0xDC00..0xE000).contains(&low) {
                                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                                } else {
                                    self.at = back;
                                }
                            }
                            // Half a pair on its own is no character: the
                            // replacement mark stands in for it (Python keeps
                            // the half, which no Rust string can hold).
                            char::from_u32(code).unwrap_or('\u{FFFD}')
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
            ("list", Json::Array(vec![1u8.into(), "two".into(), Json::Array(vec![])])),
            ("empty", Json::Object(vec![])),
        ]);
        for text in [value.to_string(), value.to_pretty()] {
            assert_eq!(Json::parse(&text).unwrap(), value, "{text}");
            assert!(text.is_ascii(), "{text}");
        }
        assert!(value.to_string().starts_with("{\"ok\":true,\"pid\":4242,\"offset\":-3,\"big\":18446744073709551615,"));
        assert!(value.to_string().contains("\\u0001 \\u00e9 \\u65e5\\u672c \\ud83d\\ude00"));
    }

    #[test]
    fn pretty_is_what_python_writes_with_indent_two() {
        // json.dumps({"install_path": "C:\\Programme\\Vispöra\\Vispora.exe", "channel": "stable",
        //             "updated_at": 1764000000.5, "n": 3, "whole": 2.0, "list": [1, "a"], "none": None,
        //             "empty": {}, "nothing": []}, indent=2)
        let value = Json::object([
            ("install_path", "C:\\Programme\\Visp\u{f6}ra\\Vispora.exe".into()),
            ("channel", "stable".into()),
            ("updated_at", 1764000000.5f64.into()),
            ("n", 3u8.into()),
            ("whole", 2.0f64.into()),
            ("list", Json::Array(vec![1u8.into(), "a".into()])),
            ("none", Json::Null),
            ("empty", Json::Object(vec![])),
            ("nothing", Json::Array(vec![])),
        ]);
        let python = "{\n  \"install_path\": \"C:\\\\Programme\\\\Visp\\u00f6ra\\\\Vispora.exe\",\n  \"channel\": \"stable\",\n  \
                      \"updated_at\": 1764000000.5,\n  \"n\": 3,\n  \"whole\": 2.0,\n  \"list\": [\n    1,\n    \"a\"\n  ],\n  \
                      \"none\": null,\n  \"empty\": {},\n  \"nothing\": []\n}";
        assert_eq!(value.to_pretty(), python);
    }

    #[test]
    fn it_reads_what_pythons_reader_reads() {
        let r = Json::parse(
            " {\"size\": 1e400, \"a\": NaN, \"b\": -Infinity, \"c\": Infinity, \"huge\": 123456789012345678901234567890, \
             \"beyond\": 1234567890123456789012345678901234567890123456789, \"f\": 2.0, \"e\": 1E3, \"neg\": -7, \
             \"twice\": 1, \"twice\": 2, \"u\": \"\\u00e9\\ud83d\\ude00\\/\", \"half\": \"\\ud83d!\"} ",
        )
        .unwrap();
        assert!(matches!(r["size"], Json::Float(f) if f.is_infinite()));
        assert!(matches!(r["a"], Json::Float(f) if f.is_nan()));
        assert!(matches!(r["b"], Json::Float(f) if f == f64::NEG_INFINITY));
        assert!(matches!(r["c"], Json::Float(f) if f == f64::INFINITY));
        assert_eq!(r["huge"].as_int(), Some(123456789012345678901234567890));
        assert!(matches!(r["beyond"], Json::Float(f) if f > 1e48));
        assert_eq!((r["f"].as_int(), r["e"].as_int(), r["neg"].as_int()), (None, None, Some(-7)));
        assert_eq!(r["twice"].as_int(), Some(2), "the last one, as in a Python dict");
        assert_eq!(r["u"].as_str(), Some("é😀/"));
        assert_eq!(r["half"].as_str(), Some("\u{FFFD}!"));
        assert!(r["not there"] == Json::Null && r["u"]["deeper"] == Json::Null);
        assert_eq!(Json::parse_bytes(b"\xEF\xBB\xBF[1]").unwrap(), Json::Array(vec![1u8.into()]));
        assert!(Json::parse_bytes(b"\xff\xfe[1]").is_err());
    }

    #[test]
    fn what_is_not_json_is_refused_not_guessed_at() {
        for bad in [
            "", " ", "{", "}", "[1,", "[1 2]", "{\"a\"}", "{\"a\":}", "{a:1}", "\"open", "\"bad \\x escape\"",
            "\"\\u12\"", "\"\\u12g4\"", "tru", "nul", "01x", "1.2.3", "--1", "1.", ".5", "1e", "-", "+1",
            "{\"a\":1} extra", "\"line\nbreak\"", "'single'", "<html>", "nan", "infinity",
        ] {
            assert!(Json::parse(bad).is_err(), "{bad:?}");
        }
        let deep = "[".repeat(100) + &"]".repeat(100);
        assert!(Json::parse(&deep).unwrap_err().contains("deeply"));
    }

    #[test]
    fn truth_and_text_are_pythons() {
        let truthy = |t: &str| Json::parse(t).unwrap().truthy();
        for t in ["null", "false", "0", "0.0", "\"\"", "[]", "{}"] {
            assert!(!truthy(t), "{t}");
        }
        for t in ["true", "1", "-1", "0.5", "\"0\"", "[0]", "{\"a\":null}", "NaN"] {
            assert!(truthy(t), "{t}");
        }
        let text = |t: &str| Json::parse(t).unwrap().py_str();
        assert_eq!(text("\"3.8.1\""), "3.8.1");
        assert_eq!((text("12345"), text("3.6"), text("2.0"), text("true"), text("null")),
                   ("12345".into(), "3.6".into(), "2.0".into(), "True".into(), "None".into()));
        assert_eq!(Json::parse("0").unwrap().py_str_or_empty(), "");
        assert_eq!(Json::parse("[\"a\"]").unwrap().py_str_or_empty(), "[\"a\"]");
        let mut o = Json::object([("a", 1u8.into())]);
        o.set("a", 2u8.into());
        o.set("b", "x".into());
        assert_eq!(o.to_string(), "{\"a\":2,\"b\":\"x\"}");
    }
}
