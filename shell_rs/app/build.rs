//! What the exe carries besides its code: the app's icon, its version, and
//! a manifest.
//!
//! The manifest is app.manifest: Tauri's own (the modern look for Windows'
//! dialogs) plus one line that says the exe runs with the rights of whoever
//! starts it.  Unlike the updater's, it does not ask for administrator
//! rights.  The shell asks for them itself, after it has looked whether a
//! copy is already running (../core/src/instance.rs): a manifest that asked
//! would put Windows' prompt in front of every start, also a second one
//! that only wants the first copy's window.
//!
//! The version is the app's, read from config.py, the one place it is
//! written.

use std::path::Path;

fn main() {
    println!("cargo:rerun-if-changed=app.manifest");
    let config = Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..").join("config.py");
    println!("cargo:rerun-if-changed={}", config.display());
    let text = std::fs::read_to_string(&config).unwrap_or_else(|e| panic!("{}: {e}", config.display()));
    let version = text
        .lines()
        .filter_map(|line| line.strip_prefix("APP_VERSION")?.trim_start().strip_prefix('=')?.trim().strip_prefix('"')?.split('"').next())
        .next()
        .expect("config.py has no line `APP_VERSION = \"...\"`");
    // (tauri-build reads what is added to tauri.conf.json from here)
    std::env::set_var("TAURI_CONFIG", format!("{{\"version\": \"{version}\"}}"));

    let manifest = include_str!("app.manifest");
    assert!(manifest.contains(r#"level="asInvoker""#) && !manifest.contains("requireAdministrator"),
            "app.manifest must leave asking for administrator rights to the shell");
    let windows = tauri_build::WindowsAttributes::new().app_manifest(manifest);
    tauri_build::try_build(tauri_build::Attributes::new().windows_attributes(windows))
        .expect("tauri-build could not prepare the shell's exe");
}
