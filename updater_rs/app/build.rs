//! What the exe carries besides its code: the app's icon, its version, and
//! the manifest that makes Windows start it as administrator.
//!
//! The manifest is app.manifest, which is Tauri's own (the modern look for
//! Windows' dialogs) plus the line that asks for administrator rights.  The
//! Python updater asked for the same (uac_admin in updater_helper.spec): it
//! writes into Program Files.
//!
//! Built without the `elevated` feature, the exe asks for nothing and runs
//! with the rights of whoever starts it.  That build is for trying the
//! updater in a folder of its own (try_it.py) and is never shipped.

const ASKS_FOR_ADMIN: &str = r#"level="requireAdministrator""#;
const ASKS_FOR_NOTHING: &str = r#"level="asInvoker""#;

fn main() {
    println!("cargo:rerun-if-changed=app.manifest");
    let manifest = include_str!("app.manifest");
    assert!(manifest.contains(ASKS_FOR_ADMIN), "app.manifest no longer asks for administrator rights");
    let manifest = if std::env::var_os("CARGO_FEATURE_ELEVATED").is_some() {
        manifest.to_string()
    } else {
        println!("cargo:warning=built without the `elevated` feature: this exe is for trying out, not for shipping");
        manifest.replace(ASKS_FOR_ADMIN, ASKS_FOR_NOTHING)
    };
    let windows = tauri_build::WindowsAttributes::new().app_manifest(manifest);
    tauri_build::try_build(tauri_build::Attributes::new().windows_attributes(windows))
        .expect("tauri-build could not prepare the updater's exe");
}
