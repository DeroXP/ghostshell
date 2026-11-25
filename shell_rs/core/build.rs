//! The app's version is written in config.py and nowhere else.  The shell
//! shows it in its page and checks that the backend it started says the same,
//! so it is read from there when the shell is built.

use std::path::Path;

fn main() {
    let config = Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..").join("config.py");
    println!("cargo:rerun-if-changed={}", config.display());
    let text = std::fs::read_to_string(&config).unwrap_or_else(|e| panic!("{}: {e}", config.display()));
    for (name, variable) in [("APP_VERSION", "VISPORA_APP_VERSION"), ("APP_NAME", "VISPORA_APP_NAME")] {
        // A line of the form: APP_VERSION = "3.8.1"
        let value = text
            .lines()
            .filter_map(|line| line.strip_prefix(name)?.trim_start().strip_prefix('=')?.trim().strip_prefix('"')?.split('"').next())
            .next()
            .unwrap_or_else(|| panic!("config.py has no line `{name} = \"...\"`"));
        assert!(!value.is_empty(), "config.py: {name} is empty");
        println!("cargo:rustc-env={variable}={value}");
    }
}
