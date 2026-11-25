//! The interface files, as the backend serves them today, compiled into the
//! shell: the window can show the page before the backend is up.
//!
//! They are the app's own three files and are not changed.  templates/
//! index.html is a Jinja template with two names in it, the version (twice)
//! and the folder the app keeps its data in; they are filled in here the way
//! Jinja fills them in, so that the page is the same page.
//! tests/test_shell_rs.py holds the two against each other.

const INDEX_HTML: &str = include_str!("../../../templates/index.html");
const APP_JS: &[u8] = include_bytes!("../../../static/js/app.js");
const STYLE_CSS: &[u8] = include_bytes!("../../../static/css/style.css");

/// Text the way Jinja writes it into a page.
fn escaped(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&#34;"),
            '\'' => out.push_str("&#39;"),
            other => out.push(other),
        }
    }
    out
}

/// The page, with the version and the data folder filled in.
pub fn index(version: &str, appdata_dir: &str) -> String {
    fill(INDEX_HTML, version, appdata_dir)
}

fn fill(template: &str, version: &str, appdata_dir: &str) -> String {
    // Jinja reads every line break as "\n" and drops one at the very end.
    let template = template.replace("\r\n", "\n").replace('\r', "\n");
    let template = template.strip_suffix('\n').unwrap_or(&template);
    let mut out = String::with_capacity(template.len() + 64);
    let mut rest = template;
    while let Some(at) = rest.find("{{ ") {
        out.push_str(&rest[..at]);
        let after = &rest[at..];
        if let Some(tail) = after.strip_prefix("{{ version }}") {
            out.push_str(&escaped(version));
            rest = tail;
        } else if let Some(tail) = after.strip_prefix("{{ appdata_dir }}") {
            out.push_str(&escaped(appdata_dir));
            rest = tail;
        } else {
            out.push_str("{{ ");
            rest = &after[3..];
        }
    }
    out.push_str(rest);
    out
}

/// What the window runs before the page's own script.
///
/// The page was written for pywebview's window and asks it three things
/// through `window.pywebview.api`: the run's secret (when the one in its
/// address got lost), to minimize, and to hide.  Here those become calls to
/// the shell, which owns the window, so that static/js/app.js does not have
/// to change.  Two more things pywebview did for the page: the parts marked
/// `pywebview-drag-region` move the window (only the marked parts
/// themselves, not the buttons inside them), and a right click does not
/// open the browser's menu.
///
/// The window gives this to its own page only; it is never served.  The
/// window runs it in every frame it ever shows, so the script itself looks
/// where it is: anywhere but in the app's own page, at the top of the
/// window, it does nothing, and hands the secret to nobody.
pub fn shim(token: &crate::token::Token, origin: &str) -> String {
    // (an origin is letters, digits, dots, a colon and slashes: nothing that
    // ends the text it is written into)
    let origin: String = origin.chars().filter(|c| c.is_ascii_alphanumeric() || ".:/-".contains(*c)).collect();
    SHIM.replace("%TOKEN%", token.as_str()).replace("%ORIGIN%", &origin)
}

/// Is `address` a page of the app's own?  `origin` is where the app's page
/// lives (`http://vispora.localhost`); the window stays there and goes
/// nowhere else.  The whole of the origin is compared, not the host alone:
/// another scheme or another port under the same host name is another place.
pub fn is_the_apps_page(address: &str, origin: &str) -> bool {
    match address.strip_prefix(origin) {
        // Exactly the origin, or the origin followed by a path, a query or a
        // fragment.  Anything else that begins the same way (a longer host
        // name, a port, a user name in front of another host) is not it.
        Some(rest) => rest.is_empty() || rest.starts_with(['/', '?', '#']),
        None => false,
    }
}

const SHIM: &str = r#"(function () {
    if (window.top !== window.self || location.origin !== "%ORIGIN%") { return; }
    if (window.pywebview && window.pywebview.api) { return; }
    var token = "%TOKEN%";
    var hidden = false;
    function ask(path) {
        return fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Vispora-Token': token },
            body: '{}'
        }).then(function (answer) { return answer.json(); })
          .then(function (said) { return !!(said && said.ok); });
    }
    window.pywebview = { api: {
        get_api_token: function () { return Promise.resolve(token); },
        minimize: function () { return ask('/api/window/minimize'); },
        close_to_tray: function () {
            return ask('/api/window/close').then(function (done) { if (done) { hidden = true; } return done; });
        },
        show: function () {
            return ask('/api/window/show').then(function (done) { if (done) { hidden = false; } return done; });
        },
        is_hidden: function () { return Promise.resolve(hidden); }
    } };
    function mark() {
        var style = document.createElement('style');
        style.textContent = '.pywebview-drag-region { -webkit-app-region: drag; } ' +
            '.pywebview-drag-region *:not(.pywebview-drag-region) { -webkit-app-region: no-drag; }';
        (document.head || document.documentElement).appendChild(style);
    }
    if (document.head) { mark(); } else { document.addEventListener('DOMContentLoaded', mark); }
    document.addEventListener('contextmenu', function (e) { e.preventDefault(); });
})();
"#;

/// One of the page's files by its path: the kind of file and its bytes.
pub fn file(path: &str) -> Option<(&'static str, &'static [u8])> {
    match path {
        "/static/js/app.js" => Some(("text/javascript; charset=utf-8", APP_JS)),
        "/static/css/style.css" => Some(("text/css; charset=utf-8", STYLE_CSS)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_two_names_are_filled_in_and_nothing_else_is_touched() {
        let page = fill("<b>v{{ version }}</b>\r\n<code>{{ appdata_dir }}</code> {{ other }} {{version}}\r\n", "3.8.1", "C:\\Users\\A & B\\<x>'\"");
        assert_eq!(page, "<b>v3.8.1</b>\n<code>C:\\Users\\A &amp; B\\&lt;x&gt;&#39;&#34;</code> {{ other }} {{version}}");
        // What is filled in is not read again as a name.
        assert_eq!(fill("{{ version }}", "{{ appdata_dir }}", "x"), "{{ appdata_dir }}");
    }

    #[test]
    fn the_real_page_has_its_version_and_no_name_left_over() {
        let page = index("9.9.9", "C:\\Data\\Vispora");
        assert!(page.contains("v9.9.9") && page.contains("C:\\Data\\Vispora"));
        assert!(!page.contains("{{") && !page.contains("{%"), "the template uses something the shell does not fill in");
        assert!(page.starts_with("<!DOCTYPE html>") && !page.contains('\r') && !page.ends_with('\n'));
        assert!(page.contains("/static/js/app.js") && page.contains("/static/css/style.css"));
    }

    #[test]
    fn the_window_stands_in_for_what_the_page_asked_pywebview_for() {
        let token = crate::token::Token::from_text("abcdefghijklmnopqrstuvwxyz0123456789-_ABCDEFG").unwrap();
        let script = shim(&token, "http://vispora.localhost");
        // Before anything else: only in the app's own page, at the top of the window.
        let guard = "if (window.top !== window.self || location.origin !== \"http://vispora.localhost\") { return; }";
        assert!(script.find(guard).unwrap() < script.find("var token").unwrap(), "the secret is named before the script has looked where it is");
        assert!(!script.contains("%ORIGIN%"));
        // An origin cannot carry script in with it.
        assert!(!shim(&token, "http://x\"; alert(1); //").contains("alert(1); //\""));
        assert!(script.contains("var token = \"abcdefghijklmnopqrstuvwxyz0123456789-_ABCDEFG\";") && !script.contains("%TOKEN%"));
        for name in ["get_api_token", "minimize", "close_to_tray", "show", "is_hidden"] {
            assert!(script.contains(&format!("        {name}: function")), "{name}");
        }
        for path in ["/api/window/minimize", "/api/window/close", "/api/window/show"] {
            assert!(script.contains(&format!("ask('{path}')")), "{path}");
        }
        assert!(script.contains("'X-Vispora-Token': token"), "the shell's own calls carry the secret too");
        assert!(script.contains(".pywebview-drag-region *:not(.pywebview-drag-region) { -webkit-app-region: no-drag; }"));
        // It leaves a pywebview that is really there alone.
        assert!(script.contains("if (window.pywebview && window.pywebview.api) { return; }"));
    }

    #[test]
    fn only_the_apps_own_origin_is_the_apps_page() {
        let origin = "http://vispora.localhost";
        for own in ["http://vispora.localhost", "http://vispora.localhost/", "http://vispora.localhost/#t=abc",
                    "http://vispora.localhost/?x=1", "http://vispora.localhost/static/js/app.js"] {
            assert!(is_the_apps_page(own, origin), "{own}");
        }
        for other in [
            "https://vispora.localhost/",           // another scheme
            "https://vispora.localhost:8443/",      // and another port
            "http://vispora.localhost:8080/",
            "ftp://vispora.localhost/",
            "file://vispora.localhost/c$/Windows/",
            "http://vispora.localhost.evil.example/",
            "http://vispora.localhost@evil.example/",
            "http://evil.example/http://vispora.localhost/",
            "http://vispora.localhostx/",
            "about:blank",
            "",
        ] {
            assert!(!is_the_apps_page(other, origin), "{other}");
        }
    }

    #[test]
    fn the_pages_two_files_are_there_and_nothing_else_is() {
        let (kind, script) = file("/static/js/app.js").unwrap();
        assert!(kind.starts_with("text/javascript") && script.len() > 100_000);
        let (kind, style) = file("/static/css/style.css").unwrap();
        assert!(kind.starts_with("text/css") && style.len() > 10_000);
        for other in ["/static/js/", "/static/js/app.js.map", "/static/../app.py", "/static/css/style.css/", "/app.py"] {
            assert!(file(other).is_none(), "{other}");
        }
    }
}
