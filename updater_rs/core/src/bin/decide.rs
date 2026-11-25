//! vispora-updater-decide: the updater's decisions, from a terminal.
//!
//! Reads one JSON object from standard input and prints the answer as JSON.
//! It decides and nothing else: no connection is opened, no file is read or
//! written.  tests/test_updater_rs.py feeds it what it feeds
//! update_sources.py and holds the two answers against each other.
//!
//!     {"ask": "version", "versions": ["3.8.0", "v3.9.0-beta.1"]}
//!     {"ask": "source_order", "setting": "auto", "server_url": "https://..."}
//!     {"ask": "github", "releases": [...], "channel": "stable"}
//!     {"ask": "args", "argv": ["--channel", "beta"]}    a command line: what is read from it, or the exit code
//!     {"ask": "info"}
//!     {"ask": "batch", "requests": [...]}      several at once, answered in order

use std::io::Read as _;

use vispora_updater_core::cli;
use vispora_updater_core::json::Json;
use vispora_updater_core::sources::{self, Offer};

fn offer(found: Option<Offer>, with_notes: bool) -> Json {
    let Some(o) = found else {
        return Json::object([("available", false.into()), ("source", "github".into())]);
    };
    let mut out = Json::object([
        ("available", true.into()),
        ("version", o.version.into()),
        ("sha256", o.sha256.into()),
        ("size_bytes", Json::Int(o.size_bytes)),
        ("download_url", o.download_url.into()),
    ]);
    if with_notes {
        out.set("notes", o.notes.into());
        out.set("released_at", o.released_at.into());
    }
    out.set("source", "github".into());
    out
}

fn answer(request: &Json) -> Result<Json, String> {
    // (as Python's `str(value or "")` would read whatever was sent)
    let text = |key: &str| request[key].py_str_or_empty();
    match request["ask"].as_str() {
        Some("version") => {
            let versions = request["versions"].as_array().ok_or("\"versions\" must be a list")?;
            Ok(Json::Array(
                versions
                    .iter()
                    .map(|v| Json::Array(sources::parse_version(&v.py_str_or_empty()).iter().map(|&n| n.into()).collect()))
                    .collect(),
            ))
        }
        Some("source_order") => Ok(Json::object([
            ("setting", sources::source_setting(&text("setting")).into()),
            ("order", sources::source_order(&text("setting"), &text("server_url")).into()),
        ])),
        Some("github") => Ok(Json::object([
            ("app", offer(sources::github_app_release(&request["releases"], &text("channel")), true)),
            ("updater", offer(sources::github_updater(&request["releases"], &text("channel")), false)),
        ])),
        Some("args") => {
            let argv: Vec<String> = request["argv"].as_array().unwrap_or(&[]).iter().map(Json::py_str).collect();
            Ok(match cli::parse_args(&argv) {
                Err(e) => Json::object([("exit", e.exit_code.into())]),
                Ok(a) => Json::object([
                    ("target_dir", a.target_dir.into()),
                    ("target_exe", a.target_exe.into()),
                    ("channel", a.channel.into()),
                    ("current_version", a.current_version.into()),
                    ("target_version", a.target_version.into()),
                    ("auto_confirm", a.auto_confirm.into()),
                    ("server", a.server.into()),
                    ("source", a.source.into()),
                    ("silent", a.silent.into()),
                ]),
            })
        }
        Some("batch") => {
            let requests = request["requests"].as_array().ok_or("\"requests\" must be a list")?;
            Ok(Json::Array(requests.iter().map(answer).collect::<Result<Vec<_>, _>>()?))
        }
        Some("info") => Ok(Json::object([
            ("updater_version", vispora_updater_core::UPDATER_VERSION.into()),
            // (what this build of the tool can be asked: a tool left over
            // from an older source is told apart by it)
            ("asks", vec!["version", "source_order", "github", "args", "info", "batch"].into()),
            ("server_url_default", sources::SERVER_URL_DEFAULT.into()),
            ("github_releases_url", sources::GITHUB_RELEASES_URL.into()),
            ("github_releases_page", sources::GITHUB_RELEASES_PAGE.into()),
            ("github_repo", sources::GITHUB_REPO.into()),
            ("app_asset", sources::APP_ASSET.into()),
            ("updater_min_for_sources", sources::UPDATER_MIN_FOR_SOURCES.into()),
            ("dead_server_host", vispora_updater_core::cli::DEAD_SERVER_HOST.into()),
            ("app_name", vispora_updater_core::paths::APP_NAME.into()),
            ("legacy_app_name", vispora_updater_core::paths::LEGACY_APP_NAME.into()),
            ("app_exe_names", vispora_updater_core::paths::APP_EXE_NAMES.to_vec().into()),
            ("github_headers", Json::Object(sources::GITHUB_HEADERS.iter().map(|(k, v)| (k.to_string(), (*v).into())).collect())),
            ("steps", Json::Array(vispora_updater_core::state::PIPELINE_STEPS.iter().map(|(key, _)| (*key).into()).collect())),
        ])),
        _ => Err("unknown \"ask\"".into()),
    }
}

fn main() {
    let mut input = String::new();
    let outcome = std::io::stdin()
        .read_to_string(&mut input)
        .map_err(|e| e.to_string())
        .and_then(|_| Json::parse(&input))
        .and_then(|request| answer(&request));
    match outcome {
        Ok(value) => println!("{value}"),
        Err(why) => {
            eprintln!("{why}");
            std::process::exit(2);
        }
    }
}
