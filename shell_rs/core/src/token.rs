//! The secret of one run.
//!
//! The backend runs as administrator, and its API can wipe the vault, delete
//! files and start programs.  Until now it made up a secret at every start
//! and gave it to its own window; every call had to carry it (app.py,
//! _reject_foreign_requests).  With the shell in front, the shell makes the
//! secret up and gives it to both sides: to the page, in the fragment of the
//! address the window is opened with, and to the backend, in its hand-over.
//! A call without it is refused by the shell and would be refused by the
//! backend.

use std::fmt;

/// The header every API call carries the secret in (app.py: _API_TOKEN_HEADER).
pub const HEADER: &str = "X-Vispora-Token";

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

#[derive(Clone, PartialEq)]
pub struct Token(String);

impl Token {
    /// 32 random bytes, written as 43 letters, digits, `-` and `_`: what
    /// Python's `secrets.token_urlsafe(32)` gave the backend until now.
    /// `fill` is the source of the bytes (platform.rs).
    pub fn random(fill: impl FnOnce(&mut [u8]) -> Result<(), String>) -> Result<Token, String> {
        let mut bytes = [0u8; 32];
        fill(&mut bytes)?;
        if bytes.iter().all(|b| *b == bytes[0]) {
            return Err("the source of random numbers handed back the same byte 32 times".into());
        }
        let mut text = String::with_capacity(43);
        for chunk in bytes.chunks(3) {
            let n = (chunk[0] as u32) << 16 | (*chunk.get(1).unwrap_or(&0) as u32) << 8 | *chunk.get(2).unwrap_or(&0) as u32;
            for i in 0..=chunk.len() {
                text.push(ALPHABET[(n >> (18 - 6 * i) & 63) as usize] as char);
            }
        }
        Ok(Token(text))
    }

    /// A secret that was made elsewhere, if it is one the backend takes: 32
    /// or more of the letters above.
    pub fn from_text(text: &str) -> Option<Token> {
        (text.len() >= 32 && text.bytes().all(|b| ALPHABET.contains(&b))).then(|| Token(text.to_string()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Is `given` the secret?  Compared to the end whatever is found on the
    /// way, so that how long the answer takes says nothing about how much
    /// of a guess was right.
    pub fn is(&self, given: &str) -> bool {
        let (mine, theirs) = (self.0.as_bytes(), given.as_bytes());
        let mut different = mine.len() ^ theirs.len();
        for i in 0..mine.len() {
            different |= (mine[i] ^ theirs.get(i).copied().unwrap_or(0)) as usize;
        }
        different == 0
    }
}

/// Never printed, not even by accident in a log line.
impl fmt::Debug for Token {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Token(not shown)")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_secret_is_43_letters_of_the_backends_alphabet() {
        let token = Token::random(|bytes| {
            bytes.iter_mut().enumerate().for_each(|(i, b)| *b = (i * 37 + 11) as u8);
            Ok(())
        })
        .unwrap();
        assert_eq!(token.as_str().len(), 43);
        assert!(token.as_str().bytes().all(|b| ALPHABET.contains(&b)));
        assert!(Token::from_text(token.as_str()).is_some(), "the backend's own check takes it");
        // Known bytes, known text (RFC 4648's alphabet for addresses, no padding).
        let known = Token::random(|bytes| {
            bytes.fill(0xFB);
            bytes[31] = 0xFF;
            Ok(())
        })
        .unwrap();
        assert_eq!(known.as_str(), "-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_8");
    }

    #[test]
    fn a_source_of_random_numbers_that_does_nothing_is_not_trusted() {
        assert!(Token::random(|_| Ok(())).is_err());
        assert!(Token::random(|bytes| {
            bytes.fill(7);
            Ok(())
        })
        .is_err());
        assert_eq!(Token::random(|_| Err("no".into())), Err("no".to_string()));
    }

    #[test]
    fn only_the_whole_secret_is_the_secret() {
        let token = Token::from_text("abcdefghijklmnopqrstuvwxyz012345-_").unwrap();
        assert!(token.is("abcdefghijklmnopqrstuvwxyz012345-_"));
        for wrong in ["", "abcdefghijklmnopqrstuvwxyz012345-", "abcdefghijklmnopqrstuvwxyz012345-_x",
                      "Abcdefghijklmnopqrstuvwxyz012345-_", "abcdefghijklmnopqrstuvwxyz012345__"] {
            assert!(!token.is(wrong), "{wrong}");
        }
        assert!(Token::from_text("short").is_none() && Token::from_text(&"x".repeat(31)).is_none());
        assert!(Token::from_text(&format!("{}!", "x".repeat(40))).is_none());
        assert_eq!(format!("{token:?}"), "Token(not shown)");
    }
}
