//! VisporaGpuReader: the graphics card's reader.
//!
//! A program that does one thing: for every line that comes in on its
//! standard input, it reads the first NVIDIA card through the driver's own
//! library and writes what the native helper says as one line on its
//! standard output.  When its input ends, it ends.
//!
//! Why it is a program of its own.  Reading the card means starting
//! NVIDIA's library (nvml.dll) in the reading process, and a library that
//! was started stays in its process until the process ends, whatever is
//! shut down or freed (tools/measure/nvml_release.py).  A driver's installer
//! has to replace that file.  The shell lives as long as the app does and
//! so never opens the library; it starts this program while somebody wants
//! readings and ends it when nobody does, and before a driver's installer
//! is started (shell_rs/core/src/moved/card.rs).  Ending the process is the
//! one thing that takes the file out of use.
//!
//! What it takes from whoever starts it: nothing.
//!
//! * Its command line is not read.
//! * What comes in on its input is not looked at: a line is a line, and
//!   every line gets the same question asked for it.
//! * Its environment is not read.  The helper would look in one variable
//!   for a second place to find the library in (drivers from before 2018);
//!   that variable is taken away before the first reading, so the library
//!   is loaded from Windows' own folder or not at all.
//! * It asks the helper one question, written here.  The helper's door for
//!   the things that change the PC is not named in this file.
//!
//! So started by anybody, by hand, it reads the card and does nothing else.
//! (Started with no input at all, by a double click, it ends at once.)
#![windows_subsystem = "windows"]

use std::io::{BufRead, Write};

/// The one question this program asks the helper.
const THE_QUESTION: &str = "{\"q\":\"gpu_live\"}";
/// Where the helper would look for a second place for the driver's library.
const A_PLACE_FROM_THE_ENVIRONMENT: &str = "ProgramW6432";

/// One answer, on one line, for every line that comes in: until the input
/// ends, or the answer cannot be written.  How many were answered.
fn serve(input: impl BufRead, mut output: impl Write, read_the_card: impl Fn() -> String) -> u64 {
    let mut answered = 0;
    for line in input.split(b'\n') {
        if line.is_err() {
            break;
        }
        // (what the line holds is not looked at)
        let mut answer = read_the_card().replace(['\r', '\n'], " ");
        answer.push('\n');
        if output.write_all(answer.as_bytes()).and_then(|()| output.flush()).is_err() {
            break;
        }
        answered += 1;
    }
    answered
}

fn main() {
    std::env::remove_var(A_PLACE_FROM_THE_ENVIRONMENT);
    let (input, output) = (std::io::stdin(), std::io::stdout());
    serve(input.lock(), output.lock(), || vispora_native::answer(THE_QUESTION));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    #[test]
    fn every_line_gets_one_answer_and_what_a_line_holds_is_not_looked_at() {
        let asked = Cell::new(0);
        let read = || {
            asked.set(asked.get() + 1);
            format!("{{\"ok\":true,\"data\":{{\"temp_c\":{}}}}}", 60 + asked.get())
        };
        let mut out = Vec::new();
        let input: &[u8] = b"\n{\"q\":\"purge_standby_list\"}\n\xff\xfe gpu_release 3600 C:\\somewhere\\nvml.dll\r\n\n";
        assert_eq!(serve(input, &mut out, read), 4);
        assert_eq!(String::from_utf8(out).unwrap(),
                   "{\"ok\":true,\"data\":{\"temp_c\":61}}\n{\"ok\":true,\"data\":{\"temp_c\":62}}\n{\"ok\":true,\"data\":{\"temp_c\":63}}\n{\"ok\":true,\"data\":{\"temp_c\":64}}\n");
        assert_eq!(asked.get(), 4);
    }

    #[test]
    fn no_input_is_no_reading_and_the_end_of_the_input_is_the_end() {
        let asked = Cell::new(0);
        let read = || {
            asked.set(asked.get() + 1);
            String::from("{}")
        };
        let mut out = Vec::new();
        assert_eq!(serve(&b""[..], &mut out, read), 0);
        assert!(out.is_empty() && asked.get() == 0, "the card was read with nobody asking");
        // A last line without its end is a line.
        assert_eq!(serve(&b"\nx"[..], &mut out, read), 2);
        assert_eq!(out, b"{}\n{}\n");
    }

    #[test]
    fn an_answer_is_one_line_whatever_the_helper_says() {
        let mut out = Vec::new();
        serve(&b"\n"[..], &mut out, || "{\"ok\":false,\r\n\"err\":\"two\nlines\"}".to_string());
        assert_eq!(String::from_utf8(out).unwrap(), "{\"ok\":false,  \"err\":\"two lines\"}\n");
    }

    #[test]
    fn nobody_listening_any_more_is_the_end() {
        struct Closed;
        impl Write for Closed {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let asked = Cell::new(0);
        let read = || {
            asked.set(asked.get() + 1);
            String::from("{}")
        };
        assert_eq!(serve(&b"\n\n\n\n"[..], Closed, read), 0);
        assert_eq!(asked.get(), 1, "it went on reading the card with nobody to tell");
    }

    #[test]
    fn an_input_that_fails_is_the_end_too() {
        // (three times, and then its end: a program that went on would
        // read the card once for each)
        struct Fails(u32);
        impl std::io::Read for Fails {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                if self.0 == 0 {
                    return Ok(0);
                }
                self.0 -= 1;
                Err(std::io::Error::other("the pipe is broken"))
            }
        }
        let asked = Cell::new(0);
        let read = || {
            asked.set(asked.get() + 1);
            String::from("{}")
        };
        let mut out = Vec::new();
        assert_eq!(serve(std::io::BufReader::new(Fails(3)), &mut out, read), 0);
        assert!(asked.get() == 0 && out.is_empty(), "the card was read {} times after the input failed", asked.get());
    }

    #[test]
    fn the_question_is_the_cards_reading_and_nothing_else() {
        assert_eq!(THE_QUESTION, r#"{"q":"gpu_live"}"#);
        // (the helper takes it for a question: an answer, of either kind)
        let said = vispora_native::answer("{\"q\":\"abi\"}");
        assert!(said.starts_with("{\"ok\":true"), "{said}");
    }
}
