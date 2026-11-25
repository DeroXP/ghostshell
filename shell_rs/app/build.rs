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
//!
//! And the backend: the app, built as a folder (backend.spec) and made into
//! one piece of bytes (a pack: ../core/src/pack.rs), which the exe carries
//! in itself.  The pack is named when the exe is built:
//!
//!     VISPORA_BACKEND_PACK=<the pack> cargo build --release --locked
//!
//! Built without one, the exe carries nothing: a trial build is given its
//! pack by the trial, and the build that ships says that its files are
//! incomplete.
//!
//! The pack is read whole here, by the code that reads it on a person's PC
//! (../core/src/pack.rs, which this script uses as the exe does): its list,
//! every file taken out once with its length and its mark, and the version
//! it was made for against the app's.  A pack that is damaged, or that is
//! another version's, stops the build.

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

    // The pack, or nothing, under one name: main.rs takes it in from there.
    println!("cargo:rerun-if-env-changed=VISPORA_BACKEND_PACK");
    let carried = Path::new(&std::env::var("OUT_DIR").expect("cargo names the folder for what a build makes")).join("backend.pack");
    match std::env::var("VISPORA_BACKEND_PACK").ok().filter(|named| !named.is_empty()) {
        Some(named) => {
            println!("cargo:rerun-if-changed={named}");
            let bytes = std::fs::read(&named).unwrap_or_else(|e| panic!("VISPORA_BACKEND_PACK names {named}: {e}"));
            if let Err(why) = vispora_shell_core::pack::fit_to_carry(&bytes, version) {
                panic!("VISPORA_BACKEND_PACK names {named}, and this exe cannot carry it: {why}. (See shell_rs/README.md, \"One file\".)");
            }
            std::fs::write(&carried, bytes).unwrap_or_else(|e| panic!("{}: {e}", carried.display()));
        }
        None => std::fs::write(&carried, b"").unwrap_or_else(|e| panic!("{}: {e}", carried.display())),
    }

    let manifest = include_str!("app.manifest");
    assert!(manifest.contains(r#"level="asInvoker""#) && !manifest.contains("requireAdministrator"),
            "app.manifest must leave asking for administrator rights to the shell");
    let windows = tauri_build::WindowsAttributes::new().app_manifest(manifest);
    tauri_build::try_build(tauri_build::Attributes::new().windows_attributes(windows))
        .expect("tauri-build could not prepare the shell's exe");
}
