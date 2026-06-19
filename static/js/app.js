/* ═══════════════════════════════════════════════════════════════
   GhostShell — Frontend Controller
   ═══════════════════════════════════════════════════════════════ */

// ─── State ───
let currentPage = 'dashboard';
let selectedDnsPreset = null;
let vaultPin = '';
let vaultCategory = '';
let logPollTimer = null;
let lastLogCount = 0;

// ═══════════════════════════════════════════════════════════════
// v2.9.9.8 — Error Reporter
// ═══════════════════════════════════════════════════════════════
// Catches:
//   • Uncaught JS exceptions       (window.onerror)
//   • Unhandled promise rejections (window.onunhandledrejection)
//   • API 5xx / network failures   (apiGet / apiPost helpers below)
//   • Manual user-triggered reports ("Report a problem" button)
//
// Shows the user a modal with the error details, a "Copy details" button,
// and a "Report on GitHub" button that opens a pre-filled issue.
//
// De-dupes: identical errors within 30 s only show once.
// Suppression: silences itself during the first 1.5 s of page load (covers
// noisy startup races where Flask isn't quite ready yet).
const _errorReporter = {
    GITHUB_ISSUES_URL: 'https://github.com/DeroXP/ghostshell/issues',
    seenRecent: new Map(),         // hash -> timestamp
    pageLoadedAt: Date.now(),
    suppressForMs: 1500,
};

function _hashError(text) {
    // Simple rolling hash so identical errors collapse
    let h = 0;
    for (let i = 0; i < text.length; i++) h = ((h << 5) - h + text.charCodeAt(i)) | 0;
    return h.toString(36);
}

// beta.14 — definition moved below the toast helpers.  See `_maybeReportError`
// further down (uses showErrorToast instead of the old modal).

// Window-level handlers — installed immediately on script load.
// beta.14: switched from showErrorReportModal (blocking modal w/ form)
// to the lightweight bottom-right toast.  The toast auto-submits the
// report to /api/errors/submit so the user doesn't have to click
// anything; they just see "we got it, we're on it" + dismiss.
window.addEventListener('error', function(ev) {
    _maybeReportError('Uncaught JS error',
        ev.message || 'unknown',
        (ev.error && ev.error.stack) || ev.filename + ':' + ev.lineno + ':' + ev.colno);
});
window.addEventListener('unhandledrejection', function(ev) {
    const reason = ev.reason;
    const msg = (reason && reason.message) || (reason && String(reason)) || 'unknown';
    _maybeReportError('Unhandled promise rejection',
        msg,
        (reason && reason.stack) || '');
});

// ═══════════════════════════════════════════════════════════════
// beta.14 — Bottom-right toast notifications
// ═══════════════════════════════════════════════════════════════
// Three flavours:
//   showErrorToast(msg, opts)  — red border, auto-submits to /errors/submit
//                                so the maintainer hears about every failure
//                                without the user clicking a "Send" button.
//   showWarnToast (msg, opts)  — amber border, validation / user-input
//                                problems.  No auto-submit (it's not a bug).
//   showInfoToast (msg, opts)  — green border, "operation completed" success
//                                notifications.  Auto-dismisses fast.
//
// Toasts stack bottom-up; max 4 visible at once (older ones auto-collapse).
// Auto-dismiss timers: error 10s, warn 6s, info 4s — overridable via
// `opts.timeoutMs`.  Pass `opts.sticky:true` to disable auto-dismiss.
//
// `opts.detail` (errors only) ships along to /errors/submit but is NOT
// shown in the toast body — the user shouldn't see a stack trace.

const _toastState = {
    container: null,
    seenRecent: new Map(),          // hash -> ts (dedupe identical toasts)
    rateWindowMs: 4000,             // suppress identical toast within window
    cssInjected: false,
};

function _ensureToastInfra() {
    if (!_toastState.cssInjected) {
        const css = (
            '#gs-toast-container{position:fixed;right:18px;bottom:18px;display:flex;' +
                'flex-direction:column-reverse;gap:10px;z-index:99999;pointer-events:none;' +
                'max-width:380px;font-family:Segoe UI,system-ui,sans-serif}' +
            '.gs-toast{pointer-events:auto;background:#13131a;border:1px solid #2a2a36;' +
                'border-left:3px solid #c4c1ff;border-radius:6px;padding:12px 14px 12px 14px;' +
                'box-shadow:0 6px 16px rgba(0,0,0,0.45);color:#f5f5f7;font-size:12.5px;' +
                'line-height:1.5;opacity:0;transform:translateX(20px);' +
                'transition:opacity 240ms ease,transform 240ms ease;position:relative}' +
            '.gs-toast.is-shown{opacity:1;transform:translateX(0)}' +
            '.gs-toast.is-leaving{opacity:0;transform:translateX(20px)}' +
            '.gs-toast.gs-error{border-left-color:#f0888c;background:#1a1316}' +
            '.gs-toast.gs-warn{border-left-color:#fbbf24;background:#1a1714}' +
            '.gs-toast.gs-info{border-left-color:#c4c1ff}' +
            '.gs-toast .gs-toast-title{font-weight:600;color:#f5f5f7;margin-bottom:4px;' +
                'display:flex;align-items:center;gap:6px}' +
            '.gs-toast.gs-error .gs-toast-title{color:#f0888c}' +
            '.gs-toast.gs-warn .gs-toast-title{color:#fbbf24}' +
            '.gs-toast.gs-info .gs-toast-title{color:#c4c1ff}' +
            '.gs-toast .gs-toast-msg{color:#c0c0c8;font-size:12px}' +
            '.gs-toast .gs-toast-sub{color:#7a7a82;font-size:11px;margin-top:6px;' +
                'line-height:1.4;font-style:italic}' +
            '.gs-toast .gs-toast-close{position:absolute;top:6px;right:8px;background:none;' +
                'border:0;color:#7a7a82;cursor:pointer;font-size:16px;padding:0 4px;' +
                'line-height:1;font-family:inherit}' +
            '.gs-toast .gs-toast-close:hover{color:#f5f5f7}' +
            '.gs-toast .gs-toast-icon{width:14px;height:14px;flex-shrink:0}'
        );
        const styleEl = document.createElement('style');
        styleEl.id = 'gs-toast-styles';
        styleEl.appendChild(document.createTextNode(css));
        document.head.appendChild(styleEl);
        _toastState.cssInjected = true;
    }
    if (!_toastState.container) {
        let c = document.getElementById('gs-toast-container');
        if (!c) {
            c = document.createElement('div');
            c.id = 'gs-toast-container';
            // Append to body if it exists, else defer to DOMContentLoaded
            if (document.body) document.body.appendChild(c);
            else document.addEventListener('DOMContentLoaded', function(){
                document.body.appendChild(c);
            });
        }
        _toastState.container = c;
    }
    return _toastState.container;
}

function _gsToastEscape(s) {
    if (typeof s !== 'string') s = String(s == null ? '' : s);
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
            .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _showToast(kind, title, message, opts) {
    opts = opts || {};
    // Dedupe: identical toast within rateWindowMs collapses
    const key = kind + '|' + (title||'') + '|' + (message||'');
    const now = Date.now();
    const prev = _toastState.seenRecent.get(key) || 0;
    if (now - prev < _toastState.rateWindowMs) return null;
    _toastState.seenRecent.set(key, now);

    const c = _ensureToastInfra();
    const node = document.createElement('div');
    node.className = 'gs-toast gs-' + kind;
    const sub = (kind === 'error' && !opts.suppressDevHint)
        ? '<div class="gs-toast-sub">Sent to the devs &mdash; we\'re on it.</div>'
        : '';
    const icon = ({
        error: '<svg class="gs-toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>',
        warn:  '<svg class="gs-toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 2L2 21h20L12 2z"/><line x1="12" y1="10" x2="12" y2="14"/><circle cx="12" cy="17.5" r="0.8" fill="currentColor"/></svg>',
        info:  '<svg class="gs-toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>',
    })[kind] || '';
    node.innerHTML =
        '<button class="gs-toast-close" aria-label="Dismiss">&times;</button>' +
        '<div class="gs-toast-title">' + icon + '<span>' + _gsToastEscape(title || '') + '</span></div>' +
        (message ? '<div class="gs-toast-msg">' + _gsToastEscape(message) + '</div>' : '') +
        sub;

    // Cap at 4 visible: collapse oldest
    while (c.children.length >= 4) c.removeChild(c.firstChild);
    c.appendChild(node);
    // Trigger transition
    requestAnimationFrame(function(){ node.classList.add('is-shown'); });

    const dismiss = function() {
        if (node._gsDismissed) return;
        node._gsDismissed = true;
        node.classList.add('is-leaving');
        setTimeout(function(){
            try { node.remove(); } catch (_) {}
        }, 280);
    };
    node.querySelector('.gs-toast-close').addEventListener('click', dismiss);

    const defaults = { error: 10000, warn: 6000, info: 4000 };
    const ms = opts.timeoutMs || defaults[kind] || 6000;
    if (!opts.sticky) setTimeout(dismiss, ms);
    return { node: node, dismiss: dismiss };
}

function showErrorToast(message, opts) {
    opts = opts || {};
    // Fire-and-forget submit to Railway.  The user doesn't see this happen;
    // it just lands in /errors/list for the maintainer to triage.
    if (opts.submit !== false) {
        try {
            apiPost('/api/errors/submit', {
                kind:      opts.kind || 'Vispora error',
                message:   String(message || '').slice(0, 1000),
                detail:    opts.detail || '',
                user_note: opts.userNote || '',
            }).catch(function(){ /* offline / network — already saved locally by backend */ });
        } catch (_) { /* apiPost might not be defined yet during early errors */ }
    }
    return _showToast('error',
        opts.title || 'Vispora ran into an error',
        message,
        opts);
}

function showWarnToast(message, opts) {
    return _showToast('warn',
        (opts && opts.title) || 'Heads up',
        message, opts || {});
}

function showInfoToast(message, opts) {
    return _showToast('info',
        (opts && opts.title) || 'Done',
        message, opts || {});
}

// beta.14 — global error handler now routes to the toast, not the modal.
// Kept showErrorReportModal as the manual "Report a problem" entry point
// since users sometimes want the rich preview + user-note workflow.
function _maybeReportError(kind, message, detail) {
    if (Date.now() - _errorReporter.pageLoadedAt < _errorReporter.suppressForMs) return;
    const key = _hashError(kind + '|' + (message || ''));
    const now = Date.now();
    const prev = _errorReporter.seenRecent.get(key) || 0;
    if (now - prev < 30000) return;
    _errorReporter.seenRecent.set(key, now);
    showErrorToast(message || 'unknown', { kind: kind, detail: detail });
}

// HTML escaper used in the modal — small local helper so we don't depend
// on escHtml being defined yet (this code runs before everything else).
function _escForModal(s) {
    if (typeof s !== 'string') s = String(s == null ? '' : s);
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Pulls the diagnostics snapshot and builds a markdown body for a new
// GitHub issue.  Pre-fills the title from the error and the body with
// app version, hardware, recent log lines, and the error detail.
async function _buildIssueBody(kind, message, detail) {
    let snap = {};
    try {
        const r = await fetch('/api/diagnostics/snapshot', { signal: AbortSignal.timeout(3000) });
        if (r.ok) snap = await r.json();
    } catch (e) { /* the snapshot is best-effort */ }

    const v = (snap.app && snap.app.version) || 'unknown';
    const os = (snap.os && (snap.os.platform || snap.os.version)) || 'unknown';
    const gpu = (snap.hardware && snap.hardware.gpu && snap.hardware.gpu.name) || 'unknown';
    const cpu = (snap.hardware && snap.hardware.cpu && snap.hardware.cpu.name) || 'unknown';
    const admin = (snap.app && snap.app.admin) ? 'yes' : 'no';

    const recentLogs = (snap.logs || []).slice(-30).map(function(e) {
        return (e.ts || '') + ' [' + (e.level || '') + '] ' + (e.module || '') + ' ' + (e.msg || '');
    }).join('\n');

    return (
        '## What happened\n\n' +
        '_(please describe what you were doing when this occurred)_\n\n' +
        '## Error\n\n' +
        '**Kind:** ' + kind + '\n' +
        '**Message:** `' + message + '`\n\n' +
        (detail ? ('**Detail:**\n```\n' + detail.slice(0, 1500) + '\n```\n\n') : '') +
        '## Environment\n\n' +
        '- Vispora: `' + v + '`\n' +
        '- OS: `' + os + '`\n' +
        '- GPU: `' + gpu + '`\n' +
        '- CPU: `' + cpu + '`\n' +
        '- Running as admin: ' + admin + '\n\n' +
        (recentLogs ? ('## Recent log lines\n\n```\n' + recentLogs.slice(0, 3500) + '\n```\n') : '')
    );
}

function showErrorReportModal(kind, message, detail) {
    // Reuse the existing modal infrastructure if openModal exists.  This
    // function is called from the global error handler which runs BEFORE
    // openModal might be defined if there's a parse error very early —
    // so we fall back to alert() in that worst-case.
    if (typeof openModal !== 'function') {
        try { console.error('[GhostShell error]', kind, message, detail); } catch (_){}
        return;
    }
    const k = _escForModal(kind), m = _escForModal(message);
    const d = detail ? _escForModal(String(detail).slice(0, 800)) : '';

    // Stash raw values on window so we can pass them to the Send handler
    // without DOM round-tripping (which mangles quotes).
    window._lastErrorReport = { kind: kind, message: message, detail: detail };

    openModal(
        '<div class="modal-title" style="color:var(--red)">⚠ ' + k + '</div>' +
        '<div style="margin-bottom:12px;padding:10px;background:var(--bg-void);' +
            'border-left:3px solid var(--red);border-radius:4px;font-family:var(--mono);' +
            'font-size:11px;color:var(--text);word-break:break-word">' + m + '</div>' +
        (d ? '<details style="margin-bottom:12px"><summary style="cursor:pointer;font-size:11px;color:var(--text-dim)">▸ Technical detail (click to expand)</summary>' +
            '<pre style="margin-top:8px;padding:8px;background:var(--bg-void);border:1px solid var(--border);' +
            'border-radius:4px;font-size:10px;color:var(--text-dim);overflow:auto;max-height:160px;white-space:pre-wrap">' +
            d + '</pre></details>' : '') +
        '<div style="font-size:11px;color:var(--text-dim);line-height:1.5;margin-bottom:8px">' +
            'Vispora hit an unexpected error.  Send it to the maintainer ' +
            '(via Railway) and it\'ll get triaged + patched — no GitHub account needed. ' +
            'Payload: your hardware, recent logs, this error.  No personal data.' +
        '</div>' +
        '<details style="margin-bottom:12px"><summary style="cursor:pointer;font-size:11px;color:var(--accent)">▸ What gets sent? (preview)</summary>' +
            '<div id="error-report-preview" style="margin-top:8px;padding:8px;background:var(--bg-void);border:1px solid var(--border);border-radius:4px;font-size:10.5px;font-family:var(--mono);color:var(--text-dim);max-height:200px;overflow:auto;white-space:pre-wrap">loading…</div>' +
        '</details>' +
        '<textarea id="error-report-note" placeholder="Optional: what were you doing when this happened?" ' +
            'style="width:100%;min-height:60px;padding:8px;background:var(--bg-void);border:1px solid var(--border);' +
            'border-radius:4px;color:var(--text);font-family:var(--font-sans);font-size:12px;resize:vertical;margin-bottom:10px"></textarea>' +
        '<div id="error-report-status" style="font-size:11px;color:var(--text-dim);margin-bottom:10px;min-height:14px"></div>' +
        '<div class="modal-actions" style="flex-wrap:wrap;gap:6px">' +
            '<button class="btn" onclick="closeModal()">Dismiss</button>' +
            '<button class="btn" onclick="copyErrorDetailsToClipboard(' +
                'window._lastErrorReport.kind, window._lastErrorReport.message, window._lastErrorReport.detail)">' +
                'Copy details</button>' +
            '<button class="btn btn-primary" id="error-report-send-btn" onclick="sendErrorReportToMaintainer()">' +
                'Send to maintainer →</button>' +
        '</div>'
    );

    // Lazy-load preview so the modal opens snappy
    setTimeout(_loadErrorReportPreview, 50);
}

async function _loadErrorReportPreview() {
    var box = document.getElementById('error-report-preview');
    if (!box || !window._lastErrorReport) return;
    try {
        var r = await apiPost('/api/errors/preview', {
            kind:    window._lastErrorReport.kind,
            message: window._lastErrorReport.message,
            detail:  window._lastErrorReport.detail,
        });
        if (!r || r.ok === false) {
            box.textContent = 'preview failed: ' + (r && r.err || 'unknown');
            return;
        }
        // Pretty-print, hide internal fields the user doesn't care about
        var env = r.env || {};
        var preview = [
            'kind:       ' + (r.kind || ''),
            'message:    ' + (r.message || '').slice(0, 200),
            'app:        v' + (env.app_version || '?'),
            'os:         ' + (env.os_platform || env.os_release || '?'),
            'cpu:        ' + (env.cpu || '?'),
            'gpu:        ' + (env.gpu || '?'),
            'admin:      ' + (env.admin ? 'yes' : 'no'),
            'anon_id:    ' + (env.anon_id || '?').slice(0, 8) + '…',
            'log lines:  ' + (r.logs || []).length,
            'hash:       ' + (r.hash || ''),
        ].join('\n');
        box.textContent = preview;
    } catch (e) {
        box.textContent = 'preview failed: ' + (e && e.message || e);
    }
}

async function sendErrorReportToMaintainer() {
    if (!window._lastErrorReport) return;
    var noteEl = document.getElementById('error-report-note');
    var statusEl = document.getElementById('error-report-status');
    var btn = document.getElementById('error-report-send-btn');
    var note = (noteEl && noteEl.value || '').slice(0, 1500);
    if (btn) { btn.disabled = true; btn.textContent = 'Sending…'; }
    if (statusEl) statusEl.textContent = 'Uploading to Railway…';
    try {
        var r = await apiPost('/api/errors/submit', {
            kind:      window._lastErrorReport.kind,
            message:   window._lastErrorReport.message,
            detail:    window._lastErrorReport.detail,
            user_note: note,
        });
        if (r && r.ok) {
            if (r.duplicate) {
                if (statusEl) statusEl.innerHTML = '<span style="color:var(--accent)">Already sent recently (deduped). Hash: ' + (r.hash || '?') + '</span>';
            } else if (r.skipped) {
                if (statusEl) statusEl.innerHTML = '<span style="color:var(--warning)">Error reporting is off in Settings.  Saved locally: ' + (r.local_path || '?') + '</span>';
            } else {
                if (statusEl) statusEl.innerHTML = '<span style="color:var(--accent)">✓ Sent.  Reference hash: ' + (r.hash || '?') + '</span>';
                setTimeout(closeModal, 1800);
            }
        } else {
            if (statusEl) statusEl.innerHTML = '<span style="color:var(--danger)">Send failed: ' + ((r && r.err) || 'unknown') + ' — saved locally so you can retry.</span>';
            if (btn) { btn.disabled = false; btn.textContent = 'Retry →'; }
        }
    } catch (e) {
        if (statusEl) statusEl.innerHTML = '<span style="color:var(--danger)">Network error: ' + (e && e.message || e) + '</span>';
        if (btn) { btn.disabled = false; btn.textContent = 'Retry →'; }
    }
}

async function copyErrorDetailsToClipboard(kind, message, detail) {
    const body = await _buildIssueBody(kind, message, detail);
    try {
        await navigator.clipboard.writeText(body);
        addLog('Error report copied to clipboard.');
    } catch (e) {
        addLog('Could not copy to clipboard: ' + (e && e.message || e));
    }
}

async function openGithubIssueWithDetails(kind, message, detail) {
    const body = await _buildIssueBody(kind, message, detail);
    // GitHub URL-length limit is ~7 KB.  Trim if needed and tell the user
    // their clipboard has the full content.
    const title = '[v' + ((window._ocState && window._ocState.appVersion) || '?') + '] ' +
                  (kind || 'Error') + ': ' + (message || '').slice(0, 80);
    const params = new URLSearchParams({ title: title, body: body.slice(0, 6000) });
    const url = _errorReporter.GITHUB_ISSUES_URL + '/new?' + params.toString();

    // Always copy to clipboard too — if the URL got truncated, the full
    // body is still available for the user to paste.
    try { await navigator.clipboard.writeText(body); } catch (_) {}

    // Open in default browser.  pywebview has a window.open hook;
    // fall back to a plain anchor click otherwise.
    try {
        if (window.pywebview) {
            // pywebview opens external links in the system browser
            window.open(url, '_blank');
        } else {
            window.open(url, '_blank');
        }
        addLog('Opened GitHub issue form (full body also copied to clipboard).');
    } catch (e) {
        addLog('Could not open browser — issue body is on your clipboard. Visit ' +
               _errorReporter.GITHUB_ISSUES_URL + '/new and paste it.');
    }
    closeModal();
}

// Manual entry point — Settings → "Report a problem" calls this.
function reportProblemManually() {
    showErrorReportModal('Manual report',
        'User-initiated bug report. Please describe what you were doing in the GitHub issue body.',
        '');
}

// ─── API Helpers ───
// Internal: parse a fetch Response into a uniform {ok, err, ...} shape.
// Always returns a plain object — never throws.  Handles the four common
// failure modes:
//   1. Network/abort/timeout            → ok: false, err: short reason
//   2. Non-JSON body (Flask HTML 500)   → ok: false, err: "<status>: <hint>"
//   3. JSON body with ok: false         → forwarded as-is
//   4. JSON body with ok: true          → forwarded as-is
//
// v2.9.9.8 — surface SERIOUS errors (5xx, total network failure) through
// the global error reporter modal so the user knows something went wrong
// instead of silently retrying or showing an obscure log line.
async function _apiHandle(url, fetchPromise) {
    // v3.3 — guard against recursive error-report storms.  If the
    // error-report endpoint ITSELF returns 5xx, _maybeReportError →
    // showErrorReportModal → apiPost('/api/errors/*') → _apiHandle →
    // _maybeReportError loops until the stack blows out with
    // "Maximum call stack size exceeded".  Never surface errors from
    // the error-reporter's own endpoints.
    const _isErrorReportPath = url && (
        url.indexOf('/api/errors/') === 0 ||
        url.indexOf('/errors/')     === 0
    );
    try {
        const r = await fetchPromise;
        const ct = (r.headers.get('content-type') || '').toLowerCase();
        if (!ct.includes('application/json')) {
            let snippet = '';
            try { snippet = (await r.text()).slice(0, 200).replace(/\s+/g, ' ').trim(); } catch (_) {}
            // 5xx with non-JSON body → backend crashed in a path that the
            // global error handler couldn't catch.  Definitely worth surfacing.
            if (r.status >= 500 && !_isErrorReportPath) {
                _maybeReportError('Server error',
                    `${r.status} on ${url}`, snippet || '(empty body)');
            }
            return {
                ok: false,
                err: `Server returned non-JSON (HTTP ${r.status})` + (snippet ? `: ${snippet}` : ''),
                _status: r.status,
            };
        }
        const body = await r.json();
        // If HTTP error code but JSON, forward the JSON (already shaped {ok:false,err:...})
        if (!r.ok && body && typeof body === 'object' && body.ok === undefined) {
            body.ok = false;
            body._status = r.status;
        }
        // Even for JSON 5xx, surface to user — backend's global error
        // handler returns clean JSON now, but that doesn't mean the user
        // shouldn't see the failure.
        if (r.status >= 500 && body && body.err && !_isErrorReportPath) {
            _maybeReportError('Server error',
                `${r.status} on ${url}`, body.err + (body.path ? `\nPath: ${body.path}` : ''));
        }
        return body;
    } catch (e) {
        // Network error, abort, JSON parse error
        const msg = (e && e.name === 'AbortError') ? 'Request timed out' : (e.message || String(e));
        console.error('API error:', url, msg);
        // Don't fire the modal for timeouts — those are usually a "Flask
        // is bogged down" hiccup, not a user-facing bug.  Real network
        // errors (TypeError "Failed to fetch") DO matter.  Also never
        // fire for our own error endpoints (would recurse).
        if (e && e.name !== 'AbortError' && /failed to fetch|network/i.test(msg)
            && !_isErrorReportPath) {
            _maybeReportError('Cannot reach Vispora backend', msg, 'URL: ' + url);
        }
        return { ok: false, err: msg };
    }
}

// v3.2.3 — global perf gate.  Skip work in polling callbacks when the
// window is minimized / tabbed away / on the lock screen.  Cuts CPU /
// Flask load from background polling that nobody can see anyway.
// Individual polls call `_pollSkipIfHidden()` at the top; many calls
// add up to noticeable lag when 15+ timers all hammer Flask every
// 1-3 seconds even with the user not looking at the window.
function _pollSkipIfHidden() {
    return (typeof document !== 'undefined') && document.hidden;
}

// Slow-tick budget — wake polls back up the moment the user returns
// so the UI doesn't show stale data for 30 s.  Visibility callbacks
// each polling group registers below.
var _visibilityListeners = [];
function _onVisible(fn) { _visibilityListeners.push(fn); }
if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', function() {
        if (!document.hidden) {
            _visibilityListeners.forEach(function(fn) {
                try { fn(); } catch (e) {}
            });
        }
    });
}


async function apiGet(url, opts = {}) {
    const ac = new AbortController();
    const timeoutMs = opts.timeoutMs || 30000;
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    try {
        return await _apiHandle(url, fetch(url, { signal: ac.signal }));
    } finally { clearTimeout(timer); }
}

async function apiPost(url, data = {}, opts = {}) {
    const ac = new AbortController();
    const timeoutMs = opts.timeoutMs || 60000;
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    try {
        return await _apiHandle(url, fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
            signal: ac.signal,
        }));
    } finally { clearTimeout(timer); }
}

async function apiDelete(url, opts = {}) {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), opts.timeoutMs || 60000);
    try {
        return await _apiHandle(url, fetch(url, {
            method: 'DELETE',
            signal: ac.signal,
        }));
    } finally { clearTimeout(timer); }
}

// ─── Window Controls ───
// Prefer pywebview's native JS API — it calls Python directly, which keeps
// working after the window is hidden and re-shown. fetch() sometimes gets
// throttled by WebView2 in hidden/restored windows.
function _pywebviewApi() {
    return (typeof window !== 'undefined' && window.pywebview && window.pywebview.api) || null;
}

async function minimizeWindow() {
    var api = _pywebviewApi();
    if (api && api.minimize) {
        try { await api.minimize(); return; } catch (e) { /* fall through */ }
    }
    apiPost('/api/window/minimize');
}

// v2.9.0 — close-button rework.
// Pressing the X opens a modal letting the user pick:
//   1. Quit GhostShell entirely (process exits, all tweaks restored)
//   2. Hide UI + arm Game Mode (auto-applies gaming profile when a game runs)
//   3. Just hide to tray (legacy behaviour, still available)
async function closeWindow() {
    showCloseChoiceModal();
}

function showCloseChoiceModal() {
    openModal(
        '<div class="modal-title">CLOSE VISPORA</div>' +
        '<div style="font-size:12px;color:var(--text-dim);margin-bottom:14px">' +
            'How would you like to close the app?' +
        '</div>' +
        // Three large action buttons stacked vertically — easiest to click.
        '<div class="close-choice-row">' +
            '<button class="close-choice-btn close-choice-game" onclick="closeAndStartGameMode()">' +
                '<div class="close-choice-title">▶ Hide + Game Mode</div>' +
                '<div class="close-choice-desc">Hide the window and auto-apply gaming tweaks ' +
                    'when a game launches. App keeps running in the system tray.</div>' +
            '</button>' +
            '<button class="close-choice-btn" onclick="closeToTrayOnly()">' +
                '<div class="close-choice-title">○ Hide to Tray</div>' +
                '<div class="close-choice-desc">Hide the window. App keeps running but does ' +
                    'nothing automatically. Click the tray icon to bring it back.</div>' +
            '</button>' +
            '<button class="close-choice-btn close-choice-quit" onclick="quitApp()">' +
                '<div class="close-choice-title">✕ Quit Vispora</div>' +
                '<div class="close-choice-desc">Fully exit the app. Any active gaming tweaks ' +
                    'are reverted to normal first.</div>' +
            '</button>' +
        '</div>' +
        '<div class="modal-actions"><button class="btn" onclick="closeModal()">Cancel</button></div>'
    );
}

// Hide UI to tray + start the game-profile monitor.
async function closeAndStartGameMode() {
    closeModal();
    addLog('Closing UI — Game Mode armed. Launch a game to apply gaming profile.');
    var api = _pywebviewApi();
    var hideOk = false;
    if (api && api.close_to_tray) {
        try { await api.close_to_tray(); hideOk = true; } catch (e) {}
    }
    // Always call the backend so the monitor starts even if the JS-side
    // hide call already succeeded.
    var r = await apiPost('/api/window/close-and-game-mode');
    if (!r || !r.ok) {
        addLog('Game Mode could not start: ' + ((r && r.err) || 'unknown'));
    }
}

async function closeToTrayOnly() {
    closeModal();
    var api = _pywebviewApi();
    if (api && api.close_to_tray) {
        try { await api.close_to_tray(); return; } catch (e) { /* fall through */ }
    }
    apiPost('/api/window/close');
}

async function quitApp() {
    closeModal();
    addLog('Quitting Vispora...');
    // Best-effort POST — the backend exits ~500ms later, the response
    // may or may not arrive depending on timing.  Either way, the app dies.
    try { await apiPost('/api/window/quit', {}, { timeoutMs: 3000 }); } catch (e) {}
}

// ─── Page Navigation ───
function switchPage(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const el = document.getElementById('page-' + page);
    if (el) el.classList.add('active');
    const nav = document.querySelector('.nav-item[data-page="' + page + '"]');
    if (nav) nav.classList.add('active');
    currentPage = page;

    if (page === 'debloat') loadDebloatScan();
    if (page === 'optimize') { loadProModeStatus(); loadCompetitiveStatus(); }   // v3.3.1-beta.8 / beta.3
    if (page === 'network') { loadNetworkInfo(); startNetMonPoll(); }
    else { stopNetMonPoll(); }
    if (page === 'gpu') loadGpuInfo();
    if (page === 'cleaner') { scanCleaner(); loadDiskList(); diskAnalyzerRefresh(); }
    if (page === 'privacy') loadPrivacyStatus();
    if (page === 'vault') checkVaultStatus();
    if (page === 'logs') loadFullLog();
    if (page === 'settings') loadSettingsPage();   // v2.9.5
}


// v2.9.5 — Settings page loader.
// Pulls four state blobs (autostart, updater, notifier, baseline) and
// renders them as toggle rows + info blocks.
async function loadSettingsPage() {
    // v3.1 — load snapshots in parallel (read-only, fast)
    loadSnapshots();
    var calls = await Promise.all([
        apiGet('/api/autostart'),
        apiGet('/api/updater/settings'),
        apiGet('/api/notifier/status'),
        apiGet('/api/gpu/oc/baseline'),
        apiGet('/api/driver/status'),     // v2.9.6
        apiGet('/api/profiles/settings'), // v2.9.9.6 — game-mode settings
        apiGet('/api/profiles/status'),   // v2.9.9.6 — game-mode current state
    ]);
    var autoStart = calls[0] || {};
    var updaterS  = (calls[1] && calls[1].settings) || {};
    var notif     = calls[2] || {};
    var baseline  = calls[3] || {};
    var driver    = calls[4] || {};
    var gpSet     = (calls[5] && calls[5].settings) || {};
    var gpStatus  = calls[6] || {};
    _renderDriverSection(driver);
    _renderGameModeSection(gpSet, gpStatus);

    // ─ Startup ─
    var sEl = document.getElementById('settings-startup-rows');
    if (sEl) {
        var methodNote = autoStart.method === 'scheduled_task'
            ? 'Uses a Windows Scheduled Task — runs silently with no popups, even at login.'
            : '';
        sEl.innerHTML =
            _settingsToggle(
                'Start Vispora at Windows login',
                'Launches in the system tray ' + (autoStart.delay_seconds || 30) +
                's after login so it doesn\'t slow your boot. ' + methodNote,
                !!autoStart.enabled,
                'toggleAutostart(this)') +
            _settingsRow(
                'Login launch delay',
                'How many seconds after login Vispora waits before launching.',
                '<input type="number" id="autostart-delay-input" min="0" max="600" value="' +
                    (autoStart.delay_seconds || 30) + '" ' +
                    'style="width:80px;background:var(--bg-input);border:1px solid var(--border);' +
                    'color:var(--text);padding:4px 8px;border-radius:4px;font-family:var(--mono)" ' +
                    'onchange="saveAutostartDelay(this.value)"> sec'
            );
        if (!autoStart.frozen) {
            sEl.innerHTML += '<div style="font-size:10px;color:var(--orange);margin-top:8px">' +
                'ⓘ Autostart only works on the built .exe (not the dev source build).</div>';
        }
        if (autoStart.legacy_run_key_present) {
            sEl.innerHTML += '<div style="font-size:10px;color:var(--orange);margin-top:8px">' +
                'ⓘ Legacy Run-key entry detected from an older Vispora version. ' +
                'Toggle autostart off then on again to clean it up.</div>';
        }
    }

    // ─ Auto-update ─
    var uEl = document.getElementById('settings-update-rows');
    if (uEl) {
        uEl.innerHTML =
            _settingsToggle('Check for updates automatically',
                'On launch and every 6 hours.',
                !!updaterS.auto_check, "toggleUpdaterField(this, 'auto_check')") +
            _settingsToggle('Download updates in the background',
                'Pulls the new build as soon as one is detected, before you click Install.',
                !!updaterS.auto_download, "toggleUpdaterField(this, 'auto_download')") +
            _settingsToggle('Install updates silently',
                'Apply updates and restart Vispora with no prompt.  Off by default.',
                !!updaterS.auto_install, "toggleUpdaterField(this, 'auto_install')") +
            // v3 — channel + server-URL.  Default 'beta' until first stable
            // build ships; 'stable' shows a "coming soon" empty state on the
            // landing page until then.
            _settingsRow(
                'Update channel',
                (updaterS.channel === 'beta'
                    ? 'Beta — newest builds.  Features land here first.'
                    : 'Stable — confirmed-working builds only.'),
                '<select id="updater-channel-select" onchange="setUpdaterChannel(this.value)" ' +
                  'style="background:var(--bg-input);border:1px solid var(--border);color:var(--text);' +
                  'padding:4px 8px;border-radius:4px;font-family:var(--mono);font-size:11px">' +
                  '<option value="stable" ' + (updaterS.channel !== 'beta' ? 'selected' : '') + '>Stable</option>' +
                  '<option value="beta" '   + (updaterS.channel === 'beta' ? 'selected' : '') + '>Beta</option>' +
                '</select>'
            ) +
            // v3 — update server URL override.  Used by self-hosters or for
            // pointing at a staging server.  Default is the Railway URL set
            // in config.UPDATE_SERVER_URL_DEFAULT.
            _settingsRow(
                'Update server',
                'Where Vispora pulls updates from.  Leave alone unless you\'re self-hosting.',
                '<input type="text" id="updater-server-input" value="' + escAttr(updaterS.server_url || '') + '" ' +
                  'onchange="setUpdaterServerUrl(this.value)" ' +
                  'placeholder="https://ghostshell-site.up.railway.app" ' +
                  'style="background:var(--bg-input);border:1px solid var(--border);color:var(--text);' +
                  'padding:4px 8px;border-radius:4px;font-family:var(--mono);font-size:11px;min-width:280px" />'
            );
    }

    // ─ Notifications ─
    var nEl = document.getElementById('settings-notifier-rows');
    if (nEl) {
        nEl.innerHTML = _settingsToggle('Windows toast notifications',
            'Pop-up alerts for game detection, boot-prep complete, updates, etc.',
            !!notif.enabled, 'toggleNotifierEnabled(this)');
    }

    // ─ Baseline ─
    var bEl = document.getElementById('settings-baseline-info');
    if (bEl) {
        if (baseline && baseline.ok) {
            var src = baseline.from_cache ? 'cached' : 'just captured';
            bEl.innerHTML =
                '<div><b style="color:var(--text)">Core stock:</b> ' + (baseline.core_stock_mhz || '?') + ' MHz</div>' +
                '<div><b style="color:var(--text)">Memory stock:</b> ' + (baseline.mem_stock_mhz || '?') + ' MHz</div>' +
                '<div style="color:var(--text-dim);margin-top:4px">Source: ' + src + '</div>';
        } else {
            bEl.innerHTML = '<div style="color:var(--orange)">' +
                escHtml((baseline && baseline.err) || 'Could not capture baseline (NVIDIA GPU required)') + '</div>';
        }
    }
}

// Helper — renders a labelled toggle-switch row used throughout Settings.
function _settingsToggle(label, desc, on, onclickJs) {
    return '<div class="toggle-row">' +
        '<div class="toggle-info"><div class="toggle-name">' + escHtml(label) + '</div>' +
        '<div class="toggle-desc">' + escHtml(desc) + '</div></div>' +
        '<button type="button" class="toggle-switch ' + (on ? 'on' : '') + '" ' +
        'role="switch" aria-checked="' + on + '" onclick="' + onclickJs + '"></button></div>';
}

// Helper — generic row with a custom right-side control.
function _settingsRow(label, desc, control) {
    return '<div class="toggle-row">' +
        '<div class="toggle-info"><div class="toggle-name">' + escHtml(label) + '</div>' +
        '<div class="toggle-desc">' + escHtml(desc) + '</div></div>' +
        '<div>' + control + '</div></div>';
}

async function toggleAutostart(el) {
    el.classList.toggle('on');
    var on = el.classList.contains('on');
    el.setAttribute('aria-checked', String(on));
    var endpoint = on ? '/api/autostart/enable' : '/api/autostart/disable';
    var r = await apiPost(endpoint, {});
    if (!r || !r.ok) {
        // Revert on failure
        el.classList.toggle('on');
        el.setAttribute('aria-checked', String(!on));
        addLog('Could not ' + (on ? 'enable' : 'disable') + ' autostart: ' + ((r && r.err) || 'unknown'));
    } else {
        addLog('Autostart ' + (on ? 'enabled — will launch ' + (r.delay_seconds || 30) +
                                    's after login' : 'disabled'));
    }
}

async function saveAutostartDelay(seconds) {
    var n = Math.max(0, Math.min(600, parseInt(seconds) || 30));
    var r = await apiPost('/api/autostart/delay', { delay_seconds: n });
    if (r && r.ok) addLog('Autostart delay set to ' + n + 's');
}

async function toggleUpdaterField(el, key) {
    el.classList.toggle('on');
    var on = el.classList.contains('on');
    el.setAttribute('aria-checked', String(on));
    var payload = {}; payload[key] = on;
    var r = await apiPost('/api/updater/settings', payload);
    if (!r || !r.ok) {
        el.classList.toggle('on');
        el.setAttribute('aria-checked', String(!on));
    }
}

// v3 — channel selector (stable / beta).  Defaults to beta until first
// stable build ships; stable channel currently empty but the UI still
// allows selecting it (will show "no release available" when checked).
async function setUpdaterChannel(channel) {
    if (channel !== 'stable' && channel !== 'beta') return;
    if (channel === 'beta') {
        if (!confirm('Switch to the Beta channel?\n\n' +
            'Beta builds get new features first and may have rough edges. ' +
            'Vispora will auto-update to whatever the latest beta is.\n\n' +
            'You can switch back to Stable anytime in Settings.\n\n' +
            'Continue?')) {
            var sel = document.getElementById('updater-channel-select');
            if (sel) sel.value = 'stable';
            return;
        }
    }
    var r = await apiPost('/api/updater/settings', { channel: channel });
    if (r && r.ok) {
        addLog('Update channel set to "' + channel + '"');
        loadSettingsPage();
    } else {
        addLog('Could not set channel: ' + ((r && r.err) || 'unknown'));
    }
}

// v3 — update server URL override.  Empty input is treated as "reset to
// default" (the Railway URL baked into config.py).
async function setUpdaterServerUrl(url) {
    url = (url || '').trim();
    if (!url) {
        // Reset to default by reading the default-server-url from the
        // current status payload and writing that back.
        var st = await apiGet('/api/updater/status');
        url = (st && st.default_server_url) || 'https://ghostshell-site.up.railway.app';
        var input = document.getElementById('updater-server-input');
        if (input) input.value = url;
    }
    if (!/^https?:\/\//.test(url)) {
        showWarnToast('Server URL must start with http:// or https://');
        return;
    }
    var r = await apiPost('/api/updater/settings', { server_url: url });
    if (r && r.ok) {
        addLog('Update server set to "' + url + '"');
    } else {
        showErrorToast('Could not set server URL: ' + ((r && r.err) || 'unknown'));
    }
}

async function toggleNotifierEnabled(el) {
    el.classList.toggle('on');
    var on = el.classList.contains('on');
    el.setAttribute('aria-checked', String(on));
    var r = await apiPost('/api/notifier/toggle', { enabled: on });
    if (!r || !r.ok) {
        el.classList.toggle('on');
        el.setAttribute('aria-checked', String(!on));
    }
}

async function testNotifierToast() {
    await apiPost('/api/notifier/test');
    addLog('Test notification sent.');
}

async function recalibrateBaseline() {
    var r = await apiPost('/api/gpu/oc/baseline', {});
    if (r && r.ok) {
        addLog('Stock baseline recaptured: core=' + r.core_stock_mhz + ' MHz, mem=' + r.mem_stock_mhz + ' MHz');
        loadSettingsPage();
    } else {
        addLog('Baseline recapture failed: ' + ((r && r.err) || 'unknown'));
    }
}

// ═══ Snapshots & Restore ═══
function _humanSize(b) {
    if (b == null) return '—';
    var u = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (Math.round(b * 10) / 10) + ' ' + u[i];
}

async function loadSnapshots() {
    var data = await apiGet('/api/snapshots');
    var snaps = (data && data.snapshots) || [];
    var c = document.getElementById('snapshots-list');
    if (!c) return;
    if (snaps.length === 0) {
        c.innerHTML = '<div class="empty-state">No snapshots yet. ' +
            'Vispora auto-snapshots before any bulk apply or full reset, ' +
            'or you can take one manually before risky changes.</div>';
        return;
    }
    var html = '';
    snaps.forEach(function(s) {
        var d = new Date(s.ts * 1000);
        var dateStr = d.toLocaleString();
        var autoBadge = s.auto
            ? '<span class="status-badge neutral" style="margin-left:6px">auto</span>'
            : '<span class="status-badge ok" style="margin-left:6px">manual</span>';
        var warnBadge = (s.failed_paths && s.failed_paths.length > 0)
            ? '<span class="status-badge warn" style="margin-left:6px">' +
              s.failed_paths.length + ' path(s) failed</span>'
            : '';
        html +=
          '<div style="padding:12px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-elevated);margin-bottom:6px">' +
            '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
              '<span style="font-weight:600;color:var(--text-bright)">' + escHtml(s.name || 'snapshot') + '</span>' +
              autoBadge + warnBadge +
              '<span style="margin-left:auto;font-family:var(--font-mono);font-size:11px;color:var(--text-tertiary)">' +
                escHtml(dateStr) +
              '</span>' +
            '</div>' +
            (s.description
              ? '<div style="margin-top:4px;font-size:12.5px;color:var(--text-secondary)">' +
                escHtml(s.description) + '</div>'
              : '') +
            '<div style="margin-top:6px;font-family:var(--font-mono);font-size:11.5px;color:var(--text-tertiary)">' +
              s.succeeded + '/' + s.total + ' paths · ' +
              _humanSize(s.size_bytes) + ' · ' +
              s.elapsed_s + 's to capture' +
            '</div>' +
            '<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">' +
              '<button class="btn btn-sm btn-primary" onclick="restoreSnapshot(\'' + escHtml(s.id) + '\', \'' + escHtml(s.name) + '\')">Restore</button>' +
              '<button class="btn btn-sm btn-danger" onclick="deleteSnapshotById(\'' + escHtml(s.id) + '\', \'' + escHtml(s.name) + '\')">Delete</button>' +
            '</div>' +
          '</div>';
    });
    c.innerHTML = html;
}

async function createSnapshot() {
    var nameEl = document.getElementById('snapshot-name');
    var name = (nameEl && nameEl.value || '').trim() || ('manual ' + new Date().toLocaleString());
    var btn = nameEl && nameEl.parentElement && nameEl.parentElement.querySelector('button');
    if (btn) { btn.disabled = true; btn.textContent = 'Capturing…'; }
    var r = await apiPost('/api/snapshots', { name: name });
    if (btn) { btn.disabled = false; btn.textContent = 'Take snapshot now'; }
    if (r && r.ok) {
        addLog('Snapshot taken: ' + r.name + ' (' + r.succeeded + '/' + r.total + ' paths)');
        if (nameEl) nameEl.value = '';
    } else {
        addLog('Snapshot failed: ' + ((r && r.err) || 'unknown'));
    }
    loadSnapshots();
}

async function restoreSnapshot(snapId, snapName) {
    if (!confirm(
        'Restore snapshot "' + snapName + '"?\n\n' +
        'This OVERWRITES all current registry values that this snapshot covered. ' +
        'Any tweaks you applied AFTER this snapshot will be reverted.\n\n' +
        'A reboot is recommended afterwards so kernel-level changes re-load.'
    )) return;
    addLog('Restoring snapshot ' + snapName + '…');
    var r = await apiPost('/api/snapshots/' + encodeURIComponent(snapId) + '/restore', {});
    if (r && r.ok) {
        addLog('✓ Restored ' + r.succeeded + '/' + r.total + ' paths in ' + r.elapsed_s + 's. REBOOT recommended.');
    } else {
        addLog('✗ Restore failed: ' + ((r && r.err) || 'unknown'));
    }
    loadSnapshots();
}

async function deleteSnapshotById(snapId, snapName) {
    if (!confirm('Permanently delete snapshot "' + snapName + '"?\n\nThis cannot be undone.')) return;
    var r = await apiDelete('/api/snapshots/' + encodeURIComponent(snapId));
    if (r && r.ok) addLog('Deleted snapshot ' + snapName);
    loadSnapshots();
}

async function pruneSnapshots() {
    if (!confirm('Delete all but the 10 most recent AUTO snapshots?\n\nManual snapshots are kept.')) return;
    var r = await apiPost('/api/snapshots/prune', { keep_last: 10 });
    addLog('Pruned ' + (r && r.deleted || 0) + ' auto-snapshots.');
    loadSnapshots();
}

// ─── v2.9.9.6 — game mode UI ────────────────────────────────────────
function _renderGameModeSection(settings, status) {
    var el = document.getElementById('settings-gamemode-rows');
    if (!el) return;
    var monitoring = !!status.monitoring;
    var activeGame = status.active_game ? (' — currently tracking: <b style="color:var(--accent)">' + escHtml(status.active_game) + '</b>') : '';
    var dbCount = status.known_game_count != null ? status.known_game_count : '?';
    el.innerHTML =
        _settingsToggle(
            'Auto-enable game mode at Vispora launch',
            'Starts the game-detection monitor automatically when Vispora boots so any game you launch afterwards gets the gaming profile applied.',
            !!settings.auto_enable_on_boot,
            "toggleGameModeAutoEnable(this)") +
        '<div style="font-size:10px;color:var(--text-dim);margin-top:8px">' +
            'Currently: <b style="color:' + (monitoring ? 'var(--accent)' : 'var(--text-dim)') + '">' +
            (monitoring ? '● MONITORING' : '○ stopped') + '</b>' + activeGame +
            ' · Database: ' + dbCount + ' games known' +
        '</div>' +
        '<div class="btn-group" style="margin-top:8px">' +
            (monitoring
                ? '<button class="btn btn-sm btn-danger" onclick="stopGameModeFromSettings()">Stop monitoring</button>'
                : '<button class="btn btn-sm btn-primary" onclick="startGameModeFromSettings()">Start monitoring now</button>') +
        '</div>';
}

async function toggleGameModeAutoEnable(el) {
    el.classList.toggle('on');
    var on = el.classList.contains('on');
    el.setAttribute('aria-checked', String(on));
    var r = await apiPost('/api/profiles/settings', { auto_enable_on_boot: on });
    if (!r || !r.ok) {
        el.classList.toggle('on');
        el.setAttribute('aria-checked', String(!on));
        addLog('Could not save game-mode setting');
    } else {
        addLog('Game-mode auto-enable: ' + (on ? 'ON' : 'OFF'));
    }
}

async function startGameModeFromSettings() {
    var r = await apiPost('/api/profiles/start');
    if (r && r.ok) addLog('Game mode started.');
    loadSettingsPage();
}

async function stopGameModeFromSettings() {
    var r = await apiPost('/api/profiles/stop');
    if (r && r.ok) addLog('Game mode stopped.');
    loadSettingsPage();
}

// ─── v2.9.6 — driver updater UI ─────────────────────────────────────
function _renderDriverSection(driver) {
    var infoEl   = document.getElementById('settings-driver-info');
    var togEl    = document.getElementById('settings-driver-toggles');
    var actsEl   = document.getElementById('settings-driver-actions');
    if (!infoEl || !togEl || !actsEl) return;

    var settings = driver.settings || {};
    var vendor   = (driver.vendor || 'unknown').toLowerCase();
    var infoHtml = '';

    if (vendor === 'nvidia' || vendor === 'amd') {
        var vendorLabel = vendor === 'nvidia' ? 'NVIDIA' : 'AMD';
        infoHtml += '<div><b style="color:var(--text)">GPU:</b> ' + escHtml(driver.model || '?') + '</div>';
        infoHtml += '<div><b style="color:var(--text)">Installed driver:</b> ' + escHtml(driver.current_version || '?') + '</div>';
        if (driver.latest_version) {
            var color = driver.update_available ? 'var(--orange)' : 'var(--accent)';
            infoHtml += '<div><b style="color:var(--text)">Latest available:</b> <span style="color:' + color + '">' +
                escHtml(driver.latest_version) + '</span>' +
                (driver.update_available ? ' — UPDATE AVAILABLE' : ' — up to date') + '</div>';
            if (driver.release_date) {
                infoHtml += '<div style="color:var(--text-dim)">Released: ' + escHtml(driver.release_date) + '</div>';
            }
        }
        if (driver.last_check_ts) {
            var d = new Date(driver.last_check_ts * 1000);
            infoHtml += '<div style="color:var(--text-dim);margin-top:4px">Last checked: ' +
                escHtml(d.toLocaleString()) + '</div>';
        }
        if (driver.error) {
            infoHtml += '<div style="color:var(--orange);margin-top:6px">' + escHtml(driver.error) + '</div>';
        }
    } else {
        infoHtml += '<div style="color:var(--orange)">No supported GPU detected. Driver auto-update only works for NVIDIA / AMD cards.</div>';
    }
    infoEl.innerHTML = infoHtml;

    // Toggles (only shown for supported vendors)
    if (vendor === 'nvidia' || vendor === 'amd') {
        togEl.innerHTML =
            _settingsToggle('Check for driver updates automatically',
                'On startup and every 12 hours.',
                !!settings.auto_check, "toggleDriverField(this, 'auto_check')") +
            _settingsToggle('Download installer in the background',
                'Pull the .exe as soon as a new version is detected (drivers can be ~800 MB).',
                !!settings.auto_download, "toggleDriverField(this, 'auto_download')");
    } else {
        togEl.innerHTML = '';
    }

    // Action buttons
    var actionsHtml = '<button class="btn btn-sm" onclick="checkDriverUpdate()">Check now</button>';
    if (driver.update_available && vendor === 'nvidia') {
        if (driver.downloaded_path) {
            actionsHtml += '<button class="btn btn-sm btn-primary" onclick="installDriver()">Install driver v' +
                escHtml(driver.latest_version) + '</button>';
        } else {
            actionsHtml += '<button class="btn btn-sm btn-primary" onclick="downloadDriver()">Download driver v' +
                escHtml(driver.latest_version) + ' (' + escHtml(driver.size_mb || '?') + ' MB)</button>';
        }
    }
    if (driver.manual_link) {
        actionsHtml += '<button class="btn btn-sm" onclick="openExternal(\'' + escAttr(driver.manual_link) +
            '\')">Open vendor page</button>';
    }
    if (driver.release_url && !driver.manual_link) {
        actionsHtml += '<button class="btn btn-sm" onclick="openExternal(\'' + escAttr(driver.release_url) +
            '\')">Release notes</button>';
    }
    actsEl.innerHTML = actionsHtml;
}

function openExternal(url) {
    // Opens in the user's default browser
    try { window.open(url, '_blank'); } catch (e) {}
}

async function toggleDriverField(el, key) {
    el.classList.toggle('on');
    var on = el.classList.contains('on');
    el.setAttribute('aria-checked', String(on));
    var payload = {}; payload[key] = on;
    var r = await apiPost('/api/driver/settings', payload);
    if (!r || !r.ok) {
        el.classList.toggle('on');
        el.setAttribute('aria-checked', String(!on));
    }
}

async function checkDriverUpdate() {
    addLog('Checking for GPU driver updates...');
    var r = await apiPost('/api/driver/check');
    if (!r) {
        addLog('Driver check failed (no response).');
        return;
    }
    if (r.update_available) {
        addLog('Driver update available: ' + r.current_version + ' -> ' + r.latest_version);
        showDriverUpdateToast(r);
    } else if (r.current_version) {
        addLog('Driver up to date: v' + r.current_version);
    } else if (r.error) {
        addLog('Driver check: ' + r.error);
    }
    loadSettingsPage();
}

function showDriverUpdateToast(driver) {
    var toast = document.getElementById('driver-update-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'driver-update-toast';
        toast.className = 'update-toast';
        document.body.appendChild(toast);
    }
    toast.innerHTML =
        '<div class="update-toast-msg"><b>NVIDIA driver</b> v' + escHtml(driver.latest_version) +
        ' available (you have v' + escHtml(driver.current_version) + ')</div>' +
        '<div class="update-toast-actions">' +
        '<button class="btn btn-sm btn-primary" onclick="downloadDriverFromToast()">Download</button>' +
        '<button class="btn btn-sm" onclick="document.getElementById(\'driver-update-toast\').classList.remove(\'show\')">Later</button>' +
        '</div>';
    toast.classList.add('show');
}

async function downloadDriverFromToast() {
    document.getElementById('driver-update-toast').classList.remove('show');
    return downloadDriver();
}

async function downloadDriver() {
    addLog('Downloading driver installer (this can take a few minutes)...');
    var r = await apiPost('/api/driver/download', {}, { timeoutMs: 900000 });  // 15 min
    if (!r || !r.ok) {
        addLog('Download failed: ' + ((r && r.err) || 'unknown'));
        return;
    }
    addLog('Driver downloaded (' + (r.size_mb || '?') + ' MB) — ready to install.');
    loadSettingsPage();
}

async function installDriver() {
    if (!confirm('Launch the NVIDIA driver installer?\n\nThis will open NVIDIA\'s setup window.\nYou\'ll be asked to choose Express vs Custom installation.\n\nA reboot may be required after install.')) return;
    addLog('Launching driver installer...');
    var r = await apiPost('/api/driver/install', {});
    if (r && r.ok) {
        addLog('Installer launched. Follow the NVIDIA setup prompts — Vispora will confirm once the new driver is active.');
    } else {
        addLog('Install failed: ' + ((r && r.err) || 'unknown'));
    }
}

// ─── Toggle Switch ───
function toggleSwitch(el) {
    el.classList.toggle('on');
}

// ─── Escape HTML ───
function escHtml(s) {
    if (typeof s !== 'string') s = String(s || '');
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escAttr(s) {
    return String(s || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ─── Terminal Log Helper ───
function termWrite(terminalId, msg, level) {
    var term = document.getElementById(terminalId);
    if (!term) return;
    var ts = new Date().toLocaleTimeString('en-US', { hour12: false });
    var cls = level === 'error' ? ' error' : level === 'warning' ? ' warning' : '';
    term.innerHTML += '<div class="terminal-line"><span class="terminal-ts">' + ts + '</span><span class="terminal-msg' + cls + '">' + escHtml(msg) + '</span></div>';
    term.scrollTop = term.scrollHeight;
}

function termLog(page, data) {
    var tid = page + '-terminal';
    if (data.results) {
        data.results.forEach(function(r) {
            termWrite(tid, (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || r.task || r.id || ''));
        });
    } else if (data.ok !== undefined) {
        termWrite(tid, data.ok ? '✓ Operation completed' : '✗ ' + (data.err || 'Failed'), data.ok ? '' : 'error');
    } else {
        termWrite(tid, JSON.stringify(data).substring(0, 200));
    }
}

function addLog(msg) {
    termWrite('dash-terminal', msg);
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
    return (bytes / 1073741824).toFixed(2) + ' GB';
}

// ═══════════════════════════════════════════════════════════════
// DASHBOARD
// ═══════════════════════════════════════════════════════════════
// ═══ System Health Audit (Dashboard card) ═══
var _SEV_GLYPH = { ok: '✓', warn: '!', crit: '✕' };

function _renderHealthRow(c) {
    var sev = c.severity || 'ok';
    var fix = '';
    if (c.fix) {
        if (c.fix.kind === 'navigate') {
            fix = '<button class="btn btn-sm" onclick="switchPage(\'' + escHtml(c.fix.page) + '\')" '
                + 'style="margin-left:8px;flex-shrink:0">Open ' + escHtml(c.fix.page) + '</button>';
        } else if (c.fix.kind === 'route' && c.fix.method === 'POST') {
            var bodyJson = JSON.stringify(c.fix.body || {}).replace(/"/g, '&quot;');
            fix = '<button class="btn btn-sm btn-primary" '
                + 'onclick="_healthFix(\'' + escHtml(c.fix.url) + '\', \'' + bodyJson + '\', this)" '
                + 'style="margin-left:8px;flex-shrink:0">Fix it</button>';
        }
    }
    var expected = (c.expected != null && c.expected !== '')
        ? '<span class="expected">target: ' + escHtml(c.expected) + '</span>'
        : '';
    return ''
        + '<div class="health-row" data-id="' + escHtml(c.id) + '">'
        +   '<div class="health-row-icon ' + sev + '">' + (_SEV_GLYPH[sev] || '·') + '</div>'
        +   '<div class="health-row-info">'
        +     '<div class="health-row-name">' + escHtml(c.name) + '</div>'
        +     '<div class="health-row-detail">' + escHtml(c.detail || '') + '</div>'
        +   '</div>'
        +   '<div class="health-row-current">' + escHtml(c.current || '—') + expected + '</div>'
        +   fix
        + '</div>';
}

async function _healthFix(url, bodyJson, btn) {
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    try {
        var body = JSON.parse(bodyJson.replace(/&quot;/g, '"'));
        var r = await apiPost(url, body);
        addLog('Health fix → ' + url + ' ok=' + (r && r.ok));
    } catch (e) {
        addLog('Health fix failed: ' + e);
    }
    setTimeout(loadHealthAudit, 800);
}

async function loadHealthAudit() {
    var data = await apiGet('/api/health/audit');
    var checksEl  = document.getElementById('health-checks');
    var summaryEl = document.getElementById('health-summary');
    var pillEl    = document.getElementById('health-overall-pill');
    if (!checksEl || !summaryEl || !pillEl) return;

    if (!data || !data.checks) {
        summaryEl.textContent = 'Audit failed.';
        pillEl.className = 'status-badge danger';
        pillEl.textContent = 'error';
        checksEl.innerHTML = '<div class="empty-state">Could not run health audit.</div>';
        return;
    }
    var c = data.counts || {};
    summaryEl.textContent = (c.ok || 0) + ' passing · '
                          + (c.warn || 0) + ' could be better · '
                          + (c.crit || 0) + ' need attention';
    var overall = data.overall || 'ok';
    pillEl.className = 'status-badge ' +
        (overall === 'ok'   ? 'ok' :
         overall === 'warn' ? 'warn' : 'danger');
    pillEl.textContent = overall === 'ok' ? 'all good'
                       : overall === 'warn' ? 'minor issues'
                       : 'attention needed';

    if (data.checks.length === 0) {
        checksEl.innerHTML = '<div class="empty-state">No checks ran.</div>';
        return;
    }
    // Sort: crit > warn > ok, then by category
    var sevOrder = { crit: 0, warn: 1, ok: 2 };
    data.checks.sort(function(a, b) {
        var s = (sevOrder[a.severity] || 9) - (sevOrder[b.severity] || 9);
        if (s !== 0) return s;
        return (a.category || '').localeCompare(b.category || '');
    });
    checksEl.innerHTML = data.checks.map(_renderHealthRow).join('');
}

async function loadDashboard() {
    // Kick off the audit in the background so it doesn't block other widgets
    loadHealthAudit();
    loadTournament();

    var data = await apiGet('/api/dashboard');
    var grid = document.getElementById('dash-stats');
    if (!data.os) {
        grid.innerHTML = '<div class="stat-card"><div class="stat-value" style="color:var(--red)">Failed to load system info</div></div>';
        return;
    }

    var cpu = data.cpu || {};
    var gpu = data.gpu || {};
    var ram = data.ram || {};
    var storage = data.storage || {};
    var os = data.os || {};
    var drives = storage.drives || [];
    var sysDrive = drives[0] || {};

    var ramPct = ram.used_pct || 0;
    var diskPct = sysDrive.used_pct || 0;
    var ramBarCls = ramPct > 85 ? 'crit' : ramPct > 70 ? 'warn' : '';
    var diskBarCls = diskPct > 85 ? 'crit' : diskPct > 70 ? 'warn' : '';

    grid.innerHTML =
        '<div class="stat-card"><div class="stat-label">Operating System</div><div class="stat-value">' + escHtml(os.name || 'Windows') + '</div><div class="stat-sub">Build ' + escHtml(os.build || '') + ' • ' + escHtml(os.arch || '') + '</div></div>' +
        '<div class="stat-card"><div class="stat-label">CPU</div><div class="stat-value">' + escHtml(cpu.name || 'Unknown') + '</div><div class="stat-sub">' + (cpu.cores || 0) + 'C / ' + (cpu.threads || 0) + 'T • ' + (cpu.max_clock || 0) + ' MHz</div></div>' +
        '<div class="stat-card"><div class="stat-label">GPU</div><div class="stat-value">' + escHtml(gpu.name || 'Unknown') + '</div><div class="stat-sub">Driver: ' + escHtml(gpu.driver || 'N/A') + ' • ' + (gpu.vram_gb || 0) + ' GB VRAM</div></div>' +
        '<div class="stat-card"><div class="stat-label">Memory</div><div class="stat-value">' + (ram.total_gb || 0) + ' GB</div><div class="stat-sub">' + (ram.speed_mhz || 0) + ' MHz • ' + (ram.sticks || 0) + ' sticks • ' + ramPct + '% used</div><div class="stat-bar"><div class="stat-bar-fill ' + ramBarCls + '" style="width:' + ramPct + '%"></div></div></div>' +
        '<div class="stat-card"><div class="stat-label">System Drive (' + escHtml(sysDrive.letter || 'C:') + ')</div><div class="stat-value">' + (sysDrive.free_gb || 0) + ' GB free</div><div class="stat-sub">' + (sysDrive.total_gb || 0) + ' GB total • ' + (storage.type || 'unknown') + ' • ' + diskPct + '% used</div><div class="stat-bar"><div class="stat-bar-fill ' + diskBarCls + '" style="width:' + diskPct + '%"></div></div></div>' +
        '<div class="stat-card"><div class="stat-label">Admin Status</div><div class="stat-value">' + (data.admin ? '<span style="color:var(--accent)">✓ Administrator</span>' : '<span style="color:var(--red)">✗ Not Admin</span>') + '</div><div class="stat-sub">' + (data.admin ? 'Full system access' : 'Some features unavailable') + '</div></div>';

    if (data.module_status) {
        Object.keys(data.module_status).forEach(function(mod) {
            var b = document.getElementById('badge-' + mod);
            if (b) {
                if (data.module_status[mod]) b.classList.add('done');
                else b.classList.remove('done');
            }
        });
    }
}

// ═══════════════════════════════════════════════════════════════
// DEBLOAT
// ═══════════════════════════════════════════════════════════════
var debloatApps = [];
var debloatServices = [];

async function loadDebloatScan() {
    var container = document.getElementById('debloat-apps');
    var svcContainer = document.getElementById('debloat-services');
    container.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Scanning installed apps...</span>';
    svcContainer.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Scanning services...</span>';

    var data = await apiGet('/api/debloat/scan');
    debloatApps = data.apps || [];
    debloatServices = data.services || [];

    var appsHtml = '';
    debloatApps.forEach(function(a, i) {
        appsHtml += '<div class="check-item"><div class="check-box ' + (a.installed ? 'checked' : '') + '" data-type="app" data-index="' + i + '" onclick="toggleCheck(this)"></div><span>' + escHtml(a.name) + '</span><span class="check-status ' + (a.installed ? 'installed' : 'removed') + '">' + (a.installed ? 'installed' : 'not found') + '</span><span class="check-cat">' + escHtml(a.cat) + '</span></div>';
    });
    container.innerHTML = appsHtml;

    var svcsHtml = '';
    debloatServices.forEach(function(s, i) {
        svcsHtml += '<div class="check-item"><div class="check-box ' + (!s.skip ? 'checked' : '') + '" data-type="svc" data-index="' + i + '" onclick="toggleCheck(this)"></div><span>' + escHtml(s.name) + '</span><span class="check-status" style="color:var(--text-dim)">' + escHtml(s.current_start) + '</span><span class="check-cat">' + escHtml(s.cat) + '</span></div>';
    });
    svcContainer.innerHTML = svcsHtml;
}

function toggleCheck(el) { el.classList.toggle('checked'); }

function debloatSelectAll(val) {
    document.querySelectorAll('#debloat-apps .check-box, #debloat-services .check-box').forEach(function(cb) {
        if (val) cb.classList.add('checked');
        else cb.classList.remove('checked');
    });
}

// 3.4.2 — undo a service-disable pass.  Re-enables every Windows
// service the debloater turned off, restoring each to its recorded
// original start type (or Manual as a safe fallback).
async function restoreDebloatServices() {
    if (!confirm('Re-enable all Windows services that Vispora disabled?\n\n'
                 + 'Each service is restored to its original start type '
                 + '(the state it was in before you debloated).')) return;
    var btn = document.getElementById('btn-restore-svc');
    if (btn) { btn.disabled = true; btn.textContent = 'Restoring…'; }
    termWrite('debloat-terminal', 'Restoring disabled services…');
    try {
        var r = await apiPost('/api/debloat/restore-services');
        if (r && r.ok) {
            termWrite('debloat-terminal', '✓ Restored ' + (r.restored || 0) + '/' + (r.total || 0) + ' services');
            showInfoToast('Re-enabled ' + (r.restored || 0) + ' of ' + (r.total || 0)
                          + ' disabled service(s).', { title: 'Services restored' });
            loadDebloatScan();
        } else {
            showErrorToast('Service restore failed: ' + ((r && r.err) || 'unknown'));
        }
    } catch (e) {
        showErrorToast('Service restore failed: ' + (e && e.message || e));
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Restore services'; }
    }
}

async function runDebloat() {
    var appIds = [];
    document.querySelectorAll('#debloat-apps .check-box.checked').forEach(function(cb) {
        var idx = parseInt(cb.dataset.index);
        if (debloatApps[idx]) appIds.push(debloatApps[idx].id);
    });
    var svcIds = [];
    document.querySelectorAll('#debloat-services .check-box.checked').forEach(function(cb) {
        var idx = parseInt(cb.dataset.index);
        if (debloatServices[idx]) svcIds.push(debloatServices[idx].id);
    });
    var doFeatures = document.getElementById('toggle-features').classList.contains('on');
    var doTasks = document.getElementById('toggle-tasks').classList.contains('on');
    var doOnedrive = document.getElementById('toggle-onedrive').classList.contains('on');

    var term = document.getElementById('debloat-terminal');
    term.innerHTML = '';
    document.getElementById('debloat-progress-wrap').style.display = 'block';
    setProgress('debloat', 10, 'Starting debloat...');
    termWrite('debloat-terminal', 'Removing ' + appIds.length + ' apps, ' + svcIds.length + ' services...');

    var result = await apiPost('/api/debloat/run', {
        apps: appIds.length > 0 ? appIds : null,
        services: svcIds.length > 0 ? svcIds : null,
        tasks: doTasks,
        features: doFeatures,
        onedrive: doOnedrive,
    });

    setProgress('debloat', 100, 'Complete');

    if (result.apps) {
        var ok = result.apps.filter(function(r) { return r.ok; }).length;
        termWrite('debloat-terminal', 'Apps: ' + ok + '/' + result.apps.length + ' removed');
    }
    if (result.services) {
        var ok2 = result.services.filter(function(r) { return r.ok; }).length;
        termWrite('debloat-terminal', 'Services: ' + ok2 + '/' + result.services.length + ' disabled');
    }
    if (result.tasks) {
        var ok3 = result.tasks.filter(function(r) { return r.ok; }).length;
        termWrite('debloat-terminal', 'Tasks: ' + ok3 + '/' + result.tasks.length + ' disabled');
    }
    if (result.features) {
        var ok4 = result.features.filter(function(r) { return r.ok; }).length;
        termWrite('debloat-terminal', 'Features: ' + ok4 + '/' + result.features.length + ' disabled');
    }
    if (result.onedrive) {
        termWrite('debloat-terminal', 'OneDrive: ' + (result.onedrive.ok ? 'removed' : 'failed'));
    }
    termWrite('debloat-terminal', '═══ Debloat complete ═══');
    updateBadge('debloat', true);
    loadDebloatScan();
}

function setProgress(mod, pct, label) {
    var fill = document.getElementById(mod + '-progress');
    var lbl = document.getElementById(mod + '-progress-label');
    if (fill) fill.style.width = pct + '%';
    if (lbl) lbl.textContent = label || (pct + '%');
}

// ═══════════════════════════════════════════════════════════════
// OPTIMIZE — Pro Mode toggle (v3.3.1-beta.8+)
// ═══════════════════════════════════════════════════════════════
async function loadProModeStatus() {
    try {
        var r = await apiGet('/api/optimize/pro-mode');
        var tog = document.getElementById('toggle-pro-mode');
        var box = document.getElementById('pro-mode-status');
        var on  = !!(r && r.enabled);
        if (tog) {
            if (on) tog.classList.add('on'); else tog.classList.remove('on');
        }
        if (box) {
            if (on) {
                box.style.color = 'var(--warning, #e0b850)';
                box.innerHTML = '<span style="color:var(--warning, #e0b850);font-weight:600">⚠ Pro Mode is ON</span> — Apply All will include HVCI / Spectre / HPET / MPO / USB MSI / TDR / Search / MemComp / hibernation tweaks.';
            } else {
                box.style.color = 'var(--text-tertiary)';
                box.innerHTML = 'Pro Mode is OFF — Apply All will skip risky tweaks (safe default).';
            }
        }
    } catch (e) {}
}

async function toggleProMode(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    var r = await apiPost('/api/optimize/pro-mode', { enabled: newOn });
    if (r && r.ok) {
        addLog('Optimizer Pro Mode ' + (newOn ? 'enabled' : 'disabled'));
    } else {
        addLog('Pro Mode toggle failed: ' + ((r && r.err) || 'unknown'));
        el.classList.toggle('on');
    }
    loadProModeStatus();
}

// ═══════════════════════════════════════════════════════════════
// OPTIMIZE
// ═══════════════════════════════════════════════════════════════
// ─── Competitive Latency Mode (beta.3) ───
async function loadCompetitiveStatus() {
    var st = document.getElementById('competitive-status');
    var btn = document.getElementById('competitive-btn');
    if (!st || !btn) return;
    try {
        var s = await apiGet('/api/competitive/status') || {};
        var bits = [];
        bits.push(s.enabled ? '<b style="color:var(--accent)">ON</b>' : 'off');
        if (s.recommended_fps_cap) {
            bits.push('set in-game FPS cap to <b>' + s.recommended_fps_cap + '</b>' +
                      (s.refresh_hz ? ' (your display: ' + s.refresh_hz + ' Hz)' : ''));
        }
        if (s.flip_model_enabled) bits.push('flip-model on');
        if (s.timer_resolution_ms != null) bits.push('timer ' + s.timer_resolution_ms + ' ms');
        st.innerHTML = 'Status: ' + bits.join(' · ');
        btn.textContent = s.enabled ? 'Disable' : 'Enable';
        btn.classList.toggle('btn-primary', !s.enabled);
    } catch (e) { st.textContent = 'Status unavailable'; }
}

async function toggleCompetitive() {
    var btn = document.getElementById('competitive-btn');
    var s = await apiGet('/api/competitive/status').catch(function(){ return {}; });
    var enabling = !(s && s.enabled);
    if (btn) { btn.disabled = true; btn.textContent = enabling ? 'Enabling…' : 'Disabling…'; }
    var r = await apiPost(enabling ? '/api/competitive/apply' : '/api/competitive/reset', {});
    if (btn) btn.disabled = false;
    if (r && r.ok) {
        showInfoToast(enabling
            ? 'Competitive Latency on. (HID queue + GlobalTimerResolution apply fully after a reboot.)'
            : 'Competitive Latency reverted to Windows defaults.',
            { title: 'Competitive Latency' });
    } else {
        showErrorToast('Could not change Competitive Latency: ' + ((r && r.err) || 'unknown'));
    }
    loadCompetitiveStatus();
}

async function runOptimize(categories) {
    var term = document.getElementById('optimize-terminal');
    term.innerHTML = '';
    termWrite('optimize-terminal', 'Starting optimization...');

    var payload = categories ? { categories: categories } : {};
    var result = await apiPost('/api/optimize/run', payload);

    Object.keys(result).forEach(function(cat) {
        var val = result[cat];
        if (Array.isArray(val)) {
            termWrite('optimize-terminal', '-- ' + cat.toUpperCase() + ' --');
            val.forEach(function(r) {
                termWrite('optimize-terminal', '  ' + (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || ''));
            });
        } else if (val && typeof val === 'object') {
            termWrite('optimize-terminal', '-- ' + cat.toUpperCase() + ': ' + (val.ok !== false ? '✓' : '✗') + ' --');
        }
    });
    termWrite('optimize-terminal', '=== Optimization complete ===');
    updateBadge('optimize', true);
}

async function resetOptimizations() {
    if (!confirm('Reset ALL optimizations back to Windows defaults?\n\nThis will:\n- Switch to Balanced power plan\n- Restore default CPU scheduling\n- Re-enable visual effects & transparency\n- Re-enable memory compression\n- Restore Game DVR defaults\n- Remove timer tweaks\n- Re-enable Windows Search\n\nContinue?')) return;

    var term = document.getElementById('optimize-terminal');
    term.innerHTML = '';
    termWrite('optimize-terminal', 'Resetting all optimizations to Windows defaults...');

    var result = await apiPost('/api/optimize/reset');

    Object.keys(result).forEach(function(cat) {
        var val = result[cat];
        if (Array.isArray(val)) {
            termWrite('optimize-terminal', '-- RESET ' + cat.toUpperCase() + ' --');
            val.forEach(function(r) {
                termWrite('optimize-terminal', '  ' + (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || ''));
            });
        }
    });
    termWrite('optimize-terminal', '=== All optimizations reset to defaults ===');
    termWrite('optimize-terminal', 'A reboot is recommended for all changes to take effect.');
    updateBadge('optimize', false);
}

async function runHardTweaks() {
    if (!confirm('HARD TWEAKS\n\nThis applies aggressive changes:\n• Spectre/Meltdown mitigations OFF\n• VBS / Memory Integrity OFF\n• Recall disabled\n• Auto HDR disabled\n• Background apps killed\n• Widgets + Copilot removed\n• Mouse acceleration OFF\n• Xbox services to manual\n• Win10-style context menu restored\n\nAll reversible via "Reset All to Default".\nContinue?')) return;
    var term = document.getElementById('optimize-terminal');
    term.innerHTML = '';
    termWrite('optimize-terminal', 'Applying HARD tweaks (aggressive)...');
    var result = await apiPost('/api/optimize/hard');
    (result.results || []).forEach(function(r) {
        termWrite('optimize-terminal', '  ' + (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || ''));
    });
    termWrite('optimize-terminal', '=== Hard tweaks applied (reboot recommended) ===');
    updateBadge('optimize', true);
}

async function runLatencyTweaks() {
    if (!confirm('LATENCY TWEAKS\n\nTargets DPC/ISR latency:\n• HPET disabled\n• Dynamic tick OFF\n• Platform tick ON\n• MSI mode for ALL network + USB devices\n• Power throttling globally OFF\n• DPC watchdog profile OFF\n\nReboot required after. Continue?')) return;
    var term = document.getElementById('optimize-terminal');
    term.innerHTML = '';
    termWrite('optimize-terminal', 'Applying LATENCY tweaks...');
    var result = await apiPost('/api/optimize/latency');
    (result.results || []).forEach(function(r) {
        termWrite('optimize-terminal', '  ' + (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || ''));
    });
    termWrite('optimize-terminal', '=== Latency tweaks applied — reboot now ===');
}

async function runWin11Tweaks() {
    if (!confirm('WINDOWS 11 DECLUTTER\n\nThis tames the Win11 UX:\n• Taskbar left-aligned\n• Chat + Task View hidden\n• Start recommendations off\n• File Explorer ads off\n• News & Interests off\n• Clipboard history + cloud sync off\n• Web search in Start disabled\n\nContinue?')) return;
    var term = document.getElementById('optimize-terminal');
    term.innerHTML = '';
    termWrite('optimize-terminal', 'Applying Windows 11 declutter...');
    var result = await apiPost('/api/optimize/win11');
    (result.results || []).forEach(function(r) {
        termWrite('optimize-terminal', '  ' + (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || ''));
    });
    termWrite('optimize-terminal', '=== Windows 11 declutter applied — sign out to see changes ===');
}

// ═══ v3 Optimize tab — category-toggle grid + Apply Selected ═══
function _optUpdateCount() {
    var sel = document.querySelectorAll('.opt-cat.selected').length;
    var el = document.getElementById('opt-selected-count');
    if (el) el.textContent = sel;
}
function optSelectAll(yes) {
    document.querySelectorAll('.opt-cat').forEach(function(el) {
        if (yes) el.classList.add('selected');
        else el.classList.remove('selected');
    });
    _optUpdateCount();
}
async function optApplySelected() {
    var cats = Array.from(document.querySelectorAll('.opt-cat.selected'))
        .map(function(el) { return el.getAttribute('data-cat'); });
    if (cats.length === 0) {
        showWarnToast('Select at least one category first.');
        return;
    }
    // Confirm if any aggressive category is in the selection
    var aggressive = Array.from(document.querySelectorAll('.opt-cat.selected[data-group="aggressive"]'))
        .map(function(el) { return el.querySelector('.opt-cat-title').firstChild.textContent.trim(); });
    if (aggressive.length > 0) {
        if (!confirm('Selected categories include AGGRESSIVE tweaks:\n\n• ' +
                     aggressive.join('\n• ') +
                     '\n\nThese have known trade-offs (security mitigations, ' +
                     'optional Windows features, services). Continue?')) {
            return;
        }
    }
    var term = document.getElementById('optimize-terminal');
    term.innerHTML = '';
    termWrite('optimize-terminal', 'Applying ' + cats.length + ' selected categories: ' + cats.join(', '));
    // The /api/optimize/run endpoint accepts the categories list
    var result = await apiPost('/api/optimize/run', { categories: cats });
    Object.keys(result.results || {}).forEach(function(cat) {
        termWrite('optimize-terminal', '── ' + cat.toUpperCase() + ' ──');
        var entries = result.results[cat];
        if (!Array.isArray(entries)) entries = [entries];
        entries.forEach(function(r) {
            termWrite('optimize-terminal', '  ' + (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || ''));
        });
    });
    termWrite('optimize-terminal', '=== Selected tweaks applied ===');
}
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.opt-cat').forEach(function(el) {
        el.addEventListener('click', function() {
            el.classList.toggle('selected');
            _optUpdateCount();
        });
    });
});

async function scanStartup() {
    var container = document.getElementById('startup-list');
    container.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Scanning...</span>';
    var data = await apiGet('/api/optimize/startup');
    var items = data.items || [];
    if (items.length === 0) {
        container.innerHTML = '<span style="color:var(--text-dim)">No startup items found</span>';
        return;
    }
    var html = '';
    items.forEach(function(item) {
        html += '<div class="check-item"><span style="flex:1">' + escHtml(item.name) + '</span><span style="font-size:10px;color:var(--text-dim);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escHtml(item.command) + '</span><button class="btn btn-sm btn-danger" onclick="disableStartup(\'' + escAttr(item.name) + '\',\'' + escAttr(item.location) + '\',this)">Disable</button></div>';
    });
    container.innerHTML = html;
}

async function disableStartup(name, location, btn) {
    btn.disabled = true;
    btn.textContent = '...';
    await apiPost('/api/optimize/disable-startup', { name: name, location: location });
    btn.textContent = 'Done';
    btn.style.borderColor = 'var(--accent)';
    btn.style.color = 'var(--accent)';
}

// ═══════════════════════════════════════════════════════════════
// NETWORK
// ═══════════════════════════════════════════════════════════════
// Bandwidth Monitor (v3.1) — 1 Hz live network usage on Network tab
// ═══════════════════════════════════════════════════════════════
var _netmonTimer = null;
var _netmonRxPeak = 0;
var _netmonTxPeak = 0;
var _netmonLastBurstPid = null;

function startNetMonPoll() {
    if (_netmonTimer) return;
    _renderNetMon();             // immediate first render
    // v3.2.3 — was 1 s; bumped to 2 s + visibility-gated.  Still feels live.
    _netmonTimer = setInterval(function() { if (_pollSkipIfHidden()) return; _renderNetMon(); }, 2000);
}

function stopNetMonPoll() {
    if (_netmonTimer) {
        clearInterval(_netmonTimer);
        _netmonTimer = null;
    }
}

async function _renderNetMon() {
    var live = await apiGet('/api/network/live');
    if (!live) return;

    // ── Top stats ────────────────────────────────────────────────
    var rx = live.total_rx_per_sec || 0;
    var tx = live.total_tx_per_sec || 0;
    if (rx > _netmonRxPeak) _netmonRxPeak = rx;
    if (tx > _netmonTxPeak) _netmonTxPeak = tx;

    var rxEl = document.getElementById('netmon-rx');
    var txEl = document.getElementById('netmon-tx');
    var rxPeakEl = document.getElementById('netmon-rx-peak');
    var txPeakEl = document.getElementById('netmon-tx-peak');
    var gameEl = document.getElementById('netmon-game');
    var samplerEl = document.getElementById('netmon-sampler');
    if (rxEl) rxEl.textContent = _fmtBps(rx);
    if (txEl) txEl.textContent = _fmtBps(tx);
    if (rxPeakEl) rxPeakEl.textContent = 'peak ' + _fmtBps(_netmonRxPeak);
    if (txPeakEl) txPeakEl.textContent = 'peak ' + _fmtBps(_netmonTxPeak);
    if (gameEl) gameEl.textContent = live.active_game || 'none';
    if (samplerEl) samplerEl.textContent = live.running ? 'sampler running' : 'sampler stopped';

    // ── Burst alert ──────────────────────────────────────────────
    var burstEl = document.getElementById('netmon-burst');
    var burstText = document.getElementById('netmon-burst-text');
    var burstBtn = document.getElementById('netmon-burst-pause');
    if (live.burst_alert) {
        var b = live.burst_alert;
        if (burstEl) burstEl.style.display = 'block';
        if (burstText) {
            burstText.innerHTML = '<b>' + escHtml(b.name) + '</b> (PID ' + b.pid +
                ') is using <b>' + b.mb_per_sec + ' MB/s</b> for ' + b.duration_sec + 's' +
                (b.active_game ? ' while <b>' + escHtml(b.active_game) + '</b> is running.' : '.');
        }
        if (burstBtn) {
            burstBtn.onclick = (function(pid, name){
                return async function(){
                    if (!confirm('Suspend ' + name + ' (PID ' + pid + ')?\n\n' +
                                 'You can resume from the Background Pauser card on the Game Profiles tab.')) return;
                    var r = await apiPost('/api/network/pause-process', { pid: pid });
                    if (r && r.ok) {
                        burstBtn.textContent = 'Suspended ✓';
                        burstBtn.disabled = true;
                    } else {
                        showErrorToast('Pause failed: ' + (r && r.err ? r.err : 'unknown'));
                    }
                };
            })(b.pid, b.name);
        }
        _netmonLastBurstPid = b.pid;
    } else {
        if (burstEl) burstEl.style.display = 'none';
        _netmonLastBurstPid = null;
    }

    // ── Top processes list ───────────────────────────────────────
    var topEl = document.getElementById('netmon-top');
    if (topEl) {
        var rows = (live.top || []).slice(0, 10);
        if (!rows.length) {
            topEl.innerHTML = '<div style="color:var(--text-tertiary);padding:8px 0;font-style:italic">No process traffic right now.</div>';
        } else {
            // Find the max for bar scaling
            var maxBps = Math.max.apply(null, rows.map(function(r){ return r.bytes_per_sec; })) || 1;
            topEl.innerHTML = rows.map(function(r){
                var pct = Math.min(100, Math.round(r.bytes_per_sec / maxBps * 100));
                var nameColor = r.is_game ? 'var(--accent)' : 'var(--text-bright)';
                var nameLabel = escHtml(r.name) + (r.is_game ? ' <span style="color:var(--accent);font-size:10px;text-transform:uppercase;letter-spacing:0.04em;margin-left:6px">game</span>' : '');
                return '<div style="display:flex;align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid var(--border-faint)">' +
                    '<div style="flex:1;min-width:0">' +
                        '<div style="font-size:13px;color:' + nameColor + ';font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + nameLabel + '</div>' +
                        '<div style="height:4px;background:var(--bg-overlay);border-radius:2px;margin-top:4px;overflow:hidden">' +
                            '<div style="height:100%;background:' + (r.is_game ? 'var(--accent)' : 'var(--text-secondary)') + ';width:' + pct + '%;transition:width 0.5s"></div>' +
                        '</div>' +
                    '</div>' +
                    '<div style="text-align:right;flex-shrink:0">' +
                        '<div style="font-size:13px;font-weight:600;color:var(--text-bright);font-variant-numeric:tabular-nums">' + _fmtBps(r.bytes_per_sec) + '</div>' +
                        '<div style="font-size:10px;color:var(--text-tertiary);font-variant-numeric:tabular-nums">PID ' + r.pid + '</div>' +
                    '</div>' +
                '</div>';
            }).join('');
        }
    }

    // ── Sparkline (60s history) ──────────────────────────────────
    _renderNetSparkline();
}

async function _renderNetSparkline() {
    var canvas = document.getElementById('netmon-spark');
    if (!canvas) return;
    var hist = await apiGet('/api/network/history?seconds=60');
    var rows = (hist && hist.history) || [];
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (!rows.length) return;
    // Compute max for y-scale
    var max = 0;
    rows.forEach(function(r){
        if (r.rx > max) max = r.rx;
        if (r.tx > max) max = r.tx;
    });
    if (max < 1024) max = 1024;     // floor at 1 KB so flat zero looks reasonable
    var scaleEl = document.getElementById('netmon-spark-scale');
    if (scaleEl) scaleEl.textContent = 'max ' + _fmtBps(max);

    // Pull theme colors so sparkline matches
    var rxColor = '#22d3ee';   // cyan
    var txColor = '#fbbf24';   // amber

    function drawSeries(getter, color) {
        ctx.beginPath();
        ctx.lineWidth = 2;
        ctx.strokeStyle = color;
        ctx.lineJoin = 'round';
        rows.forEach(function(r, i){
            var x = (i / (rows.length - 1 || 1)) * W;
            var y = H - (getter(r) / max) * (H - 4) - 2;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();
    }
    drawSeries(function(r){ return r.rx; }, rxColor);
    drawSeries(function(r){ return r.tx; }, txColor);
}

function _fmtBps(b) {
    if (b == null) return '—';
    if (b < 1024) return b + ' B/s';
    if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB/s';
    return (b / (1024 * 1024)).toFixed(2) + ' MB/s';
}

// ═══════════════════════════════════════════════════════════════
// Map adapter kind → icon + label for the network UI
var ADAPTER_KIND_META = {
    ethernet:  {icon: '🖧',  label: 'Ethernet'},
    wifi:      {icon: '📶',  label: 'Wi-Fi'},
    bluetooth: {icon: '🔵',  label: 'Bluetooth'},
    cellular:  {icon: '📱',  label: 'Cellular'},
    other:     {icon: '🔌',  label: 'Network'},
};

async function loadNetworkInfo() {
    var data = await apiGet('/api/network/info');
    var dnsEl = document.getElementById('current-dns');

    // Show every adapter Windows recognizes, with type + status.
    // Active adapters are listed first with their current DNS settings;
    // disconnected ones (e.g. WiFi when on Ethernet) appear below in
    // muted styling so users can see they ARE supported.
    var allAdapters = data.all_adapters || data.adapters || [];
    var dnsByIndex = {};
    (data.dns || []).forEach(function(d){ dnsByIndex[d.index] = d; });

    if (!allAdapters.length) {
        dnsEl.textContent = 'No network adapters detected';
    } else {
        var rows = allAdapters.map(function(a){
            var meta = ADAPTER_KIND_META[a.kind] || ADAPTER_KIND_META.other;
            var dns = dnsByIndex[a.index];
            var isUp = (a.status === 'Up');
            var dnsLabel = '';
            if (isUp) {
                dnsLabel = dns && dns.dns && dns.dns.length
                    ? dns.dns.map(escHtml).join(', ')
                    : '<i style="color:var(--text-tertiary)">DHCP / auto</i>';
            } else {
                dnsLabel = '<span style="color:var(--text-tertiary)">' + escHtml(a.status || 'Disconnected') + '</span>';
            }
            return '<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border-faint);font-size:13px' + (isUp ? '' : ';opacity:0.55') + '">' +
                '<span style="font-size:14px">' + meta.icon + '</span>' +
                '<div style="min-width:80px;font-weight:600;color:var(--text-bright)">' + meta.label + '</div>' +
                '<div style="flex:1;color:var(--text-secondary)">' + escHtml(a.name) + ' <span style="color:var(--text-tertiary);font-size:11px">' + escHtml(a.desc || '') + '</span></div>' +
                '<div style="font-family:var(--font-mono,monospace);font-size:12px;color:var(--text-bright)">' + dnsLabel + '</div>' +
                '</div>';
        }).join('');
        dnsEl.innerHTML = '<div style="font-size:11px;color:var(--text-tertiary);font-weight:600;letter-spacing:0.04em;text-transform:uppercase;margin-bottom:6px">Detected adapters · DNS applies to all connected (Up) adapters</div>' + rows;
    }

    var presets = data.presets || {};
    var grid = document.getElementById('dns-grid');
    var html = '';
    Object.keys(presets).forEach(function(key) {
        var p = presets[key];
        html += '<div class="dns-preset ' + (selectedDnsPreset === key ? 'selected' : '') + '" onclick="selectDnsPreset(\'' + key + '\', this)"><div class="dns-preset-name">' + escHtml(p.name) + '</div><div class="dns-preset-ips">' + escHtml(p.primary) + ' / ' + escHtml(p.secondary) + '</div><div class="dns-preset-desc">' + escHtml(p.desc) + '</div></div>';
    });
    grid.innerHTML = html;
}

function selectDnsPreset(key, el) {
    selectedDnsPreset = key;
    document.querySelectorAll('.dns-preset').forEach(function(e) { e.classList.remove('selected'); });
    if (el) el.classList.add('selected');
    document.getElementById('dns-custom-primary').value = '';
    document.getElementById('dns-custom-secondary').value = '';
}

async function applyDns() {
    var primary = document.getElementById('dns-custom-primary').value.trim();
    var secondary = document.getElementById('dns-custom-secondary').value.trim();
    var payload = {};
    if (primary) {
        payload = { primary: primary, secondary: secondary };
    } else if (selectedDnsPreset) {
        payload = { preset: selectedDnsPreset };
    } else {
        termWrite('network-terminal', 'Select a preset or enter custom DNS', 'warning');
        return;
    }
    termWrite('network-terminal', 'Applying DNS...');
    var r = await apiPost('/api/network/dns', payload);
    termWrite('network-terminal', r.ok ? '✓ DNS applied' : '✗ ' + (r.err || 'Failed'), r.ok ? '' : 'error');
    loadNetworkInfo();
}

async function testDnsLatency() {
    termWrite('network-terminal', 'Testing DNS latency (this takes ~20s)...');
    var data = await apiGet('/api/network/dns/test');
    (data.results || []).forEach(function(r) {
        termWrite('network-terminal', '  ' + r.name + ' (' + r.server + '): ' + (r.avg_ms !== null ? r.avg_ms + 'ms' : 'timeout'));
    });
}

async function runNetworkOptimize() {
    termWrite('network-terminal', 'Applying network optimizations...');
    var result = await apiPost('/api/network/optimize');
    Object.keys(result).forEach(function(cat) {
        var items = result[cat];
        if (Array.isArray(items)) {
            termWrite('network-terminal', '── ' + cat.toUpperCase() + ' ──');
            items.forEach(function(r) {
                termWrite('network-terminal', '  ' + (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || r.guid || r.adapter || ''));
            });
        }
    });
    termWrite('network-terminal', '═══ Network optimization complete ═══');
    updateBadge('network', true);
}

async function restoreNetworking() {
    if (!confirm('Restore networking to safe defaults?\n\n' +
                 'This will:\n' +
                 '  • Re-enable IPv6 fully\n' +
                 '  • Restart WLAN AutoConfig + related services\n' +
                 '  • Re-enable adapter bindings\n' +
                 '  • Reset Winsock + IP stack\n\n' +
                 'Reboot is recommended after for full effect.\n' +
                 'Use this if WiFi disappeared, DNS won\'t apply, or networking acts strange.')) return;
    termWrite('network-terminal', 'Running network recovery...');
    var r = await apiPost('/api/network/restore', {});
    if (!r) {
        termWrite('network-terminal', '✗ Recovery failed', 'error');
        return;
    }
    (r.results || []).forEach(function(step){
        termWrite('network-terminal', '  ' + (step.ok ? '✓' : '✗') + ' ' + step.name);
    });
    termWrite('network-terminal', '═══ ' + (r.msg || 'Recovery complete') + ' ═══');
    if (r.reboot_recommended) {
        showInfoToast('Network recovery complete. ' + (r.msg || '') +
              ' Reboot recommended for full effect — particularly the Winsock reset.',
              { timeoutMs: 9000 });
    }
    setTimeout(loadNetworkInfo, 500);
}

async function runPingTest() {
    termWrite('network-terminal', 'Running ping test (10 pings each)...');
    var data = await apiGet('/api/network/ping');
    (data.results || []).forEach(function(r) {
        termWrite('network-terminal', '  ' + r.name + ' (' + r.ip + '): avg=' + (r.avg_ms || '?') + 'ms min=' + (r.min_ms || '?') + 'ms max=' + (r.max_ms || '?') + 'ms loss=' + (r.loss_pct || '?') + '%');
    });
}

function toggleIPv6(el) {
    el.classList.toggle('on');
    var enable = el.classList.contains('on');
    apiPost('/api/network/ipv6', { enable: enable }).then(function(r) {
        termWrite('network-terminal', r.ok ? '✓ IPv6 ' + (enable ? 'enabled' : 'disabled') : '✗ Failed');
    });
}

async function runNetshTweaks() {
    termWrite('network-terminal', 'Applying netsh TCP stack tweaks...');
    var r = await apiPost('/api/network/netsh');
    (r.results || []).forEach(function(x) {
        termWrite('network-terminal', '  ' + (x.ok !== false ? '✓' : '✗') + ' ' + x.name);
    });
    termWrite('network-terminal', '=== netsh TCP tweaks applied ===');
}

async function runNetbiosTeredo() {
    termWrite('network-terminal', 'Disabling NetBIOS / Teredo / LLMNR...');
    var r = await apiPost('/api/network/netbios');
    (r.results || []).forEach(function(x) {
        termWrite('network-terminal', '  ' + (x.ok !== false ? '✓' : '✗') + ' ' + x.name);
    });
}

async function runDoh() {
    termWrite('network-terminal', 'Enabling DNS-over-HTTPS...');
    var r = await apiPost('/api/network/doh');
    (r.results || []).forEach(function(x) {
        termWrite('network-terminal', '  ' + (x.ok !== false ? '✓' : '✗') + ' ' + x.name);
    });
    termWrite('network-terminal', '(Apply DNS above for Cloudflare/Google/Quad9 to complete the DoH upgrade)');
}

async function runGameQos() {
    termWrite('network-terminal', 'Applying DSCP 46 QoS to common game ports...');
    var r = await apiPost('/api/network/game-qos');
    (r.results || []).forEach(function(x) {
        termWrite('network-terminal', '  ' + (x.ok !== false ? '✓' : '✗') + ' ' + x.name);
    });
}

async function applyAppPriority() {
    var exe = (document.getElementById('app-prio-exe').value || '').trim();
    var dscp = parseInt(document.getElementById('app-prio-dscp').value, 10) || 46;
    if (!exe) { showWarnToast('Enter an executable name (e.g. game.exe)'); return; }
    var r = await apiPost('/api/network/app-priority', { exe: exe, dscp: dscp });
    termWrite('network-terminal', r.ok ? '✓ Priority ' + dscp + ' set for ' + exe : '✗ ' + (r.err || 'Failed'));
}

async function clearAppPriorities() {
    var r = await apiPost('/api/network/app-priority/clear');
    termWrite('network-terminal', r.ok ? '✓ All per-app priorities cleared' : '✗ Failed');
}

// ═══════════════════════════════════════════════════════════════
// GPU
// ═══════════════════════════════════════════════════════════════
async function loadGpuInfo() {
    var data = await apiGet('/api/gpu/info');
    var el = document.getElementById('gpu-info');
    var vendorLabel = (data.vendor || 'unknown').toUpperCase();
    var vendorColor = data.nvidia ? '#76b900' : (data.amd ? '#ed1c24' : 'var(--accent)');
    var html = '<div class="stat-card"><div class="stat-label">GPU</div><div class="stat-value">' + escHtml(data.name || 'Unknown') + '</div><div class="stat-sub" style="color:' + vendorColor + '">' + vendorLabel + ' • ' + (data.vram_mb || 0) + ' MB VRAM</div></div>';
    html += '<div class="stat-card"><div class="stat-label">Driver</div><div class="stat-value">' + escHtml(data.driver || 'N/A') + '</div><div class="stat-sub">' + vendorLabel + ' driver</div></div>';

    if (data.telemetry_services) {
        var svcs = Object.keys(data.telemetry_services).map(function(k) {
            var v = data.telemetry_services[k];
            return k + ': <span style="color:' + (v === 'running' ? 'var(--red)' : 'var(--accent)') + '">' + v + '</span>';
        }).join(' • ');
        html += '<div class="stat-card" style="grid-column:1/-1"><div class="stat-label">Telemetry Services</div><div class="stat-value" style="font-size:12px">' + svcs + '</div></div>';
    }
    el.innerHTML = html;

    // Also load overclocking capability + saved profile
    loadOcCapability();
}

async function runNvidiaTweaks() {
    var term = document.getElementById('gpu-terminal');
    termWrite('gpu-terminal', 'Applying NVIDIA-specific tweaks...');
    var r = await apiPost('/api/gpu/nvidia-tweaks');
    (r.results || []).forEach(function(x) {
        termWrite('gpu-terminal', '  ' + (x.ok !== false ? '✓' : '✗') + ' ' + x.name);
    });
    termWrite('gpu-terminal', '=== NVIDIA tweaks complete ===');
    updateBadge('gpu', true);
}

async function runAmdTweaks() {
    var term = document.getElementById('gpu-terminal');
    termWrite('gpu-terminal', 'Applying AMD-specific tweaks...');
    var r = await apiPost('/api/gpu/amd-tweaks');
    (r.results || []).forEach(function(x) {
        termWrite('gpu-terminal', '  ' + (x.ok !== false ? '✓' : '✗') + ' ' + x.name);
    });
    termWrite('gpu-terminal', '=== AMD tweaks complete ===');
    updateBadge('gpu', true);
}

async function prepareCleanInstall() {
    var el = document.getElementById('clean-install-result');
    el.innerHTML = '<span class="pulse">Preparing...</span>';
    var data = await apiPost('/api/gpu/clean-prepare');
    if (data.ok) {
        var stepsHtml = (data.steps_done || []).map(function(s) { return '<div style="font-size:11px;color:var(--text-dim)">• ' + escHtml(s) + '</div>'; }).join('');
        var instrHtml = (data.instructions || []).map(function(s) { return '<div style="font-size:10px;color:var(--text-dim);padding:2px 0">→ ' + escHtml(s) + '</div>'; }).join('');
        el.innerHTML = '<div style="color:var(--accent);margin-bottom:8px">✓ Clean install prepared</div>' + stepsHtml + '<div style="margin-top:8px;padding:10px;background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius)"><div style="font-size:11px;color:var(--accent);margin-bottom:4px">Next Steps:</div>' + instrHtml + '</div>';
    } else {
        el.innerHTML = '<span style="color:var(--red)">✗ ' + escHtml(data.err || 'Failed') + '</span>';
    }
}

async function runGpuTweaks() {
    termWrite('gpu-terminal', 'Applying GPU tweaks...');
    var result = await apiPost('/api/gpu/tweaks');
    (result.tweaks || []).forEach(function(r) {
        termWrite('gpu-terminal', '  ' + (r.ok !== false ? '✓' : '✗') + ' ' + (r.name || ''));
    });
    termWrite('gpu-terminal', '═══ GPU tweaks complete ═══');
    updateBadge('gpu', true);
    loadGpuInfo();
}

// ═══════════════════════════════════════════════════════════════
// CLEANER
// ═══════════════════════════════════════════════════════════════
async function scanCleaner() {
    var el = document.getElementById('cleaner-scan');
    el.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Scanning temp files...</span>';
    var data = await apiGet('/api/cleaner/scan');
    var locs = data.locations || [];
    if (locs.length === 0) {
        el.innerHTML = '<span style="color:var(--text-dim)">No cleanable locations found</span>';
        return;
    }
    var totalSize = 0;
    locs.forEach(function(l) { totalSize += (l.size || 0); });
    var html = '<div style="margin-bottom:12px;font-size:13px;color:var(--accent)">Total reclaimable: <strong>' + formatSize(totalSize) + '</strong></div>';
    locs.forEach(function(l) {
        html += '<div class="check-item"><span style="flex:1">' + escHtml(l.name) + '</span><span style="color:var(--text-dim);font-size:10px">' + l.files + ' files</span><span style="color:var(--accent);font-size:11px;min-width:70px;text-align:right">' + escHtml(l.size_str) + '</span><span class="check-cat">' + escHtml(l.category) + '</span></div>';
    });
    el.innerHTML = html;
}

async function runCleaner() {
    termWrite('cleaner-terminal', 'Cleaning all temp files...');
    var result = await apiPost('/api/cleaner/run');
    if (result.ok) {
        termWrite('cleaner-terminal', '✓ Cleaned ' + result.total_files + ' files, freed ' + result.total_freed_str);
        (result.locations || []).forEach(function(l) {
            if (l.files > 0 || l.freed > 0) {
                termWrite('cleaner-terminal', '  ' + l.name + ': ' + l.files + ' files, ' + (l.freed_str || formatSize(l.freed || 0)));
            }
        });
    } else {
        termWrite('cleaner-terminal', '✗ Clean failed', 'error');
    }
    termWrite('cleaner-terminal', '═══ Cleaning complete ═══');
    updateBadge('cleaner', true);
    scanCleaner();
}

// ─── RAM Cleaner ───
async function cleanRAM() {
    var el = document.getElementById('ram-clean-result');
    if (el) el.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Cleaning RAM...</span>';
    termWrite('cleaner-terminal', 'Cleaning RAM...');
    var result = await apiPost('/api/cleaner/ram');
    if (result.ok) {
        termWrite('cleaner-terminal', '✓ RAM cleaned — freed ~' + result.freed_str);
        termWrite('cleaner-terminal', '  Before: ' + result.free_before_mb + ' MB free → After: ' + result.free_after_mb + ' MB free');
        if (el) el.innerHTML = '<span style="color:var(--accent)">✓ Freed ~' + escHtml(result.freed_str) + '</span> (' + result.free_before_mb + '→' + result.free_after_mb + ' MB free)';
    } else {
        if (el) el.innerHTML = '<span style="color:var(--red)">✗ Failed</span>';
    }
}

// ─── Disk Cleaner ───
var _selectedDrives = {};

async function loadDiskList() {
    var el = document.getElementById('disk-list');
    if (!el) return;
    el.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Loading drives...</span>';
    var data = await apiGet('/api/cleaner/disks');
    var drives = data.drives || [];
    if (drives.length === 0) {
        el.innerHTML = '<span style="color:var(--text-dim)">No drives found</span>';
        return;
    }
    _selectedDrives = {};
    var html = '';
    drives.forEach(function(d) {
        _selectedDrives[d.letter] = true;
        var barCls = d.used_pct > 90 ? 'crit' : d.used_pct > 75 ? 'warn' : '';
        html += '<div class="check-item" style="padding:8px"><div class="check-box checked" data-drive="' + escAttr(d.letter) + '" onclick="toggleDriveSelect(this)"></div><span style="flex:1">' + escHtml(d.letter) + ' ' + escHtml(d.name || '') + '</span><span style="font-size:10px;color:var(--text-dim)">' + d.free_gb + ' GB free / ' + d.total_gb + ' GB</span><div class="stat-bar" style="width:60px;margin-left:8px"><div class="stat-bar-fill ' + barCls + '" style="width:' + d.used_pct + '%"></div></div></div>';
    });
    el.innerHTML = html;
}

function toggleDriveSelect(el) {
    el.classList.toggle('checked');
    var drive = el.dataset.drive;
    _selectedDrives[drive] = el.classList.contains('checked');
}

function _getSelectedDrives() {
    var drives = [];
    Object.keys(_selectedDrives).forEach(function(k) {
        if (_selectedDrives[k]) drives.push(k);
    });
    return drives;
}

async function scanSelectedDisks() {
    var drives = _getSelectedDrives();
    if (drives.length === 0) { termWrite('cleaner-terminal', 'No drives selected', 'warning'); return; }
    var el = document.getElementById('disk-scan-result');
    if (el) el.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Scanning ' + drives.join(', ') + '...</span>';
    var allJunk = [];
    for (var i = 0; i < drives.length; i++) {
        var data = await apiPost('/api/cleaner/disk-scan', { drive: drives[i] });
        (data.junk || []).forEach(function(j) {
            j.drive = drives[i];
            allJunk.push(j);
        });
    }
    if (el) {
        var totalSize = 0;
        allJunk.forEach(function(j) { totalSize += j.size; });
        var html = '<div style="color:var(--accent);font-size:12px;margin-bottom:4px">Found: ' + formatSize(totalSize) + ' reclaimable</div>';
        allJunk.forEach(function(j) {
            if (j.size > 0) html += '<div style="font-size:10px;color:var(--text-dim)">' + escHtml(j.drive) + ' ' + escHtml(j.name) + ': ' + j.files + ' files, ' + escHtml(j.size_str) + '</div>';
        });
        el.innerHTML = html;
    }
}

async function cleanSelectedDisks() {
    var drives = _getSelectedDrives();
    if (drives.length === 0) { termWrite('cleaner-terminal', 'No drives selected', 'warning'); return; }
    termWrite('cleaner-terminal', 'Cleaning drives: ' + drives.join(', ') + '...');
    var result = await apiPost('/api/cleaner/disk-clean', { drives: drives });
    if (result.ok) {
        termWrite('cleaner-terminal', '✓ Disk clean: ' + result.total_files + ' files, ' + result.total_freed_str + ' freed');
        (result.drives || []).forEach(function(d) {
            termWrite('cleaner-terminal', '  ' + d.drive + ': ' + d.files + ' files, ' + d.freed_str);
        });
    }
    var el = document.getElementById('disk-scan-result');
    if (el) el.innerHTML = '<span style="color:var(--accent)">✓ Cleaned ' + (result.total_freed_str || '0 B') + '</span>';
}

// ═══════════════════════════════════════════════════════════════
// PRIVACY
// ═══════════════════════════════════════════════════════════════
async function loadPrivacyStatus() {
    var data = await apiGet('/api/privacy/status');
    var hostsEl = document.getElementById('hosts-status');
    if (data.hosts) {
        hostsEl.innerHTML = data.hosts.active
            ? '<span class="status-badge ok">Active</span> ' + data.hosts.blocked_count + ' domains blocked'
            : '<span class="status-badge neutral">Inactive</span> No telemetry blocks applied';
    }
    if (data.devices) {
        var camToggle = document.getElementById('toggle-webcam');
        if (camToggle) { if (data.devices.webcam_enabled) camToggle.classList.add('on'); else camToggle.classList.remove('on'); }
        // Mic toggle was removed in v3.1 — see core/privacy.py for context.
    }
}

async function runFullPrivacy() {
    var term = document.getElementById('privacy-terminal');
    term.innerHTML = '';
    termWrite('privacy-terminal', 'Running full privacy hardening...');
    var result = await apiPost('/api/privacy/run');

    if (result.telemetry) {
        var ok = result.telemetry.filter(function(r) { return r.ok; }).length;
        termWrite('privacy-terminal', 'Telemetry: ' + ok + '/' + result.telemetry.length + ' tweaks applied');
    }
    if (result.hosts) {
        termWrite('privacy-terminal', result.hosts.ok ? '✓ Hosts: ' + (result.hosts.count || 0) + ' domains blocked' : '✗ Hosts: ' + (result.hosts.err || 'failed'));
    }
    if (result.firewall) {
        var ok2 = result.firewall.filter(function(r) { return r.ok; }).length;
        termWrite('privacy-terminal', 'Firewall: ' + ok2 + '/' + result.firewall.length + ' rules created');
    }
    if (result.updates) {
        var ok3 = result.updates.filter(function(r) { return r.ok; }).length;
        termWrite('privacy-terminal', 'Updates: ' + ok3 + '/' + result.updates.length + ' tweaks applied');
    }
    termWrite('privacy-terminal', '═══ Privacy hardening complete ═══');
    updateBadge('privacy', true);
    loadPrivacyStatus();
}

function toggleDevice(device, el) {
    el.classList.toggle('on');
    var enable = el.classList.contains('on');
    apiPost('/api/privacy/' + device, { enable: enable }).then(function(r) {
        termWrite('privacy-terminal', r.ok ? '✓ ' + device + ' ' + (enable ? 'enabled' : 'disabled') : '✗ Failed');
    });
}

// ═══════════════════════════════════════════════════════════════
// VAULT
// ═══════════════════════════════════════════════════════════════
async function checkVaultStatus() {
    var data = await apiGet('/api/vault/status');
    if (data.unlocked) {
        showVaultUnlocked();
        loadVaultEntries();
    } else {
        showVaultLocked(data.pin_set);
    }
}

function showVaultLocked(pinSet) {
    document.getElementById('vault-lock-screen').style.display = 'block';
    document.getElementById('vault-unlocked').style.display = 'none';
    var title = document.getElementById('vault-lock-title');
    var msg = document.getElementById('vault-lock-msg');
    if (pinSet) {
        title.textContent = 'ENTER PIN';
        msg.textContent = 'Enter your vault PIN to unlock';
    } else {
        title.textContent = 'SETUP PIN';
        msg.textContent = 'Create a 4-8 digit PIN to secure your vault';
    }
    vaultPin = '';
    updatePinDots();
}

function showVaultUnlocked() {
    document.getElementById('vault-lock-screen').style.display = 'none';
    document.getElementById('vault-unlocked').style.display = 'block';
}

function pinInput(digit) {
    if (vaultPin.length >= 8) return;
    vaultPin += digit;
    updatePinDots();
}

function pinClear() {
    vaultPin = '';
    updatePinDots();
    document.getElementById('vault-lock-error').textContent = '';
}

function updatePinDots() {
    var dots = document.querySelectorAll('#pin-dots .pin-dot');
    dots.forEach(function(dot, i) {
        if (i < vaultPin.length) dot.classList.add('filled');
        else dot.classList.remove('filled');
    });
}

async function pinSubmit() {
    if (vaultPin.length < 4) {
        document.getElementById('vault-lock-error').textContent = 'PIN must be at least 4 digits';
        return;
    }
    var errEl = document.getElementById('vault-lock-error');
    errEl.textContent = '';

    var status = await apiGet('/api/vault/status');
    var result;
    if (status.pin_set) {
        result = await apiPost('/api/vault/unlock', { pin: vaultPin });
    } else {
        result = await apiPost('/api/vault/setup', { pin: vaultPin });
    }

    if (result.ok) {
        showVaultUnlocked();
        loadVaultEntries();
    } else {
        errEl.textContent = result.err || 'Failed';
        vaultPin = '';
        updatePinDots();
        var pad = document.getElementById('pin-pad');
        pad.style.animation = 'none';
        void pad.offsetHeight;
        pad.style.animation = 'shake 0.3s ease';
    }
}

async function lockVault() {
    await apiPost('/api/vault/lock');
    checkVaultStatus();
}

async function loadVaultEntries() {
    var search = (document.getElementById('vault-search') || {}).value || '';
    var data = await apiGet('/api/vault/entries?search=' + encodeURIComponent(search) + '&category=' + encodeURIComponent(vaultCategory));
    var el = document.getElementById('vault-entries');
    if (!data.ok) {
        el.innerHTML = '<div style="color:var(--red)">' + escHtml(data.err || 'Error') + '</div>';
        return;
    }
    var entries = data.entries || [];
    if (entries.length === 0) {
        el.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:30px">No entries yet. Click "Add Entry" to get started.</div>';
        return;
    }
    var html = '';
    entries.forEach(function(e) {
        html += '<div class="vault-entry"><div class="vault-entry-icon">' + ((e.service || '?')[0] || '?').toUpperCase() + '</div><div class="vault-entry-info"><div class="vault-entry-service">' + escHtml(e.service) + '</div><div class="vault-entry-user">' + escHtml(e.username) + ' • ' + escHtml(e.password_masked) + '</div></div><span class="vault-cat-chip">' + escHtml(e.category || 'General') + '</span><div class="vault-entry-actions"><button class="btn btn-sm" onclick="copyPassword(\'' + e.id + '\')">Copy</button><button class="btn btn-sm" onclick="openEditEntry(\'' + e.id + '\')">Edit</button><button class="btn btn-sm btn-danger" onclick="deleteVaultEntry(\'' + e.id + '\')">✗</button></div></div>';
    });
    el.innerHTML = html;
}

function filterVaultCat(cat, chip) {
    vaultCategory = cat;
    document.querySelectorAll('#vault-cats .chip').forEach(function(c) { c.classList.remove('active'); });
    chip.classList.add('active');
    loadVaultEntries();
}

async function copyPassword(id) {
    var data = await apiGet('/api/vault/entry/' + id + '/password');
    if (data.ok && data.password) {
        try {
            await navigator.clipboard.writeText(data.password);
        } catch (ex) {
            var ta = document.createElement('textarea');
            ta.value = data.password;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }
    }
}

async function deleteVaultEntry(id) {
    if (!confirm('Delete this entry permanently?')) return;
    await fetch('/api/vault/entry/' + id, { method: 'DELETE' });
    loadVaultEntries();
}

// ─── Vault Modals ───
function openAddEntry() {
    openModal(
        '<div class="modal-title">ADD ENTRY</div>' +
        '<div class="form-group"><label class="form-label">Service / Website</label><input type="text" id="entry-service" placeholder="e.g. Google, Steam, Netflix"></div>' +
        '<div class="form-group"><label class="form-label">Username / Email</label><input type="text" id="entry-username" placeholder="user@example.com"></div>' +
        '<div class="form-group"><label class="form-label">Password</label><div style="display:flex;gap:6px"><input type="password" id="entry-password" placeholder="Enter password"><button class="btn btn-sm" onclick="genForField(\'entry-password\')">Gen</button></div></div>' +
        '<div class="form-row"><div class="form-group"><label class="form-label">URL</label><input type="text" id="entry-url" placeholder="https://..."></div><div class="form-group"><label class="form-label">Category</label><select id="entry-category"><option>General</option><option>Social</option><option>Email</option><option>Gaming</option><option>Banking</option><option>Work</option></select></div></div>' +
        '<div class="form-group"><label class="form-label">Notes</label><textarea id="entry-notes" rows="2" placeholder="Optional notes..."></textarea></div>' +
        '<div class="modal-actions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn btn-primary" onclick="saveEntry()">Save</button></div>'
    );
}

async function saveEntry() {
    var data = {
        service: document.getElementById('entry-service').value,
        username: document.getElementById('entry-username').value,
        password: document.getElementById('entry-password').value,
        url: document.getElementById('entry-url').value,
        category: document.getElementById('entry-category').value,
        notes: document.getElementById('entry-notes').value,
    };
    if (!data.service || !data.password) { showWarnToast('Service and password are required'); return; }
    var r = await apiPost('/api/vault/entry', data);
    if (r.ok) { closeModal(); loadVaultEntries(); }
    else { showErrorToast(r.err || 'Failed to save'); }
}

async function openEditEntry(id) {
    var data = await apiGet('/api/vault/entries');
    var entry = null;
    (data.entries || []).forEach(function(e) { if (e.id === id) entry = e; });
    if (!entry) return;
    var pwData = await apiGet('/api/vault/entry/' + id + '/password');

    openModal(
        '<div class="modal-title">EDIT ENTRY</div>' +
        '<div class="form-group"><label class="form-label">Service / Website</label><input type="text" id="edit-service" value="' + escAttr(entry.service) + '"></div>' +
        '<div class="form-group"><label class="form-label">Username / Email</label><input type="text" id="edit-username" value="' + escAttr(entry.username) + '"></div>' +
        '<div class="form-group"><label class="form-label">Password</label><div style="display:flex;gap:6px"><input type="text" id="edit-password" value="' + escAttr(pwData.password || '') + '"><button class="btn btn-sm" onclick="genForField(\'edit-password\')">Gen</button></div></div>' +
        '<div class="form-row"><div class="form-group"><label class="form-label">URL</label><input type="text" id="edit-url" value="' + escAttr(entry.url || '') + '"></div><div class="form-group"><label class="form-label">Category</label><select id="edit-category">' + ['General','Social','Email','Gaming','Banking','Work'].map(function(c) { return '<option' + (c === entry.category ? ' selected' : '') + '>' + c + '</option>'; }).join('') + '</select></div></div>' +
        '<div class="form-group"><label class="form-label">Notes</label><textarea id="edit-notes" rows="2">' + escHtml(entry.notes || '') + '</textarea></div>' +
        '<div class="modal-actions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn btn-primary" onclick="updateEntry(\'' + id + '\')">Update</button></div>'
    );
}

async function updateEntry(id) {
    var data = {
        service: document.getElementById('edit-service').value,
        username: document.getElementById('edit-username').value,
        password: document.getElementById('edit-password').value,
        url: document.getElementById('edit-url').value,
        category: document.getElementById('edit-category').value,
        notes: document.getElementById('edit-notes').value,
    };
    var r = await fetch('/api/vault/entry/' + id, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).then(function(res) { return res.json(); });
    if (r.ok) { closeModal(); loadVaultEntries(); }
}

function openPasswordGen() {
    openModal(
        '<div class="modal-title">PASSWORD GENERATOR</div>' +
        '<div class="form-group"><label class="form-label">Length: <span id="gen-length-val">20</span></label><input type="range" id="gen-length" min="8" max="64" value="20" oninput="document.getElementById(\'gen-length-val\').textContent=this.value"></div>' +
        '<div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap"><label style="font-size:11px;color:var(--text);cursor:pointer"><input type="checkbox" id="gen-upper" checked> Uppercase</label><label style="font-size:11px;color:var(--text);cursor:pointer"><input type="checkbox" id="gen-lower" checked> Lowercase</label><label style="font-size:11px;color:var(--text);cursor:pointer"><input type="checkbox" id="gen-digits" checked> Digits</label><label style="font-size:11px;color:var(--text);cursor:pointer"><input type="checkbox" id="gen-symbols" checked> Symbols</label><label style="font-size:11px;color:var(--text);cursor:pointer"><input type="checkbox" id="gen-exclude"> No ambiguous</label></div>' +
        '<div style="background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);padding:12px;font-size:14px;color:var(--accent);word-break:break-all;min-height:44px;font-family:var(--mono)" id="gen-result">Click generate...</div>' +
        '<div class="modal-actions"><button class="btn" onclick="generatePassword()">Generate</button><button class="btn btn-primary" onclick="copyGenerated()">Copy</button><button class="btn" onclick="closeModal()">Close</button></div>'
    );
}

async function generatePassword() {
    var len = document.getElementById('gen-length').value;
    var params = 'length=' + len +
        '&upper=' + document.getElementById('gen-upper').checked +
        '&lower=' + document.getElementById('gen-lower').checked +
        '&digits=' + document.getElementById('gen-digits').checked +
        '&symbols=' + document.getElementById('gen-symbols').checked +
        '&exclude_ambiguous=' + document.getElementById('gen-exclude').checked;
    var data = await apiGet('/api/vault/generate?' + params);
    document.getElementById('gen-result').textContent = data.password || 'Error';
}

async function copyGenerated() {
    var pw = document.getElementById('gen-result').textContent;
    try { await navigator.clipboard.writeText(pw); } catch (ex) {}
}

async function genForField(fieldId) {
    var data = await apiGet('/api/vault/generate?length=20');
    var field = document.getElementById(fieldId);
    if (field && data.password) {
        field.value = data.password;
        field.type = 'text';
    }
}

// ═══════════════════════════════════════════════════════════════
// FULL GHOST
// ═══════════════════════════════════════════════════════════════
async function runFullGhost() {
    if (!confirm('⚡ FULL SPECTRUM MODE\n\nThis will run ALL modules:\n• Debloat\n• Optimize\n• Network\n• GPU\n• Privacy\n• Cleaner\n\nCreate a restore point first!\nContinue?')) return;

    switchPage('logs');
    termWrite('full-log', '╔══════════════════════════════════════╗');
    termWrite('full-log', '║   FULL SPECTRUM MODE ACTIVATED       ║');
    termWrite('full-log', '╚══════════════════════════════════════╝');

    await apiPost('/api/full-ghost');

    termWrite('full-log', '');
    termWrite('full-log', '═══ FULL SPECTRUM COMPLETE ═══');
    termWrite('full-log', 'A system restart is recommended to apply all changes.');

    ['debloat', 'optimize', 'network', 'gpu', 'privacy', 'cleaner'].forEach(function(m) { updateBadge(m, true); });
}

// ═══════════════════════════════════════════════════════════════
// LOGS
// ═══════════════════════════════════════════════════════════════
async function loadFullLog() {
    var data = await apiGet('/api/logs');
    var el = document.getElementById('full-log');
    var html = '';
    (data.entries || []).forEach(function(e) {
        var cls = e.level === 'WARNING' ? ' warning' : e.level === 'ERROR' ? ' error' : '';
        html += '<div class="terminal-line"><span class="terminal-ts">' + escHtml(e.ts) + '</span><span class="terminal-mod">' + escHtml(e.module) + '</span><span class="terminal-msg' + cls + '">' + escHtml(e.msg) + '</span></div>';
    });
    el.innerHTML = html;
    el.scrollTop = el.scrollHeight;
    lastLogCount = (data.entries || []).length;
}

// v3.3.1-beta.7: cap terminal DOM growth at 2000 lines.  Previously
// `term.innerHTML += '<div…>'` appended forever for the entire app
// lifetime — a multi-hour session would accumulate tens of thousands
// of <div> nodes in either the dashboard mini-terminal or the
// full-log viewer.  Backend ring buffer caps logs at ~2000 entries
// anyway, so trim the DOM to match.
var _LOG_TERMINAL_CAP = 2000;

function _trimTerminalToCap(term) {
    while (term.childNodes.length > _LOG_TERMINAL_CAP) {
        term.removeChild(term.firstChild);
    }
}

function pollLogs() {
    if (currentPage === 'dashboard' || currentPage === 'logs') {
        apiGet('/api/logs?since=' + lastLogCount).then(function(data) {
            if (data.entries && data.entries.length > 0) {
                var termId = currentPage === 'dashboard' ? 'dash-terminal' : 'full-log';
                var term = document.getElementById(termId);
                if (!term) return;
                data.entries.forEach(function(e) {
                    var cls = e.level === 'WARNING' ? ' warning' : e.level === 'ERROR' ? ' error' : '';
                    // Defense-in-depth: escape `module` too — it comes
                    // from Python's logger name which is currently
                    // hard-coded but might not always be.
                    term.innerHTML += '<div class="terminal-line"><span class="terminal-ts">' + escHtml(e.ts) + '</span><span class="terminal-mod">' + escHtml(e.module) + '</span><span class="terminal-msg' + cls + '">' + escHtml(e.msg) + '</span></div>';
                });
                _trimTerminalToCap(term);
                term.scrollTop = term.scrollHeight;
                lastLogCount = data.total;
            }
        });
    }
}

// ═══════════════════════════════════════════════════════════════
// MODAL
// ═══════════════════════════════════════════════════════════════
function openModal(html) {
    document.getElementById('modal-content').innerHTML = html;
    document.getElementById('modal-overlay').classList.add('open');
}

function closeModal() {
    document.getElementById('modal-overlay').classList.remove('open');
}

// ═══════════════════════════════════════════════════════════════
// BADGE UPDATER
// ═══════════════════════════════════════════════════════════════
function updateBadge(mod, done) {
    var b = document.getElementById('badge-' + mod);
    if (b) {
        if (done) b.classList.add('done');
        else b.classList.remove('done');
    }
}

// ═══════════════════════════════════════════════════════════════
// BOOT SPLASH — the one place the matrix aesthetic still lives.
// Renders the matrix rain for ~1.5s while the app initializes, then
// fades the splash overlay out to reveal the clean Linear UI.
// ═══════════════════════════════════════════════════════════════
function runBootSplash() {
    var splash = document.getElementById('boot-splash');
    var canvas = document.getElementById('boot-splash-canvas');
    if (!splash || !canvas) return;

    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    var fontSize = 14;
    var columns = 0;
    var drops = [];
    var chars = 'ゴーストシェル01アイウエオカキクケコ♦♣♠█▓▒░'.split('');

    function resize() {
        var w = splash.clientWidth;
        var h = splash.clientHeight;
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        canvas.style.width = w + 'px';
        canvas.style.height = h + 'px';
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr);
        columns = Math.max(1, Math.floor(w / fontSize));
        drops = [];
        for (var i = 0; i < columns; i++) drops[i] = Math.random() * (h / fontSize);
    }
    resize();
    window.addEventListener('resize', resize);

    // 2.5s of splash before the fade kicks in.  Coordinated with the
    // CSS letter-stagger which finishes around 1500ms, so the user sees
    // the fully-revealed wordmark hold for ~1s before fade-out begins.
    var stopAt = performance.now() + 2500;
    var faded = false;

    function draw() {
        var w = canvas.width / dpr;
        var h = canvas.height / dpr;
        ctx.fillStyle = 'rgba(10, 11, 13, 0.12)';
        ctx.fillRect(0, 0, w, h);
        ctx.font = fontSize + 'px JetBrains Mono, monospace';
        // Iridescent oil-slick rain — each column cycles through the four
        // Nacre stops (mint → periwinkle → lilac → rose) by column index.
        var rainStops = ['#a7f0e4', '#b8c8ff', '#d9b8ff', '#ffc4e6'];
        for (var i = 0; i < drops.length; i++) {
            ctx.fillStyle = rainStops[i % 4];
            var ch = chars[Math.floor(Math.random() * chars.length)];
            ctx.fillText(ch, i * fontSize, drops[i] * fontSize);
            if (drops[i] * fontSize > h && Math.random() > 0.975) drops[i] = 0;
            drops[i]++;
        }
    }

    function tick(now) {
        draw();
        if (now < stopAt) {
            requestAnimationFrame(tick);
        } else if (!faded) {
            faded = true;
            splash.classList.add('hidden');
            // Stop drawing once the fade-out transition starts.
            setTimeout(function () {
                splash.parentNode && splash.parentNode.removeChild(splash);
            }, 600);
        }
    }
    requestAnimationFrame(tick);
}

// ═══════════════════════════════════════════════════════════════
// KEYBOARD SHORTCUTS
// ═══════════════════════════════════════════════════════════════
document.addEventListener('keydown', function(e) {
    if (currentPage === 'vault' && document.getElementById('vault-lock-screen').style.display !== 'none') {
        if (e.key >= '0' && e.key <= '9') pinInput(e.key);
        else if (e.key === 'Backspace') { vaultPin = vaultPin.slice(0, -1); updatePinDots(); }
        else if (e.key === 'Enter') pinSubmit();
        else if (e.key === 'Escape') pinClear();
    }
    if (e.key === 'Escape' && document.getElementById('modal-overlay').classList.contains('open')) {
        closeModal();
    }
});

// ═══════════════════════════════════════════════════════════════
// CSS ANIMATION INJECTION
// ═══════════════════════════════════════════════════════════════
var shakeStyle = document.createElement('style');
shakeStyle.textContent = '@keyframes shake { 0%, 100% { transform: translateX(0); } 20% { transform: translateX(-8px); } 40% { transform: translateX(8px); } 60% { transform: translateX(-4px); } 80% { transform: translateX(4px); } }';
document.head.appendChild(shakeStyle);

// ═══════════════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════════════
// AUTO-UPDATER
// ═══════════════════════════════════════════════════════════════
// Backend handles auto-check + auto-download in the background.  The UI
// just polls /api/updater/status periodically and surfaces:
//   • the version badge in the title bar
//   • a non-intrusive toast when a new build finishes downloading
//   • a modal with release notes + a one-click Install button
// User can change auto-check / auto-download / auto-install in the modal.

var _updaterState = { lastSeen: '', toastShown: false, statusPollTimer: null };

async function checkForUpdate() {
    addLog('Checking for updates...');
    var r = await apiPost('/api/updater/check');
    if (!r || !r.ok) {
        addLog('Update check failed: ' + ((r && r.err) || 'unknown error'));
        return;
    }
    if (r.update_available) {
        addLog('Update available: v' + r.latest + ' (current v' + r.current + ')');
        _refreshUpdateBadge(r.latest);
        showUpdateModal();
    } else {
        addLog('Already up to date (v' + r.current + ')');
        showUpdateModal();   // still show — confirms "you're current"
    }
}

function _refreshUpdateBadge(latest) {
    var badge = document.getElementById('update-badge');
    if (badge && latest) { badge.style.display = 'inline'; badge.textContent = 'v' + latest; }
}

// Lightweight toast for "Update ready"
function showUpdateToast(message, action) {
    var toast = document.getElementById('update-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'update-toast';
        toast.className = 'update-toast';
        document.body.appendChild(toast);
    }
    toast.innerHTML =
        '<div class="update-toast-msg">' + escHtml(message) + '</div>' +
        '<div class="update-toast-actions">' +
            '<button class="btn btn-sm btn-primary" onclick="installUpdateFromToast()">Install &amp; Restart</button>' +
            '<button class="btn btn-sm" onclick="dismissUpdateToast()">Later</button>' +
        '</div>';
    toast.classList.add('show');
}

function dismissUpdateToast() {
    var toast = document.getElementById('update-toast');
    if (toast) toast.classList.remove('show');
}

async function installUpdateFromToast() {
    dismissUpdateToast();
    return installUpdateNow();
}

function showUpdateModal() {
    apiGet('/api/updater/status').then(function(s) {
        if (!s) return;
        if (!s.update_available) {
            openModal(
                '<div class="modal-title">UP TO DATE</div>' +
                '<p style="color:var(--text-dim)">Vispora v' + escHtml(s.current_version) + ' is the latest version.</p>' +
                _renderUpdaterSettingsBlock(s.settings || {}) +
                '<div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>'
            );
            return;
        }
        var notes = s.notes ? ('<div style="background:var(--bg-void);border:1px solid var(--border);border-radius:var(--radius);padding:10px;font-size:11px;color:var(--text-dim);max-height:180px;overflow:auto;margin-bottom:12px;white-space:pre-wrap">' + escHtml(s.notes) + '</div>') : '';
        var primaryBtn = s.download_complete
            ? '<button class="btn btn-primary" onclick="installUpdateNow()">Install &amp; Restart</button>'
            : '<button class="btn btn-primary" onclick="downloadAndInstall()">Download &amp; Install</button>';
        var dlBadge = '';
        if (s.downloading)        dlBadge = '<span style="color:var(--accent2);font-size:10px;margin-left:8px">Downloading...</span>';
        else if (s.download_complete) dlBadge = '<span style="color:var(--accent);font-size:10px;margin-left:8px">✓ Ready to install</span>';
        // v3 — surface why auto-install is waiting (if it is).  Manual install
        // bypasses this gate so the "Install & Restart" button still works.
        var safetyBadge = '';
        if (s.download_complete && s.settings && s.settings.auto_install) {
            if (s.install_safe) {
                safetyBadge = '<div style="margin:8px 0 12px;padding:8px 10px;background:rgba(74,222,128,0.08);border:1px solid rgba(74,222,128,0.3);border-radius:4px;font-size:11px;color:var(--accent)">⚙ Auto-install will fire shortly (system is idle).</div>';
            } else {
                safetyBadge = '<div style="margin:8px 0 12px;padding:8px 10px;background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.3);border-radius:4px;font-size:11px;color:var(--orange,#fbbf24)">⏸ Auto-install deferred: ' + escHtml(s.install_blocked_reason || 'system busy') + '.  Will retry when you go idle.  Click <b>Install &amp; Restart</b> to apply now anyway.</div>';
            }
        }
        openModal(
            '<div class="modal-title">UPDATE AVAILABLE' + dlBadge + '</div>' +
            '<div style="margin-bottom:12px"><span style="color:var(--text-dim)">Current:</span> v' + escHtml(s.current_version) +
            ' &rarr; <span style="color:var(--accent)">v' + escHtml(s.latest_version) + '</span></div>' +
            notes +
            safetyBadge +
            _renderUpdaterSettingsBlock(s.settings || {}) +
            '<div class="modal-actions"><button class="btn" onclick="closeModal()">Later</button>' + primaryBtn + '</div>'
        );
    });
}

function _renderUpdaterSettingsBlock(s) {
    var row = function(key, label, desc) {
        var on = !!s[key];
        return '<div class="toggle-row" style="border-bottom:1px solid var(--border);padding:6px 0">' +
            '<div class="toggle-info"><div class="toggle-name">' + escHtml(label) + '</div>' +
            '<div class="toggle-desc">' + escHtml(desc) + '</div></div>' +
            '<button type="button" class="toggle-switch ' + (on ? 'on' : '') +
                '" onclick="toggleUpdaterSetting(this, \'' + key + '\')" ' +
                'role="switch" aria-checked="' + on + '"></button>' +
            '</div>';
    };
    return '<div style="margin:10px 0 6px;font-size:10px;letter-spacing:1px;color:var(--text-dim);text-transform:uppercase">Update settings</div>' +
        row('auto_check',    'Check automatically',     'Look for new versions on launch + every 6 hours') +
        row('auto_download', 'Download in background',  'Pull the new build as soon as one is detected') +
        row('auto_install',  'Install silently',        'Apply update + restart Vispora with no prompt');
}

async function toggleUpdaterSetting(el, key) {
    el.classList.toggle('on');
    var on = el.classList.contains('on');
    el.setAttribute('aria-checked', String(on));
    var payload = {}; payload[key] = on;
    var r = await apiPost('/api/updater/settings', payload);
    if (!r || !r.ok) {
        // Revert on failure
        el.classList.toggle('on');
        el.setAttribute('aria-checked', String(!on));
        addLog('Could not save updater setting: ' + ((r && r.err) || 'unknown'));
    }
}

async function downloadAndInstall() {
    var modal = document.getElementById('modal-content');
    if (modal) modal.innerHTML =
        '<div class="modal-title">DOWNLOADING...</div>' +
        '<div class="progress-container"><div class="progress-bar"><div class="progress-fill" id="update-progress" style="width:30%"></div></div></div>' +
        '<p style="color:var(--text-dim);font-size:11px;text-align:center" id="update-msg">Fetching the new build...</p>';

    var dl = await apiPost('/api/updater/download');
    if (!dl || !dl.ok) {
        var msgEl = document.getElementById('update-msg');
        if (msgEl) msgEl.innerHTML = '<span style="color:var(--red)">Download failed: ' + escHtml((dl && dl.err) || 'unknown') + '</span>';
        addLog('Update download failed: ' + ((dl && dl.err) || 'unknown'));
        return;
    }
    addLog('Update downloaded — installing & restarting...');
    return installUpdateNow();
}

async function installUpdateNow() {
    var modal = document.getElementById('modal-content');
    if (modal) modal.innerHTML =
        '<div class="modal-title">INSTALLING...</div>' +
        '<p style="color:var(--text-dim);font-size:11px;text-align:center">Vispora will close and restart in a moment.</p>';

    var r = await apiPost('/api/updater/install', {}, { timeoutMs: 10000 });
    if (r && r.ok) {
        addLog('Update install triggered — exiting in ~1s.');
        if (modal) modal.innerHTML =
            '<div class="modal-title" style="color:var(--accent)">UPDATING</div>' +
            '<p style="color:var(--text-dim);font-size:11px;text-align:center">' + escHtml(r.msg || 'Vispora is restarting.') + '</p>';
    } else {
        var err = (r && r.err) || 'unknown';
        addLog('Install failed: ' + err);
        if (modal) modal.innerHTML =
            '<div class="modal-title" style="color:var(--red)">INSTALL FAILED</div>' +
            '<p style="color:var(--text-dim);font-size:11px;text-align:center">' + escHtml(err) + '</p>' +
            '<div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>';
    }
}

// Poll status periodically — surfaces backend auto-download progress.
async function _pollUpdaterStatus() {
    try {
        var s = await apiGet('/api/updater/status');
        if (!s) return;
        if (s.update_available) {
            _refreshUpdateBadge(s.latest_version);
            // Toast once, the moment a download finishes (auto-download path)
            if (s.download_complete && !_updaterState.toastShown && _updaterState.lastSeen !== s.latest_version) {
                _updaterState.toastShown = true;
                _updaterState.lastSeen = s.latest_version;
                showUpdateToast('Vispora v' + s.latest_version + ' is ready to install.');
            }
        }
    } catch (e) { /* silent — periodic poll */ }

    // v2.9.6 — also poll the driver-update state and toast once per
    // newly-detected driver version.
    try {
        var d = await apiGet('/api/driver/status');
        if (d && d.vendor === 'nvidia') {
            // Closed-loop install confirmation (beta.4): toast once when a
            // launched install is verified live via nvidia-smi.
            if (d.install_result === 'success' && d.current_version &&
                _updaterState.lastInstallToast !== d.current_version) {
                _updaterState.lastInstallToast = d.current_version;
                showInfoToast('NVIDIA driver updated to v' + d.current_version + '.',
                              { title: 'Driver updated' });
            }
            if (d.update_available &&
                _updaterState.lastDriverSeen !== d.latest_version) {
                _updaterState.lastDriverSeen = d.latest_version;
                showDriverUpdateToast(d);
            }
        }
    } catch (e) { /* silent */ }
}

// Initial status check 5s after load (lets backend finish its initial GitHub check)
setTimeout(function() {
    _pollUpdaterStatus();
    // Then poll every 60s for the rest of the session
    _updaterState.statusPollTimer = setInterval(_pollUpdaterStatus, 60000);
}, 5000);

// ═══════════════════════════════════════════════════════════════
// beta.16 — Integrity-scan finding watcher
// ═══════════════════════════════════════════════════════════════
// The integrity_scan scheduler in core/integrity_scan.py writes its
// latest actionable finding (low disk space, SMART warning, RAM hog,
// flood of service errors) into state.last_findings.  We poll status
// every 90s and surface ONE warn toast per unique finding signature
// per session.  Routine cleanup (deleted N files freed M MB) is NOT
// toasted — it lives in the history endpoint for the user to inspect
// when they want.
var _integrityWatcher = { seen: {}, timer: null };

async function _pollIntegrityFindings() {
    try {
        var s = await apiGet('/api/integrity/status');
        if (!s) return;
        var f = s.last_findings;
        if (!f || !f.summary) return;
        // De-dupe: same {kind, ts} only surfaces once.
        var key = (f.kind || '?') + '|' + (f.ts || '?');
        if (_integrityWatcher.seen[key]) return;
        _integrityWatcher.seen[key] = true;
        // Severity 'warn' fires showWarnToast; future 'critical' would
        // map to showErrorToast — for now everything is warn.
        if ((f.severity || 'warn') === 'warn') {
            showWarnToast(f.summary, {
                title:     'Health check — needs attention',
                timeoutMs: 15000,
            });
        }
    } catch (e) { /* silent — scan is best-effort */ }
}

// Kick off 30s after page load so we don't compete with the boot
// blizzard, then poll every 90s for the rest of the session.
setTimeout(function() {
    _pollIntegrityFindings();
    _integrityWatcher.timer = setInterval(_pollIntegrityFindings, 90000);
}, 30000);

// v2.9.4 — post-install toast.
// Right after an auto-updater swap, the new exe's first launch sees
// `state: 'updated'` here and shows the user a green confirmation.
// `state: 'install_failed'` shows a red warning telling them the swap
// didn't take (usually because antivirus locked the file).
async function _checkPostInstallState() {
    try {
        var r = await apiGet('/api/updater/post-install-state');
        if (!r) return;
        if (r.state === 'updated') {
            showPostInstallToast('success',
                'Updated to v' + (r.current || '?'),
                'Successfully upgraded from v' + (r.previous || '?') + '. Welcome to the new build.');
            addLog('Update installed: v' + r.previous + ' -> v' + r.current);
        } else if (r.state === 'install_failed') {
            showPostInstallToast('error',
                'Update install failed',
                'Expected v' + (r.expected || '?') + ' but still running v' + (r.current || '?') +
                '. Check that antivirus isn\'t locking the .exe, then try Install again from the update menu.');
            addLog('Update install FAILED: still v' + r.current + ' (expected v' + r.expected + ')');
        }
    } catch (e) { /* silent */ }
}

function showPostInstallToast(kind, title, msg) {
    var existing = document.getElementById('post-install-toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.id = 'post-install-toast';
    toast.className = 'post-install-toast ' + (kind === 'error' ? 'err' : 'ok');
    toast.innerHTML =
        '<div class="post-install-toast-title">' + (kind === 'error' ? '⚠ ' : '✓ ') + escHtml(title) + '</div>' +
        '<div class="post-install-toast-msg">' + escHtml(msg) + '</div>' +
        '<button class="post-install-toast-close" onclick="document.getElementById(\'post-install-toast\').remove()">×</button>';
    document.body.appendChild(toast);
    requestAnimationFrame(function(){ toast.classList.add('show'); });
    // Success toasts auto-dismiss after 8s; error toasts stay until clicked
    if (kind !== 'error') {
        setTimeout(function() {
            var t = document.getElementById('post-install-toast');
            if (t) t.classList.remove('show');
            setTimeout(function() { var t2 = document.getElementById('post-install-toast'); if (t2) t2.remove(); }, 400);
        }, 8000);
    }
}

// Run on page load (let DOM settle first so the toast can append)
setTimeout(_checkPostInstallState, 500);

// v2.9.9.2 — also check on load whether a recent GPU crash was recorded
// (e.g. user came back to GhostShell after their screen went black).
// Show the banner so the user knows what happened and that the offsets
// were already auto-reset to stock.
async function _checkRecentCrash() {
    try {
        var c = await apiGet('/api/gpu/oc/crash-state');
        if (!c || !c.crashed_ts || c.crashed_ts <= 0) return;
        // Only show if the crash was within the last 10 minutes
        var ageS = (Date.now() / 1000) - c.crashed_ts;
        if (ageS > 600) return;
        var off = c.crashed_at_offset || {};
        await _showCrashBanner('Recent GPU crash detected',
            'GPU recovered from a ' + (c.kind || 'crash') +
            ' at core+' + (off.core || 0) + ' / mem+' + (off.mem || 0) +
            ' (' + Math.round(ageS) + 's ago). ' +
            'NVAPI offsets were automatically reset to stock. ' +
            'Try a smaller offset next time.');
        // Acknowledge so we don't show it again on next reload
        apiPost('/api/gpu/oc/crash-state', {});
    } catch (e) { /* silent */ }
}
setTimeout(_checkRecentCrash, 2000);

// INIT
// ═══════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', function() {
    // v3 — split first-paint work into critical-now vs nice-to-have-later.
    // The boot splash + dashboard data are what the user sees first;
    // everything else can wait a beat so the UI feels snappier on launch.
    runBootSplash();
    loadDashboard();
    addLog('Vispora initialized');

    // Defer the secondary widgets to the next idle frame — pollLogs alone
    // won't fire for 2 s after that, well past first paint.
    requestAnimationFrame(function() {
        loadNotifierStatus();
        loadBootPrepStatus();
        var prepPollCount = 0;
        var prepPoll = setInterval(function() {
            loadBootPrepStatus();
            if (++prepPollCount > 15) clearInterval(prepPoll);
        }, 2000);
        logPollTimer = setInterval(pollLogs, 2000);

        // v3.3.1-beta.7: removed the global on-boot auto-start of
        // /api/monitor/start + toggleOcLive().  Both spawn 1-Hz
        // PowerShell / nvidia-smi subprocess loops that run forever
        // (~5 PS invocations + 1 ping per second from hw_monitor;
        // 1 nvidia-smi per second from OC live).  On a user who
        // never opens the Monitor or GPU page, this is pure waste.
        //
        // The page-switch handlers (see switchPage at line 7644 for
        // hwmon, 7670 for gpu) already auto-start the relevant
        // sampler when the user navigates to that page and stop it
        // when they leave.  That's the right place for the
        // "shouldn't have to click Start" UX, not boot.
    });
});

// ═══════════════════════════════════════════════════════════════
// BOOT PREP + NOTIFIER + POWER (dashboard widgets)
// ═══════════════════════════════════════════════════════════════
async function loadBootPrepStatus() {
    var el = document.getElementById('boot-prep-status');
    if (!el) return;
    var data = await apiGet('/api/boot-prep/status');
    var label;
    if (data.running) {
        label = '<span class="pulse" style="color:var(--accent)">Priming system... ' + (data.steps || []).length + ' steps done</span>';
    } else if (data.done) {
        var dur = Math.max(0, (data.finished_at || 0) - (data.started_at || 0)).toFixed(1);
        var okCount = (data.steps || []).filter(function(s) { return s.ok; }).length;
        label = '<span style="color:var(--accent)">✓ Ready</span> &middot; ' + okCount + '/' + (data.steps || []).length + ' tweaks in ' + dur + 's';
    } else {
        label = '<span style="color:var(--text-dim)">Not yet run this session</span>';
    }
    el.innerHTML = label;
}

async function rerunBootPrep() {
    addLog('Re-running gaming-ready prep...');
    await apiPost('/api/boot-prep/run');
    // Force a fresh status read after a beat
    setTimeout(loadBootPrepStatus, 500);
    var prepPollCount = 0;
    var prepPoll = setInterval(function() {
        loadBootPrepStatus();
        if (++prepPollCount > 15) clearInterval(prepPoll);
    }, 2000);
}

async function loadNotifierStatus() {
    var data = await apiGet('/api/notifier/status');
    var t = document.getElementById('toggle-notifier');
    if (!t) return;
    if (data.enabled) t.classList.add('on');
    else t.classList.remove('on');
}

async function toggleNotifier(el) {
    el.classList.toggle('on');
    var enabled = el.classList.contains('on');
    await apiPost('/api/notifier/toggle', { enabled: enabled });
    addLog('Notifications ' + (enabled ? 'enabled' : 'disabled'));
}

async function requestShutdown(kind) {
    var label = kind === 'shutdown' ? 'shut down' : 'restart';
    if (!confirm('This will ' + label + ' your PC after a 10-second cleanup.\n\nContinue?')) return;
    var clean = document.getElementById('toggle-power-clean').classList.contains('on');
    var url = kind === 'shutdown' ? '/api/power/shutdown' : '/api/power/restart';
    var resEl = document.getElementById('power-result');
    if (resEl) resEl.innerHTML = '<span class="pulse">Running cleanup... PC will ' + label + ' shortly.</span>';
    addLog((clean ? 'Clean ' : '') + label + ' requested');
    var r = await apiPost(url, { delay: 10, clean: clean });
    if (resEl) resEl.textContent = r.ok ? ('Scheduled: ' + label + ' in ~' + (r.delay || 10) + 's after cleanup.') : 'Failed to schedule';
}

async function cleanupOnly() {
    var resEl = document.getElementById('power-result');
    if (resEl) resEl.innerHTML = '<span class="pulse">Running cleanup...</span>';
    addLog('Running cleanup pass...');
    var r = await apiPost('/api/power/cleanup');
    if (!resEl) return;
    if (r.ok !== false) {
        var freed = r.freed_bytes ? ' — freed ' + formatSize(r.freed_bytes) : '';
        resEl.textContent = 'Cleanup done in ' + (r.elapsed_sec || 0) + 's' + freed;
        addLog('Cleanup done' + freed);
    } else {
        resEl.textContent = 'Cleanup failed';
    }
}

// ═══════════════════════════════════════════════════════════════
// KERNEL TWEAKS PAGE
// ═══════════════════════════════════════════════════════════════
var _hwmonPollTimer = null;

async function loadKernelPage() {
    loadVBSStatus();
    loadTimerStatus();
    loadISLCStatus();
    loadIRQDevices();
    loadInputDevices();
}

// ─── Input Device Priority (Kernel tab) — v3.1 grouped flow ──────────
// Three intentional surfaces:
//   1. Boost all keyboards    (button — bulk)
//   2. Boost all mice         (button — bulk)
//   3. Identify-and-boost a SINGLE controller via the HTML5 Gamepad API
//      (so we don't accidentally boost a paired-but-asleep DualSense)

var _identifiedController = null;   // { vid, pid, id } from gamepadconnected
var _gamepadPollTimer = null;

async function loadInputDevices() {
    var data = await apiGet('/api/kernel/input-devices');
    if (!data) return;
    _renderInputBucket('kb', data.keyboards || []);
    _renderInputBucket('mc', data.mice || []);
    _renderInputBucket('ct', data.controllers || []);
    _startGamepadDetection();
}

function _renderInputBucket(prefix, list) {
    var listEl  = document.getElementById(prefix + '-list');
    var countEl = document.getElementById(prefix + '-count');
    if (countEl) countEl.textContent = list.length + ' detected';
    if (!listEl) return;
    if (!list.length) {
        listEl.innerHTML = '<div style="color:var(--text-tertiary);font-style:italic">None detected.</div>';
        return;
    }
    listEl.innerHTML = list.map(function(d){
        var pri = parseInt(d.priority || 0, 10);
        var pill = pri === 3
            ? '<span style="color:var(--accent);font-weight:600;font-size:11px">✓ HIGH</span>'
            : '<span style="color:var(--text-tertiary);font-size:11px">default</span>';
        return '<div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;gap:10px">' +
                 '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escHtml(d.name || '(unnamed)') + '</span>' +
                 pill +
               '</div>';
    }).join('');
}

async function boostAllKeyboards() {
    var r = await apiPost('/api/kernel/boost-keyboards', {});
    if (!r) return;
    var n = (r.boosted || []).length;
    termWrite('kernel-terminal',
              '✓ Boosted ' + n + ' keyboard(s) to High priority — reboot required');
    if ((r.failed || []).length) {
        termWrite('kernel-terminal', '✗ ' + r.failed.length + ' failed (HKLM\\Enum write blocked?)');
    }
    setTimeout(loadInputDevices, 300);
}

async function boostAllMice() {
    var r = await apiPost('/api/kernel/boost-mice', {});
    if (!r) return;
    var n = (r.boosted || []).length;
    termWrite('kernel-terminal',
              '✓ Boosted ' + n + ' mouse/mice to High priority — reboot required');
    if ((r.failed || []).length) {
        termWrite('kernel-terminal', '✗ ' + r.failed.length + ' failed (HKLM\\Enum write blocked?)');
    }
    setTimeout(loadInputDevices, 300);
}

// ─── Controller identification via HTML5 Gamepad API ──────────────────
// On gamepadconnected we don't yet know which controller the USER is
// holding (could be 2 paired pads in standby).  So we poll button state
// and the FIRST controller to register a button press wins.
function _startGamepadDetection() {
    if (_gamepadPollTimer) return;     // already polling
    _gamepadPollTimer = setInterval(_pollGamepads, 100);   // 10Hz
    _refreshGamepadDetectList();
}

function _stopGamepadDetection() {
    if (_gamepadPollTimer) {
        clearInterval(_gamepadPollTimer);
        _gamepadPollTimer = null;
    }
}

function _refreshGamepadDetectList() {
    var listEl = document.getElementById('ct-detect-list');
    if (!listEl) return;
    var pads = navigator.getGamepads ? navigator.getGamepads() : [];
    var connected = [];
    for (var i = 0; i < pads.length; i++) {
        if (pads[i]) connected.push(pads[i]);
    }
    if (!connected.length) {
        listEl.textContent = 'No controllers connected to the system.';
    } else {
        listEl.innerHTML = '<b>Paired:</b> ' + connected.map(function(p){
            return escHtml((p.id || 'Unknown').split('(')[0].trim()) + ' [slot ' + p.index + ']';
        }).join(', ');
    }
}

function _pollGamepads() {
    // v3.3.0-beta.5: this fires at 10 Hz and was running globally
    // forever — it never stopped after leaving the Kernel page.  On a
    // Ryzen 5 / RX 6600 class machine that 100 ms cadence × Gamepad API
    // read × paired-list re-render was a visible chunk of the perceived
    // UI lag.  Cheap gate: do nothing unless the user is actually
    // looking at the Kernel page.
    if (typeof currentPage !== 'undefined' && currentPage !== 'kernel') return;
    if (_pollSkipIfHidden()) return;
    if (_identifiedController) {
        // Already locked in — just refresh paired list occasionally
        _refreshGamepadDetectList();
        return;
    }
    var pads = navigator.getGamepads ? navigator.getGamepads() : [];
    for (var i = 0; i < pads.length; i++) {
        var p = pads[i];
        if (!p) continue;
        // Detect any button press
        var buttonPressed = false;
        for (var b = 0; b < p.buttons.length; b++) {
            if (p.buttons[b].pressed) { buttonPressed = true; break; }
        }
        // Or significant axis movement (>0.5 / <-0.5 — past the deadzone)
        if (!buttonPressed) {
            for (var a = 0; a < p.axes.length; a++) {
                if (Math.abs(p.axes[a]) > 0.5) { buttonPressed = true; break; }
            }
        }
        if (buttonPressed) {
            // Parse VID/PID from id string.  Chrome format:
            //   "Xbox 360 Controller (Vendor: 045e Product: 028e)"
            //   "Wireless Controller (Vendor: 054c Product: 0ce6)"
            var idStr = p.id || '';
            var m = idStr.match(/Vendor:\s*([0-9a-fA-F]{4})\s+Product:\s*([0-9a-fA-F]{4})/);
            var vid = m ? m[1] : null;
            var pid = m ? m[2] : null;
            // Some controllers (DualSense over BT, Xbox via Gaming Services)
            // expose hex without the "Vendor:" prefix.  Try shorter match.
            if (!vid) {
                var m2 = idStr.match(/([0-9a-fA-F]{4})-([0-9a-fA-F]{4})/);
                if (m2) { vid = m2[1]; pid = m2[2]; }
            }
            if (!vid || !pid) {
                // We can still let the user proceed if we got nothing —
                // the backend will fall back to first-controller match.
                console.warn('Gamepad id has no VID/PID:', idStr);
            }
            _identifiedController = {
                vid: vid, pid: pid,
                id: idStr,
                index: p.index,
                short: idStr.split('(')[0].trim() || ('Slot ' + p.index),
            };
            _renderControllerIdentified();
            return;
        }
    }
    // Update the paired-list every ~2s
    _refreshGamepadDetectList();
}

function _renderControllerIdentified() {
    var msgEl = document.getElementById('ct-detect-msg');
    var btnEl = document.getElementById('ct-boost-btn');
    if (!_identifiedController) {
        if (msgEl) msgEl.textContent = 'Connect a controller and press any button…';
        if (btnEl) btnEl.disabled = true;
        return;
    }
    var c = _identifiedController;
    if (msgEl) {
        msgEl.innerHTML = '<b style="color:var(--accent)">Identified:</b> ' +
            escHtml(c.short) +
            (c.vid && c.pid
                ? ' <span style="color:var(--text-tertiary);font-size:11px">(VID ' + c.vid + ' / PID ' + c.pid + ')</span>'
                : '');
    }
    if (btnEl) btnEl.disabled = false;
}

function resetIdentifiedController() {
    _identifiedController = null;
    _renderControllerIdentified();
}

async function boostIdentifiedController() {
    if (!_identifiedController) {
        showWarnToast('No controller identified yet — press a button on the controller you want to boost.');
        return;
    }

    // ── Build the request body.  Three fallback tiers:
    //   1. VID + PID parsed from gamepad.id (desktop Chrome style)
    //   2. instance_id matched by name against the backend's controller list
    //      (pywebview's Chromium often returns just the friendly name)
    //   3. Try sending vid/pid even if they came from the looser fallback regex
    var body = null;

    if (_identifiedController.vid && _identifiedController.pid) {
        body = { vid: _identifiedController.vid, pid: _identifiedController.pid };
    } else {
        // Fallback: match the gamepad's friendly name against the
        // backend's controllers list and use the instance_id directly.
        var devices = await apiGet('/api/kernel/input-devices');
        var ctrls = (devices && devices.controllers) || [];
        var hint = (_identifiedController.short || '').toLowerCase().trim();
        var match = null;
        if (hint) {
            // Prefer exact (case-insensitive) match
            match = ctrls.find(function(c){
                return (c.name || '').toLowerCase().trim() === hint;
            });
            // Then partial — gamepad.id might be shorter or longer than the
            // PnP friendly name (e.g. "Xbox 360 Controller for Windows"
            // vs gamepad.id "Xbox Controller").
            if (!match) {
                match = ctrls.find(function(c){
                    var n = (c.name || '').toLowerCase();
                    return n && (n.indexOf(hint) >= 0 || hint.indexOf(n) >= 0);
                });
            }
        }
        // Last resort: if there's only one controller in the system, just use it.
        if (!match && ctrls.length === 1) {
            match = ctrls[0];
        }
        if (match) {
            body = { instance_id: match.instance_id };
        } else {
            showErrorToast('Could not match the identified controller to any device in the system list. ' +
                  'Identified: ' + _identifiedController.short + '. ' +
                  'System sees: ' + (ctrls.map(function(c){ return c.name; }).join(', ') || '(none)'));
            return;
        }
    }

    var r = await apiPost('/api/kernel/boost-controller', body);
    if (!r) return;
    if (r.ok) {
        var n = (r.boosted || []).length;
        termWrite('kernel-terminal',
                  '✓ Boosted controller "' + _identifiedController.short +
                  '" (' + n + ' interface' + (n === 1 ? '' : 's') + ') — reboot required');
    } else {
        termWrite('kernel-terminal', '✗ Boost failed: ' + (r.err || 'unknown'));
    }
    setTimeout(loadInputDevices, 300);
}

async function resetAllInputPriority() {
    if (!confirm('Reset interrupt priority on EVERY keyboard, mouse, and controller back to Windows default?\n\nReboot required.')) return;
    var r = await apiPost('/api/kernel/reset-input-priorities', {});
    if (!r) return;
    var nk = (r.keyboards && r.keyboards.cleared || []).length;
    var nm = (r.mice && r.mice.cleared || []).length;
    var nc = (r.controllers && r.controllers.cleared || []).length;
    termWrite('kernel-terminal',
              '✓ Reset priorities — keyboards: ' + nk + ', mice: ' + nm +
              ', controllers: ' + nc + ' — reboot required');
    setTimeout(loadInputDevices, 300);
}

async function loadVBSStatus() {
    var data = await apiGet('/api/vbs/status');
    var el = document.getElementById('vbs-status');
    if (!el) return;
    var vbs = data.vbs_running ? '<span class="status-badge danger">VBS Running</span>' : '<span class="status-badge ok">VBS Off</span>';
    var hvci = data.hvci_running ? '<span class="status-badge danger">HVCI Running</span>' : '<span class="status-badge ok">HVCI Off</span>';
    el.innerHTML = vbs + ' ' + hvci + ' <span style="font-size:10px;color:var(--text-dim)">Est. impact if enabled: ' + escHtml(data.estimated_impact || '?') + '</span>';
}

async function disableVBS() {
    termWrite('kernel-terminal', 'Disabling VBS + HVCI...');
    var r = await apiPost('/api/vbs/disable-all');
    if (r.results) {
        r.results.forEach(function(x) { termWrite('kernel-terminal', '  ' + (x.ok ? '✓' : '✗') + ' ' + x.name); });
    }
    termWrite('kernel-terminal', r.ok ? '✓ VBS disabled — REBOOT REQUIRED' : '✗ Failed');
    loadVBSStatus();
}

async function enableVBS() {
    var r = await apiPost('/api/vbs/enable-all');
    termWrite('kernel-terminal', r.ok ? '✓ VBS re-enabled — reboot required' : '✗ Failed');
    loadVBSStatus();
}

async function loadTimerStatus() {
    var data = await apiGet('/api/timer/status');
    var el = document.getElementById('timer-status');
    if (!el) return;
    var dtick = data.dynamic_tick === false ? '<span class="status-badge ok">Dynamic Tick Off</span>' : '<span class="status-badge warn">Dynamic Tick On</span>';
    var hpet = data.hpet_enabled === false ? '<span class="status-badge ok">HPET Off</span>' : '<span class="status-badge warn">HPET On</span>';
    el.innerHTML = dtick + ' ' + hpet;

    if (data.memory) {
        var mb = document.getElementById('memory-bars');
        if (mb) {
            mb.innerHTML = '<div style="font-size:10px;color:var(--text-dim)">Free: ' + (data.memory.free_mb || 0) + 'MB • Standby: ' + (data.memory.standby_mb || 0) + 'MB</div>';
        }
    }
}

async function applyTimerTweaks() {
    termWrite('kernel-terminal', 'Applying timer tweaks...');
    var r = await apiPost('/api/timer/apply');
    (r.results || []).forEach(function(x) { termWrite('kernel-terminal', '  ' + (x.ok ? '✓' : '✗') + ' ' + x.name); });
    termWrite('kernel-terminal', '═══ Timer tweaks applied — reboot recommended ═══');
    loadTimerStatus();
}

async function restoreTimerDefaults() {
    var r = await apiPost('/api/timer/restore');
    (r.results || []).forEach(function(x) { termWrite('kernel-terminal', '  ' + (x.ok ? '✓' : '✗') + ' ' + x.name); });
    loadTimerStatus();
}

var _islcRunning = false;
async function loadISLCStatus() {
    var data = await apiGet('/api/timer/status');
    if (data.islc) {
        _islcRunning = data.islc.running;
        var el = document.getElementById('islc-status');
        if (el) {
            el.innerHTML = _islcRunning
                ? '<span class="status-badge ok">ISLC Running</span> Clears: ' + data.islc.total_clears
                : '<span class="status-badge neutral">ISLC Stopped</span>';
        }
        var btn = document.getElementById('btn-islc');
        if (btn) btn.textContent = _islcRunning ? 'Stop ISLC' : 'Start ISLC';
    }
}

async function toggleISLC() {
    if (_islcRunning) {
        await apiPost('/api/timer/islc/stop');
        termWrite('kernel-terminal', 'ISLC stopped');
    } else {
        await apiPost('/api/timer/islc/start', { free_threshold: 1024, standby_threshold: 1024, interval: 10 });
        termWrite('kernel-terminal', 'ISLC started (free<1024MB & standby>1024MB)');
    }
    _islcRunning = !_islcRunning;
    loadISLCStatus();
}

async function runSchedulerOptimize() {
    termWrite('kernel-terminal', 'Applying scheduler optimizations...');
    var r = await apiPost('/api/scheduler/run');
    Object.keys(r).forEach(function(cat) {
        var val = r[cat];
        if (Array.isArray(val)) {
            val.forEach(function(x) { termWrite('kernel-terminal', '  ' + (x.ok !== false ? '✓' : '✗') + ' ' + (x.name || '')); });
        } else if (val && val.ok !== undefined) {
            termWrite('kernel-terminal', '  ' + (val.ok ? '✓' : '✗') + ' ' + cat);
        }
    });
    termWrite('kernel-terminal', '═══ Scheduler optimization complete ═══');
}

function toggleCpuIdle(el) {
    el.classList.toggle('on');
    var disable = el.classList.contains('on');
    apiPost('/api/scheduler/cpu-idle', { disable: disable }).then(function(r) {
        termWrite('kernel-terminal', r.ok ? '✓ CPU idle ' + (disable ? 'disabled' : 'enabled') : '✗ Failed');
    });
}

function toggleSpectre(el) {
    el.classList.toggle('on');
    var disable = el.classList.contains('on');
    apiPost('/api/scheduler/spectre', { disable: disable }).then(function(r) {
        termWrite('kernel-terminal', r.ok ? '✓ Spectre/Meltdown mitigations ' + (disable ? 'disabled' : 'enabled') + ' — reboot required' : '✗ Failed');
    });
}

async function loadIRQDevices() {
    var el = document.getElementById('irq-devices');
    if (!el) return;
    el.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Scanning PCI devices...</span>';
    var data = await apiGet('/api/irq/devices');
    var devs = data.devices || [];
    if (devs.length === 0) {
        el.innerHTML = '<span style="color:var(--text-dim)">No PCI devices found</span>';
        return;
    }
    var html = '';
    devs.forEach(function(d) {
        var msi = d.msi_enabled ? '<span class="status-badge ok">MSI</span>' : '<span class="status-badge neutral">Line</span>';
        html += '<div class="check-item"><span style="flex:1;font-size:11px">' + escHtml(d.name || 'Unknown') + '</span>' + msi + '<span class="check-cat">' + escHtml(d.class || '') + '</span></div>';
    });
    el.innerHTML = html;
}

async function autoOptimizeIRQ() {
    termWrite('kernel-terminal', 'Auto-optimizing interrupts...');
    var r = await apiPost('/api/irq/auto-optimize');
    (r.results || []).forEach(function(x) {
        termWrite('kernel-terminal', '  ' + (x.ok ? '✓' : '✗') + ' ' + (x.name || ''));
    });
    termWrite('kernel-terminal', '═══ Interrupt optimization complete — reboot required ═══');
    loadIRQDevices();
}

// ═══════════════════════════════════════════════════════════════
// GAME PROFILES PAGE
// ═══════════════════════════════════════════════════════════════
var _profileEngineRunning = false;
var _profilePollTimer = null;

async function loadProfilesPage() {
    loadProfileEngineStatus();
    loadCustomGames();
    loadAdaptiveSettings();
    loadAdaptiveGames();
    loadAdaptiveHistory();
    loadLibrary();
    loadPauser();
    loadGamepadMapper();
    _startAdaptivePoll();
}

// ═══ Gamepad Mapper (Game Profiles tab) ═══
// Common VK codes the user is likely to bind.  More can be typed in the
// raw "VK code" input but this gives a clean dropdown for the common case.
var GPM_KEY_OPTIONS = [
    {label: 'A',     vk: 0x41},  {label: 'B', vk: 0x42},  {label: 'C', vk: 0x43},
    {label: 'D',     vk: 0x44},  {label: 'E', vk: 0x45},  {label: 'F', vk: 0x46},
    {label: 'G',     vk: 0x47},  {label: 'H', vk: 0x48},  {label: 'I', vk: 0x49},
    {label: 'J',     vk: 0x4A},  {label: 'K', vk: 0x4B},  {label: 'L', vk: 0x4C},
    {label: 'M',     vk: 0x4D},  {label: 'N', vk: 0x4E},  {label: 'O', vk: 0x4F},
    {label: 'P',     vk: 0x50},  {label: 'Q', vk: 0x51},  {label: 'R', vk: 0x52},
    {label: 'S',     vk: 0x53},  {label: 'T', vk: 0x54},  {label: 'U', vk: 0x55},
    {label: 'V',     vk: 0x56},  {label: 'W', vk: 0x57},  {label: 'X', vk: 0x58},
    {label: 'Y',     vk: 0x59},  {label: 'Z', vk: 0x5A},
    {label: '0', vk: 0x30}, {label: '1', vk: 0x31}, {label: '2', vk: 0x32},
    {label: '3', vk: 0x33}, {label: '4', vk: 0x34}, {label: '5', vk: 0x35},
    {label: '6', vk: 0x36}, {label: '7', vk: 0x37}, {label: '8', vk: 0x38},
    {label: '9', vk: 0x39},
    {label: 'F1',  vk: 0x70}, {label: 'F2',  vk: 0x71}, {label: 'F3',  vk: 0x72},
    {label: 'F4',  vk: 0x73}, {label: 'F5',  vk: 0x74}, {label: 'F6',  vk: 0x75},
    {label: 'F7',  vk: 0x76}, {label: 'F8',  vk: 0x77}, {label: 'F9',  vk: 0x78},
    {label: 'F10', vk: 0x79}, {label: 'F11', vk: 0x7A}, {label: 'F12', vk: 0x7B},
    {label: 'Space',  vk: 0x20}, {label: 'Enter',  vk: 0x0D},
    {label: 'Shift',  vk: 0x10}, {label: 'Ctrl',   vk: 0x11},
    {label: 'Alt',    vk: 0x12}, {label: 'Tab',    vk: 0x09},
    {label: 'Esc',    vk: 0x1B}, {label: 'Backspace', vk: 0x08},
    {label: '↑',  vk: 0x26}, {label: '↓', vk: 0x28},
    {label: '←', vk: 0x25}, {label: '→', vk: 0x27},
    {label: 'Home', vk: 0x24}, {label: 'End', vk: 0x23},
    {label: 'PgUp', vk: 0x21}, {label: 'PgDn', vk: 0x22},
];

var _gpmState = null;
var _gpmCurrentProfile = null;

// beta.12 — normalize whatever profile shape comes back from the backend
// so the click handlers can never bail with "Cannot read properties of
// undefined (reading 'DPAD_UP' / 'RB' / …)".  Old state.json files written
// by pre-3.4 builds, or partial profiles created before the user touched
// any binding, can be missing `buttons` / `alt_mappings` / `sticks` etc.
// Call this immediately after every `_gpmCurrentProfile = …` assignment.
function _gpmNormalizeProfile(p) {
    if (!p || typeof p !== 'object') return p;
    if (!p.buttons       || typeof p.buttons       !== 'object') p.buttons       = {};
    if (!p.sticks        || typeof p.sticks        !== 'object') p.sticks        = {};
    if (!p.triggers      || typeof p.triggers      !== 'object') p.triggers      = {};
    if (!Array.isArray(p.alt_mappings))                          p.alt_mappings  = [];
    if (typeof p.remap_mode !== 'string')                         p.remap_mode   = 'key_emulation';
    return p;
}
var _gpmCaptureCancelled = false;
var _gpmMacroEditorBtn = null;
var _gpmMacroEditorSteps = [];

function _vkLabel(vk) {
    var found = GPM_KEY_OPTIONS.find(function(o){ return o.vk === vk; });
    return found ? found.label : ('VK 0x' + vk.toString(16).toUpperCase());
}

async function loadGamepadMapper() {
    var statusLine = document.getElementById('gpm-status-line');
    var data = await apiGet('/api/gamepad/status');
    if (!data) {
        if (statusLine) statusLine.textContent = 'Failed to load gamepad mapper status.';
        return;
    }
    _gpmState = data;

    // Master toggle
    var t = document.getElementById('toggle-gpm-master');
    if (t) {
        if (data.enabled) t.classList.add('on');
        else t.classList.remove('on');
    }

    // Profile select
    var sel = document.getElementById('gpm-profile-select');
    if (sel) {
        var prev = data.active_profile || '';
        sel.innerHTML = '<option value="">— none —</option>' +
            (data.profiles || []).map(function(name){
                return '<option value="' + escAttr(name) + '"' + (name === prev ? ' selected' : '') + '>' + escHtml(name) + '</option>';
            }).join('');
    }

    // Live poll-rate display
    var hzEl  = document.getElementById('gpm-actual-hz');
    var usefulEl = document.getElementById('gpm-useful-hz');
    var p99El = document.getElementById('gpm-p99');
    if (hzEl)  hzEl.textContent  = data.actual_hz ? (data.actual_hz.toFixed(0) + ' / ' + data.target_hz + ' Hz') : (data.target_hz + ' Hz target');
    if (usefulEl) usefulEl.textContent = (data.useful_hz || 0).toFixed(0) + ' Hz';
    if (p99El) p99El.textContent = data.frame_p99_us ? (data.frame_p99_us + ' µs (max ' + data.frame_max_us + ' µs)') : '—';

    // Status line
    if (statusLine) {
        var slots = data.connected_slots || [];
        var slotsTxt = slots.length
            ? slots.length + ' controller' + (slots.length === 1 ? '' : 's') + ' connected (slot ' + slots.join(', ') + ')'
            : 'No controllers connected via XInput';
        var profCount = (data.profiles || []).length;
        statusLine.innerHTML = slotsTxt + ' · <b>' + profCount + '</b> profile' + (profCount === 1 ? '' : 's') + ' saved' +
            (data.running ? ' · <span style="color:var(--accent)">poll loop active</span>' : ' · <span style="color:var(--text-tertiary)">poll loop stopped</span>');
    }

    // Render bindings if a profile is selected
    if (data.active_profile) {
        await loadGpmProfileBindings(data.active_profile);
    } else {
        var b = document.getElementById('gpm-bindings');
        if (b) b.style.display = 'none';
    }

    // Visual controller overlay — start the 60 Hz loop + wire clicks
    startGpmSvgLoop();
    _gpmInitSvgClicks();

    // ViGEmBus install banner (shown only when a virtual_controller profile
    // is active but the driver isn't installed)
    _refreshGpmInstallBanner();
}

async function _refreshGpmInstallBanner() {
    var vBanner = document.getElementById('gpm-vigembus-banner');
    var hBanner = document.getElementById('gpm-hidhide-banner');
    var prof = _gpmCurrentProfile;

    // ── ViGEmBus banner (shown when virtual mode active + driver missing)
    var needsViGEm = prof && prof.remap_mode === 'virtual_controller';
    if (vBanner) {
        if (!needsViGEm) {
            vBanner.style.display = 'none';
        } else {
            var inst = await apiGet('/api/gamepad/install-status');
            if (inst && inst.available) {
                vBanner.style.display = 'none';
            } else {
                vBanner.style.display = 'block';
                var link = document.getElementById('gpm-vigembus-link');
                if (link && inst) {
                    link.href = inst.install_url || '#';
                    link.target = '_blank';
                    link.rel = 'noopener noreferrer';
                }
            }
        }
    }

    // ── HidHide banner (shown when hide_physical=true + driver missing)
    var needsHide = prof && prof.hide_physical;
    if (hBanner) {
        if (!needsHide) {
            hBanner.style.display = 'none';
        } else {
            var hStatus = await apiGet('/api/gamepad/hidhide-status');
            if (hStatus && hStatus.installed) {
                hBanner.style.display = 'none';
            } else {
                hBanner.style.display = 'block';
                var hLink = document.getElementById('gpm-hidhide-link');
                if (hLink && hStatus) {
                    hLink.href = hStatus.install_url || '#';
                    hLink.target = '_blank';
                    hLink.rel = 'noopener noreferrer';
                }
            }
        }
    }
}

async function loadGpmProfileBindings(name) {
    // Fetch from backend (used on initial load + after profile-list changes
    // like delete/create).  After local edits, use _gpmRenderBindingsLocal()
    // instead so we don't race autosave and clobber unsaved fields.
    var data = await apiGet('/api/gamepad/profiles');
    var profile = (data && data.profiles && data.profiles[name]) || null;
    _gpmCurrentProfile = _gpmNormalizeProfile(profile);
    _gpmRenderBindingsLocal();
}

function _gpmRenderBindingsLocal() {
    // Render the bindings UI purely from `_gpmCurrentProfile` (the
    // in-memory copy).  Used after every on-change handler so the user
    // sees their edit reflected without a backend round-trip.
    var profile = _gpmCurrentProfile;
    var b = document.getElementById('gpm-bindings');
    if (!profile) {
        if (b) b.style.display = 'none';
        return;
    }
    if (b) b.style.display = 'block';

    // Remap-mode selector
    var ms = document.getElementById('gpm-remap-mode');
    if (ms) ms.value = profile.remap_mode || 'key_emulation';

    // Poll-rate selector
    var phz = document.getElementById('gpm-poll-hz');
    if (phz) phz.value = String(profile.poll_hz || 4000);

    // Virtual pad type selector — only meaningful in virtual_controller mode
    var ptWrap = document.getElementById('gpm-pad-type-wrap');
    var pt     = document.getElementById('gpm-pad-type');
    if (ptWrap) {
        ptWrap.style.display = (profile.remap_mode === 'virtual_controller') ? 'flex' : 'none';
    }
    if (pt) pt.value = profile.virtual_pad_type || 'x360';

    // HidHide status — only relevant in virtual_controller mode.
    // Format depends on engagement state:
    //   green ✓ engaged          → physical hidden, game sees only virtual
    //   amber installed-not-engaged → no virtual mode active
    //   amber install-needed     → driver missing
    //   neutral key-emulation    → not relevant
    _updateHidHideStatusBadge();

    // v3.4 mappings table — one row per mapping.  v3.5: which layer
    // is shown depends on whether the user clicked "Edit alt layer".
    var rowsEl = document.getElementById('gpm-binding-rows');
    if (rowsEl) {
        var mappings = _gpmEditingAltLayer
            ? (profile.alt_mappings || [])
            : (profile.mappings || []);
        var emptyMsg = _gpmEditingAltLayer
            ? 'No alt-layer mappings yet. These activate while you hold the modifier input. Click <b>+ Add mapping</b>.'
            : 'No mappings yet. Click <b>+ Add mapping</b> to create one.';
        if (!mappings.length) {
            rowsEl.innerHTML =
                '<div style="color:var(--text-tertiary);padding:14px;font-style:italic;text-align:center">' +
                emptyMsg + '</div>';
        } else {
            rowsEl.innerHTML = mappings.map(_renderMappingRow).join('');
        }
    }

    _refreshGpmInstallBanner();
    _gpmRenderSvgLabels();
    _refreshAdvancedDisplay();
}

function onGpmRemapModeChange() {
    if (!_gpmCurrentProfile) return;
    var ms = document.getElementById('gpm-remap-mode');
    if (!ms) return;
    _gpmCurrentProfile.remap_mode = ms.value;
    // v3.4 — virtual_controller mode now AUTO-ENGAGES HidHide.
    _gpmCurrentProfile.hide_physical = (ms.value === 'virtual_controller');
    // Default to Xbox 360 the first time virtual mode is picked
    if (ms.value === 'virtual_controller' && !_gpmCurrentProfile.virtual_pad_type) {
        _gpmCurrentProfile.virtual_pad_type = 'x360';
    }
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
    _refreshGpmInstallBanner();
    _updateHidHideStatusBadge();
    // Show/hide the pad-type picker
    var ptWrap = document.getElementById('gpm-pad-type-wrap');
    if (ptWrap) ptWrap.style.display = (ms.value === 'virtual_controller') ? 'flex' : 'none';
}

function onGpmPollHzChange() {
    if (!_gpmCurrentProfile) return;
    var sel = document.getElementById('gpm-poll-hz');
    if (!sel) return;
    _gpmCurrentProfile.poll_hz = parseInt(sel.value, 10) || 4000;
    _gpmAutoSave();
}

function onGpmPadTypeChange() {
    if (!_gpmCurrentProfile) return;
    var sel = document.getElementById('gpm-pad-type');
    if (!sel) return;
    _gpmCurrentProfile.virtual_pad_type = sel.value;
    _gpmAutoSave();
    // The mapper watches profile.virtual_pad_type each tick and recreates
    // the virtual pad when it changes.  HidHide stays engaged through
    // the swap.  Status badge reflects the new pad type within ~1 sec.
    setTimeout(_updateHidHideStatusBadge, 200);
}

// HidHide status badge — small inline indicator on the bindings card.
// Replaces the v3.3 "Hide physical from games" checkbox; in v3.4 we
// auto-engage HidHide whenever a virtual_controller profile is active.
async function _updateHidHideStatusBadge() {
    var el = document.getElementById('gpm-hidhide-status');
    if (!el) return;
    var prof = _gpmCurrentProfile;
    if (!prof || prof.remap_mode !== 'virtual_controller') {
        el.innerHTML = '';
        return;
    }
    var st = await apiGet('/api/gamepad/hidhide-status');
    var live = await apiGet('/api/gamepad/status');
    var engaged = live && live.hidhide_engaged;
    var padTypeLabel = '';
    if (live && live.current_pad_type) {
        padTypeLabel = ' · pad: ' + (live.current_pad_type === 'ds4' ? 'DualShock 4' : 'Xbox 360');
    }
    var diagLink = ' · <a href="#" onclick="showHidHideDiagnostic();return false" ' +
        'style="color:var(--text-tertiary);text-decoration:underline;cursor:pointer;font-size:11px" ' +
        'title="See every controller-class device on the system. Useful for finding interfering virtual gamepad layers (Razer Synapse, Steam Input, DS4Windows, etc.)">' +
        'show devices</a>';
    if (st && st.installed) {
        if (engaged) {
            var hiddenN = (st.hidden_count != null ? st.hidden_count : 0);
            var hiddenLabel = ' · ' + hiddenN + ' device' + (hiddenN === 1 ? '' : 's') + ' hidden';
            el.innerHTML = '<span style="color:var(--accent)">● HidHide engaged' + hiddenLabel + padTypeLabel + '</span>' + diagLink;
        } else {
            // Click → force retry.  Surface last error if there was one.
            var errDetail = (st.last_engage_err) ? ' (' + escHtml(st.last_engage_err.slice(0, 60)) + ')' : '';
            el.innerHTML = '<a href="#" onclick="forceEngageHidHide();return false" ' +
                'title="Click to retry HidHide engagement now" ' +
                'style="color:var(--orange,#fbbf24);text-decoration:underline;cursor:pointer">' +
                '○ HidHide installed but not engaged — click to retry' + padTypeLabel + errDetail + '</a>' + diagLink;
        }
    } else {
        el.innerHTML = '<span style="color:var(--orange,#fbbf24)">⚠ HidHide not installed — game may see both pads</span>' + diagLink;
    }
}

async function showHidHideDiagnostic() {
    var d = await apiGet('/api/gamepad/hidhide-diagnostic');
    if (!d || !d.ok) {
        showErrorToast('Could not load device diagnostic: ' + ((d && d.err) || 'unknown'));
        return;
    }
    var devs = d.devices || [];
    var lines = devs.map(function(x){
        var icon = x.is_vigembus ? '✓' : (x.will_hide ? '◉' : '○');
        var note = x.is_vigembus ? 'Vispora virtual (kept visible)'
                  : x.will_hide ? 'Will be hidden from games'
                  : 'Not currently in hide list';
        var vidPid = (x.vid && x.pid) ? (' [VID_' + x.vid + ' PID_' + x.pid + ']') : '';
        return icon + '  ' + (x.name || '(unnamed)') + vidPid + '\n     ' + note;
    });
    var msg =
        'Controller-class devices Windows currently sees:\n\n' +
        (lines.length ? lines.join('\n\n') : '(none — connect a controller and try again)') +
        '\n\n──────────────────────────────────────────\n' +
        '✓ = our ViGEmBus virtual pad (kept visible)\n' +
        '◉ = will be hidden when HidHide engages\n' +
        '○ = not in hide list (rare — usually means the device looked safe to leave visible)\n\n' +
        'If you see your real controller plus a "Razer Virtual XInput Game Pad" or similar,\n' +
        'they all need to be hidden so the game only sees ours.  Re-engage HidHide if not yet engaged.';
    showInfoToast(msg);
}

async function forceEngageHidHide() {
    var el = document.getElementById('gpm-hidhide-status');
    if (el) el.innerHTML = '<span style="color:var(--text-tertiary)">Engaging HidHide…</span>';
    var r = await apiPost('/api/gamepad/hidhide-engage', {});
    if (r && r.ok && r.engaged) {
        // Re-render to show the green engaged state
        _updateHidHideStatusBadge();
    } else {
        var msg = (r && r.err) ? r.err : 'Engagement failed for an unknown reason';
        showErrorToast('HidHide engagement failed: ' + msg +
              '. Common causes: HidHide CLI missing (reinstall HidHide), ' +
              'controller not detected (reconnect), or Vispora not running as admin.',
              { timeoutMs: 12000 });
        _updateHidHideStatusBadge();
    }
}

async function testVirtualPad() {
    var r = await apiPost('/api/gamepad/test-virtual-pad', {});
    if (!r || !r.ok) {
        showErrorToast('Could not start test: ' + (r && r.err || 'unknown'));
        return;
    }
    showInfoToast(r.msg || 'Test sequence started.');
}

// ─── v3.5 advanced settings — stick tuning ──────────────────────────
var _gpmEditingAltLayer = false;   // toggled by toggleAltLayer()

function _renderStickTuningPanel() {
    var wrap = document.getElementById('gpm-stick-tuning');
    if (!wrap) return;
    var prof = _gpmCurrentProfile;
    if (!prof) { wrap.innerHTML = ''; return; }
    var sticks = prof.sticks || {};
    var swap = document.getElementById('gpm-stick-swap');
    if (swap) swap.checked = !!sticks.swap;
    var sides = ['left', 'right'];
    var html = sides.map(function(side){
        var c = sticks[side] || {};
        var cur = c.curve || 'linear';
        var curveOpts = ['linear','power','expo','s_curve'].map(function(k){
            return '<option value="' + k + '"' + (cur === k ? ' selected' : '') + '>' + k + '</option>';
        }).join('');
        return '<div style="min-width:240px;border:1px solid var(--border-faint);border-radius:6px;padding:10px;background:var(--bg-elevated)">' +
            '<div style="font-weight:600;color:var(--text-bright);margin-bottom:8px;text-transform:capitalize">' + side + ' stick</div>' +
            _stickField(side, 'deadzone_in',    'Inner DZ',     c.deadzone_in   || 0,   '0..0.95, fraction near center treated as 0',         0,    0.95, 0.01) +
            _stickField(side, 'deadzone_out',   'Outer DZ',     c.deadzone_out  || 1,   '0..1, where the stick is treated as fully pushed',   0.05, 1.0,  0.01) +
            _stickField(side, 'curve_strength', 'Curve N',      c.curve_strength|| 1.5, 'Exponent / steepness for power & expo curves',       0.1,  5.0,  0.05) +
            '<label style="display:block;margin-top:6px"><span style="display:inline-block;min-width:80px">Curve</span>' +
                '<select onchange="onStickCfgChangeField(\'' + side + '\', \'curve\', this.value)" style="padding:3px 8px;border-radius:4px;background:var(--bg-card,var(--bg-overlay));color:var(--text);border:1px solid var(--border-faint);font-size:12px">' + curveOpts + '</select>' +
            '</label>' +
            '<label style="display:block;margin-top:6px"><input type="checkbox" onchange="onStickCfgChangeField(\'' + side + '\', \'invert_x\', this.checked)"' + (c.invert_x ? ' checked' : '') + ' /> Invert X</label>' +
            '<label style="display:block;margin-top:2px"><input type="checkbox" onchange="onStickCfgChangeField(\'' + side + '\', \'invert_y\', this.checked)"' + (c.invert_y ? ' checked' : '') + ' /> Invert Y</label>' +
            '<label style="display:block;margin-top:2px"><input type="checkbox" onchange="onStickCfgChangeField(\'' + side + '\', \'circular_gate\', this.checked)"' + (c.circular_gate ? ' checked' : '') + ' /> Circular gate</label>' +
        '</div>';
    }).join('');
    wrap.innerHTML = html;
}

function _stickField(side, key, label, val, tip, min, max, step) {
    return '<label style="display:flex;align-items:center;gap:6px;margin-bottom:4px" title="' + escAttr(tip) + '">' +
        '<span style="display:inline-block;min-width:80px">' + label + '</span>' +
        '<input type="number" step="' + step + '" min="' + min + '" max="' + max + '" value="' + val + '" ' +
            'onchange="onStickCfgChangeField(\'' + side + '\', \'' + key + '\', parseFloat(this.value))" ' +
            'style="width:75px;padding:3px 6px;border-radius:4px;background:var(--bg-card,var(--bg-overlay));color:var(--text);border:1px solid var(--border-faint);font-size:12px" />' +
    '</label>';
}

function onStickCfgChange() {
    if (!_gpmCurrentProfile) return;
    _gpmCurrentProfile.sticks = _gpmCurrentProfile.sticks || {};
    var swap = document.getElementById('gpm-stick-swap');
    if (swap) _gpmCurrentProfile.sticks.swap = !!swap.checked;
    _gpmAutoSave();
}

function onStickCfgChangeField(side, key, val) {
    if (!_gpmCurrentProfile) return;
    _gpmCurrentProfile.sticks = _gpmCurrentProfile.sticks || {};
    _gpmCurrentProfile.sticks[side] = _gpmCurrentProfile.sticks[side] || {};
    _gpmCurrentProfile.sticks[side][key] = val;
    _gpmAutoSave();
}

async function calibrateSticksAtRest() {
    var status = document.getElementById('gpm-stick-calibrate-status');
    if (status) status.innerHTML = '<span style="color:var(--orange,#fbbf24)">Don\'t touch the sticks for 2 sec…</span>';
    var r = await apiPost('/api/gamepad/calibrate-sticks',
                          {duration_sec: 2.0, margin: 0.01});
    if (!r || !r.ok) {
        if (status) status.innerHTML = '<span style="color:var(--danger,#f87171)">Failed: ' + escHtml((r && r.err) || 'unknown') + '</span>';
        return;
    }
    if (status) {
        status.innerHTML = '<span style="color:var(--accent)">' +
            'L wobble ' + (r.max_left  * 100).toFixed(2) + '% → DZ ' + (r.deadzone_left  * 100).toFixed(2) + '%, ' +
            'R wobble ' + (r.max_right * 100).toFixed(2) + '% → DZ ' + (r.deadzone_right * 100).toFixed(2) + '%' +
            (r.written ? ' (saved)' : ' (no profile)') +
            '</span>';
    }
    // Refetch profile from backend so the panel shows new deadzone values
    if (r.written) {
        var st = await apiGet('/api/gamepad/status');
        if (st && st.active_profile_obj) {
            _gpmCurrentProfile = _gpmNormalizeProfile(st.active_profile_obj);
            _renderStickTuningPanel();
        }
    }
}

// ─── v3.5 advanced settings — alt layer toggle ──────────────────────
function toggleAltLayer() {
    _gpmEditingAltLayer = !_gpmEditingAltLayer;
    if (_gpmEditingAltLayer && _gpmCurrentProfile && !Array.isArray(_gpmCurrentProfile.alt_mappings)) {
        _gpmCurrentProfile.alt_mappings = [];
    }
    _refreshLayerButtonLabel();
    _gpmRenderBindingsLocal();
}
function _refreshLayerButtonLabel() {
    var btn = document.getElementById('gpm-alt-layer-btn');
    if (!btn) return;
    btn.textContent = _gpmEditingAltLayer ? 'Layer: ALT (click for Primary)' : 'Layer: Primary (click for Alt)';
}

// ─── v3.5 advanced settings — modifier capture ──────────────────────
async function captureModifier() {
    var capture = await apiPost('/api/gamepad/capture-button', {timeout: 8});
    if (!capture || !capture.ok || !capture.value) {
        showWarnToast('No input detected within timeout.');
        return;
    }
    var parts = String(capture.value).split(':');
    if (parts.length < 2) return;
    if (!_gpmCurrentProfile) return;
    _gpmCurrentProfile.modifier_source  = parts[0];
    _gpmCurrentProfile.modifier_trigger = parts.slice(1).join(':');
    _gpmAutoSave();
    _refreshAdvancedDisplay();
}
function clearModifier() {
    if (!_gpmCurrentProfile) return;
    _gpmCurrentProfile.modifier_source  = '';
    _gpmCurrentProfile.modifier_trigger = '';
    _gpmAutoSave();
    _refreshAdvancedDisplay();
}

// ─── v3.5 — auto-switch foreground exe ──────────────────────────────
function onForegroundExeChange() {
    if (!_gpmCurrentProfile) return;
    var inp = document.getElementById('gpm-foreground-exe');
    if (!inp) return;
    _gpmCurrentProfile.foreground_exe = (inp.value || '').trim();
    _gpmAutoSave();
}

// ─── v3.5 — global toggle hotkey ────────────────────────────────────
async function captureToggleHotkey() {
    var capture = await apiPost('/api/gamepad/capture-button', {timeout: 8, source: 'keyboard'});
    if (!capture || !capture.ok || !capture.value) {
        showWarnToast('No keyboard key detected within timeout.');
        return;
    }
    var parts = String(capture.value).split(':');
    if (parts[0] !== 'keyboard' || !capture.vk) {
        showWarnToast('Toggle hotkey must be a keyboard key. Captured: ' + capture.value);
        return;
    }
    var name = parts.slice(1).join(':');
    var r = await apiPost('/api/gamepad/toggle-hotkey', {vk: capture.vk, name: name});
    if (r && r.ok) _refreshAdvancedDisplay();
}
async function clearToggleHotkey() {
    var r = await apiPost('/api/gamepad/toggle-hotkey', {vk: 0, name: ''});
    if (r && r.ok) _refreshAdvancedDisplay();
}

// ─── v3.5 — latency probe ───────────────────────────────────────────
async function runLatencyProbe() {
    var label = document.getElementById('gpm-latency-result');
    if (label) label.innerHTML = '<span style="color:var(--text-tertiary)">measuring (~1 sec)…</span>';
    var r = await apiPost('/api/gamepad/measure-latency', {samples: 50});
    if (!label) return;
    if (!r || !r.ok) {
        var hint = '';
        if (r && /disabled/.test(r.err || '')) {
            hint = ' — flip the master switch on first';
        } else if (r && /virtual_controller/.test(r.err || '')) {
            hint = ' — set this profile to Virtual controller mode first';
        } else if (r && /not running/.test(r.err || '')) {
            hint = ' — the mapper polling loop isn\'t running';
        }
        label.innerHTML = '<span style="color:var(--orange,#fbbf24)">probe failed: ' +
            escHtml((r && r.err) || 'unknown') + escHtml(hint) + '</span>';
        return;
    }
    // Color the avg by quality bucket (rough, based on typical poll-rate budgets)
    var avg = r.avg_us || 0;
    var avgColor = avg <  300 ? 'var(--accent)' :       // <0.3 ms = great
                   avg <  800 ? 'var(--text-bright)' :   // <0.8 ms = fine
                   avg < 1500 ? 'var(--orange,#fbbf24)' : // <1.5 ms = meh
                   'var(--danger,#f87171)';               // >=1.5 ms = problem
    label.innerHTML =
        '<span style="color:' + avgColor + ';font-weight:600">' + avg + ' µs avg</span>' +
        '<span style="color:var(--text-tertiary)"> · p50 ' + r.p50_us + ' · p99 ' + r.p99_us + ' · max ' + r.max_us + ' µs</span>' +
        '<br><span style="color:var(--text-tertiary);font-size:10px">' +
        r.valid + '/' + r.samples + ' samples observed' +
        (r.timeouts ? ' · ' + r.timeouts + ' timeouts' : '') +
        ' · Vispora pipeline only (excludes ViGEmBus/USB/game poll)' +
        '</span>';
}

// ─── v3.5 — profile import / export ────────────────────────────────
async function exportProfile() {
    if (!_gpmCurrentProfile || !_gpmCurrentProfile.name) {
        showWarnToast('No profile selected.');
        return;
    }
    var r = await apiPost('/api/gamepad/profile/export', {name: _gpmCurrentProfile.name});
    if (!r || !r.ok) {
        showErrorToast('Export failed: ' + ((r && r.err) || 'unknown'));
        return;
    }
    var text = JSON.stringify(r.blob, null, 2);
    try {
        await navigator.clipboard.writeText(text);
        showInfoToast('Profile copied to clipboard (' + text.length + ' chars). Paste into a text file or share with another Vispora user via Import.');
    } catch (e) {
        // Fallback — show in a prompt() so the user can manually copy
        prompt('Copy this profile JSON:', text);
    }
}
async function importProfilePrompt() {
    var text = prompt('Paste an exported profile JSON here:');
    if (!text) return;
    var blob;
    try { blob = JSON.parse(text); }
    catch (e) { showWarnToast('Not valid JSON.'); return; }
    var r = await apiPost('/api/gamepad/profile/import', {blob: blob});
    if (!r || !r.ok) {
        showErrorToast('Import failed: ' + ((r && r.err) || 'unknown'));
        return;
    }
    showInfoToast('Imported as profile "' + r.name + '". Reloading…');
    await loadGamepadMapper();
}

// ─── v3.5 — refresh the advanced-panel summary widgets ──────────────
async function _refreshAdvancedDisplay() {
    var prof = _gpmCurrentProfile;
    var modDisplay = document.getElementById('gpm-modifier-display');
    if (modDisplay) {
        if (prof && prof.modifier_trigger) {
            modDisplay.textContent = (prof.modifier_source || '?') + ':' + prof.modifier_trigger;
        } else {
            modDisplay.textContent = 'none';
        }
    }
    var fgInp = document.getElementById('gpm-foreground-exe');
    if (fgInp && prof) fgInp.value = prof.foreground_exe || '';
    // Hotkey lives on the global state, not the profile
    var hkDisplay = document.getElementById('gpm-hotkey-display');
    if (hkDisplay) {
        try {
            var st = await apiGet('/api/gamepad/status');
            var name = (st && st.toggle_hotkey_name) || '';
            var vk   = (st && st.toggle_hotkey_vk)   || 0;
            hkDisplay.textContent = vk ? (name || ('VK_0x' + vk.toString(16).toUpperCase())) : 'none';
        } catch (e) { hkDisplay.textContent = 'none'; }
    }
    _renderStickTuningPanel();
}

// ─── Driver auto-install (HidHide / ViGEmBus) ───────────────────────
async function installHidHide() {
    var btn = document.getElementById('gpm-hidhide-install-btn');
    var prog = document.getElementById('gpm-hidhide-progress');
    if (btn) { btn.disabled = true; btn.textContent = 'Installing…'; }
    if (prog) prog.style.display = 'block';
    var r = await apiPost('/api/gamepad/install-hidhide', {});
    if (!r) {
        if (btn) { btn.disabled = false; btn.textContent = 'Install now'; }
        if (prog) prog.textContent = 'Install kickoff failed.';
        return;
    }
    _pollInstallProgress('hidhide');
}

async function installViGEmBus() {
    var btn = document.getElementById('gpm-vigembus-install-btn');
    var prog = document.getElementById('gpm-vigembus-progress');
    if (btn) { btn.disabled = true; btn.textContent = 'Installing…'; }
    if (prog) prog.style.display = 'block';
    var r = await apiPost('/api/gamepad/install-vigembus', {});
    if (!r) {
        if (btn) { btn.disabled = false; btn.textContent = 'Install now'; }
        if (prog) prog.textContent = 'Install kickoff failed.';
        return;
    }
    _pollInstallProgress('vigembus');
}

function _pollInstallProgress(target) {
    var progElId = target === 'hidhide' ? 'gpm-hidhide-progress' : 'gpm-vigembus-progress';
    var btnElId  = target === 'hidhide' ? 'gpm-hidhide-install-btn' : 'gpm-vigembus-install-btn';
    var prog = document.getElementById(progElId);
    var btn  = document.getElementById(btnElId);
    var timer = setInterval(async function(){
        var r = await apiGet('/api/gamepad/install-progress');
        if (!r || !r.status) return;
        var s = r.status;
        if (prog) {
            var line = '<b>' + escHtml(s.phase || 'idle') + '</b> · ' + escHtml(s.msg || '');
            if (s.phase === 'downloading' && s.progress_pct) {
                line += ' (' + s.progress_pct + '%)';
            }
            prog.innerHTML = line;
        }
        if (s.in_progress) return;
        // Done — refresh full banner state to hide if installed
        clearInterval(timer);
        if (btn) {
            btn.disabled = false;
            btn.textContent = s.installed ? 'Installed ✓' : 'Try again';
        }
        if (s.installed) {
            setTimeout(function(){ _refreshGpmInstallBanner(); loadGamepadMapper(); }, 800);
        }
        if (s.err) {
            if (prog) prog.innerHTML = '<span style="color:var(--red,#ef4444)">' + escHtml(s.msg || s.err) + '</span>';
        }
    }, 1000);
}

// ─── v3.4 mappings UI ────────────────────────────────────────────────
// Each mapping is one row: source ▾ trigger ▾ → target ▾ details [✕]

var GPM_MOUSE_TRIGGERS = ['M_LEFT','M_RIGHT','M_MIDDLE','M_X1','M_X2',
                          'M_WHEEL_UP','M_WHEEL_DOWN'];
var GPM_VSTICK_DIRECTIONS = ['up','down','left','right'];

function _findMapping(mid) {
    if (!_gpmCurrentProfile) return null;
    // Search the layer the user is currently editing first, fall back
    // to the other so click handlers from a stale render still hit
    // the right object.
    var primary = _gpmEditingAltLayer
        ? (_gpmCurrentProfile.alt_mappings || [])
        : (_gpmCurrentProfile.mappings || []);
    var fallback = _gpmEditingAltLayer
        ? (_gpmCurrentProfile.mappings || [])
        : (_gpmCurrentProfile.alt_mappings || []);
    var i;
    for (i = 0; i < primary.length;  i++) if (primary[i].id === mid)  return primary[i];
    for (i = 0; i < fallback.length; i++) if (fallback[i].id === mid) return fallback[i];
    return null;
}

function _renderMappingRow(m) {
    var mid = m.id || '';
    var inp = m.input || {};
    var out = m.output || {};
    var src = inp.source || 'controller';

    // ── Source dropdown
    var srcOpts = (_gpmState && _gpmState.input_sources || ['controller','keyboard','mouse']).map(function(s){
        return '<option value="' + s + '"' + (src === s ? ' selected' : '') + '>' +
            (s === 'controller' ? 'Controller' : s === 'keyboard' ? 'Keyboard' : 'Mouse') +
            '</option>';
    }).join('');
    var srcSel = '<select onchange="onMappingSourceChange(\'' + mid + '\', this.value)" ' +
        'style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;min-width:90px">' +
        srcOpts + '</select>';

    // ── Trigger picker (depends on source)
    var trig = inp.trigger || '';
    var trigPicker;
    if (src === 'controller') {
        var btnList = (_gpmState && _gpmState.available_buttons) || [];
        var opts = btnList.map(function(b){
            return '<option value="' + b + '"' + (trig === b ? ' selected' : '') + '>' + escHtml(b) + '</option>';
        }).join('');
        trigPicker = '<select onchange="onMappingTriggerChange(\'' + mid + '\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;min-width:110px">' +
            '<option value="">— pick —</option>' + opts + '</select>';
    } else if (src === 'mouse') {
        var mopts = GPM_MOUSE_TRIGGERS.map(function(b){
            return '<option value="' + b + '"' + (trig === b ? ' selected' : '') + '>' + b + '</option>';
        }).join('');
        trigPicker = '<select onchange="onMappingTriggerChange(\'' + mid + '\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;min-width:120px">' +
            '<option value="">— pick —</option>' + mopts + '</select>';
    } else { // keyboard
        // Keyboard has many keys — show captured value as text + capture button
        trigPicker = '<span style="font-family:var(--font-mono,monospace);color:var(--accent);font-size:12px;min-width:80px;display:inline-block">' +
            (trig ? escHtml(trig) : '— press a key →') + '</span>';
    }

    // ── Target dropdown
    var target = out.target || 'key';
    var targetOpts = (_gpmState && _gpmState.output_targets || ['key','mouse_button','vbutton','vstick','vtrigger','macro','disabled']).map(function(t){
        var label = {key:'Keyboard key', mouse_button:'Mouse button',
                     vbutton:'Virtual button', vstick:'Virtual stick',
                     vtrigger:'Virtual trigger',
                     macro:'Macro', disabled:'Disabled'}[t] || t;
        return '<option value="' + t + '"' + (target === t ? ' selected' : '') + '>' + label + '</option>';
    }).join('');
    var targetSel = '<select onchange="onMappingTargetChange(\'' + mid + '\', this.value)" ' +
        'style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;min-width:120px">' +
        targetOpts + '</select>';

    // ── Target details (depends on target)
    var details = '';
    if (target === 'key') {
        var keyOpts = GPM_KEY_OPTIONS.map(function(o){
            return '<option value="' + o.vk + '"' + (out.vk === o.vk ? ' selected' : '') + '>' + escHtml(o.label) + '</option>';
        }).join('');
        details = '<select onchange="onMappingDetailChange(\'' + mid + '\', \'vk\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;min-width:100px">' + keyOpts + '</select>';
    } else if (target === 'mouse_button') {
        var mbOpts = GPM_MOUSE_TRIGGERS.map(function(b){
            return '<option value="' + b + '"' + (out.button === b ? ' selected' : '') + '>' + b + '</option>';
        }).join('');
        details = '<select onchange="onMappingDetailChange(\'' + mid + '\', \'button\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;min-width:120px">' + mbOpts + '</select>';
    } else if (target === 'vbutton') {
        // Prefer available_vbuttons (which includes DS4-only specials
        // like TOUCHPAD / PS); fall back to available_buttons for older
        // backends.
        var vbList = (_gpmState && (_gpmState.available_vbuttons || _gpmState.available_buttons)) || [];
        var vbOpts = vbList.map(function(b){
            var label = b;
            if (b === 'TOUCHPAD' || b === 'PS') label = b + ' (DS4)';
            return '<option value="' + b + '"' + (out.vbutton === b ? ' selected' : '') + '>' + escHtml(label) + '</option>';
        }).join('');
        details = '<select onchange="onMappingDetailChange(\'' + mid + '\', \'vbutton\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;min-width:110px">' + vbOpts + '</select>';
    } else if (target === 'vstick') {
        var stickSide = out.stick || 'left';
        var stickDir  = out.direction || 'up';
        var sideOpts = ['left','right'].map(function(s){
            return '<option value="' + s + '"' + (stickSide === s ? ' selected' : '') + '>' + s.charAt(0).toUpperCase() + s.slice(1) + ' stick</option>';
        }).join('');
        var dirOpts = GPM_VSTICK_DIRECTIONS.map(function(d){
            return '<option value="' + d + '"' + (stickDir === d ? ' selected' : '') + '>' + d.charAt(0).toUpperCase() + d.slice(1) + '</option>';
        }).join('');
        details = '<select onchange="onMappingDetailChange(\'' + mid + '\', \'stick\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px">' + sideOpts + '</select>' +
                  ' <select onchange="onMappingDetailChange(\'' + mid + '\', \'direction\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px">' + dirOpts + '</select>';
    } else if (target === 'vtrigger') {
        var trigSide = out.trigger || 'lt';
        var trigVal  = (out.value !== undefined ? out.value : 1.0);
        var tSideOpts = ['lt','rt'].map(function(s){
            return '<option value="' + s + '"' + (trigSide === s ? ' selected' : '') + '>' + s.toUpperCase() + ' (' + (s === 'lt' ? 'L' : 'R') + ' trigger)</option>';
        }).join('');
        details = '<select onchange="onMappingDetailChange(\'' + mid + '\', \'trigger\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" title="Which virtual trigger to drive">' + tSideOpts + '</select>' +
            ' <label style="font-size:11px;color:var(--text-tertiary)" title="0 = released, 1 = fully pulled">value</label>' +
            '<input type="number" step="0.05" min="0" max="1" value="' + trigVal + '" onchange="onMappingDetailChange(\'' + mid + '\', \'value\', this.value)" style="width:55px;padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" title="0.0 = released, 1.0 = fully pulled" />';
    } else if (target === 'macro') {
        var stepCount = (out.steps || []).length;
        details = '<button class="btn btn-sm" onclick="openMappingMacroEditor(\'' + mid + '\')">Edit (' + stepCount + ' step' + (stepCount === 1 ? '' : 's') + ')</button>';
    } else if (target === 'disabled') {
        details = '<span style="color:var(--text-tertiary);font-size:11px;font-style:italic">input silenced</span>';
    }

    // ── v3.5 behavior selector (toggle / turbo / chord / normal)
    var behavior = (inp.behavior || 'normal');
    var behaviorOpts = ['normal','toggle','turbo','chord'].map(function(b){
        var label = {normal:'Normal', toggle:'Toggle',
                     turbo:'Turbo', chord:'Chord'}[b] || b;
        return '<option value="' + b + '"' + (behavior === b ? ' selected' : '') + '>' + label + '</option>';
    }).join('');
    var behaviorSel = '<select onchange="onMappingBehaviorChange(\'' + mid + '\', this.value)" ' +
        'title="Normal=press/release. Toggle=press locks ON, press again releases. Turbo=auto-fire while held at the configured Hz. Chord=fires only while ALSO holding a partner input." ' +
        'style="padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:11px">' +
        behaviorOpts + '</select>';
    // Conditional behavior fields
    var behaviorExtra = '';
    if (behavior === 'turbo') {
        var thz = parseInt(inp.turbo_hz, 10) || 10;
        behaviorExtra = '<label style="font-size:11px;color:var(--text-tertiary)" title="Auto-press cycles per second (1-60)">Hz</label>' +
            '<input type="number" min="1" max="60" value="' + thz + '" onchange="onMappingBehaviorField(\'' + mid + '\', \'turbo_hz\', parseInt(this.value, 10))" style="width:48px;padding:3px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:11px" />';
    } else if (behavior === 'chord') {
        var partner = inp.chord_with || '';
        // Only useful for controller inputs — pick another button as partner
        var partnerOpts = ((_gpmState && _gpmState.available_buttons) || []).map(function(b){
            return '<option value="' + b + '"' + (partner === b ? ' selected' : '') + '>' + escHtml(b) + '</option>';
        }).join('');
        behaviorExtra = '<label style="font-size:11px;color:var(--text-tertiary)" title="Mapping fires only when this PARTNER input is also held simultaneously">+</label>' +
            '<select onchange="onMappingBehaviorField(\'' + mid + '\', \'chord_with\', this.value)" style="padding:3px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:11px">' +
            '<option value="">— pick —</option>' + partnerOpts + '</select>';
    }

    return '<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--bg-elevated);border-radius:6px;font-size:12.5px;flex-wrap:wrap">' +
              srcSel +
              trigPicker +
              '<span style="color:var(--text-tertiary)">→</span>' +
              targetSel +
              '<div style="flex:1;min-width:140px">' + details + '</div>' +
              behaviorSel + behaviorExtra +
              '<button class="btn btn-sm" onclick="captureMappingTrigger(\'' + mid + '\')" title="Press a button on any source to assign" style="font-size:11px">capture</button>' +
              '<button class="btn btn-sm btn-danger" onclick="deleteMapping(\'' + mid + '\')" title="Delete mapping" style="font-size:11px">✕</button>' +
           '</div>';
}

function onMappingBehaviorChange(mid, newBehavior) {
    var m = _findMapping(mid);
    if (!m) return;
    m.input = m.input || {};
    m.input.behavior = newBehavior;
    if (newBehavior === 'turbo')   m.input.turbo_hz   = m.input.turbo_hz   || 10;
    if (newBehavior === 'chord')   m.input.chord_with = m.input.chord_with || '';
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

function onMappingBehaviorField(mid, field, val) {
    var m = _findMapping(mid);
    if (!m) return;
    m.input = m.input || {};
    m.input[field] = val;
    _gpmAutoSave();
}

// v3.5 — which mappings array we're operating on (primary or alt).
function _gpmActiveMappingsArray(profile) {
    if (!profile) return null;
    var key = _gpmEditingAltLayer ? 'alt_mappings' : 'mappings';
    if (!Array.isArray(profile[key])) profile[key] = [];
    return profile[key];
}

function addNewMapping() {
    if (!_gpmCurrentProfile) {
        showWarnToast('Pick or create a profile first.');
        return;
    }
    var arr = _gpmActiveMappingsArray(_gpmCurrentProfile);
    arr.push({
        id: 'm' + Math.random().toString(36).slice(2, 10),
        input:  {source: 'controller', trigger: '', behavior: 'normal'},
        output: {target: 'key', vk: 0x20},
    });
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

function deleteMapping(mid) {
    if (!_gpmCurrentProfile) return;
    var arr = _gpmActiveMappingsArray(_gpmCurrentProfile);
    var key = _gpmEditingAltLayer ? 'alt_mappings' : 'mappings';
    _gpmCurrentProfile[key] = arr.filter(function(m){ return m.id !== mid; });
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

function onMappingSourceChange(mid, newSource) {
    var m = _findMapping(mid);
    if (!m) return;
    m.input = m.input || {};
    m.input.source = newSource;
    m.input.trigger = '';   // reset trigger — different sources use different vocabularies
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

function onMappingTriggerChange(mid, newTrigger) {
    var m = _findMapping(mid);
    if (!m) return;
    m.input = m.input || {};
    m.input.trigger = newTrigger;
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

function onMappingTargetChange(mid, newTarget) {
    var m = _findMapping(mid);
    if (!m) return;
    // Default-fill the target's required fields
    var defaults = {
        key:          {target: 'key',          vk: 0x20},
        mouse_button: {target: 'mouse_button', button: 'M_LEFT'},
        vbutton:      {target: 'vbutton',      vbutton: 'A'},
        vstick:       {target: 'vstick',       stick: 'left', direction: 'up', magnitude: 1.0},
        vtrigger:     {target: 'vtrigger',     trigger: 'rt', value: 1.0},
        macro:        {target: 'macro',        steps: []},
        disabled:     {target: 'disabled'},
    };
    m.output = defaults[newTarget] || {target: newTarget};
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

function onMappingDetailChange(mid, field, value) {
    var m = _findMapping(mid);
    if (!m) return;
    m.output = m.output || {};
    if (field === 'vk') {
        m.output[field] = parseInt(value, 10);
    } else if (field === 'value' || field === 'magnitude') {
        // 0..1 float fields (vtrigger value, vstick magnitude)
        var n = parseFloat(value);
        if (isNaN(n)) n = 0;
        m.output[field] = Math.max(0, Math.min(1, n));
    } else {
        m.output[field] = value;
    }
    _gpmAutoSave();
    _gpmRenderSvgLabels();
}

// Capture a trigger (any source) for the given mapping.  Backend's
// capture-button route now returns a "source:trigger" string, e.g.
// "keyboard:F1" or "controller:A".  We split + assign.
async function captureMappingTrigger(mid) {
    var m = _findMapping(mid);
    if (!m) return;
    var modal = document.getElementById('gpm-capture-modal');
    var msg = document.getElementById('gpm-capture-msg');
    if (modal) modal.style.display = 'flex';
    if (msg) msg.textContent = 'Press any controller button, keyboard key, or mouse button…';
    _gpmCaptureCancelled = false;
    var r = await apiPost('/api/gamepad/capture-button', {timeout: 8.0});
    if (modal) modal.style.display = 'none';
    if (_gpmCaptureCancelled) return;
    if (!r || !r.ok) {
        showErrorToast(r && r.err ? r.err : 'Capture failed.');
        return;
    }
    // r.button is "source:trigger"
    var captured = (r.button || '').split(':');
    if (captured.length !== 2) {
        showWarnToast('Unexpected capture format: ' + r.button);
        return;
    }
    m.input = {source: captured[0], trigger: captured[1]};
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

// Macro editor for new mappings
// Macro-level state held while the modal is open
var _gpmMacroPlayMode = 'once';
var _gpmMacroRepeatCount = 2;

function openMappingMacroEditor(mid) {
    var m = _findMapping(mid);
    if (!m) return;
    _gpmMacroEditorBtn = mid;       // reuse existing modal state vars
    var binding = m.output;
    if (!binding || binding.target !== 'macro') return;
    _gpmMacroEditorSteps = JSON.parse(JSON.stringify(binding.steps || []));
    _gpmMacroPlayMode = binding.play_mode || 'once';
    _gpmMacroRepeatCount = binding.repeat_count || 2;
    document.getElementById('gpm-macro-btn-name').textContent = 'mapping ' + mid;

    // Sync the play-mode UI to the loaded values
    var pm = document.getElementById('gpm-macro-play-mode');
    if (pm) pm.value = _gpmMacroPlayMode;
    var rcWrap = document.getElementById('gpm-macro-repeat-wrap');
    if (rcWrap) rcWrap.style.display = (_gpmMacroPlayMode === 'repeat_n' ? '' : 'none');
    var rc = document.getElementById('gpm-macro-repeat-count');
    if (rc) rc.value = String(_gpmMacroRepeatCount);

    _renderMacroSteps();
    var modal = document.getElementById('gpm-macro-modal');
    if (modal) modal.style.display = 'flex';
}

function onMacroPlayModeChange() {
    var sel = document.getElementById('gpm-macro-play-mode');
    if (!sel) return;
    _gpmMacroPlayMode = sel.value;
    var wrap = document.getElementById('gpm-macro-repeat-wrap');
    if (wrap) wrap.style.display = (_gpmMacroPlayMode === 'repeat_n' ? '' : 'none');
}

function onMacroRepeatCountChange() {
    var inp = document.getElementById('gpm-macro-repeat-count');
    if (!inp) return;
    var n = parseInt(inp.value, 10);
    _gpmMacroRepeatCount = (isNaN(n) || n < 1) ? 1 : n;
}

// Auto-save the current profile to the backend.  All on-change handlers
// call this so user edits persist immediately — eliminates the "dropdown
// reverts" bug caused by re-fetching from the backend after a local edit
// that hasn't been saved yet.
async function _gpmAutoSave() {
    if (!_gpmCurrentProfile) return;
    try {
        await apiPost('/api/gamepad/profiles', _gpmCurrentProfile);
    } catch (e) {
        console.error('gamepad profile autosave failed', e);
    }
}

function onGpmTypeChanged(btn, newType) {
    if (!_gpmCurrentProfile) return;
    if (newType === 'passthrough') {
        delete _gpmCurrentProfile.buttons[btn];
    } else if (newType === 'key') {
        _gpmCurrentProfile.buttons[btn] = {type: 'key', vk: 0x20};   // default Space
    } else if (newType === 'macro') {
        _gpmCurrentProfile.buttons[btn] = {type: 'macro', steps: []};
    } else if (newType === 'disabled') {
        _gpmCurrentProfile.buttons[btn] = {type: 'disabled'};
    } else if (newType === 'vbutton') {
        _gpmCurrentProfile.buttons[btn] = {type: 'vbutton', vbutton: btn};
    }
    // Persist + re-render rows from LOCAL state (don't refetch — that
    // would clobber unsaved edits if the request races the next change)
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

function onGpmKeyChanged(btn, vkStr) {
    if (!_gpmCurrentProfile) return;
    var vk = parseInt(vkStr, 10);
    _gpmCurrentProfile.buttons[btn] = {type: 'key', vk: vk};
    _gpmAutoSave();
    _gpmRenderSvgLabels();
}

function onGpmProfileSelect() {
    var sel = document.getElementById('gpm-profile-select');
    if (!sel) return;
    var name = sel.value || null;
    apiPost('/api/gamepad/active', {name: name}).then(function(){
        loadGamepadMapper();
    });
}

async function newGpmProfile() {
    var name = prompt('Profile name (e.g. "Default FPS")');
    if (!name) return;
    name = name.trim();
    if (!name) return;
    var prof = {
        name: name,
        // v3.4: always-on by default — no more foreground filter
        active_only_in_game: false,
        mappings: [],
    };
    var r = await apiPost('/api/gamepad/profiles', prof);
    if (!r || !r.ok) { showErrorToast('Save failed.'); return; }
    await apiPost('/api/gamepad/active', {name: name});
    loadGamepadMapper();
}

async function deleteGpmProfile() {
    if (!_gpmCurrentProfile) {
        showWarnToast('No profile selected.');
        return;
    }
    if (!confirm('Delete profile "' + _gpmCurrentProfile.name + '"?')) return;
    await fetch('/api/gamepad/profiles/' + encodeURIComponent(_gpmCurrentProfile.name), {method: 'DELETE'});
    loadGamepadMapper();
}

async function saveGpmProfile() {
    if (!_gpmCurrentProfile) return;
    // v3.4 — foreground filter and explicit hide_physical removed.
    // Always-on; HidHide auto-engages with virtual_controller mode.
    _gpmCurrentProfile.active_only_in_game = false;
    _gpmCurrentProfile.hide_physical = (_gpmCurrentProfile.remap_mode === 'virtual_controller');
    var phz = document.getElementById('gpm-poll-hz');
    if (phz) _gpmCurrentProfile.poll_hz = parseInt(phz.value, 10) || 4000;
    var r = await apiPost('/api/gamepad/profiles', _gpmCurrentProfile);
    if (r && r.ok) showInfoToast('Profile saved.');
    else showErrorToast('Save failed.');
}

async function toggleGpmMaster(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    var r = await apiPost('/api/gamepad/enabled', {enabled: newOn});
    if (!r || !r.ok) {
        el.classList.toggle('on');
    }
}

// ─── Live SVG controller visualizer (60 Hz) ─────────────────────────
// Stick well centers + radius — must match the SVG above.
var _GPM_STICKS = {
    L: { cx: 220, cy: 200, r: 22 },
    R: { cx: 380, cy: 200, r: 22 },
};
var _gpmSvgRafId = null;
var _gpmSvgVisible = false;
var _gpmGamepadConnected = false;        // set by `gamepadconnected` event

// Chromium (incl. Edge WebView2) requires user input on the gamepad
// before navigator.getGamepads() returns anything — this is a privacy
// gate identical to the one Chrome desktop uses.  We listen for the
// `gamepadconnected` event AND watch for a populated getGamepads()
// list so we can flip the wake-up hint off as soon as the API unlocks.
window.addEventListener('gamepadconnected', function(e) {
    _gpmGamepadConnected = true;
    console.log('Gamepad connected:', e.gamepad && e.gamepad.id);
});
window.addEventListener('gamepaddisconnected', function() {
    // Don't reset _gpmGamepadConnected — the controller may just be in
    // a wake state.  Let the polling loop figure it out from getGamepads().
});

// Backend-driven state cache.  Refreshed at 60 Hz by a polling timer
// (cheap — same XInput call our mapper loop already makes).  Used as
// fallback when navigator.getGamepads() returns nothing.
var _gpmBackendState = null;
var _gpmBackendPollTimer = null;
var _gpmActiveSource = "none";   // "browser" | "backend" | "none"

function _gpmStartBackendPoll() {
    if (_gpmBackendPollTimer) return;
    var lastLogT = 0;
    var poll = function() {
        if (currentPage !== 'profiles' || !_gpmSvgVisible) return;
        apiGet('/api/gamepad/live-state').then(function(r){
            if (r && r.controllers && r.controllers.length) {
                var newState = r.controllers[0].state;
                // Tack the v3.5 virtual-pad snapshot onto our state object
                // so the visualizer renderer can read both streams from
                // the same place.
                if (r.vpad) newState._vpad = r.vpad;
                _gpmBackendState = newState;
                // Track packet number for staleness detection
                var pkt = newState.packet || 0;
                if (pkt !== _gpmLastPacketSeen) {
                    _gpmLastPacketSeen = pkt;
                    _gpmLastPacketChangeT = Date.now();
                }
            } else {
                _gpmBackendState = null;
            }
            // Diagnostic log — every 5 seconds with packet info
            var now = Date.now();
            if (now - lastLogT > 5000) {
                lastLogT = now;
                if (_gpmBackendState) {
                    var sinceChange = now - _gpmLastPacketChangeT;
                    console.log('[gpm-backend]', {
                        packet:  _gpmBackendState.packet,
                        last_change_ms_ago: sinceChange,
                        pressed: _gpmBackendState.pressed,
                        lx: _gpmBackendState.lx,
                        ly: _gpmBackendState.ly,
                        lt: _gpmBackendState.lt,
                        rt: _gpmBackendState.rt,
                    });
                } else {
                    console.log('[gpm-backend] no controllers in live-state');
                }
            }
        }).catch(function(e){
            console.error('[gpm-backend] live-state poll failed:', e);
        });
    };
    poll();
    _gpmBackendPollTimer = setInterval(poll, 33);
}

function _gpmStopBackendPoll() {
    if (_gpmBackendPollTimer) {
        clearInterval(_gpmBackendPollTimer);
        _gpmBackendPollTimer = null;
    }
}

// Convert a backend XInput state row to a "synthetic gamepad" object
// shaped like the JS Gamepad API so the rest of the loop doesn't care.
// Backend stick range: -32768..32767 → normalize to -1..1 for SVG transform.
function _gpmBackendStateToPad(state) {
    if (!state) return null;
    var pressed = new Set(state.pressed || []);
    var BTN_ORDER = ["A","B","X","Y","LB","RB","LT","RT","BACK","START",
                     "LSTICK","RSTICK","DPAD_UP","DPAD_DOWN","DPAD_LEFT","DPAD_RIGHT"];
    var buttons = BTN_ORDER.map(function(name){
        if (name === "LT") return {pressed: state.lt > 30, value: (state.lt || 0) / 255};
        if (name === "RT") return {pressed: state.rt > 30, value: (state.rt || 0) / 255};
        return {pressed: pressed.has(name), value: pressed.has(name) ? 1 : 0};
    });
    var axes = [
        (state.lx || 0) / 32767,
        -(state.ly || 0) / 32767,   // XInput Y is up-positive; Gamepad API is down-positive
        (state.rx || 0) / 32767,
        -(state.ry || 0) / 32767,
    ];
    return {buttons: buttons, axes: axes, _source: "backend"};
}

function startGpmSvgLoop() {
    if (_gpmSvgRafId !== null) return;
    var wrap = document.getElementById('gpm-visual-wrap');
    if (wrap) wrap.style.display = 'block';
    _gpmSvgVisible = true;
    _gpmStartBackendPoll();    // arm the fallback

    var loop = function() {
        // ALWAYS reschedule first — if anything below throws, the loop
        // still keeps running.  Without this, a single silent exception
        // would freeze the visualizer until page refresh, which matches
        // the "stops responding after first input" symptom users hit.
        _gpmSvgRafId = requestAnimationFrame(loop);
        if (currentPage !== 'profiles' || !_gpmSvgVisible) return;

        try {
            _gpmSvgRenderTick();
        } catch (e) {
            console.error('[gpm-svg] render tick failed:', e);
        }
    };
    loop();
}

function _gpmSvgRenderTick() {
    // v3.3 update — BACKEND IS PRIMARY now.  Reason: WebView2's
    // navigator.getGamepads() in pywebview tends to "wake" on the first
    // button press but then return STALE state forever after — a known
    // Chromium quirk that's worse for us than a 33 ms HTTP roundtrip.
    // The backend feed is fed by direct XInput at 30 Hz from a Python
    // thread that has no WebView weirdness.  Reliable.
    var pad = null;
    var sourceUsed = "none";
    if (_gpmBackendState) {
        pad = _gpmBackendStateToPad(_gpmBackendState);
        sourceUsed = "backend";
    } else {
        // Last resort: try the browser API (e.g. if the backend route
        // has somehow stopped responding)
        var pads = navigator.getGamepads ? navigator.getGamepads() : [];
        for (var i = 0; i < pads.length; i++) {
            if (pads[i]) { pad = pads[i]; sourceUsed = "browser"; break; }
        }
    }

    // Update diagnostic line + the source indicator
    if (sourceUsed !== _gpmActiveSource) {
        _gpmActiveSource = sourceUsed;
        _gpmUpdateDiagnostic();
    }

        var noctrl = document.getElementById('gpm-svg-noctrl');
        if (!pad) {
            if (noctrl) {
                var xinputSeesIt = !!(_gpmState && _gpmState.connected_slots && _gpmState.connected_slots.length);
                if (xinputSeesIt) {
                    noctrl.textContent = 'XInput sees your controller — backend feed will appear shortly';
                    noctrl.setAttribute('fill', 'var(--orange,#fbbf24)');
                } else {
                    noctrl.textContent = 'No controller detected — plug in or wake yours';
                    noctrl.setAttribute('fill', 'var(--text-tertiary)');
                }
                noctrl.style.display = '';
            }
            _gpmSvgClearAll();
            return;
        }
        if (noctrl) noctrl.style.display = 'none';

        // Standard mapping (Xbox-like — what Chromium gives us by default,
        // and exactly what _gpmBackendStateToPad emits).
        var STD_MAP = ["A","B","X","Y","LB","RB","LT","RT","BACK","START",
                       "LSTICK","RSTICK","DPAD_UP","DPAD_DOWN","DPAD_LEFT","DPAD_RIGHT"];
        for (var b = 0; b < pad.buttons.length && b < STD_MAP.length; b++) {
            var name = STD_MAP[b];
            var pressed = pad.buttons[b].pressed;
            var el = document.getElementById('gpm-svg-btn-' + name);
            if (el) el.classList.toggle('is-pressed', !!pressed);
        }

        var lt = pad.buttons[6] ? pad.buttons[6].value : 0;
        var rt = pad.buttons[7] ? pad.buttons[7].value : 0;
        var ltFill = document.getElementById('gpm-trigger-lt-fill');
        var rtFill = document.getElementById('gpm-trigger-rt-fill');
        if (ltFill) ltFill.setAttribute('width', String(60 * Math.max(0, Math.min(1, lt))));
        if (rtFill) rtFill.setAttribute('width', String(60 * Math.max(0, Math.min(1, rt))));

        var lx = pad.axes[0] || 0, ly = pad.axes[1] || 0;
        var rx = pad.axes[2] || 0, ry = pad.axes[3] || 0;
        var lDot = document.getElementById('gpm-svg-stick-l-dot');
        var rDot = document.getElementById('gpm-svg-stick-r-dot');
        if (lDot) lDot.setAttribute('transform',
            'translate(' + (lx * _GPM_STICKS.L.r) + ',' + (ly * _GPM_STICKS.L.r) + ')');
        if (rDot) rDot.setAttribute('transform',
            'translate(' + (rx * _GPM_STICKS.R.r) + ',' + (ry * _GPM_STICKS.R.r) + ')');

        // v3.5 — overlay the VIRTUAL pad's stick position too, in a
        // different colour, so the user can see what the game receives
        // (e.g. anti-recoil pull, deadzone shaping, layer modifiers).
        // The vpad snapshot is included alongside live-state when the
        // mapper is in virtual_controller mode.
        if (_gpmBackendState && _gpmBackendState._vpad) {
            var v = _gpmBackendState._vpad;
            // vpad uses XInput int16; visualizer uses W3C Gamepad API
            // convention (+y = DOWN), so we negate Y to match.
            var vlx = (v.lx || 0) / 32767;
            var vly = -(v.ly || 0) / 32767;
            var vrx = (v.rx || 0) / 32767;
            var vry = -(v.ry || 0) / 32767;
            _ensureVpadOverlayDots();
            var vlDot = document.getElementById('gpm-svg-vstick-l-dot');
            var vrDot = document.getElementById('gpm-svg-vstick-r-dot');
            if (vlDot) vlDot.setAttribute('transform',
                'translate(' + (vlx * _GPM_STICKS.L.r) + ',' + (vly * _GPM_STICKS.L.r) + ')');
            if (vrDot) vrDot.setAttribute('transform',
                'translate(' + (vrx * _GPM_STICKS.R.r) + ',' + (vry * _GPM_STICKS.R.r) + ')');
        }
}

function _ensureVpadOverlayDots() {
    // Lazily clone the physical-stick dots with a different fill (cyan
    // for high contrast against the typical white/grey physical dot).
    // Idempotent — only creates the overlays once, then we just show
    // the legend whenever the virtual stream is active.
    var legend = document.getElementById('gpm-visual-legend');
    if (legend) legend.style.display = 'block';
    if (document.getElementById('gpm-svg-vstick-l-dot')) return;
    ['L', 'R'].forEach(function(side){
        var src = document.getElementById('gpm-svg-stick-' + side.toLowerCase() + '-dot');
        if (!src || !src.parentNode) return;
        var clone = src.cloneNode(true);
        clone.id = 'gpm-svg-vstick-' + side.toLowerCase() + '-dot';
        clone.setAttribute('opacity', '0.85');
        clone.setAttribute('stroke', '#0891b2');
        clone.setAttribute('stroke-width', '1.5');
        // Slightly smaller than the physical dot so both stay readable.
        // Cyan (#22d3ee) is well outside the typical white/grey physical
        // dot range so the two never visually merge even at center.
        var children = clone.querySelectorAll('circle');
        children.forEach(function(c){
            var r = parseFloat(c.getAttribute('r')) || 6;
            c.setAttribute('r', String(Math.max(2, r * 0.55)));
            c.setAttribute('fill', '#22d3ee');
            c.setAttribute('stroke', '#0891b2');
        });
        src.parentNode.appendChild(clone);
    });
}

function _gpmUpdateDiagnostic() {
    var el = document.getElementById('gpm-visual-source');
    if (!el) return;
    var apiAvail = !!(navigator.getGamepads);
    var msg = '';
    if (_gpmActiveSource === 'browser') {
        msg = '<span style="color:var(--accent)">live · WebView Gamepad API (fallback)</span>';
    } else if (_gpmActiveSource === 'backend') {
        msg = '<span style="color:var(--accent)">live · backend XInput @ 30 Hz</span>';
    } else if (apiAvail) {
        msg = '<span style="color:var(--text-tertiary)">waiting for controller…</span>';
    } else {
        msg = '<span style="color:var(--text-tertiary)">Gamepad API unavailable in this WebView</span>';
    }
    el.innerHTML = msg;
}

// Track backend packet number so the visualizer knows when state is stale
var _gpmLastPacketSeen = -1;
var _gpmLastPacketChangeT = 0;

function stopGpmSvgLoop() {
    _gpmSvgVisible = false;
    if (_gpmSvgRafId !== null) {
        cancelAnimationFrame(_gpmSvgRafId);
        _gpmSvgRafId = null;
    }
    _gpmStopBackendPoll();
}

function _gpmSvgClearAll() {
    var STD = ["A","B","X","Y","LB","RB","BACK","START","LSTICK","RSTICK",
               "DPAD_UP","DPAD_DOWN","DPAD_LEFT","DPAD_RIGHT"];
    STD.forEach(function(n){
        var el = document.getElementById('gpm-svg-btn-' + n);
        if (el) el.classList.remove('is-pressed');
    });
    var ltFill = document.getElementById('gpm-trigger-lt-fill');
    var rtFill = document.getElementById('gpm-trigger-rt-fill');
    if (ltFill) ltFill.setAttribute('width', '0');
    if (rtFill) rtFill.setAttribute('width', '0');
    var lDot = document.getElementById('gpm-svg-stick-l-dot');
    var rDot = document.getElementById('gpm-svg-stick-r-dot');
    if (lDot) lDot.setAttribute('transform', 'translate(0,0)');
    if (rDot) rDot.setAttribute('transform', 'translate(0,0)');
}

// Click any SVG button → focus the matching binding row.  If no row exists
// yet (button is currently passthrough), default to a sensible binding so
// the user has something to edit.
function _gpmInitSvgClicks() {
    var els = document.querySelectorAll('#gpm-svg .gpm-svg-btn');
    els.forEach(function(el){
        if (el._gpmClickWired) return;
        el._gpmClickWired = true;
        el.addEventListener('click', function(){
            var btn = el.getAttribute('data-gpm-btn');
            if (!btn || !_gpmCurrentProfile) {
                if (!_gpmCurrentProfile) {
                    showWarnToast('Pick or create a profile first.');
                }
                return;
            }
            // Pulse the SVG button briefly
            el.classList.add('is-binding');
            setTimeout(function(){ el.classList.remove('is-binding'); }, 1200);

            // Default to a passthrough → key binding so the user has
            // something to edit.  If a binding already exists, leave it.
            // beta.12 — belt-and-suspenders: even after the normalizer
            // runs, fail safe rather than throwing if the profile shape
            // somehow drifted.
            if (!_gpmCurrentProfile.buttons) _gpmCurrentProfile.buttons = {};
            if (!_gpmCurrentProfile.buttons[btn]) {
                if (_gpmCurrentProfile.remap_mode === 'virtual_controller') {
                    _gpmCurrentProfile.buttons[btn] = {type: 'vbutton', vbutton: btn};
                } else {
                    _gpmCurrentProfile.buttons[btn] = {type: 'key', vk: 0x20};
                }
                loadGpmProfileBindings(_gpmCurrentProfile.name);
            }
            // Scroll the matching row into view + flash it
            var rowsContainer = document.getElementById('gpm-binding-rows');
            if (rowsContainer) {
                // Find the row whose first <div> text matches the button
                var rows = rowsContainer.children;
                for (var i = 0; i < rows.length; i++) {
                    var label = rows[i].querySelector('div');
                    if (label && label.textContent.trim() === btn) {
                        rows[i].scrollIntoView({behavior: 'smooth', block: 'center'});
                        rows[i].style.transition = 'background 0.3s';
                        rows[i].style.background = 'rgba(251,191,36,0.15)';
                        setTimeout(function(r){
                            return function(){ r.style.background = ''; };
                        }(rows[i]), 1500);
                        break;
                    }
                }
            }
        });
    });
}

// Update the small target labels under each button on the SVG so the
// user can see at a glance what each button is currently bound to.
function _gpmRenderSvgLabels() {
    var labelsG = document.getElementById('gpm-svg-labels');
    if (!labelsG) return;
    labelsG.innerHTML = '';
    if (!_gpmCurrentProfile) return;
    // SVG x/y coordinates of where to drop each button's label.  Lined
    // up just below the corresponding visual element.
    var POS = {
        A:          {x: 450, y: 220},
        B:          {x: 488, y: 185},
        X:          {x: 412, y: 185},
        Y:          {x: 450, y: 100},
        LB:         {x: 130, y: 62},
        RB:         {x: 470, y: 62},
        BACK:       {x: 262, y: 152},
        START:      {x: 338, y: 152},
        LSTICK:     {x: 220, y: 250},
        RSTICK:     {x: 380, y: 250},
        DPAD_UP:    {x: 141, y: 119},
        DPAD_DOWN:  {x: 141, y: 200},
        DPAD_LEFT:  {x: 95,  y: 161},
        DPAD_RIGHT: {x: 188, y: 161},
    };
    // v3.4 — read from mappings array.  Group by controller-source
    // trigger so a button mapped to multiple outputs shows them all.
    var byBtn = {};
    (_gpmCurrentProfile.mappings || []).forEach(function(m){
        var inp = m.input || {};
        if (inp.source !== 'controller') return;
        if (!POS[inp.trigger]) return;
        var out = m.output || {};
        var label = '';
        if (out.target === 'key') {
            label = '→ ' + (_vkLabel(out.vk) || ('VK 0x' + (out.vk||0).toString(16)));
        } else if (out.target === 'mouse_button') {
            label = '→ ' + (out.button || '?');
        } else if (out.target === 'vbutton') {
            label = '→ ' + (out.vbutton || '?');
        } else if (out.target === 'vstick') {
            label = '→ ' + (out.stick||'L') + ' ' + (out.direction||'?');
        } else if (out.target === 'vtrigger') {
            label = '→ ' + (out.trigger||'rt').toUpperCase() + ' ' + Math.round((out.value||1)*100) + '%';
        } else if (out.target === 'macro') {
            label = '→ macro';
        } else if (out.target === 'disabled') {
            label = '✕ off';
        }
        if (!label) return;
        if (!byBtn[inp.trigger]) byBtn[inp.trigger] = [];
        byBtn[inp.trigger].push(label);
    });
    Object.keys(byBtn).forEach(function(btn){
        var label = byBtn[btn].join(' · ');
        var t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        t.setAttribute('x', POS[btn].x);
        t.setAttribute('y', POS[btn].y);
        t.setAttribute('text-anchor', 'middle');
        t.setAttribute('font-size', '9');
        t.setAttribute('font-family', 'monospace');
        t.setAttribute('fill', 'var(--accent)');
        t.textContent = label;
        labelsG.appendChild(t);
    });
}

// ─── Capture-button modal ───────────────────────────────────────────
async function captureGpmButton(btn) {
    var modal = document.getElementById('gpm-capture-modal');
    var msg = document.getElementById('gpm-capture-msg');
    if (modal) modal.style.display = 'flex';
    if (msg) msg.textContent = 'Press a controller button to assign to "' + btn + '"… (8s)';
    _gpmCaptureCancelled = false;
    var r = await apiPost('/api/gamepad/capture-button', {timeout: 8.0});
    if (modal) modal.style.display = 'none';
    if (_gpmCaptureCancelled) return;
    if (!r || !r.ok) {
        showErrorToast(r && r.err ? r.err : 'Capture failed.');
        return;
    }
    if (r.button === btn) {
        showWarnToast('Captured — bound the same physical button to itself? Pick a target action above.');
        return;
    }
    // Reassign: physical button r.button → whatever target btn is now
    if (!_gpmCurrentProfile) return;
    var existing = _gpmCurrentProfile.buttons[btn] || {type: 'passthrough'};
    if (existing.type === 'passthrough') {
        existing = {type: 'key', vk: 0x20};
    }
    delete _gpmCurrentProfile.buttons[btn];
    _gpmCurrentProfile.buttons[r.button] = existing;

    // Also stash the controller's VID/PID for HidHide so "hide physical"
    // knows which device to cloak.  We pull it from the JS Gamepad API
    // (running gamepad list).
    if (!_gpmCurrentProfile.controller_vid_pid) {
        var pads = navigator.getGamepads ? navigator.getGamepads() : [];
        for (var i = 0; i < pads.length; i++) {
            var p = pads[i];
            if (!p || !p.id) continue;
            // Look for "(Vendor: xxxx Product: yyyy)" — desktop Chrome format
            var m = p.id.match(/Vendor:\s*([0-9a-fA-F]{4})\s+Product:\s*([0-9a-fA-F]{4})/);
            if (m) {
                _gpmCurrentProfile.controller_vid_pid = 'VID_' + m[1].toUpperCase() + '&PID_' + m[2].toUpperCase();
                break;
            }
        }
    }

    loadGpmProfileBindings(_gpmCurrentProfile.name);
    showInfoToast('Reassigned to physical button: ' + r.button);
}

function cancelGpmCapture() {
    _gpmCaptureCancelled = true;
    var modal = document.getElementById('gpm-capture-modal');
    if (modal) modal.style.display = 'none';
}

// ─── Macro editor ───────────────────────────────────────────────────
function openGpmMacroEditor(btn) {
    if (!_gpmCurrentProfile) return;
    _gpmMacroEditorBtn = btn;
    var binding = _gpmCurrentProfile.buttons[btn];
    if (!binding || binding.type !== 'macro') {
        binding = {type: 'macro', steps: []};
        _gpmCurrentProfile.buttons[btn] = binding;
    }
    _gpmMacroEditorSteps = JSON.parse(JSON.stringify(binding.steps || []));
    document.getElementById('gpm-macro-btn-name').textContent = btn;
    _renderMacroSteps();
    var modal = document.getElementById('gpm-macro-modal');
    if (modal) modal.style.display = 'flex';
}

// v3.4 — macro steps are now polymorphic: {key:{vk,...}} | {mouse:{button,...}}
// | {vbutton:{vbutton,...}} | {vstick:{stick, x, y, ...}}.  Legacy
// {vk, hold_ms, delay_after_ms} is also rendered for back-compat (old
// saved profiles).
function _macroStepKind(step) {
    if (!step || typeof step !== 'object') return 'key';
    if (step.key)      return 'key';
    if (step.mouse)    return 'mouse';
    if (step.vbutton)  return 'vbutton';
    if (step.vstick)   return 'vstick';
    if (step.vtrigger) return 'vtrigger';
    if (step.text)     return 'text';
    if ('vk' in step)  return 'legacy';
    return 'key';
}

function _macroStepInner(step, kind) {
    if (kind === 'legacy') return step;     // legacy lives on the step itself
    return step[kind] || {};
}

function _renderMacroSteps() {
    var stepsEl = document.getElementById('gpm-macro-steps');
    if (!stepsEl) return;
    if (!_gpmMacroEditorSteps.length) {
        stepsEl.innerHTML = '<div style="color:var(--text-tertiary);font-style:italic;padding:12px 0">No steps yet — click <b>● Record</b> to capture a sequence, or <b>+ Add step manually</b>.</div>';
        return;
    }
    stepsEl.innerHTML = _gpmMacroEditorSteps.map(function(step, i){
        var kind = _macroStepKind(step);
        var inner = _macroStepInner(step, kind);
        var mode = inner.mode || 'tap';
        var disabled = !!step.disabled;
        // Step kind picker (key / mouse / vbutton / vstick / vtrigger / text)
        var kindOpts = ['key','mouse','vbutton','vstick','vtrigger','text'].map(function(k){
            var label = {key:'Key', mouse:'Mouse', vbutton:'V-btn',
                         vstick:'V-stick', vtrigger:'V-trig', text:'Text'}[k];
            return '<option value="' + k + '"' +
                ((kind === k || (kind === 'legacy' && k === 'key')) ? ' selected' : '') +
                '>' + label + '</option>';
        }).join('');

        // Mode picker (tap / down / up)
        var modeOpts = [
            ['tap',  'Tap'],
            ['down', 'Hold ↓'],
            ['up',   'Release ↑'],
        ].map(function(o){
            return '<option value="' + o[0] + '"' + (mode === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
        }).join('');

        // Target picker (depends on kind)
        var targetSel = '';
        if (kind === 'key' || kind === 'legacy') {
            var keyOpts = GPM_KEY_OPTIONS.map(function(o){
                return '<option value="' + o.vk + '"' + (inner.vk === o.vk ? ' selected' : '') + '>' + escHtml(o.label) + '</option>';
            }).join('');
            targetSel = '<select onchange="onMacroStepChange(' + i + ', \'vk\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;flex:1;min-width:80px">' + keyOpts + '</select>';
        } else if (kind === 'mouse') {
            var mb = ['M_LEFT','M_RIGHT','M_MIDDLE','M_X1','M_X2','M_WHEEL_UP','M_WHEEL_DOWN'];
            var mbOpts = mb.map(function(b){
                return '<option value="' + b + '"' + (inner.button === b ? ' selected' : '') + '>' + b + '</option>';
            }).join('');
            targetSel = '<select onchange="onMacroStepChange(' + i + ', \'button\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;flex:1;min-width:80px">' + mbOpts + '</select>';
        } else if (kind === 'vbutton') {
            var vbList = (_gpmState && (_gpmState.available_vbuttons || _gpmState.available_buttons)) || [];
            var vbOpts = vbList.map(function(b){
                var label = b;
                if (b === 'TOUCHPAD' || b === 'PS') label = b + ' (DS4)';
                return '<option value="' + b + '"' + (inner.vbutton === b ? ' selected' : '') + '>' + escHtml(label) + '</option>';
            }).join('');
            targetSel = '<select onchange="onMacroStepChange(' + i + ', \'vbutton\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px;flex:1;min-width:80px">' + vbOpts + '</select>';
        } else if (kind === 'vstick') {
            var stickSide = inner.stick || 'left';
            var stickX = (inner.x !== undefined ? inner.x : 0);
            var stickY = (inner.y !== undefined ? inner.y : 0);
            var rampMs = inner.ramp_ms || 0;
            var sideOpts = [['left','L stick'],['right','R stick']].map(function(o){
                return '<option value="' + o[0] + '"' + (stickSide === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
            }).join('');
            // Ranges:  -1 = full up/left,  0 = center,  +1 = full down/right.
            // For a "small constant pull" (anti-recoil style) use a small
            // POSITIVE y like y=+0.2 (gentle 20% pull DOWN).  Negative y
            // pulls UP.  Snapping is instant unless ramp>0.
            targetSel = '<select onchange="onMacroStepChange(' + i + ', \'stick\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" title="Which virtual stick to drive">' + sideOpts + '</select>' +
                ' <label style="font-size:11px;color:var(--text-tertiary)" title="-1 = full left, 0 = center, +1 = full right">x</label>' +
                '<input type="number" step="0.05" min="-1" max="1" value="' + stickX + '" onchange="onMacroStepChange(' + i + ', \'x\', this.value)" style="width:55px;padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" title="-1 = full left, 0 = center, +1 = full right" />' +
                ' <label style="font-size:11px;color:var(--text-tertiary)" title="-1 = full up, 0 = center, +1 = full down">y</label>' +
                '<input type="number" step="0.05" min="-1" max="1" value="' + stickY + '" onchange="onMacroStepChange(' + i + ', \'y\', this.value)" style="width:55px;padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" title="-1 = full up, 0 = center, +1 = full down (use small values like -0.2 for a gentle pull)" />' +
                ' <label style="font-size:11px;color:var(--text-tertiary)" title="ms to smoothly ramp from center to target. 0 = snap instantly. Higher = smoother continuous-pull feel.">ramp</label>' +
                '<input type="number" min="0" value="' + rampMs + '" onchange="onMacroStepChange(' + i + ', \'ramp_ms\', this.value)" style="width:55px;padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" title="ms to smoothly ramp from center to target. 0 = snap instantly. Higher = smoother continuous-pull feel." />';
        } else if (kind === 'vtrigger') {
            var trigSide = inner.trigger || 'lt';
            var trigValue = (inner.value !== undefined ? inner.value : 1.0);
            var sideOpts = [['lt','LT'],['rt','RT']].map(function(o){
                return '<option value="' + o[0] + '"' + (trigSide === o[0] ? ' selected' : '') + '>' + o[1] + ' (' + (o[0] === 'lt' ? 'L' : 'R') + ' trigger)</option>';
            }).join('');
            targetSel = '<select onchange="onMacroStepChange(' + i + ', \'trigger\', this.value)" style="padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px">' + sideOpts + '</select>' +
                ' <label style="font-size:11px;color:var(--text-tertiary)" title="0 = released, 1 = fully pulled">value</label>' +
                '<input type="number" step="0.05" min="0" max="1" value="' + trigValue + '" onchange="onMacroStepChange(' + i + ', \'value\', this.value)" style="width:55px;padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" title="0.0 = trigger released, 1.0 = trigger fully pulled" />';
        } else if (kind === 'text') {
            var textVal = inner.text || '';
            var perChar = inner.delay_per_char_ms || 0;
            targetSel =
                '<input type="text" placeholder="text to type" value="' + escAttr(textVal) + '" onchange="onMacroStepChange(' + i + ', \'text\', this.value)" style="flex:1;min-width:120px;padding:4px 8px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" />' +
                ' <label style="font-size:11px;color:var(--text-tertiary)">per-char</label>' +
                '<input type="number" min="0" value="' + perChar + '" onchange="onMacroStepChange(' + i + ', \'delay_per_char_ms\', this.value)" style="width:50px;padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" />';
        }

        // Hold + delay (works for all kinds — read down_ms with hold_ms fallback for legacy)
        var holdVal = inner.down_ms !== undefined ? inner.down_ms : (inner.hold_ms || 30);
        var delayVal = inner.delay_after_ms || 0;
        // Text kind ignores tap/hold/up modes — the burst types and exits.
        // Hide the mode picker + down field for text steps to keep it clean.
        var showMode = (kind !== 'text');
        var showDown = showMode && (mode === 'tap');
        var modeSel = showMode
            ? '<select onchange="onMacroStepChange(' + i + ', \'mode\', this.value)" style="padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:11px" title="Tap = press+release, Hold ↓ = press only, Release ↑ = release prior hold">' + modeOpts + '</select>'
            : '';
        var rowStyle = 'display:flex;align-items:center;gap:6px;padding:6px 0;font-size:12.5px;flex-wrap:wrap'
            + (disabled ? ';opacity:0.45;text-decoration:line-through' : '');
        return '<div style="' + rowStyle + '">' +
                  '<span style="color:var(--text-tertiary);min-width:20px">' + (i+1) + '.</span>' +
                  // Step actions: move up, move down, duplicate, disable
                  '<button class="btn btn-sm" onclick="moveMacroStep(' + i + ', -1)" title="Move up" style="font-size:11px;padding:2px 6px"' + (i === 0 ? ' disabled' : '') + '>↑</button>' +
                  '<button class="btn btn-sm" onclick="moveMacroStep(' + i + ', 1)" title="Move down" style="font-size:11px;padding:2px 6px"' + (i === _gpmMacroEditorSteps.length - 1 ? ' disabled' : '') + '>↓</button>' +
                  '<button class="btn btn-sm" onclick="toggleMacroStepDisabled(' + i + ')" title="' + (disabled ? 'Enable' : 'Disable') + ' this step" style="font-size:11px;padding:2px 6px">' + (disabled ? '◯' : '⊘') + '</button>' +
                  '<button class="btn btn-sm" onclick="duplicateMacroStep(' + i + ')" title="Duplicate" style="font-size:11px;padding:2px 6px">⎘</button>' +
                  '<select onchange="onMacroStepKindChange(' + i + ', this.value)" style="padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:11px">' + kindOpts + '</select>' +
                  modeSel +
                  targetSel +
                  (showDown ?
                    '<label style="font-size:11px;color:var(--text-tertiary)" title="How long to hold this input down before releasing">down</label>' +
                    '<input type="number" value="' + holdVal + '" onchange="onMacroStepChange(' + i + ', \'down_ms\', this.value)" style="width:55px;padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" />'
                    : '') +
                  '<label style="font-size:11px;color:var(--text-tertiary)" title="ms to wait before moving to the next macro step">then wait</label>' +
                  '<input type="number" value="' + delayVal + '" onchange="onMacroStepChange(' + i + ', \'delay_after_ms\', this.value)" style="width:55px;padding:4px 6px;border-radius:4px;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-faint);font-size:12px" />' +
                  '<button class="btn btn-sm btn-danger" onclick="removeMacroStep(' + i + ')" style="font-size:11px;padding:2px 6px">×</button>' +
               '</div>';
    }).join('');
}

// ─── Step action helpers ─────────────────────────────────────────────
function moveMacroStep(i, direction) {
    var j = i + direction;
    if (j < 0 || j >= _gpmMacroEditorSteps.length) return;
    var tmp = _gpmMacroEditorSteps[i];
    _gpmMacroEditorSteps[i] = _gpmMacroEditorSteps[j];
    _gpmMacroEditorSteps[j] = tmp;
    _renderMacroSteps();
}

function duplicateMacroStep(i) {
    var src = _gpmMacroEditorSteps[i];
    if (!src) return;
    var copy = JSON.parse(JSON.stringify(src));
    _gpmMacroEditorSteps.splice(i + 1, 0, copy);
    _renderMacroSteps();
}

function toggleMacroStepDisabled(i) {
    var step = _gpmMacroEditorSteps[i];
    if (!step) return;
    step.disabled = !step.disabled;
    _renderMacroSteps();
}

function addGpmMacroStep() {
    // Default delay_after_ms is 0 — new steps fire back-to-back instantly
    // unless the user explicitly types in a delay.  Avoids the 50ms
    // implicit pause that surprised users in early v3.4 builds.
    _gpmMacroEditorSteps.push({key: {vk: 0x20, down_ms: 30,
                                      delay_after_ms: 0, mode: 'tap'}});
    _renderMacroSteps();
}

function removeMacroStep(i) {
    _gpmMacroEditorSteps.splice(i, 1);
    _renderMacroSteps();
}

function onMacroStepKindChange(i, newKind) {
    if (!_gpmMacroEditorSteps[i]) return;
    var step = _gpmMacroEditorSteps[i];
    var oldKind = _macroStepKind(step);
    var oldInner = _macroStepInner(step, oldKind);
    var hold = oldInner.down_ms !== undefined ? oldInner.down_ms : (oldInner.hold_ms || 30);
    var delay = oldInner.delay_after_ms || 0;
    var mode = oldInner.mode || 'tap';
    var newStep = {};
    if (newKind === 'key')      newStep.key      = {vk: 0x20,         down_ms: hold, delay_after_ms: delay, mode: mode};
    if (newKind === 'mouse')    newStep.mouse    = {button: 'M_LEFT', down_ms: hold, delay_after_ms: delay, mode: mode};
    if (newKind === 'vbutton')  newStep.vbutton  = {vbutton: 'A',     down_ms: hold, delay_after_ms: delay, mode: mode};
    if (newKind === 'vstick')   newStep.vstick   = {stick: 'left', x: 0, y: -1, down_ms: hold, delay_after_ms: delay, mode: mode};
    if (newKind === 'vtrigger') newStep.vtrigger = {trigger: 'rt', value: 1.0, down_ms: hold, delay_after_ms: delay, mode: mode};
    if (newKind === 'text')     newStep.text     = {text: '',           delay_per_char_ms: 0, delay_after_ms: delay};
    // Preserve disabled flag across kind changes
    if (step.disabled) newStep.disabled = true;
    _gpmMacroEditorSteps[i] = newStep;
    _renderMacroSteps();
}

function onMacroStepChange(i, field, val) {
    var step = _gpmMacroEditorSteps[i];
    if (!step) return;
    var kind = _macroStepKind(step);
    if (kind === 'legacy') {
        // Convert legacy to v3.4 key step on first edit
        var newStep = {key: {
            vk: step.vk,
            down_ms: step.hold_ms || 30,
            delay_after_ms: step.delay_after_ms || 0,
            mode: 'tap',
        }};
        _gpmMacroEditorSteps[i] = newStep;
        step = newStep;
        kind = 'key';
    }
    var inner = step[kind];
    // Field types:
    //   vk / down_ms / delay_after_ms  → integer
    //   x / y                           → float (-1..1)
    //   stick / button / vbutton / mode → string
    if (field === 'vk' || field === 'down_ms' || field === 'delay_after_ms'
            || field === 'ramp_ms' || field === 'delay_per_char_ms') {
        inner[field] = Math.max(0, parseInt(val, 10) || 0);
    } else if (field === 'x' || field === 'y') {
        var n = parseFloat(val);
        if (isNaN(n)) n = 0;
        inner[field] = Math.max(-1, Math.min(1, n));
    } else if (field === 'value') {
        // 0..1 float — vtrigger pull amount
        var v = parseFloat(val);
        if (isNaN(v)) v = 0;
        inner[field] = Math.max(0, Math.min(1, v));
    } else {
        inner[field] = val;
    }
    // If mode changed, the row's down/wait label changes — re-render
    if (field === 'mode') _renderMacroSteps();
}

function saveGpmMacroEditor() {
    if (!_gpmCurrentProfile || !_gpmMacroEditorBtn) return;
    // v3.4 — _gpmMacroEditorBtn is a mapping ID (random hex string)
    var m = _findMapping(_gpmMacroEditorBtn);
    if (m) {
        m.output = {
            target:        'macro',
            steps:         _gpmMacroEditorSteps,
            play_mode:     _gpmMacroPlayMode || 'once',
            repeat_count:  _gpmMacroRepeatCount || 2,
        };
    }
    var modal = document.getElementById('gpm-macro-modal');
    if (modal) modal.style.display = 'none';
    _gpmAutoSave();
    _gpmRenderBindingsLocal();
}

function cancelGpmMacroEditor() {
    var modal = document.getElementById('gpm-macro-modal');
    if (modal) modal.style.display = 'none';
    _gpmMacroEditorBtn = null;
    _gpmMacroEditorSteps = [];
    // If a recording session is active, kill it
    if (_gpmRecordingTimer) {
        cancelMacroRecording();
    }
}

// ─── v3.4 macro recording ────────────────────────────────────────────
var _gpmRecordingTimer = null;

// ─── Recoil preset picker ──────────────────────────────────────────
var _gpmRecoilPresets = null;

async function openRecoilPresetPicker() {
    var modal = document.getElementById('gpm-recoil-modal');
    var listEl = document.getElementById('gpm-recoil-list');
    if (!modal || !listEl) return;
    modal.style.display = 'flex';
    listEl.innerHTML = '<div style="color:var(--text-tertiary)">Loading presets…</div>';
    // Always re-fetch so freshly-captured user presets appear immediately
    var r = await apiGet('/api/gamepad/recoil-presets');
    _gpmRecoilPresets = (r && r.presets) || [];

    // Capture availability — hide the buttons gracefully if libs missing
    var avail = await apiGet('/api/gamepad/capture/availability');
    var captureRow = '';
    if (avail && avail.available) {
        captureRow =
            '<div style="display:flex;gap:8px;flex-wrap:wrap;padding:10px 12px;background:var(--bg-elevated);border-radius:6px;margin-bottom:14px;border:1px dashed var(--border-faint)">' +
                '<div style="flex:1;min-width:200px;font-size:11px;color:var(--text-tertiary);line-height:1.4">' +
                    '<b style="color:var(--text-bright)">Capture your own pattern</b><br/>' +
                    'Pick a method.  Counter-pull is the simplest: fire + manually compensate, we record what you do.' +
                '</div>' +
                '<button class="btn btn-sm" onclick="openCaptureCounterPullModal()" title="Fire + counter-pull manually with your mouse or right stick — we record what you do as the pattern.  Works in any game; quality depends on your counter-pull technique.">✋ Counter-pull</button>' +
                '<button class="btn btn-sm" onclick="openCaptureScreenModal()" title="Fire while we screenshot the foreground window — phase correlation extracts the camera drift">📹 From gameplay</button>' +
                '<button class="btn btn-sm" onclick="openCaptureBulletModal()" title="Snapshot before + after firing — we diff and detect bullet hole positions on the wall">🎯 From bullet holes</button>' +
            '</div>';
    } else if (avail && !avail.available) {
        captureRow = '<div style="padding:8px 12px;background:var(--bg-elevated);border-radius:6px;margin-bottom:14px;font-size:11px;color:var(--text-tertiary)">' +
            'Capture features unavailable in this build (' + escHtml(avail.err || 'libs missing') + ')' +
        '</div>';
    }

    if (!_gpmRecoilPresets.length) {
        listEl.innerHTML = captureRow + '<div style="color:var(--text-tertiary);font-style:italic">No presets yet — capture one above, or check the bundled list.</div>';
        return;
    }
    // Group by game, render cards.  User-captured presets get an extra
    // delete button + a "captured" badge.
    var byGame = {};
    _gpmRecoilPresets.forEach(function(p){
        if (!byGame[p.game]) byGame[p.game] = [];
        byGame[p.game].push(p);
    });
    var sortedGames = Object.keys(byGame).sort(function(a, b){
        // Put "(captured)" group first so user-made stuff is visible
        if (a === '(captured)') return -1;
        if (b === '(captured)') return 1;
        return a.localeCompare(b);
    });
    listEl.innerHTML = captureRow + sortedGames.map(function(game){
        var rows = byGame[game].map(function(p){
            var userBadge = p.is_user
                ? '<span style="margin-left:6px;padding:1px 6px;background:var(--accent);color:var(--bg);border-radius:3px;font-size:10px;font-weight:600">CAPTURED</span>'
                : '';
            var deleteBtn = p.is_user
                ? '<button class="btn btn-sm btn-danger" onclick="event.stopPropagation();deleteUserRecoilPreset(\'' + escAttr(p.id) + '\')" style="font-size:10px;padding:2px 6px;margin-left:8px" title="Delete this captured preset">✕</button>'
                : '';
            return '<div onclick="applyRecoilPreset(\'' + escAttr(p.id) + '\')" style="cursor:pointer;padding:10px 12px;background:var(--bg-overlay);border:1px solid var(--border-faint);border-radius:6px;margin-bottom:6px;transition:background 0.12s" onmouseover="this.style.background=\'var(--bg-elevated)\'" onmouseout="this.style.background=\'var(--bg-overlay)\'">' +
                '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:4px">' +
                    '<div><b style="color:var(--text-bright);font-size:13px">' + escHtml(p.weapon || p.name) + '</b>' + userBadge + '</div>' +
                    '<div style="display:flex;align-items:center;gap:4px">' +
                        '<span style="font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono,monospace)">' + (p.shots || 0) + ' steps · ' + (p.duration_ms || 0) + ' ms each · ' + escHtml(p.play_mode) + '</span>' +
                        deleteBtn +
                    '</div>' +
                '</div>' +
                '<div style="font-size:12px;color:var(--text-secondary);line-height:1.5">' + escHtml(p.description || '') + '</div>' +
            '</div>';
        }).join('');
        return '<div style="margin-bottom:12px">' +
                  '<div style="font-size:11px;color:var(--text-tertiary);font-weight:600;letter-spacing:0.04em;text-transform:uppercase;margin-bottom:6px">' + escHtml(game) + '</div>' +
                  rows +
               '</div>';
    }).join('');
}

async function deleteUserRecoilPreset(presetId) {
    if (!confirm('Delete this captured recoil preset? This cannot be undone.')) return;
    var r = await apiPost('/api/gamepad/capture/delete', {id: presetId});
    if (r && r.ok) {
        _gpmRecoilPresets = null;   // force refetch
        await openRecoilPresetPicker();
    } else {
        showErrorToast('Delete failed: ' + ((r && r.err) || 'unknown'));
    }
}

function closeRecoilPresetPicker() {
    var modal = document.getElementById('gpm-recoil-modal');
    if (modal) modal.style.display = 'none';
}

function applyRecoilPreset(presetId) {
    var preset = (_gpmRecoilPresets || []).find(function(p){ return p.id === presetId; });
    if (!preset) return;
    if (!confirm('Replace current macro steps with the "' + preset.name + '" preset?\n\n' +
                 'This will overwrite ' + (_gpmMacroEditorSteps.length || 0) + ' existing step(s).\n\n' +
                 'Anti-cheat warning: do not use in ranked competitive games.')) {
        return;
    }
    _gpmMacroEditorSteps = JSON.parse(JSON.stringify(preset.steps || []));
    _gpmMacroPlayMode = preset.play_mode || 'once';
    // Sync the play-mode UI
    var pm = document.getElementById('gpm-macro-play-mode');
    if (pm) pm.value = _gpmMacroPlayMode;
    var rcWrap = document.getElementById('gpm-macro-repeat-wrap');
    if (rcWrap) rcWrap.style.display = (_gpmMacroPlayMode === 'repeat_n' ? '' : 'none');
    closeRecoilPresetPicker();
    _renderMacroSteps();
}

// ─── v3.6 — recoil pattern capture from gameplay ─────────────────────
var _captureScreenState = {
    captured: null,    // server-returned binned trace after stop
    pollTimer: null,
};

function openCaptureScreenModal() {
    var m = document.getElementById('gpm-capture-screen-modal');
    if (m) m.style.display = 'flex';
    _captureScreenState = {captured: null, pollTimer: null};
    var status = document.getElementById('gpm-capture-screen-status');
    if (status) status.innerHTML = '<span style="color:var(--text-tertiary)">Idle — fill in details and click Start.</span>';
    var saveBtn = document.getElementById('gpm-capture-screen-save');
    if (saveBtn) saveBtn.disabled = true;
}

function closeCaptureScreenModal() {
    var m = document.getElementById('gpm-capture-screen-modal');
    if (m) m.style.display = 'none';
    if (_captureScreenState.pollTimer) {
        clearInterval(_captureScreenState.pollTimer);
        _captureScreenState.pollTimer = null;
    }
}

async function startCaptureScreen() {
    var rpm = parseInt(document.getElementById('gpm-capture-screen-rpm').value, 10) || 600;
    var dur = parseFloat(document.getElementById('gpm-capture-screen-dur').value) || 6.0;
    var autoFire = !!(document.getElementById('gpm-capture-screen-autofire') &&
                       document.getElementById('gpm-capture-screen-autofire').checked);
    var status = document.getElementById('gpm-capture-screen-status');
    var saveBtn = document.getElementById('gpm-capture-screen-save');
    var startBtn = document.getElementById('gpm-capture-screen-start');
    var stopBtn  = document.getElementById('gpm-capture-screen-stop');
    if (saveBtn) saveBtn.disabled = true;

    if (status) status.innerHTML = '<span style="color:var(--orange,#fbbf24)">3… 2… 1… alt-tab to your game now' +
        (autoFire ? ' (we\'ll fire RT for you)' : ' and FIRE') + '</span>';
    // Brief countdown so the user has time to focus the game window
    await new Promise(function(res){ setTimeout(res, 3000); });

    var r = await apiPost('/api/gamepad/capture/screen/start',
                          {rpm: rpm, max_duration_sec: dur, capture_hz: 120,
                           auto_fire: autoFire});
    if (!r || !r.ok) {
        if (status) status.innerHTML = '<span style="color:var(--danger,#f87171)">Failed: ' + escHtml((r && r.err) || 'unknown') + '</span>';
        return;
    }
    if (autoFire && !r.autofire_engaged) {
        // Asked for autofire, didn't get it — warn but continue (user
        // can still fire manually).  Don't block the capture; the
        // common case is "user enabled the box but mapper isn't on".
        if (status) status.innerHTML = '<span style="color:var(--orange,#fbbf24)">Auto-fire couldn\'t engage (virtual pad not running) — fire RT manually instead.  Capturing…</span>';
    }
    if (startBtn) startBtn.disabled = true;
    if (stopBtn)  stopBtn.disabled  = false;

    if (_captureScreenState.pollTimer) clearInterval(_captureScreenState.pollTimer);
    _captureScreenState.pollTimer = setInterval(async function(){
        var s = await apiGet('/api/gamepad/capture/status');
        if (!s || !s.active) {
            // Capture ended on its own — pull final result
            clearInterval(_captureScreenState.pollTimer);
            _captureScreenState.pollTimer = null;
            await stopCaptureScreen(true);
            return;
        }
        if (status) status.innerHTML = '<span style="color:var(--accent)">Capturing… ' + (s.elapsed_sec || 0).toFixed(1) + 's, ' + (s.frames || 0) + ' frames</span>';
    }, 200);
}

async function stopCaptureScreen(autoTriggered) {
    var startBtn = document.getElementById('gpm-capture-screen-start');
    var stopBtn  = document.getElementById('gpm-capture-screen-stop');
    var status   = document.getElementById('gpm-capture-screen-status');
    var saveBtn  = document.getElementById('gpm-capture-screen-save');
    if (_captureScreenState.pollTimer) {
        clearInterval(_captureScreenState.pollTimer);
        _captureScreenState.pollTimer = null;
    }
    var r = await apiPost('/api/gamepad/capture/screen/stop', {});
    if (startBtn) startBtn.disabled = false;
    if (stopBtn)  stopBtn.disabled  = true;
    if (!r || !r.ok) {
        if (status) status.innerHTML = '<span style="color:var(--danger,#f87171)">Stop failed: ' + escHtml((r && r.err) || 'unknown') + '</span>';
        return;
    }
    _captureScreenState.captured = r;
    var binned = r.binned || [];
    if (status) {
        status.innerHTML =
            '<span style="color:var(--accent)">Captured ' + binned.length + ' shots from ' + (r.raw_count || 0) + ' frames.</span>' +
            '<br/><span style="font-size:11px;color:var(--text-tertiary)">' +
            _summarizeBinnedTrace(binned) +
            '</span>';
    }
    if (saveBtn) saveBtn.disabled = false;
}

function _summarizeBinnedTrace(binned) {
    if (!binned.length) return 'No shots binned — try a higher RPM or longer duration';
    var maxAbsX = 0, maxAbsY = 0, sumDx = 0, sumDy = 0;
    binned.forEach(function(b){
        if (Math.abs(b.dx) > maxAbsX) maxAbsX = Math.abs(b.dx);
        if (Math.abs(b.dy) > maxAbsY) maxAbsY = Math.abs(b.dy);
        sumDx += b.dx; sumDy += b.dy;
    });
    return 'Σ dx=' + sumDx + ' px · Σ dy=' + sumDy + ' px · max |dx|=' + maxAbsX + ' · max |dy|=' + maxAbsY;
}

async function saveCaptureScreen() {
    var captured = _captureScreenState.captured;
    if (!captured) return;
    var name    = (document.getElementById('gpm-capture-screen-name').value || '').trim();
    var weapon  = (document.getElementById('gpm-capture-screen-weapon').value || '').trim();
    var game    = (document.getElementById('gpm-capture-screen-game').value || '').trim();
    var sens    = parseFloat(document.getElementById('gpm-capture-screen-sens').value) || 0.005;
    var playMode = document.getElementById('gpm-capture-screen-playmode').value || 'once';
    if (!name) { showWarnToast('Give the preset a name first.'); return; }
    var r = await apiPost('/api/gamepad/capture/save', {
        name: name, weapon: weapon, game: game,
        rpm: captured.rpm, binned: captured.binned,
        method: 'screen', sensitivity_scale: sens,
        play_mode: playMode, auto_hold_rt: true,
    });
    if (!r || !r.ok) { showErrorToast('Save failed: ' + ((r && r.err) || 'unknown')); return; }
    showInfoToast('Saved as "' + (r.preset && r.preset.name) + '" (' + (r.preset && r.preset.shots) + ' steps).  Find it in the recoil presets list.');
    closeCaptureScreenModal();
    _gpmRecoilPresets = null;
    await openRecoilPresetPicker();
}

// ─── Bullet-hole capture workflow ────────────────────────────────────
function openCaptureBulletModal() {
    var m = document.getElementById('gpm-capture-bullet-modal');
    if (m) m.style.display = 'flex';
    _refreshBulletState();
}

function closeCaptureBulletModal() {
    var m = document.getElementById('gpm-capture-bullet-modal');
    if (m) m.style.display = 'none';
}

async function _refreshBulletState() {
    var s = await apiGet('/api/gamepad/capture/bullet-holes/state');
    var b = document.getElementById('gpm-capture-bullet-before');
    var a = document.getElementById('gpm-capture-bullet-after');
    var d = document.getElementById('gpm-capture-bullet-detect');
    if (b) b.textContent = s && s.have_before ? '✓ Before captured' : 'Capture before';
    if (a) a.textContent = s && s.have_after  ? '✓ After captured'  : 'Capture after';
    if (d) d.disabled = !(s && s.have_before && s.have_after);
}

async function captureBulletBefore() {
    var r = await apiPost('/api/gamepad/capture/bullet-holes/snapshot', {which: 'before'});
    if (!r || !r.ok) showErrorToast('Capture before failed: ' + ((r && r.err) || 'unknown'));
    _refreshBulletState();
}

async function captureBulletAfter() {
    var r = await apiPost('/api/gamepad/capture/bullet-holes/snapshot', {which: 'after'});
    if (!r || !r.ok) showErrorToast('Capture after failed: ' + ((r && r.err) || 'unknown'));
    _refreshBulletState();
}

// ─── v3.6 — counter-pull capture (option B) ──────────────────────────
var _captureCounterPullState = {captured: null, pollTimer: null};

function openCaptureCounterPullModal() {
    var m = document.getElementById('gpm-capture-cp-modal');
    if (m) m.style.display = 'flex';
    _captureCounterPullState = {captured: null, pollTimer: null};
    var status = document.getElementById('gpm-capture-cp-status');
    if (status) status.innerHTML = '<span style="color:var(--text-tertiary)">Idle — fill in the form and click Start.</span>';
    var saveBtn = document.getElementById('gpm-capture-cp-save');
    if (saveBtn) saveBtn.disabled = true;
}

function closeCaptureCounterPullModal() {
    var m = document.getElementById('gpm-capture-cp-modal');
    if (m) m.style.display = 'none';
    if (_captureCounterPullState.pollTimer) {
        clearInterval(_captureCounterPullState.pollTimer);
        _captureCounterPullState.pollTimer = null;
    }
}

async function startCaptureCounterPull() {
    var rpm    = parseInt(document.getElementById('gpm-capture-cp-rpm').value, 10) || 600;
    var dur    = parseFloat(document.getElementById('gpm-capture-cp-dur').value) || 6.0;
    var source = document.getElementById('gpm-capture-cp-source').value;
    var autoFire = !!(document.getElementById('gpm-capture-cp-autofire') &&
                      document.getElementById('gpm-capture-cp-autofire').checked);
    var status = document.getElementById('gpm-capture-cp-status');
    var saveBtn = document.getElementById('gpm-capture-cp-save');
    var startBtn = document.getElementById('gpm-capture-cp-start');
    var stopBtn  = document.getElementById('gpm-capture-cp-stop');
    if (saveBtn) saveBtn.disabled = true;

    if (status) status.innerHTML = '<span style="color:var(--orange,#fbbf24)">3… 2… 1… alt-tab to your game now and FIRE while compensating</span>';
    await new Promise(function(res){ setTimeout(res, 3000); });

    var r = await apiPost('/api/gamepad/capture/counter-pull/start', {
        rpm: rpm, max_duration_sec: dur, source: source, auto_fire: autoFire
    });
    if (!r || !r.ok) {
        if (status) status.innerHTML = '<span style="color:var(--danger,#f87171)">Failed: ' + escHtml((r && r.err) || 'unknown') + '</span>';
        return;
    }
    if (autoFire && !r.autofire_engaged) {
        if (status) status.innerHTML = '<span style="color:var(--orange,#fbbf24)">Auto-fire couldn\'t engage — fire RT manually.  Recording…</span>';
    }
    if (startBtn) startBtn.disabled = true;
    if (stopBtn)  stopBtn.disabled  = false;

    if (_captureCounterPullState.pollTimer) clearInterval(_captureCounterPullState.pollTimer);
    _captureCounterPullState.pollTimer = setInterval(async function(){
        var s = await apiGet('/api/gamepad/capture/status');
        if (!s || !s.active) {
            clearInterval(_captureCounterPullState.pollTimer);
            _captureCounterPullState.pollTimer = null;
            await stopCaptureCounterPull(true);
            return;
        }
        if (status) {
            var n = s.samples || 0;
            var color = n > 0 ? 'var(--accent)' : 'var(--orange,#fbbf24)';
            var hint = n > 0
                ? ''
                : ' — no input arriving yet, are you actually moving the stick / mouse?';
            status.innerHTML = '<span style="color:' + color + '">Recording counter-pull… ' + (s.elapsed_sec || 0).toFixed(1) + 's · ' + n + ' samples' + hint + '</span>';
        }
    }, 200);
}

async function stopCaptureCounterPull(autoTriggered) {
    var startBtn = document.getElementById('gpm-capture-cp-start');
    var stopBtn  = document.getElementById('gpm-capture-cp-stop');
    var status   = document.getElementById('gpm-capture-cp-status');
    var saveBtn  = document.getElementById('gpm-capture-cp-save');
    if (_captureCounterPullState.pollTimer) {
        clearInterval(_captureCounterPullState.pollTimer);
        _captureCounterPullState.pollTimer = null;
    }
    var r = await apiPost('/api/gamepad/capture/counter-pull/stop', {});
    if (startBtn) startBtn.disabled = false;
    if (stopBtn)  stopBtn.disabled  = true;
    if (!r || !r.ok) {
        if (status) status.innerHTML = '<span style="color:var(--danger,#f87171)">Stop failed: ' + escHtml((r && r.err) || 'unknown') + '</span>';
        return;
    }
    _captureCounterPullState.captured = r;
    var binned = r.binned || [];
    if (status) {
        status.innerHTML =
            '<span style="color:var(--accent)">Captured ' + binned.length + ' shots from ' + (r.raw_count || 0) + ' samples (source: ' + escHtml(r.source) + ').</span>' +
            '<br/><span style="font-size:11px;color:var(--text-tertiary)">' +
            _summarizeBinnedTrace(binned) +
            '</span>';
    }
    if (saveBtn) saveBtn.disabled = false;
}

async function saveCaptureCounterPull() {
    var captured = _captureCounterPullState.captured;
    if (!captured) return;
    var name    = (document.getElementById('gpm-capture-cp-name').value || '').trim();
    var weapon  = (document.getElementById('gpm-capture-cp-weapon').value || '').trim();
    var game    = (document.getElementById('gpm-capture-cp-game').value || '').trim();
    var sens    = parseFloat(document.getElementById('gpm-capture-cp-sens').value) || 0.005;
    var playMode = document.getElementById('gpm-capture-cp-playmode').value || 'once';
    if (!name) { showWarnToast('Give the preset a name first.'); return; }
    var r = await apiPost('/api/gamepad/capture/save', {
        name: name, weapon: weapon, game: game,
        rpm: captured.rpm, binned: captured.binned,
        method: 'counter_pull_' + (captured.source || 'mouse'),
        sensitivity_scale: sens,
        play_mode: playMode, auto_hold_rt: true,
    });
    if (!r || !r.ok) { showErrorToast('Save failed: ' + ((r && r.err) || 'unknown')); return; }
    showInfoToast('Saved as "' + (r.preset && r.preset.name) + '" (' + (r.preset && r.preset.shots) + ' steps).');
    closeCaptureCounterPullModal();
    _gpmRecoilPresets = null;
    await openRecoilPresetPicker();
}

async function autoFireMag() {
    var dur = parseFloat(document.getElementById('gpm-capture-bullet-fire-dur').value) || 3.0;
    var status = document.getElementById('gpm-capture-bullet-status');
    if (status) status.innerHTML = '<span style="color:var(--orange,#fbbf24)">Alt-tab to your game now — firing RT for ' + dur + 's…</span>';
    // Give the user a brief moment to focus the game window
    await new Promise(function(res){ setTimeout(res, 1500); });
    var r = await apiPost('/api/gamepad/capture/auto-fire-mag', {duration_sec: dur});
    if (!r || !r.ok) {
        if (status) status.innerHTML = '<span style="color:var(--danger,#f87171)">Auto-fire failed: ' + escHtml((r && r.err) || 'unknown') + '</span>';
        return;
    }
    // Wait for the firing to actually finish (with margin), then prompt
    setTimeout(function(){
        if (status) status.innerHTML = '<span style="color:var(--accent)">Done firing.  Click <b>Capture after</b> to snapshot the wall.</span>';
    }, (dur + 0.3) * 1000);
}

async function detectBulletHoles() {
    var rpm = parseInt(document.getElementById('gpm-capture-bullet-rpm').value, 10) || 600;
    var thr = parseInt(document.getElementById('gpm-capture-bullet-threshold').value, 10) || 60;
    var status = document.getElementById('gpm-capture-bullet-status');
    if (status) status.innerHTML = '<span style="color:var(--text-tertiary)">Diffing snapshots…</span>';
    var r = await apiPost('/api/gamepad/capture/bullet-holes/detect',
                          {rpm: rpm, threshold: thr});
    if (!r || !r.ok) {
        if (status) status.innerHTML = '<span style="color:var(--danger,#f87171)">Detect failed: ' + escHtml((r && r.err) || 'unknown') + '</span>';
        return;
    }
    _captureBulletState = r;     // store for save
    if (status) {
        status.innerHTML = '<span style="color:var(--accent)">Found ' + r.count + ' bullet holes.</span>' +
            '<br/><span style="font-size:11px;color:var(--text-tertiary)">' +
            _summarizeBinnedTrace(r.binned) + '</span>';
    }
    var saveBtn = document.getElementById('gpm-capture-bullet-save');
    if (saveBtn) saveBtn.disabled = false;
}

var _captureBulletState = null;

async function resetBulletCapture() {
    await apiPost('/api/gamepad/capture/bullet-holes/reset', {});
    _captureBulletState = null;
    _refreshBulletState();
    var status = document.getElementById('gpm-capture-bullet-status');
    if (status) status.innerHTML = '';
    var saveBtn = document.getElementById('gpm-capture-bullet-save');
    if (saveBtn) saveBtn.disabled = true;
}

async function saveCaptureBullet() {
    var captured = _captureBulletState;
    if (!captured) return;
    var name    = (document.getElementById('gpm-capture-bullet-name').value || '').trim();
    var weapon  = (document.getElementById('gpm-capture-bullet-weapon').value || '').trim();
    var game    = (document.getElementById('gpm-capture-bullet-game').value || '').trim();
    var sens    = parseFloat(document.getElementById('gpm-capture-bullet-sens').value) || 0.005;
    var playMode = document.getElementById('gpm-capture-bullet-playmode').value || 'once';
    if (!name) { showWarnToast('Give the preset a name first.'); return; }
    var r = await apiPost('/api/gamepad/capture/save', {
        name: name, weapon: weapon, game: game,
        rpm: captured.rpm, binned: captured.binned,
        method: 'bullet_holes', sensitivity_scale: sens,
        play_mode: playMode, auto_hold_rt: true,
    });
    if (!r || !r.ok) { showErrorToast('Save failed: ' + ((r && r.err) || 'unknown')); return; }
    showInfoToast('Saved as "' + (r.preset && r.preset.name) + '" (' + (r.preset && r.preset.shots) + ' steps).');
    closeCaptureBulletModal();
    _gpmRecoilPresets = null;
    await openRecoilPresetPicker();
}

// v3.6 — options-first record flow.  Click "Record" → modal with
// source-filter checkboxes + countdown — Start → 2-sec countdown
// → recording.  Backwards-compat: if the modal isn't in the DOM
// (older builds) we fall back to the previous immediate-start flow.
function startMacroRecording() {
    var modal = document.getElementById('gpm-macro-record-options-modal');
    if (!modal) return _startMacroRecordingImmediate();
    modal.style.display = 'flex';
}

function closeMacroRecordOptions() {
    var modal = document.getElementById('gpm-macro-record-options-modal');
    if (modal) modal.style.display = 'none';
}

async function confirmMacroRecordOptions() {
    var get = function(id){
        var el = document.getElementById(id);
        return el ? !!el.checked : false;
    };
    var filters = {
        keyboard:            get('gpm-rec-opt-keyboard'),
        mouse_buttons:       get('gpm-rec-opt-mouse-buttons'),
        mouse_move:          get('gpm-rec-opt-mouse-move'),
        controller_buttons:  get('gpm-rec-opt-ctrl-buttons'),
        controller_sticks:   get('gpm-rec-opt-ctrl-sticks'),
        controller_triggers: get('gpm-rec-opt-ctrl-triggers'),
    };
    if (!Object.values(filters).some(Boolean)) {
        showWarnToast('Pick at least one source to record.');
        return;
    }
    var delaySec = parseFloat(document.getElementById('gpm-rec-opt-delay').value);
    if (!isFinite(delaySec) || delaySec < 0) delaySec = 2.0;
    closeMacroRecordOptions();
    await _startMacroRecordingWith(filters, delaySec);
}

async function _startMacroRecordingImmediate() {
    // Legacy fallback — record everything digital, no countdown.
    await _startMacroRecordingWith({
        keyboard: true, mouse_buttons: true, mouse_move: false,
        controller_buttons: true, controller_sticks: false,
        controller_triggers: true,
    }, 0.0);
}

async function _startMacroRecordingWith(filters, delaySec) {
    var r = await apiPost('/api/gamepad/macro/record/start', {
        skip_move: !filters.mouse_move,
        start_delay_sec: delaySec,
        filters: filters,
    });
    if (!r || !r.ok) {
        showErrorToast('Could not start recording: ' + (r && r.err || 'unknown'));
        return;
    }
    var rec = document.getElementById('gpm-macro-recording');
    if (rec) rec.style.display = 'block';
    var btn = document.getElementById('gpm-macro-record-btn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = (r.phase === 'countdown') ? 'Starting in ' + delaySec.toFixed(0) + 's…' : 'Recording…';
    }
    if (_gpmRecordingTimer) clearInterval(_gpmRecordingTimer);
    _gpmRecordingTimer = setInterval(async function(){
        var s = await apiGet('/api/gamepad/macro/record/status');
        var lbl = document.getElementById('gpm-macro-rec-timer');
        if (lbl && s) {
            if (s.phase === 'countdown') {
                lbl.textContent = 'Starting in ' + (s.countdown_remaining_sec || 0).toFixed(1) + 's…';
            } else if (s.phase === 'recording') {
                var dig = s.events || 0;
                var ana = (s.stick_samples || 0) + (s.trigger_samples || 0) + (s.mouse_moves || 0);
                lbl.textContent = (s.elapsed_sec || 0).toFixed(1) + 's · ' +
                    dig + ' digital · ' + ana + ' analog';
            }
        }
        if (s && s.phase === 'countdown') {
            if (btn) btn.textContent = 'Starting in ' + (s.countdown_remaining_sec || 0).toFixed(1) + 's…';
        } else if (s && s.phase === 'recording') {
            if (btn) btn.textContent = 'Recording…';
        }
        if (s && s.phase !== 'countdown' && s.phase !== 'recording') {
            // Backend stopped without us — refresh UI state
            clearInterval(_gpmRecordingTimer);
            _gpmRecordingTimer = null;
        }
    }, 200);
}

async function stopMacroRecording(append) {
    if (_gpmRecordingTimer) {
        clearInterval(_gpmRecordingTimer);
        _gpmRecordingTimer = null;
    }
    var r = await apiPost('/api/gamepad/macro/record/stop', {});
    var rec = document.getElementById('gpm-macro-recording');
    if (rec) rec.style.display = 'none';
    var btn = document.getElementById('gpm-macro-record-btn');
    if (btn) { btn.disabled = false; btn.textContent = '● Record'; }
    if (!r || !r.ok) {
        showErrorToast('Recording stop failed: ' + (r && r.err || 'unknown'));
        return;
    }
    var newSteps = r.steps || [];
    if (!newSteps.length) {
        showWarnToast('No mappable events captured. Try again — make sure to press at least one key, button, or click.');
        return;
    }
    if (append) {
        _gpmMacroEditorSteps = (_gpmMacroEditorSteps || []).concat(newSteps);
    } else {
        _gpmMacroEditorSteps = newSteps;
    }
    _renderMacroSteps();
}

async function cancelMacroRecording() {
    if (_gpmRecordingTimer) {
        clearInterval(_gpmRecordingTimer);
        _gpmRecordingTimer = null;
    }
    await apiPost('/api/gamepad/macro/record/cancel', {});
    var rec = document.getElementById('gpm-macro-recording');
    if (rec) rec.style.display = 'none';
    var btn = document.getElementById('gpm-macro-record-btn');
    if (btn) { btn.disabled = false; btn.textContent = '● Record'; }
}

// ═══ Background Pauser ═══
async function loadPauser() {
    var data = await apiGet('/api/pauser/status');
    if (!data) return;
    var s = data.settings || {};

    // Global toggle
    var t = document.getElementById('toggle-pauser-global');
    if (t) {
        if (s.enabled) t.classList.add('on');
        else t.classList.remove('on');
    }

    // Categories
    var catEl = document.getElementById('pauser-categories');
    if (catEl) {
        var cats = data.targets || {};
        var html = Object.keys(cats).map(function(catKey){
            var cat = cats[catKey];
            var on = !!(s.categories && s.categories[catKey]);
            var exes = (cat.exes || []).map(function(e){return escHtml(e);}).join(', ');
            return '<div style="display:flex;align-items:flex-start;justify-content:space-between;padding:14px 0;border-bottom:1px solid var(--border-faint);gap:14px">' +
                '<div style="min-width:0;flex:1">' +
                    '<div style="color:var(--text-bright);font-weight:600;font-size:13.5px">' + escHtml(cat.label) + '</div>' +
                    '<div style="color:var(--text-secondary);font-size:12.5px;margin-top:4px;line-height:1.5">' + escHtml(cat.desc) + '</div>' +
                    '<div style="color:var(--text-tertiary);font-size:11.5px;margin-top:6px;font-family:var(--font-mono,monospace);line-height:1.4">' + exes + '</div>' +
                '</div>' +
                '<div class="toggle-switch ' + (on ? 'on' : '') + '" onclick="togglePauserCategory(\'' + catKey + '\', this)"></div>' +
            '</div>';
        }).join('');
        catEl.innerHTML = html || '<div style="color:var(--text-tertiary)">No target categories defined.</div>';
    }

    // Live block
    var live = document.getElementById('pauser-live');
    var liveC = document.getElementById('pauser-live-content');
    if (live && liveC) {
        if (!s.enabled) {
            live.style.display = 'none';
        } else {
            live.style.display = 'block';
            var msg;
            if (data.currently_paused) {
                msg = '<span style="color:#fbbf24;font-weight:600">⏸ ' + data.paused.length +
                    ' process(es) currently suspended.</span> They will resume automatically when the game exits.';
            } else {
                msg = '<b style="color:var(--text-bright)">' + data.live_target_count + '</b> running target processes · ' +
                    '~<b style="color:var(--accent)">' + data.estimated_savings_mb + ' MB</b> RAM will be freed when a game launches';
            }
            liveC.innerHTML = msg;
        }
    }

    // Detail
    var detail = document.getElementById('pauser-status-detail');
    if (detail) {
        if (data.currently_paused) {
            var lines = data.paused.map(function(p){
                var rss = (p.rss / (1024*1024)).toFixed(0);
                return '· ' + escHtml(p.name) + ' (pid ' + p.pid + ', ' + rss + ' MB)';
            });
            detail.innerHTML = '<b>Suspended now:</b><br>' + lines.join('<br>');
        } else {
            detail.innerHTML = '';
        }
    }
}

async function togglePauserGlobal(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    var r = await apiPost('/api/pauser/settings', { enabled: newOn });
    if (!r) {
        el.classList.toggle('on');
        return;
    }
    setTimeout(loadPauser, 200);
}

async function togglePauserCategory(catKey, el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    var r = await apiPost('/api/pauser/category', { category: catKey, enabled: newOn });
    if (!r || !r.ok) {
        el.classList.toggle('on');
        showErrorToast(r && r.err ? r.err : 'Update failed.');
        return;
    }
    setTimeout(loadPauser, 200);
}

async function manualPauseNow() {
    var r = await apiPost('/api/pauser/pause_now', {});
    if (!r) return;
    if (r.ok) {
        showInfoToast('Paused ' + (r.paused ? r.paused.length : 0) + ' process(es), freed ~' + (r.total_freed_mb || 0) + ' MB.');
    } else {
        showErrorToast(r.err || 'Pause failed. Is the master switch on?');
    }
    loadPauser();
}

async function manualResumeNow() {
    var r = await apiPost('/api/pauser/resume_now', {});
    if (!r) return;
    if (r.ok) {
        showInfoToast('Resumed ' + (r.resumed ? r.resumed.length : 0) + ' process(es).');
    } else {
        showErrorToast('Resume failed.');
    }
    loadPauser();
}

// ═══ Tournament Mode (Dashboard hero card) ═══
var _tournamentTimer = null;

async function loadTournament() {
    var s = await apiGet('/api/tournament/status');
    if (!s) return;
    _renderTournament(s);
    if (s.enabled && s.expires_at) {
        _startTournamentCountdown();
    } else {
        _stopTournamentCountdown();
    }
}

function _renderTournament(s) {
    var pill = document.getElementById('tournament-status-pill');
    var idle = document.getElementById('tournament-idle-block');
    var active = document.getElementById('tournament-active-block');
    var summary = document.getElementById('tournament-summary');
    var actions = document.getElementById('tournament-actions');
    if (!pill || !idle || !active) return;

    if (s.enabled) {
        pill.textContent = 'ON';
        pill.className = 'status-badge ok';
        idle.style.display = 'none';
        active.style.display = 'block';
        if (summary) {
            summary.textContent = 'Distractions paused. Game-priority mode locked in. ' +
                (s.expires_at ? 'Auto-reverts on timer.' : 'Will stay on until you disable.');
        }
        if (actions && s.last_actions) {
            actions.innerHTML = s.last_actions.map(function(a){
                return '· ' + escHtml(a);
            }).join('<br>');
        }
        _renderCountdown(s.seconds_remaining || 0, !!s.expires_at);
    } else {
        pill.textContent = 'OFF';
        pill.className = 'status-badge neutral';
        idle.style.display = 'flex';
        active.style.display = 'none';
        if (summary) {
            summary.textContent = 'One-button "before a match" macro: pauses browsers, kills OneDrive, mutes notifications, switches to High Performance, applies competitive AT.';
        }
    }
}

function _renderCountdown(secs, hasTimer) {
    var el = document.getElementById('tournament-countdown');
    if (!el) return;
    if (!hasTimer) {
        el.textContent = 'Until you disable';
        el.style.color = 'var(--text-bright)';
        return;
    }
    if (secs <= 0) {
        el.textContent = 'Reverting…';
        return;
    }
    var m = Math.floor(secs / 60);
    var s = secs % 60;
    el.textContent = m + ':' + (s < 10 ? '0' : '') + s + ' remaining';
    el.style.color = (secs < 60) ? 'var(--orange,#fbbf24)' : 'var(--text-bright)';
}

function _startTournamentCountdown() {
    if (_tournamentTimer) clearInterval(_tournamentTimer);
    _tournamentTimer = setInterval(async function(){
        var s = await apiGet('/api/tournament/status');
        if (!s || !s.enabled) {
            // Server-side auto-disable fired — refresh full UI
            _stopTournamentCountdown();
            loadTournament();
            return;
        }
        _renderCountdown(s.seconds_remaining || 0, !!s.expires_at);
    }, 1000);
}

function _stopTournamentCountdown() {
    if (_tournamentTimer) {
        clearInterval(_tournamentTimer);
        _tournamentTimer = null;
    }
}

async function enableTournament() {
    var sel = document.getElementById('tournament-duration');
    var dur = sel ? parseInt(sel.value, 10) : 60;
    if (isNaN(dur)) dur = 60;
    var r = await apiPost('/api/tournament/enable', { duration_min: dur });
    if (!r) { showErrorToast('Failed to start Tournament Mode.'); return; }
    if (!r.ok) { showErrorToast(r.err || 'Failed.'); return; }
    var msg = 'Tournament Mode is ON.\n\n' + (r.actions || []).join('\n');
    showInfoToast(msg);
    loadTournament();
}

async function disableTournament() {
    if (!confirm('End Tournament Mode and restore all paused services / notifications?')) return;
    var r = await apiPost('/api/tournament/disable', {});
    if (!r) return;
    if (r.ok) {
        var msg = 'Tournament Mode is OFF.\n\n' + (r.actions || []).join('\n');
        showInfoToast(msg);
    } else {
        showErrorToast(r.err || 'Failed to disable.');
    }
    loadTournament();
}

async function extendTournament(extraMin) {
    var r = await apiPost('/api/tournament/extend', { extra_min: extraMin });
    if (!r) return;
    if (!r.ok) { showErrorToast(r.err || 'Extend failed.'); return; }
    loadTournament();
}

// ═══ Game Library Scanner (Detected Library card) ═══
var _libraryFilter = 'known';   // 'known' | 'all'
var _libraryCache = null;       // last response from /api/library

async function loadLibrary(forceScan) {
    var summary = document.getElementById('library-summary');
    var list = document.getElementById('library-list');
    if (!summary || !list) return;
    summary.textContent = forceScan ? 'Scanning library… this can take 5-15 seconds.' : 'Loading library…';
    list.innerHTML = '';
    var url = '/api/library';
    var data;
    if (forceScan) {
        data = await apiPost('/api/library/scan', { force: true });
    } else {
        data = await apiGet(url);
    }
    if (!data) {
        summary.textContent = 'Library scan unavailable.';
        return;
    }
    _libraryCache = data;
    _renderLibrary();
}

function _renderLibrary() {
    var data = _libraryCache || { games: [], total: 0, known: 0, scanned_at: 0 };
    var summary = document.getElementById('library-summary');
    var list = document.getElementById('library-list');
    if (!summary || !list) return;

    if (!data.scanned_at) {
        summary.innerHTML = 'No scan yet — click <b>Scan now</b> to discover installed games.';
        list.innerHTML = '';
        return;
    }
    var when = new Date(data.scanned_at * 1000);
    summary.innerHTML = '<b>' + data.total + '</b> installed games detected · ' +
        '<b>' + data.known + '</b> recognized by Vispora · ' +
        'last scan ' + when.toLocaleString();

    var games = (data.games || []).slice();
    if (_libraryFilter === 'known') {
        games = games.filter(function(g){ return g.known; });
    }
    if (!games.length) {
        list.innerHTML = '<div style="color:var(--text-tertiary);padding:8px 0">' +
            (_libraryFilter === 'known'
                ? 'No recognized games found. Switch to <b>all</b> to see what was scanned, or use Custom Games to add an unknown title.'
                : 'No games found by any launcher scanner.') +
            '</div>';
        _updateLibraryFilterLinks();
        return;
    }

    // Build per-game adaptive-tuning state lookup so we can show toggle position.
    // /api/adaptive/games returns { games: { "exe": state, ... } } — a dict
    // keyed by exe, NOT an array.  Don't call forEach on it.
    apiGet('/api/adaptive/games').then(function(at){
        var atMap = {};
        if (at && at.games && typeof at.games === 'object') {
            Object.keys(at.games).forEach(function(k){
                atMap[k.toLowerCase()] = at.games[k];
            });
        }
        var html = games.map(function(g){
            var exe = (g.exe || '').toLowerCase();
            var atState = atMap[exe];
            var isOn = atState && atState.enabled;
            var hint = g.mode_hint ? '<span style="color:var(--accent);font-size:11px;text-transform:uppercase;letter-spacing:0.04em;margin-left:8px">' + g.mode_hint + '</span>' : '';
            var knownBadge = g.known ? '' : '<span style="color:var(--text-tertiary);font-size:11px;margin-left:8px">unrecognized</span>';
            var btnLabel = isOn ? 'AT enabled — click to disable' : (g.known ? 'Enable AT' : 'Add as custom + enable AT');
            var btnClass = isOn ? 'btn btn-sm' : 'btn btn-sm btn-primary';
            return '<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border-faint);gap:10px">' +
                '<div style="min-width:0;flex:1">' +
                    '<div style="color:var(--text-bright);font-weight:600;font-size:13.5px">' +
                        escHtml(g.title) + hint + knownBadge +
                    '</div>' +
                    '<div style="color:var(--text-tertiary);font-size:12px;margin-top:2px">' +
                        escHtml(g.launcher) + ' · ' + escHtml(exe || '(no exe found)') +
                    '</div>' +
                '</div>' +
                (exe ?
                    '<button class="' + btnClass + '" onclick="toggleATForLibraryGame(\'' + exe.replace(/'/g, "\\'") + '\',' + (isOn ? 'false' : 'true') + ')">' + btnLabel + '</button>'
                    : '<span style="color:var(--text-tertiary);font-size:12px">no exe</span>'
                ) +
            '</div>';
        }).join('');
        list.innerHTML = html;
        _updateLibraryFilterLinks();
    });
}

function _updateLibraryFilterLinks() {
    var k = document.getElementById('lib-filter-known');
    var a = document.getElementById('lib-filter-all');
    if (!k || !a) return;
    if (_libraryFilter === 'known') {
        k.style.color = 'var(--accent)'; k.style.fontWeight = '600';
        a.style.color = 'var(--text-secondary)'; a.style.fontWeight = '400';
    } else {
        a.style.color = 'var(--accent)'; a.style.fontWeight = '600';
        k.style.color = 'var(--text-secondary)'; k.style.fontWeight = '400';
    }
}

function setLibraryFilter(mode) {
    _libraryFilter = (mode === 'all') ? 'all' : 'known';
    _renderLibrary();
}

async function scanLibrary() {
    await loadLibrary(true);
}

async function toggleATForLibraryGame(exe, enable) {
    if (!exe) return;
    var r = await apiPost('/api/library/enable_at', { exe: exe, enabled: !!enable });
    if (!r || !r.ok) {
        showErrorToast('Failed to update Adaptive Tuning state.');
        return;
    }
    _renderLibrary();        // refresh the row's button label
    loadAdaptiveGames();     // refresh AT card so the user sees the new entry
}

async function enableATForLibrary(enable) {
    if (!_libraryCache || !_libraryCache.games) {
        showWarnToast('Run a scan first.');
        return;
    }
    var verb = enable ? 'Enable' : 'Disable';
    var known = _libraryCache.games.filter(function(g){ return g.known && g.exe; });
    if (!known.length) {
        showWarnToast('No recognized games to flip.');
        return;
    }
    if (!confirm(verb + ' Adaptive Tuning for ' + known.length + ' recognized games?')) return;
    var exes = known.map(function(g){ return g.exe; });
    var r = await apiPost('/api/library/enable_at_bulk', {
        exes: exes, enabled: !!enable, only_known: true
    });
    if (r && r.ok) {
        showErrorToast(verb + 'd AT for ' + r.flipped + ' games.');
        _renderLibrary();
        loadAdaptiveGames();
    } else {
        showErrorToast('Bulk update failed.');
    }
}

// ═══ Adaptive Tuning (Game Profiles tab) ═══
var _adaptivePollTimer = null;

async function loadAdaptiveSettings() {
    var s = await apiGet('/api/adaptive/settings');
    if (!s) return;
    var t = document.getElementById('toggle-adaptive-global');
    if (t) {
        if (s.enabled_globally) t.classList.add('on');
        else t.classList.remove('on');
    }
}

async function toggleAdaptiveGlobal(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    await apiPost('/api/adaptive/settings', { enabled_globally: newOn });
    loadAdaptiveSettings();
}

async function _refreshAdaptiveLive() {
    // Pull AT status AND game-mode active-game state in parallel.  We
    // need both to render the "tracked but not tuning" state — that's
    // when game_profiles knows about a game but AT hasn't engaged yet
    // (either because global is off, or per-game toggle is off).
    var [s, ge] = await Promise.all([
        apiGet('/api/adaptive/status'),
        apiGet('/api/profiles/status'),
    ]);
    // beta.14 — surface the "install PresentMon 2 for better AT accuracy"
    // nudge as a toast once per session.  The toast helper itself dedupes
    // identical messages so even if this poll fires hourly, the user
    // sees it once.
    try {
        if (s && s.pm2_install_hint && s.pm2_install_hint.needs_pm2
              && !window._pm2NudgeShown) {
            window._pm2NudgeShown = true;
            showInfoToast(s.pm2_install_hint.msg,
                { title: 'Adaptive Tuning — recommendation',
                  timeoutMs: 14000 });
        }
    } catch (_e) { /* nudge is cosmetic */ }
    var box  = document.getElementById('adaptive-live');
    var cont = document.getElementById('adaptive-live-content');
    if (!box || !cont) return;

    var atActive = s && s.active_exe;
    var gmActive = ge && ge.active_exe;
    if (!atActive && !gmActive) {
        box.style.display = 'none';
        return;
    }
    box.style.display = 'block';

    if (atActive) {
        // AT is actively tuning this game — full status row
        var verdict = s.last_verdict || 'no-data';
        var verdictColor =
            verdict === 'healthy'  ? 'var(--accent)' :
            verdict === 'warn'     ? 'var(--warning)' :
            verdict === 'unstable' ? 'var(--danger)' :
            'var(--text-tertiary)';
        // v3.1 — confidence + paused + opening-phase + hot-streak
        var conf = s.confidence || 'low';
        var confColor = conf === 'high' ? 'var(--accent)' :
                        conf === 'medium' ? 'var(--warning)' :
                        'var(--text-tertiary)';
        var confBadge = '<span class="status-badge" style="background:' + confColor + '22;color:' + confColor + '" title="' +
                        'AT confidence in this game\'s learned profile. Low = exploring, Medium = settling, High = converged.">' +
                        conf + '</span>';
        var pausedBadge = s.paused
            ? '<span class="status-badge danger" title="AT paused — sources: ' +
              ((s.pause_sources||[]).join(', ') || 'unknown') + '">paused</span>'
            : '';
        var openingBadge = s.in_opening_phase
            ? '<span class="status-badge" style="background:var(--accent-soft);color:var(--accent)" title="First 5 min of a fresh session — AT explores faster with halved thresholds.">opening</span>'
            : '';
        var hotBadge = (s.hot_streak || 0) >= 3
            ? '<span class="status-badge" style="background:var(--warning-soft,#ffaa0022);color:var(--warning)" title="Hot streak: ' + s.hot_streak + ' consecutive step-ups without instability. Step size is doubled.">🔥' + s.hot_streak + '</span>'
            : '';
        var dryBadge = s.dry_run
            ? '<span class="status-badge neutral" title="Dry-run mode — AT logs steps but does not apply them to the GPU.">dry-run</span>'
            : '';
        // v3.2-beta.3 — gameplay-vs-menu pill.  Reading the pill tells
        // the user at a glance why AT may not be making decisions
        // (e.g. "menu detected").
        var gp = s.gameplay_phase || 'unknown';
        var gpLabel = gp === 'gameplay'           ? '▶ in game' :
                      gp === 'menu'               ? '⏸ menu' :
                      gp === 'transitional'      ? '… transitioning' :
                      gp === 'baseline-building' ? '… baselining' :
                                                    '… ' + gp;
        var gpColor = gp === 'gameplay'           ? 'var(--accent)' :
                      gp === 'menu'               ? 'var(--warning)' :
                                                    'var(--text-tertiary)';
        var gpReasons = (s.gameplay_reasons || []).join(' · ') || 'gathering data';
        var gpBadge = '<span class="status-badge" style="background:' + gpColor +
                      '22;color:' + gpColor + '" title="' + escHtml(gpReasons) + '">' +
                      gpLabel + (s.gameplay_conf ? ' ' + Math.round((s.gameplay_conf || 0) * 100) + '%' : '') +
                      '</span>';
        var healthLine = '';
        if (s.gpu_health && (s.gpu_health.temp_c != null || s.gpu_health.power_pct != null)) {
            var t = s.gpu_health.temp_c;
            var p = s.gpu_health.power_pct;
            healthLine =
                '<span style="margin-left:14px;color:var(--text-tertiary);font-size:11.5px;font-family:var(--font-mono)">' +
                (t != null ? 'temp ' + Math.round(t) + '°' : '') +
                (t != null && p != null ? ' · ' : '') +
                (p != null ? 'pwr ' + Math.round(p) + '%' : '') +
                '</span>';
        }
        // v3.2 — frame telemetry line (FPS / 1%-low / frametime σ).  Only
        // shown when PresentMon is feeding us data.  Without this, the
        // user can't tell whether AT is actually receiving frame info
        // or running blind on GPU metrics alone.
        // v3.3.1-beta.5: source label updated (was 'RTSS frame data')
        // since RTSS path was removed.
        var frameLine = '';
        if (s.frame_stats && s.frame_stats.available) {
            var f = s.frame_stats;
            var sigColor = f.frametime_var_pct >= 40 ? 'var(--danger)' :
                           f.frametime_var_pct >= 25 ? 'var(--warning)' :
                           'var(--accent)';
            frameLine =
                '<div style="margin-top:6px;font-family:var(--font-mono);font-size:12px;color:var(--text-secondary);' +
                'padding:6px 8px;background:rgba(0,0,0,0.18);border-radius:4px;border-left:3px solid var(--accent)">' +
                '<span style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase;margin-right:8px">PresentMon frame data</span>' +
                'fps <span style="color:var(--accent-bright)">' + (f.avg_fps != null ? f.avg_fps : '–') + '</span>' +
                '  ·  1%-low <span style="color:var(--accent-bright)">' + (f.min_fps_1pct != null ? f.min_fps_1pct : '–') + '</span>' +
                '  ·  σ <span style="color:' + sigColor + '">' + (f.frametime_std_ms != null ? f.frametime_std_ms : '–') + ' ms</span>' +
                '  ·  p99 <span style="color:' + sigColor + '">' + (f.frametime_p99_ms != null ? f.frametime_p99_ms : '–') + ' ms</span>' +
                '  ·  var ' + (f.frametime_var_pct != null ? f.frametime_var_pct : '–') + '%' +
                '</div>';
        }
        // Phase-specific status string — distinguishes "still in warmup
        // window" from "post-warmup but GPU is too idle to score".
        var statusStr;
        if (s.last_score != null) {
            statusStr = 'score ' + s.last_score + ' (' + verdict + ')';
        } else if (s.phase === 'warmup') {
            statusStr = 'warmup ' + Math.round(s.warmup_remaining) + 's remaining';
        } else if (verdict === 'idle') {
            statusStr = 'GPU at ' + (s.last_util != null ? s.last_util + '%' : '?') +
                        ' — waiting for ≥15% sustained load';
        } else if (verdict === 'no-data') {
            statusStr = 'collecting samples…';
        } else {
            statusStr = verdict;
        }
        var mode = s.active_mode || 'balanced';
        var modeColor =
            mode === 'competitive' ? 'var(--warning)' :
            mode === 'visual'      ? 'var(--accent)'  :
            'var(--text-secondary)';
        cont.innerHTML =
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap">' +
              (s.paused ? pausedBadge : '<span class="status-badge ok">tuning</span>') +
              '<span class="status-badge" style="background:' + modeColor + '22;color:' + modeColor + '">' + escHtml(mode) + '</span>' +
              confBadge + openingBadge + hotBadge + dryBadge + gpBadge +
              '<span style="font-weight:600;color:var(--text-bright)">▶ ' +
              escHtml(s.active_display || s.active_exe) + '</span>' +
              healthLine +
              '<span style="margin-left:auto;font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono)">' +
              (s.seconds_in_session != null ? Math.round(s.seconds_in_session) + 's in session' : '') +
              '</span>' +
            '</div>' +
            '<div style="font-family:var(--font-mono);font-size:13px">' +
              'core+' + s.current_core + ' / mem+' + s.current_mem +
              ' &nbsp;·&nbsp; steps: ' + s.step_count +
              ' &nbsp;·&nbsp; good: ' + s.consecutive_good + '/' + (s.settings && s.settings.consecutive_good_required || 5) +
              ' &nbsp;·&nbsp; <span style="color:' + verdictColor + '">' + statusStr + '</span>' +
            '</div>' +
            (s.last_score == null && s.phase === 'tuning' && verdict === 'idle'
              ? '<div style="margin-top:6px;font-size:11.5px;color:var(--text-tertiary);line-height:1.5">' +
                  'This game isn\'t loading the GPU enough for AT to assess stability. ' +
                  'A score will appear once GPU util sustains above 15%.' +
                '</div>'
              : '') +
            (s.active_preset
              ? '<div style="margin-top:6px;font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono)">' +
                  'preset: step ±' + s.active_preset.core_step_mhz + '/±' + s.active_preset.mem_step_mhz +
                  ' · threshold ' + s.active_preset.max_score_to_step_down + '/' + s.active_preset.min_score_to_step_up +
                  ' · cap ' + s.active_preset.max_core_offset + '/' + s.active_preset.max_mem_offset +
                '</div>'
              : '') +
            frameLine;
    } else {
        // game-mode tracks the game but AT didn't engage
        var settings = await apiGet('/api/adaptive/settings');
        var gameState = (await apiGet('/api/adaptive/games')).games || {};
        var key = (ge.active_exe || '').toLowerCase();
        // Strip any path prefix to match _norm_exe
        var keyShort = key.split(/[\\/]/).pop();
        var gs = gameState[keyShort] || gameState[key];

        var why = '';
        var fixBtn = '';
        if (settings && !settings.enabled_globally) {
            why = 'AT master switch is off.';
            fixBtn =
                '<button class="btn btn-sm btn-primary" onclick="_quickEnableAtGlobal()">' +
                'Enable Adaptive Tuning</button>';
        } else if (gs && !gs.enabled) {
            why = 'AT is off for this specific game.';
            fixBtn =
                '<button class="btn btn-sm btn-primary" onclick="_quickEnableAtForGame(\'' +
                escHtml(keyShort) + '\')">Enable for this game</button>';
        } else {
            why = 'Tracked. AT will engage on next game launch.';
        }

        cont.innerHTML =
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">' +
              '<span class="status-badge neutral">tracked</span>' +
              '<span style="font-weight:600;color:var(--text-bright)">▶ ' +
              escHtml(ge.active_game || ge.active_exe) + '</span>' +
              '<span style="margin-left:auto">' + fixBtn + '</span>' +
            '</div>' +
            '<div style="font-size:12.5px;color:var(--text-secondary)">' +
              escHtml(why) +
            '</div>';
    }
}

async function _quickEnableAtGlobal() {
    await apiPost('/api/adaptive/settings', { enabled_globally: true });
    loadAdaptiveSettings();
    setTimeout(function() { _refreshAdaptiveLive(); loadAdaptiveGames(); }, 400);
}
async function _quickEnableAtForGame(exe) {
    await apiPost('/api/adaptive/game-toggle', { exe: exe, enabled: true });
    setTimeout(function() { _refreshAdaptiveLive(); loadAdaptiveGames(); }, 400);
}

async function loadAdaptiveGames() {
    // Pull games + active-game state in parallel so we can mark the
    // currently-running one with a live indicator
    var [data, ge] = await Promise.all([
        apiGet('/api/adaptive/games'),
        apiGet('/api/profiles/status'),
    ]);
    var games = (data && data.games) || {};
    var activeKey = ge && ge.active_exe
        ? (ge.active_exe.split(/[\\/]/).pop() || '').toLowerCase()
        : null;
    var c = document.getElementById('adaptive-games-list');
    if (!c) return;
    var keys = Object.keys(games);
    if (keys.length === 0) {
        c.innerHTML = '<div class="empty-state">No games tracked yet. AT will start learning the first time a known game launches with the master switch on.</div>';
        return;
    }
    var html = '';
    keys.sort();
    keys.forEach(function(exe) {
        var g = games[exe];
        var isActive = (exe.toLowerCase() === activeKey);
        var verdictPill = g.last_session_outcome === 'crash'
            ? '<span class="status-badge danger" style="margin-left:6px">last: crash</span>'
            : g.last_session_outcome === 'clean'
            ? '<span class="status-badge ok" style="margin-left:6px">last: clean</span>'
            : '';
        var convergedPill = g.converged
            ? '<span class="status-badge ok" style="margin-left:6px">converged</span>'
            : '';
        var livePill = isActive
            ? '<span class="status-badge ok" style="margin-left:6px;display:inline-flex;align-items:center;gap:6px">'
              + '<span class="live-dot" style="width:6px;height:6px"></span>playing now</span>'
            : '';
        var enabledTog = '<div class="toggle-switch ' + (g.enabled ? 'on' : '') + '" ' +
                          'onclick="toggleAdaptiveGame(\'' + escHtml(exe) + '\', this)" ' +
                          'style="margin-left:auto"></div>';
        var mode = g.mode || 'balanced';
        var modeColor =
            mode === 'competitive' ? 'var(--warning)' :
            mode === 'visual'      ? 'var(--accent)'  :
            'var(--text-secondary)';
        var hasBest = (g.best_stable_core || 0) > 0 || (g.best_stable_mem || 0) > 0;
        // v3.1 — confidence badge + dry-run + baseline display
        var conf = g.confidence || 'low';
        var confColor = conf === 'high' ? 'var(--accent)' :
                        conf === 'medium' ? 'var(--warning)' :
                        'var(--text-tertiary)';
        var confTitle = 'AT confidence in this game\'s profile · ' +
                        'sessions:' + (g.stable_sessions||0) + ' / minutes:' + Math.round(g.stable_minutes||0);
        var confPill = '<span class="status-badge" style="margin-left:6px;background:' + confColor +
                       '22;color:' + confColor + '" title="' + escHtml(confTitle) + '">conf: ' + conf + '</span>';
        var dryPill = g.dry_run
            ? '<span class="status-badge neutral" style="margin-left:6px" title="Dry-run mode — AT logs steps but does not push to GPU">dry-run</span>'
            : '';
        var baselineCore = g.baseline_core || 0;
        var baselineMem  = g.baseline_mem  || 0;
        var hotPill = (g.hot_streak || 0) >= 3
            ? '<span class="status-badge" style="margin-left:6px;background:var(--warning-soft,#ffaa0022);color:var(--warning)" title="' +
              g.hot_streak + ' consecutive step-ups without trouble — step size temporarily doubled">🔥' + g.hot_streak + '</span>'
            : '';
        // Default knob values (matching adaptive_tuning._empty_state)
        var thermal = (g.thermal_limit_c != null) ? g.thermal_limit_c : 82;
        var powerB  = (g.power_bound_pct != null) ? g.power_bound_pct : 95;
        var stepMul = (g.step_multiplier != null) ? g.step_multiplier : 1.0;
        var knobsId = 'at-knobs-' + exe.replace(/[^a-z0-9]/gi,'_');
        html +=
          '<div style="padding:12px 14px;border:1px solid ' +
            (isActive ? 'var(--accent)' : 'var(--border)') +
            ';border-radius:var(--radius-sm);background:' +
            (isActive ? 'var(--accent-soft)' : 'var(--bg-elevated)') +
            ';margin-bottom:6px">' +
            '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
              '<span style="font-weight:600;color:var(--text-bright);font-family:var(--font-mono);font-size:13px">' + escHtml(exe) + '</span>' +
              '<span class="status-badge neutral" style="background:' + modeColor + '22;color:' + modeColor + ';margin-left:6px">' + escHtml(mode) + '</span>' +
              confPill + hotPill + dryPill +
              livePill + verdictPill + convergedPill +
              enabledTog +
            '</div>' +
            '<div style="margin-top:6px;font-family:var(--font-mono);font-size:12px;color:var(--text-secondary);line-height:1.6">' +
              'current: <span style="color:var(--accent)">core+' + (g.current_core||0) + ' / mem+' + (g.current_mem||0) + '</span>' +
              ' &nbsp;·&nbsp; baseline floor: <span style="color:var(--text-tertiary)">core+' + baselineCore + ' / mem+' + baselineMem + '</span>' +
              ' &nbsp;·&nbsp; best stable: <span style="color:var(--accent-bright)">core+' + (g.best_stable_core||0) + ' / mem+' + (g.best_stable_mem||0) + '</span>' +
              '<br>' +
              'sessions: ' + (g.session_count||0) +
              ' &nbsp;·&nbsp; total play: ' + Math.round(g.session_minutes||0) + ' min' +
              ' &nbsp;·&nbsp; steps taken: ' + (g.step_count||0) +
              ' &nbsp;·&nbsp; warned: ' + (g.warned_offsets||[]).length +
              ' &nbsp;·&nbsp; stable: ' + (g.stable_sessions||0) + ' sess / ' + Math.round(g.stable_minutes||0) + ' min' +
            '</div>' +
            '<div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
              '<label style="font-size:11.5px;color:var(--text-tertiary);font-weight:600;letter-spacing:0.04em;text-transform:uppercase">Mode</label>' +
              '<select onchange="setAdaptiveGameMode(\'' + escHtml(exe) + '\', this.value)" style="width:auto;min-width:130px;padding:5px 28px 5px 10px;font-size:12.5px">' +
                '<option value="balanced"' + (mode === 'balanced' ? ' selected' : '') + '>Balanced</option>' +
                '<option value="competitive"' + (mode === 'competitive' ? ' selected' : '') + '>Competitive</option>' +
                '<option value="visual"' + (mode === 'visual' ? ' selected' : '') + '>Visual</option>' +
              '</select>' +
              (hasBest
                ? '<button class="btn btn-sm" onclick="applyAdaptiveBest(\'' + escHtml(exe) + '\')">Apply best now</button>'
                : '') +
              '<button class="btn btn-sm" onclick="reseedAdaptiveGame(\'' + escHtml(exe) + '\')" title="Restart this game\'s exploration from your CURRENT manual OC profile.  Keeps session count + warned offsets — just bumps the floor so AT doesn\'t crawl up from 0.">Reseed from current OC</button>' +
              '<button class="btn btn-sm" onclick="toggleAtKnobs(\'' + knobsId + '\')">Advanced…</button>' +
              '<button class="btn btn-sm" onclick="clearAdaptiveBlacklist(\'' + escHtml(exe) + '\')" style="margin-left:auto">Clear blacklist</button>' +
              '<button class="btn btn-sm btn-danger" onclick="resetAdaptiveGame(\'' + escHtml(exe) + '\')">Reset</button>' +
            '</div>' +
            // Advanced knobs panel — collapsed by default
            '<div id="' + knobsId + '" style="display:none;margin-top:12px;padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-overlay)">' +
              '<div style="font-size:11px;color:var(--text-tertiary);letter-spacing:0.04em;text-transform:uppercase;font-weight:600;margin-bottom:8px">Advanced knobs</div>' +
              '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;font-size:12px">' +
                '<div>' +
                  '<label style="display:block;margin-bottom:4px;color:var(--text-secondary)" title="AT skips step-ups when GPU temp is at or above this">Thermal limit °C: <span id="' + knobsId + '-thermal-val">' + thermal + '</span></label>' +
                  '<input type="range" min="60" max="95" step="1" value="' + thermal + '" ' +
                    'oninput="document.getElementById(\'' + knobsId + '-thermal-val\').textContent = this.value" ' +
                    'onchange="setAtKnob(\'' + escHtml(exe) + '\', \'thermal_limit_c\', this.value)" ' +
                    'style="width:100%">' +
                '</div>' +
                '<div>' +
                  '<label style="display:block;margin-bottom:4px;color:var(--text-secondary)" title="AT skips CORE step-ups when GPU power draw is at or above this % of TDP">Power bound %: <span id="' + knobsId + '-power-val">' + powerB + '</span></label>' +
                  '<input type="range" min="50" max="100" step="1" value="' + powerB + '" ' +
                    'oninput="document.getElementById(\'' + knobsId + '-power-val\').textContent = this.value" ' +
                    'onchange="setAtKnob(\'' + escHtml(exe) + '\', \'power_bound_pct\', this.value)" ' +
                    'style="width:100%">' +
                '</div>' +
                '<div>' +
                  '<label style="display:block;margin-bottom:4px;color:var(--text-secondary)" title="Global scaler on step size (hot-streak still compounds on top)">Step multiplier: <span id="' + knobsId + '-mul-val">' + stepMul.toFixed(2) + '</span>×</label>' +
                  '<input type="range" min="0.5" max="2.0" step="0.05" value="' + stepMul + '" ' +
                    'oninput="document.getElementById(\'' + knobsId + '-mul-val\').textContent = parseFloat(this.value).toFixed(2)" ' +
                    'onchange="setAtKnob(\'' + escHtml(exe) + '\', \'step_multiplier\', this.value)" ' +
                    'style="width:100%">' +
                '</div>' +
              '</div>' +
              '<div style="margin-top:10px;display:flex;align-items:center;gap:8px">' +
                '<div class="toggle-switch ' + (g.dry_run ? 'on' : '') + '" ' +
                  'onclick="toggleAtDryRun(\'' + escHtml(exe) + '\', this)" style="transform:scale(0.85);transform-origin:left"></div>' +
                '<span style="font-size:12.5px;color:var(--text-secondary)" title="When on, AT runs all logic but does NOT push OC changes to the GPU. History still records what AT would have done.">Dry-run (log only, do not apply OC)</span>' +
              '</div>' +
            '</div>' +
          '</div>';
    });
    c.innerHTML = html;
}

async function reseedAdaptiveGame(exe) {
    if (!confirm('Reseed ' + exe + ' from your current manual OC?\n\n' +
                 'AT will read your saved OC profile (from the GPU Overclock page) and use those offsets as the new baseline floor.  Session history + warned offsets are kept; only the search starting point moves.\n\n' +
                 'Use this after you\'ve manually tuned your card higher.')) return;
    var r = await apiPost('/api/adaptive/reseed', { exe: exe });
    if (r && r.ok) {
        addLog('Reseeded ' + exe + ' to core+' + r.baseline_core + ' / mem+' + r.baseline_mem);
    } else {
        addLog('Reseed failed: ' + ((r && r.err) || 'unknown'));
    }
    loadAdaptiveGames();
}

async function reseedAdaptiveAll() {
    if (!confirm('Reseed EVERY tracked game from your current manual OC?\n\nKeeps each game\'s session history + warned offsets but moves all baselines to where your manual OC currently sits.')) return;
    var r = await apiPost('/api/adaptive/reseed', {});
    if (r && r.ok) {
        addLog('Reseeded all games to core+' + r.baseline_core + ' / mem+' + r.baseline_mem);
    }
    loadAdaptiveGames();
}

async function toggleAdaptiveGame(exe, el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    await apiPost('/api/adaptive/game-toggle', { exe: exe, enabled: newOn });
    loadAdaptiveGames();
}

async function resetAdaptiveGame(exe) {
    if (!confirm('Wipe ALL learned offsets + history for ' + exe + '?\n\n' +
                 'Next launch will start from your current manual OC profile as the new baseline (was hardcoded 0 in older versions).\n\n' +
                 'If you only want to bump the search starting point WITHOUT losing session history, use "Reseed from current OC" instead.')) return;
    await apiPost('/api/adaptive/reset', { exe: exe });
    loadAdaptiveGames();
}

async function setAdaptiveGameMode(exe, mode) {
    var r = await apiPost('/api/adaptive/game-mode', { exe: exe, mode: mode });
    if (r && r.ok) {
        addLog('AT mode for ' + exe + ' → ' + mode);
    }
    loadAdaptiveGames();
    _refreshAdaptiveLive();
}

async function applyAdaptiveBest(exe) {
    if (!confirm('Apply ' + exe + '\'s best-known-stable offsets to your GPU now?\n\nThis is the same OC AT would apply at the next game launch — no risk beyond what you\'ve already played at.')) return;
    var r = await apiPost('/api/adaptive/apply-best', { exe: exe });
    if (r && r.ok) {
        addLog('Applied best OC for ' + exe + ': core+' + r.core + ' / mem+' + r.mem);
    } else {
        addLog('Apply-best failed: ' + (r && r.err));
    }
    loadAdaptiveGames();
}

async function clearAdaptiveBlacklist(exe) {
    if (!confirm('Clear the crash blacklist for ' + exe + '?\n\nKeeps best-stable + session counts.  Useful after a driver update — old crashed offsets may now be stable.')) return;
    var r = await apiPost('/api/adaptive/clear-blacklist', { exe: exe });
    addLog('Blacklist cleared for ' + exe);
    loadAdaptiveGames();
}

async function resetAdaptiveAll() {
    if (!confirm('Wipe ALL learned per-game profiles?\n\nThis is permanent — every game will start from scratch.')) return;
    await apiPost('/api/adaptive/reset', {});
    loadAdaptiveGames();
}

// ── v3.1 — pause/resume, per-game knobs, cross-game blacklist ───────────
function toggleAtKnobs(panelId) {
    var el = document.getElementById(panelId);
    if (!el) return;
    el.style.display = (el.style.display === 'none' || !el.style.display) ? 'block' : 'none';
}

async function setAtKnob(exe, knob, value) {
    var body = { exe: exe };
    body[knob] = value;
    var r = await apiPost('/api/adaptive/game-knobs', body);
    if (r && r.ok) {
        addLog('AT ' + knob + ' for ' + exe + ' → ' + value);
    } else {
        addLog('AT knob update failed: ' + ((r && r.err) || 'unknown'));
    }
}

async function toggleAtDryRun(exe, el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    var r = await apiPost('/api/adaptive/game-knobs', { exe: exe, dry_run: newOn });
    if (r && r.ok) {
        addLog('AT dry-run for ' + exe + ' → ' + (newOn ? 'ON (no OC applied)' : 'OFF (live)'));
    }
    loadAdaptiveGames();
}

async function pauseAtManually() {
    var r = await apiPost('/api/adaptive/pause', { source: 'user' });
    if (r && r.ok) {
        addLog('Adaptive Tuning paused (manual)');
        _refreshAdaptiveLive();
    }
}

async function resumeAtManually() {
    var r = await apiPost('/api/adaptive/resume', { source: 'user' });
    if (r && r.ok) {
        addLog('Adaptive Tuning resumed (manual)');
        _refreshAdaptiveLive();
    }
}

async function loadCrossGameBlacklist() {
    var r = await apiGet('/api/adaptive/cross-blacklist');
    var c = document.getElementById('adaptive-cross-blacklist');
    if (!c) return;
    var entries = (r && r.entries) || [];
    if (entries.length === 0) {
        c.innerHTML = '<div class="empty-state" style="padding:8px;font-size:12px">No cross-game blacklist entries. Driver-level crashes (TDR / BSOD / WER) at specific offsets land here automatically.</div>';
        return;
    }
    var html = '<div style="font-size:12px;font-family:var(--font-mono);line-height:1.7">';
    entries.forEach(function(e) {
        var when = e.ts ? new Date(e.ts * 1000).toLocaleString() : '?';
        html += '<div style="padding:4px 0;border-bottom:1px solid var(--border)">' +
                  '<span style="color:var(--danger)">core+' + e.core + ' / mem+' + e.mem + '</span>' +
                  ' &nbsp;·&nbsp; <span style="color:var(--text-tertiary)">' + escHtml(e.reason || '') + '</span>' +
                  (e.source_exe ? ' &nbsp;·&nbsp; <span style="color:var(--text-secondary)">from ' + escHtml(e.source_exe) + '</span>' : '') +
                  ' &nbsp;·&nbsp; <span style="color:var(--text-tertiary);font-size:11px">' + when + '</span>' +
                '</div>';
    });
    html += '</div>';
    c.innerHTML = html;
}

// ── v3.2 — Diagnostic panel (the "what is AT doing?" surface) ───────
var _adaptiveDiagPoll = null;

function startAdaptiveDiagnosticsPoll() {
    if (_adaptiveDiagPoll) clearInterval(_adaptiveDiagPoll);
    _refreshAdaptiveDiagnostics();
    // v3.3.0-beta.5: was 4 s.  Diagnostics panel = explainer for AT's
    // decisions; it changes maybe once every 10-30 s in real workloads.
    // 6 s is responsive enough and matches the capture poll cadence.
    _adaptiveDiagPoll = setInterval(function() {
        if (_pollSkipIfHidden()) return;
        if (currentPage !== 'profiles') return;
        _refreshAdaptiveDiagnostics();
    }, 6000);
}

function stopAdaptiveDiagnosticsPoll() {
    if (_adaptiveDiagPoll) { clearInterval(_adaptiveDiagPoll); _adaptiveDiagPoll = null; }
}

async function _refreshAdaptiveDiagnostics() {
    var d = await apiGet('/api/adaptive/diagnostics');
    var c = document.getElementById('adaptive-diagnostics');
    if (!c || !d) return;

    var html = '';

    // ── Blockers section (the answer to "why isn't AT acting?") ────
    if (d.blockers && d.blockers.length) {
        html += '<div style="margin-bottom:12px">';
        d.blockers.forEach(function(b) {
            var color =
                b.level === 'error' ? 'var(--danger)' :
                b.level === 'warn'  ? 'var(--warning)' :
                'var(--text-tertiary)';
            var bg =
                b.level === 'error' ? 'rgba(255,80,80,0.10)' :
                b.level === 'warn'  ? 'rgba(255,180,80,0.10)' :
                'var(--bg-overlay)';
            html +=
                '<div style="padding:8px 10px;margin-bottom:6px;border-left:3px solid ' + color +
                ';background:' + bg + ';border-radius:4px">' +
                  '<div style="color:' + color + ';font-weight:600;font-size:12px">' + escHtml(b.msg) + '</div>' +
                  '<div style="color:var(--text-secondary);font-size:11.5px;margin-top:2px">' + escHtml(b.fix) + '</div>' +
                '</div>';
        });
        html += '</div>';
    }

    // ── Foreground game ────────────────────────────────────────────
    html += '<div style="margin-bottom:10px;font-family:var(--font-mono);font-size:12px;line-height:1.7">' +
              '<span style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase;display:block;margin-bottom:4px">Foreground game</span>';
    if (d.foreground) {
        html += '<span style="color:var(--text-bright);font-weight:600">' + escHtml(d.foreground.exe || '?') + '</span>' +
                ' · pid ' + (d.foreground.pid || '?') +
                ' · ' + (d.foreground_known
                    ? '<span style="color:var(--accent)">known game</span>'
                    : '<span style="color:var(--warning)">not in known list</span>' +
                      ' · <button class="btn btn-sm" style="margin-left:8px;padding:2px 8px;font-size:11px" ' +
                      'onclick="addThisGameToAt(\'' + escHtml(d.foreground.exe) + '\')">Track this game</button>');
    } else {
        html += '<span style="color:var(--text-tertiary)">none — game_profiles polls every 3 s</span>';
    }
    html += '</div>';

    // ── perf_monitor (GPU clock/util sampler) ──────────────────────
    var pm = d.perf_monitor || {};
    var pmColor = pm.running ? 'var(--accent)' : 'var(--danger)';
    html += '<div style="margin-bottom:10px;font-family:var(--font-mono);font-size:12px;line-height:1.7">' +
              '<span style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase;display:block;margin-bottom:4px">GPU sampler (nvidia-smi)</span>' +
              '<span style="color:' + pmColor + ';font-weight:600">' + (pm.running ? 'running' : 'stopped') + '</span>' +
              ' · samples ' + (pm.samples || 0) +
              (pm.last_util != null ? ' · util ' + pm.last_util + '%' : '') +
              (pm.last_core_mhz != null ? ' · core ' + pm.last_core_mhz + ' MHz' : '') +
              (pm.last_temp != null ? ' · ' + pm.last_temp + '°C' : '') +
            '</div>';

    // ── frame_telemetry (PresentMon 1.x + 2.x; RTSS removed in v3.3.1-beta.5) ──
    var ft = d.frame_telemetry || {};
    var ftAvail = ft.available && ft.available.available;
    var sourceLabel = ftAvail ? (ft.available.label || ft.available.source || '') :
                                 'none';
    html += '<div style="margin-bottom:10px;font-family:var(--font-mono);font-size:12px;line-height:1.7">' +
              '<span style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase;display:block;margin-bottom:4px">Frame telemetry' +
              ' — <span style="color:' + (ftAvail ? 'var(--accent-bright)' : 'var(--warning)') + '">' +
              escHtml(sourceLabel) + '</span></span>';
    if (ftAvail) {
        var live = ft.live || {};
        var recent = ft.recent || {};
        var pmh    = ft.pm_health || null;
        var srcLbl = (ft.available.source === 'presentmon') ? 'PresentMon (Microsoft)' :
                     ft.available.source;
        // Lead with the truth — installed AND producing frames is what
        // "connected" should mean.  PresentMon being installed but not
        // capturing this game (OpenGL etc) is the case that confused
        // people in beta.4.
        var pmProducing = pmh ? pmh.producing_frames : true;
        var headlineColor = pmProducing ? 'var(--accent)' : 'var(--warning)';
        var headlineText  = pmProducing ? 'connected & capturing frames' :
                            (pmh && !pmh.alive)                ? 'subprocess exited' :
                                                                  'attached, but no frames yet';
        html += '<span style="color:' + headlineColor + ';font-weight:600">' + headlineText + '</span>' +
                ' · source <span style="color:var(--accent-bright)">' + escHtml(srcLbl || '?') + '</span>' +
                (ft.available.version ? ' v' + escHtml(ft.available.version) : '') +
                (ft.tracked_name ? ' · target <span style="color:var(--text-bright)">' + escHtml(ft.tracked_name) + '</span>' : '');

        // PresentMon health row — only when PresentMon is the source
        if (pmh) {
            html += '<br>presentmon: ' +
                    (pmh.alive ? '<span style="color:var(--accent)">running</span>'
                              : '<span style="color:var(--danger)">exited (rc=' + (pmh.rc != null ? pmh.rc : '?') + ')</span>') +
                    ' · csv ' + (pmh.csv_size != null ? Math.round(pmh.csv_size / 1024) + ' KB' : '?') +
                    ' · buffered ' + (pmh.buffer_samples || 0) + ' samples';
            if (!pmh.producing_frames) {
                html += '<br><span style="color:var(--warning);font-size:11px">' +
                        '⚠ PresentMon is running but no frame events are coming through. ' +
                        'This usually means the game uses OpenGL or an older Vulkan path that ' +
                        'PresentMon 1.x can\'t capture (e.g. Minecraft Java). AT will fall back to ' +
                        'GPU-metrics-only scoring — still works, just without frametime variance signals.' +
                        '</span>';
            }
        }

        if (live.available) {
            html += '<br>live: fps ' + (live.fps_avg || '?') +
                    ' · frametime ' + (live.frametime_ms || '?') + ' ms' +
                    ' · age ' + (live.age_sec || '?') + ' s' +
                    ' · exe ' + escHtml(live.exe || '?');
        }
        if (recent.available && recent.sample_count) {
            var sigColor = recent.frametime_var_pct >= 40 ? 'var(--danger)' :
                           recent.frametime_var_pct >= 25 ? 'var(--warning)' :
                           'var(--accent)';
            html += '<br>30 s window: avg ' + (recent.avg_fps || '–') + ' fps' +
                    ' · 1%-low ' + (recent.min_fps_1pct || '–') +
                    ' · frametime ' + (recent.frametime_avg_ms || '–') + ' ms ' +
                    '(<span style="color:' + sigColor + '">σ ' + (recent.frametime_std_ms || '–') + ' ms, ' +
                    'var ' + (recent.frametime_var_pct || 0) + '%</span>)' +
                    ' · samples ' + recent.sample_count;
        }
    } else {
        // Not available — show download button for PresentMon
        var ftReason = (ft.available && ft.available.reason) || 'No frame-timing source.';
        var ftInstall = ft.install || {};
        html += '<span style="color:var(--warning);font-weight:600">no source active</span>' +
                '<br><span style="color:var(--text-secondary);font-size:11.5px">' +
                escHtml(ftReason) +
                '</span>';
        // Install button — visible when PresentMon not yet installed
        if (ftInstall.state === 'downloading') {
            var pct = ftInstall.total > 0
                ? Math.min(100, Math.round((ftInstall.downloaded / ftInstall.total) * 100))
                : 0;
            html += '<br><div style="margin-top:6px;font-size:11.5px;color:var(--text-secondary)">' +
                    'Downloading PresentMon... ' + pct + '% (' +
                    Math.round(ftInstall.downloaded / 1024) + ' KB' +
                    (ftInstall.total ? ' / ' + Math.round(ftInstall.total / 1024) + ' KB' : '') + ')' +
                    '<div style="margin-top:4px;height:4px;background:var(--bg-elevated);border-radius:2px;overflow:hidden">' +
                      '<div style="width:' + pct + '%;height:100%;background:var(--accent);transition:width 0.3s"></div>' +
                    '</div></div>';
        } else if (ftInstall.state === 'installed' || ftInstall.path) {
            html += '<br><span style="color:var(--accent);font-size:11.5px">' +
                    'PresentMon installed — will activate next time AT engages on a game.' +
                    '</span>';
        } else if (ftInstall.state === 'error') {
            html += '<br><span style="color:var(--danger);font-size:11.5px">' +
                    'Install failed: ' + escHtml(ftInstall.err || 'unknown') +
                    '</span>' +
                    '<br><button class="btn btn-sm btn-primary" style="margin-top:6px" ' +
                    'onclick="installPresentMon()">Retry download</button>';
        } else {
            html += '<br><button class="btn btn-sm btn-primary" style="margin-top:6px" ' +
                    'onclick="installPresentMon()">Enable PresentMon 1.x (~380 KB, one-time)</button>' +
                    '<div style="margin-top:4px;font-size:10.5px;color:var(--text-tertiary)">' +
                    'Downloads Microsoft PresentMon from its official GitHub. Runs only when AT engages on a game; no background process. <b>Does not capture OpenGL or Vulkan</b>.' +
                    '</div>';
        }
    }

    // v3.2-beta.6 — sources matrix.  Shows EVERY known source with its
    // coverage + install link.  Lets the user pick the right tool for
    // their games without us prescribing one.
    var allSources = (ft.available && ft.available.sources_tried) || [];
    if (allSources.length) {
        html += '<div style="margin-top:10px;padding:8px 10px;border:1px solid var(--border);' +
                'border-radius:4px;background:var(--bg-overlay)">' +
                '<div style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase;margin-bottom:6px">' +
                'Available sources (pick what fits your games)</div>';
        allSources.forEach(function(s) {
            // v3.2-beta.9 — 3-state status:
            //   green dot = ok (installed AND running)
            //   yellow dot = installed but not running (RTSS specifically)
            //   grey dot = not installed
            // s.ok comes from is_available's "ok" flag (running check).
            // For RTSS we may have installed=true but ok=false.
            var partial = !s.ok && s.installed_only;
            var dotColor = s.ok      ? 'var(--accent)' :
                            partial   ? 'var(--warning)' :
                                        'var(--text-tertiary)';
            var dot = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' +
                      dotColor + ';margin-right:6px"></span>';
            var rowColor = s.ok ? 'var(--text-bright)' : 'var(--text-secondary)';
            html += '<div style="margin-bottom:6px;font-size:11.5px;line-height:1.4">' +
                    dot + '<span style="color:' + rowColor + ';font-weight:600">' +
                    escHtml(s.label || s.name) + '</span>' +
                    (s.size ? ' <span style="color:var(--text-tertiary)">(' + escHtml(s.size) + ')</span>' : '') +
                    '<br><span style="color:var(--text-tertiary);font-size:10.5px;margin-left:14px">' +
                    'covers: ' + escHtml(s.coverage || '?') +
                    (s.reason ? ' · <span style="color:' + (partial ? 'var(--warning)' : 'var(--warning)') + '">' + escHtml(s.reason) + '</span>' : '') +
                    '</span>';
            if (!s.ok) {
                // v3.3.1-beta.5: RTSS removed as a frame source.
                if (s.name === 'presentmon') {
                    html += '<br><button class="btn btn-sm" style="margin:4px 0 0 14px;padding:2px 8px;font-size:10.5px" onclick="installPresentMon()">Install PresentMon 1.x</button>';
                } else if (s.install_url) {
                    html += '<br><a href="' + escHtml(s.install_url) +
                            '" target="_blank" class="btn btn-sm" ' +
                            'style="margin:4px 0 0 14px;padding:2px 8px;font-size:10.5px;display:inline-block">' +
                            'Open download page ↗</a>';
                }
            }
            html += '</div>';
        });
        html += '<div style="margin-top:8px;padding-top:6px;border-top:1px solid var(--border);font-size:10.5px;color:var(--text-tertiary);line-height:1.5">' +
                '<b>Quick guide:</b> For most modern D3D9-12 / DXGI games (almost every AAA), <b>PresentMon 1.x</b> is zero-config and tiny.  For OpenGL / Vulkan titles (Minecraft Java, Quake, DOOM Eternal Vulkan, etc), install <b>PresentMon 2.x</b> — Microsoft first-party, ~122 MB but covers everything PresentMon 1.x can\'t.' +
                '</div>' +
                '</div>';
    }

    html += '</div>';

    // ── Score sub-breakdown ────────────────────────────────────────
    var subs = (d.runtime || {}).last_score_subs || {};
    if (subs && Object.keys(subs).length) {
        html += '<div style="margin-bottom:10px;font-family:var(--font-mono);font-size:12px;line-height:1.7">' +
                  '<span style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase;display:block;margin-bottom:4px">GPU stability score breakdown</span>' +
                  'clock <span style="color:var(--accent-bright)">' + (subs.clock_score != null ? subs.clock_score : '?') + '/40</span>' +
                  ' · temp <span style="color:var(--accent-bright)">' + (subs.temp_score != null ? subs.temp_score : '?') + '/30</span>' +
                  ' · util <span style="color:var(--accent-bright)">' + (subs.util_score != null ? subs.util_score : '?') + '/30</span>' +
                  '<br>clock σ ' + (subs.clock_std != null ? subs.clock_std + ' MHz' : '?') +
                  ' · temp range ' + (subs.temp_range != null ? subs.temp_range + '°' : '?') +
                  ' · util σ ' + (subs.util_std != null ? subs.util_std + '%' : '?') +
                  ' · active ' + (subs.active_pct != null ? subs.active_pct + '%' : '?') +
                '</div>';
    }

    // ── Gameplay-vs-menu classifier ────────────────────────────────
    var rt = d.runtime || {};
    var gp = rt.gameplay_phase || 'unknown';
    var gpConf = Math.round((rt.gameplay_conf || 0) * 100);
    var gpBase = rt.gameplay_baseline || {};
    var gpColor = gp === 'gameplay' ? 'var(--accent)' :
                  gp === 'menu'     ? 'var(--warning)' :
                                       'var(--text-tertiary)';
    var gpDesc =
        gp === 'gameplay'           ? 'AT is making decisions normally.' :
        gp === 'menu'               ? 'AT is HOLDING — menu / lobby / loading screen detected. Decisions resume when gameplay returns.' :
        gp === 'transitional'       ? 'Recently switched states — waiting for ' + (3 - (rt.consecutive_alt || 0)) + ' more consistent ticks before changing.' :
        gp === 'baseline-building'  ? 'Gathering reference samples (need ~60 s of any activity).' :
                                       'Unknown — no telemetry yet.';
    html += '<div style="margin-bottom:10px;font-family:var(--font-mono);font-size:12px;line-height:1.7">' +
              '<span style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase;display:block;margin-bottom:4px">Gameplay vs menu classifier</span>' +
              '<span style="color:' + gpColor + ';font-weight:600">' + escHtml(gp) + '</span>' +
              ' · confidence <span style="color:var(--accent-bright)">' + gpConf + '%</span>' +
              '<br><span style="color:var(--text-secondary);font-size:11.5px">' + escHtml(gpDesc) + '</span>';

    // Baseline reference (what's considered "normal" for this game)
    if (gpBase && gpBase.fps_p50 != null) {
        var manualTag = gpBase.manual
            ? '<span style="color:var(--accent);font-size:10.5px"> (manual)</span>'
            : '';
        html += '<br><span style="color:var(--text-tertiary);font-size:11px">' +
                'baseline' + manualTag + ': fps <span style="color:var(--text-bright)">' +
                Math.round(gpBase.fps_p50 || 0) + '</span>' +
                ' · util <span style="color:var(--text-bright)">' +
                Math.round(gpBase.util_p50 || 0) + '%</span>' +
                ' · ftvar <span style="color:var(--text-bright)">' +
                (gpBase.ftvar_p50 != null ? gpBase.ftvar_p50.toFixed(1) + '%' : '?') + '</span>' +
                ' · ' + (gpBase.sample_count || 0) + ' samples' +
                '</span>';
    }

    // Reasons (the signals driving the current classification)
    var gpReasons = rt.gameplay_reasons || [];
    if (gpReasons.length) {
        html += '<div style="margin-top:6px;padding-left:10px;border-left:2px solid ' +
                gpColor + ';color:var(--text-secondary);font-size:11.5px">';
        gpReasons.forEach(function(r) {
            html += '• ' + escHtml(r) + '<br>';
        });
        html += '</div>';
    }

    // Manual recalibrate button (anchor "this is gameplay" for poor auto-detection)
    if (rt.active_exe) {
        html += '<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">' +
                '<button class="btn btn-sm" onclick="recalibrateGameplay(\'' + escHtml(rt.active_exe) + '\')" ' +
                'title="If the classifier got it wrong, anchor the baseline to RIGHT NOW. ' +
                'Use this when you\'re sure you\'re in actual gameplay so AT learns what your real-play numbers look like.">' +
                '▶ Anchor as gameplay</button>' +
                '<button class="btn btn-sm" onclick="markCurrentAsMenu(\'' + escHtml(rt.active_exe) + '\')" ' +
                'title="Teach the classifier that the CURRENT readings are a menu. ' +
                'AT will recognize this menu instantly the next time you enter it (no waiting on hysteresis).">' +
                '⏸ Mark as menu</button>' +
                '<button class="btn btn-sm" onclick="resetGameplayBaseline(\'' + escHtml(rt.active_exe) + '\')">' +
                'Reset baseline</button>' +
                '</div>';
    }

    // ── Learned menu signatures (v3.2-beta.4) ──────────────────────
    var sigs = (gpBase && gpBase.menu_signatures) || [];
    if (sigs.length) {
        html += '<div style="margin-top:8px;padding:6px 10px;background:rgba(255,180,80,0.05);border-radius:4px;border-left:2px solid var(--warning)">' +
                '<span style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase">Learned menu signatures (' + sigs.length + ')</span>';
        sigs.forEach(function(sig, i) {
            var srcTag = sig.source === 'manual' ? ' ✋' : ' 🤖';
            html += '<div style="margin-top:4px;font-size:11.5px;display:flex;align-items:center;gap:6px">' +
                    '<span style="color:var(--text-secondary);flex:1">' +
                    srcTag + ' fps ' + sig.fps_lo + '–' + sig.fps_hi +
                    ' · util ' + sig.util_lo + '–' + sig.util_hi +
                    '</span>' +
                    '<button class="btn btn-sm" style="padding:1px 6px;font-size:10.5px" ' +
                    'onclick="removeMenuSignature(\'' + escHtml(rt.active_exe) + '\', ' + i + ')">×</button>' +
                    '</div>';
        });
        html += '</div>';
    }
    html += '</div>';

    // ── Next action (the "what's AT waiting on?" line) ─────────────
    if (d.next_action) {
        var na = d.next_action;
        html += '<div style="padding:8px 10px;margin-top:8px;border-radius:4px;background:var(--accent-soft);font-size:12px;line-height:1.5">' +
                  '<span style="color:var(--text-tertiary);font-size:10.5px;letter-spacing:0.05em;text-transform:uppercase">Next action</span><br>' +
                  '<span style="color:var(--text-bright)">' + escHtml(na.msg) + '</span>' +
                '</div>';
    }
    c.innerHTML = html;
}

async function recalibrateGameplay(exe) {
    var r = await apiPost('/api/adaptive/gameplay-recalibrate', { exe: exe });
    if (r && r.ok) {
        addLog('Gameplay baseline anchored for ' + exe);
        _refreshAdaptiveDiagnostics();
    } else {
        addLog('Recalibrate failed: ' + ((r && r.err) || 'unknown'));
    }
}

async function resetGameplayBaseline(exe) {
    if (!confirm('Wipe gameplay/menu samples + baseline for ' + exe + '?\n\n' +
                 'AT will rebuild from scratch (~60 s of any activity before it can classify again).')) return;
    var r = await apiPost('/api/adaptive/gameplay-reset', { exe: exe });
    if (r && r.ok) {
        addLog('Gameplay baseline reset for ' + exe);
        _refreshAdaptiveDiagnostics();
    }
}

async function markCurrentAsMenu(exe) {
    var r = await apiPost('/api/adaptive/menu-signature/add', { exe: exe });
    if (r && r.ok) {
        if (r.added) {
            addLog('Menu signature learned for ' + exe + ': fps≈' +
                   r.signature.fps + ', util≈' + r.signature.util);
        } else if (r.duplicate_of) {
            addLog('Already matched an existing signature — nothing new to learn');
        }
        _refreshAdaptiveDiagnostics();
    } else {
        addLog('Mark-as-menu failed: ' + ((r && r.err) || 'unknown'));
    }
}

async function removeMenuSignature(exe, index) {
    var r = await apiPost('/api/adaptive/menu-signature/remove',
                          { exe: exe, index: index });
    if (r && r.ok) {
        addLog('Removed menu signature for ' + exe);
        _refreshAdaptiveDiagnostics();
    }
}

// beta.15 — startRtss kept as a stub so any cached UI fragment that
// still calls it doesn't blow up.  GhostShell no longer starts RTSS
// from any path; if a user wants it running they launch it themselves.
async function startRtss() {
    showInfoToast('RTSS is no longer auto-launched by Vispora. Start it '
        + 'yourself from the Start menu if you want it running — Vispora '
        + 'will still detect it for the OC-tool conflict warning.',
        { title: 'RTSS auto-launch removed', timeoutMs: 8000 });
}

async function installDepNow(key) {
    addLog('Installing ' + key + ' via dependency manager…');
    var r = await apiPost('/api/dependencies/install', { keys: [key] });
    if (r && r.ok) {
        addLog(key + ' install started — watch the banner at the top of the page for progress');
    } else {
        addLog(key + ' install failed: ' + ((r && r.err) || 'unknown'));
    }
    setTimeout(_refreshAdaptiveDiagnostics, 1500);
}

async function installPresentMon() {
    addLog('Downloading PresentMon from Microsoft GitHub...');
    var r = await apiPost('/api/frame-telemetry/install', {});
    if (!r || !r.ok) {
        addLog('PresentMon install failed: ' + ((r && r.err) || 'unknown'));
        return;
    }
    if (r.already_installed) {
        addLog('PresentMon already installed at ' + r.path);
        _refreshAdaptiveDiagnostics();
        return;
    }
    // Refresh diagnostics aggressively while download is in flight so
    // the progress bar updates smoothly.
    var pollCount = 0;
    var pollInterval = setInterval(async function() {
        pollCount++;
        var prog = await apiGet('/api/frame-telemetry/install/progress');
        if (!prog) return;
        _refreshAdaptiveDiagnostics();
        if (prog.state === 'installed') {
            clearInterval(pollInterval);
            addLog('PresentMon installed successfully — frame telemetry will activate on next game launch');
        } else if (prog.state === 'error') {
            clearInterval(pollInterval);
            addLog('PresentMon install error: ' + (prog.err || 'unknown'));
        } else if (pollCount > 60) {
            // 60 ticks * 1.5s = 90s timeout
            clearInterval(pollInterval);
            addLog('PresentMon install timed out');
        }
    }, 1500);
}

async function addThisGameToAt(exe) {
    if (!exe) return;
    var r = await apiPost('/api/profiles/add-game', { exe: exe });
    if (r && r.ok) {
        addLog('Added ' + exe + ' to known games — enabling AT for it');
        // Tracking just got added; flip per-game AT switch on AND try to
        // retroactively engage so the user doesn't have to relaunch the game.
        await apiPost('/api/adaptive/game-toggle', { exe: exe, enabled: true });
        setTimeout(function() {
            _refreshAdaptiveLive();
            loadAdaptiveGames();
            _refreshAdaptiveDiagnostics();
        }, 800);
    } else {
        addLog('Could not add ' + exe + ': ' + ((r && r.err) || 'unknown'));
    }
}

async function clearCrossGameBlacklist() {
    if (!confirm('Wipe the cross-game offset blacklist?\n\nThese are driver-level crashes (TDR/BSOD/WER) where AT decided NO game should try this offset.  Clear after a driver update that may have fixed the underlying instability.')) return;
    var r = await apiPost('/api/adaptive/clear-cross-blacklist', {});
    if (r && r.ok) {
        addLog('Cross-game blacklist cleared');
    }
    loadCrossGameBlacklist();
}

async function loadAdaptiveHistory() {
    var data = await apiGet('/api/adaptive/history');
    var history = (data && data.history) || [];
    var c = document.getElementById('adaptive-history');
    if (!c) return;
    if (history.length === 0) {
        c.innerHTML = '<div class="empty-state">No tuning steps yet.</div>';
        return;
    }
    var html = '';
    history.slice().reverse().forEach(function(h) {
        var d = new Date(h.ts * 1000);
        var arrow = h.action === 'step_up'    ? '↑' :
                    h.action === 'step_down'  ? '↓' :
                    h.action === 'converged'  ? '✓' : '·';
        var color = h.action === 'step_up'    ? 'var(--accent)' :
                    h.action === 'step_down'  ? 'var(--danger)' :
                    h.action === 'converged'  ? 'var(--accent)' : 'var(--text-secondary)';
        html +=
          '<div style="padding:6px 10px;border-left:2px solid ' + color + ';margin-bottom:4px;background:var(--bg-elevated);font-family:var(--font-mono)">' +
            '<span style="color:var(--text-tertiary);font-size:11px">' + d.toLocaleString() + '</span> · ' +
            '<span style="color:' + color + ';font-weight:600">' + arrow + ' ' + h.action + '</span> · ' +
            escHtml(h.display_name || h.exe) + ' · ' +
            'core+' + h.from_core + '→' + h.to_core + ' / ' +
            'mem+' + h.from_mem + '→' + h.to_mem +
            (h.score != null ? ' · score ' + h.score : '') +
            (h.reason ? ' <span style="color:var(--text-tertiary)">(' + escHtml(h.reason) + ')</span>' : '') +
          '</div>';
    });
    c.innerHTML = html;
}

function _startAdaptivePoll() {
    if (_adaptivePollTimer) return;
    _refreshAdaptiveLive();
    // v3.3.0-beta.5: was 3 s.  AT decisions land every few seconds at
    // most; the live readout is a status indicator, not a chart.  5 s
    // is plenty live and cuts Flask hits by 40%.  Also added a
    // currentPage gate so the poll stops doing fetches when the user
    // navigates away (the interval gets cleared by _stopAdaptivePoll
    // when leaving profiles, but the in-flight refresh that started
    // just before navigation can still race).
    _adaptivePollTimer = setInterval(function() {
        if (_pollSkipIfHidden()) return;
        if (currentPage !== 'profiles') return;
        _refreshAdaptiveLive();
    }, 5000);
    // Gamepad mapper live Hz/p99 readout — was 1.5 s, bumped to 3 s.
    if (!_gpmStatsTimer) {
        _gpmStatsTimer = setInterval(function(){
            if (currentPage !== 'profiles') return;
            apiGet('/api/gamepad/status').then(function(s){
                if (!s) return;
                var hzEl  = document.getElementById('gpm-actual-hz');
                var p99El = document.getElementById('gpm-p99');
                if (hzEl)  hzEl.textContent  = s.actual_hz ? (s.actual_hz.toFixed(1) + ' Hz / ' + s.target_hz + ' target') : (s.target_hz + ' Hz target');
                if (p99El) p99El.textContent = s.frame_p99_us ? (s.frame_p99_us + ' µs (max ' + s.frame_max_us + ' µs)') : '—';
            });
        }, 3000);
    }
}

var _gpmStatsTimer = null;

function _stopAdaptivePoll() {
    if (_adaptivePollTimer) { clearInterval(_adaptivePollTimer); _adaptivePollTimer = null; }
    if (_gpmStatsTimer) { clearInterval(_gpmStatsTimer); _gpmStatsTimer = null; }
    stopGpmSvgLoop();
}

async function loadProfileEngineStatus() {
    var data = await apiGet('/api/profiles/status');
    _profileEngineRunning = data.monitoring;
    var el = document.getElementById('profile-engine-status');
    var btn = document.getElementById('btn-profile-engine');

    if (el) {
        if (data.gaming_mode && data.active_game) {
            el.innerHTML = '<span class="status-badge ok" style="font-size:11px">GAMING MODE ACTIVE</span> ' +
                '<span style="color:var(--accent);font-size:13px;font-weight:600">' + escHtml(data.active_game) + '</span>' +
                ' <span style="color:var(--text-dim);font-size:10px">(PID ' + data.active_pid + ')</span>';
        } else if (data.monitoring) {
            el.innerHTML = '<span class="status-badge ok">Monitoring</span> <span style="font-size:11px;color:var(--text-dim)">Watching for games... (' + (data.known_game_count || 0) + ' known games)</span>';
        } else {
            el.innerHTML = '<span class="status-badge neutral">Stopped</span>';
        }
    }
    if (btn) btn.textContent = _profileEngineRunning ? 'Stop Monitoring' : 'Start Monitoring';

    var agi = document.getElementById('active-game-info');
    if (agi) {
        if (data.gaming_mode && data.active_game) {
            var changesHtml = (data.applied_changes || []).map(function(c) {
                return '<div style="font-size:10px;color:var(--accent);padding:1px 0">  + ' + escHtml(c) + '</div>';
            }).join('');
            agi.innerHTML = '<div style="font-size:16px;color:var(--accent);font-weight:600;margin-bottom:4px">' + escHtml(data.active_game) + '</div>' +
                '<div style="font-size:11px;color:var(--text-dim);margin-bottom:4px">' + escHtml(data.active_exe || '') + ' (PID ' + data.active_pid + ')</div>' +
                '<div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">Hard tweaks applied:</div>' + changesHtml +
                '<div style="font-size:10px;color:var(--orange);margin-top:8px">All tweaks will revert automatically when the game exits.</div>';
        } else if (data.monitoring) {
            agi.textContent = 'No game detected. Waiting...';
        } else {
            agi.textContent = 'Start monitoring to enable auto-detection.';
        }
    }
}

async function toggleProfileEngine() {
    if (_profileEngineRunning) {
        await apiPost('/api/profiles/stop');
        if (_profilePollTimer) { clearInterval(_profilePollTimer); _profilePollTimer = null; }
        addLog('Game monitor stopped');
    } else {
        await apiPost('/api/profiles/start');
        _profilePollTimer = setInterval(function() { if (currentPage === 'profiles') loadProfileEngineStatus(); }, 3000);
        addLog('Game monitor started — watching for games');
    }
    _profileEngineRunning = !_profileEngineRunning;
    loadProfileEngineStatus();
}

async function addCustomGame() {
    var exe = document.getElementById('custom-game-exe').value.trim();
    var name = document.getElementById('custom-game-name').value.trim();
    if (!exe) { showWarnToast('Enter an exe name'); return; }
    var r = await apiPost('/api/profiles/add-game', { exe: exe, name: name });
    if (r.ok) {
        document.getElementById('custom-game-exe').value = '';
        document.getElementById('custom-game-name').value = '';
        addLog('Added custom game: ' + exe);
        loadCustomGames();
    }
}

async function loadCustomGames() {
    var data = await apiGet('/api/profiles');
    var el = document.getElementById('custom-games-list');
    if (!el) return;
    var custom = (data.profiles || {}).custom;
    if (!custom || !custom.exes || custom.exes.length === 0) {
        el.innerHTML = '';
        return;
    }
    var html = '';
    custom.exes.forEach(function(g) {
        html += '<div class="check-item"><span style="flex:1">' + escHtml(g.name || g.exe) + '</span><span style="font-size:10px;color:var(--text-dim)">' + escHtml(g.exe) + '</span><button class="btn btn-sm btn-danger" onclick="removeCustomGame(\'' + escAttr(g.exe) + '\')">x</button></div>';
    });
    el.innerHTML = html;
}

async function removeCustomGame(exe) {
    await apiPost('/api/profiles/remove-game', { exe: exe });
    loadCustomGames();
}

async function forceRestoreNormal() {
    addLog('Forcing normal mode...');
    var r = await apiPost('/api/profiles/restore-normal');
    if (r.ok) {
        addLog('Normal mode restored — all gaming tweaks reverted');
    }
    loadProfileEngineStatus();
}

async function recaptureBaseline() {
    addLog('Recapturing system baseline...');
    var r = await apiPost('/api/profiles/recapture-baseline');
    if (r.ok) {
        addLog('Baseline recaptured: ' + JSON.stringify(r.baseline || {}));
    }
}

async function refreshPC() {
    addLog('🔄 Manual PC refresh started — clearing RAM, flushing DNS, killing orphans...');
    var r = await apiPost('/api/profiles/refresh');
    if (r.ok) {
        addLog('✓ Refresh started in background — check the log for details (takes ~10s)');
    } else {
        addLog('✗ Refresh failed: ' + (r.err || 'unknown'));
    }
}

// ═══════════════════════════════════════════════════════════════
// HARDWARE MONITOR PAGE
// ═══════════════════════════════════════════════════════════════
var _monitorRunning = false;

async function loadHWMonPage() {
    var data = await apiGet('/api/monitor/snapshot');
    _monitorRunning = data.running;
    document.getElementById('btn-monitor').textContent = _monitorRunning ? 'Stop Monitor' : 'Start Monitor';
    if (_monitorRunning) updateHWMonDisplay(data);
    // v3 — also kick the perf sampler.  It refcounts so the game-mode
    // consumer keeps it alive even after we leave this page.
    apiPost('/api/perf/start', { consumer: 'hwmon-page' });
    _startPerfPoll();
}

// ═══ Live Performance sampler (Hardware Monitor card) ═══
var _perfPollTimer = null;
var _perfChartColors = {
    core:  '#c4c1ff',
    mem:   '#a7f0e4',
    temp:  '#f0b341',
    power: '#b8c8ff',
    util:  '#d9b8ff',
    fan:   '#a0a0a8',
};
var _perfChartUnits = {
    core: 'MHz', mem: 'MHz', temp: '°C',
    power: 'W',  util: '%',  fan: '%',
};

function _drawSparkline(canvas, values, color) {
    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    // Match backing-store size to displayed CSS size for crispness
    var cssW = canvas.clientWidth || canvas.width;
    var cssH = canvas.clientHeight || canvas.height;
    if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
        canvas.width = cssW * dpr;
        canvas.height = cssH * dpr;
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);

    if (!values || values.length < 2) {
        ctx.fillStyle = '#5a5a64';
        ctx.font = '11px "JetBrains Mono", monospace';
        ctx.fillText('no data', 4, 14);
        return;
    }

    var min = Infinity, max = -Infinity;
    for (var i = 0; i < values.length; i++) {
        var v = values[i];
        if (v < min) min = v;
        if (v > max) max = v;
    }
    if (max === min) { max = min + 1; }
    var range = max - min;

    // Subtle filled area
    ctx.beginPath();
    var stepX = cssW / (values.length - 1);
    for (var j = 0; j < values.length; j++) {
        var x = j * stepX;
        var y = cssH - 4 - ((values[j] - min) / range) * (cssH - 8);
        if (j === 0) ctx.moveTo(x, y);
        else         ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Soft fill below
    ctx.lineTo(cssW, cssH);
    ctx.lineTo(0, cssH);
    ctx.closePath();
    ctx.fillStyle = color + '22';   // ~13% alpha hex suffix
    ctx.fill();
}

function _renderPerfMeta(key, values) {
    var meta = document.querySelector('[data-meta="' + key + '"]');
    if (!meta) return;
    if (!values || values.length === 0) {
        meta.textContent = '—';
        return;
    }
    var cur = values[values.length - 1];
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    var avg = values.reduce(function(s, v) { return s + v; }, 0) / values.length;
    var u = _perfChartUnits[key] || '';
    var fmt = function(n) { return (n % 1 === 0) ? n.toFixed(0) : n.toFixed(1); };
    meta.textContent = fmt(cur) + ' ' + u
        + ' · avg ' + fmt(avg)
        + ' · min ' + fmt(min)
        + ' · max ' + fmt(max);
}

async function loadPerfMonitor() {
    var data = await apiGet('/api/perf/samples?seconds=60');
    if (!data) return;
    ['core', 'mem', 'temp', 'power', 'util', 'fan'].forEach(function(k) {
        var canvas = document.querySelector('canvas[data-spark="' + k + '"]');
        if (!canvas) return;
        _drawSparkline(canvas, data[k] || [], _perfChartColors[k]);
        _renderPerfMeta(k, data[k] || []);
    });

    var s = await apiGet('/api/perf/score?seconds=30');
    var scoreEl  = document.getElementById('perf-stability-score');
    var pillEl   = document.getElementById('perf-stability-pill');
    var detailEl = document.getElementById('perf-stability-detail');
    if (!scoreEl || !pillEl) return;
    if (!s || s.score == null) {
        scoreEl.textContent = '—';
        pillEl.className = 'status-badge neutral';
        pillEl.textContent = (s && s.verdict) || 'no data';
        if (detailEl) detailEl.textContent = (s && s.reason) || '';
        return;
    }
    scoreEl.textContent = s.score;
    var verdict = s.verdict || 'unknown';
    pillEl.textContent = verdict;
    pillEl.className = 'status-badge ' +
        (verdict === 'healthy'  ? 'ok' :
         verdict === 'warn'     ? 'warn' :
         verdict === 'unstable' ? 'danger' : 'neutral');
    if (detailEl && s.subs) {
        detailEl.textContent =
            'clock σ ' + s.subs.clock_std + 'MHz · ' +
            'temp Δ ' + s.subs.temp_range + '°C · ' +
            'util σ ' + s.subs.util_std + '% · ' +
            s.subs.active_pct + '% active · ' +
            s.samples_used + ' samples';
    }
}

function _startPerfPoll() {
    if (_perfPollTimer) return;
    loadPerfMonitor();
    // v3.2.3 — was 1 s (5 Hz on top of perf_monitor's own 5 Hz sampler).
    // 2 s is plenty for live sparkline visualization and roughly halves
    // the Flask request rate from this poll.
    _perfPollTimer = setInterval(function() {
        if (_pollSkipIfHidden()) return;
        loadPerfMonitor();
    }, 2000);
}

function _stopPerfPoll() {
    if (_perfPollTimer) { clearInterval(_perfPollTimer); _perfPollTimer = null; }
    // Release our consumer slot — sampler keeps running if game-mode etc.
    // are still using it.
    apiPost('/api/perf/stop', { consumer: 'hwmon-page' });
}

async function toggleMonitor() {
    if (_monitorRunning) {
        await apiPost('/api/monitor/stop');
        if (_hwmonPollTimer) { clearInterval(_hwmonPollTimer); _hwmonPollTimer = null; }
    } else {
        await apiPost('/api/monitor/start');
        _hwmonPollTimer = setInterval(function() { if (_pollSkipIfHidden()) return; pollHWMon(); }, 3000);
    }
    _monitorRunning = !_monitorRunning;
    document.getElementById('btn-monitor').textContent = _monitorRunning ? 'Stop Monitor' : 'Start Monitor';
}

async function pollHWMon() {
    if (currentPage !== 'hwmon' || !_monitorRunning) return;
    var data = await apiGet('/api/monitor/snapshot');
    updateHWMonDisplay(data);
}

function updateHWMonDisplay(data) {
    var l = data.latest || {};

    var cpuT = document.getElementById('hw-cpu-temp');
    var cpuC = document.getElementById('hw-cpu-clock');
    var cpuB = document.getElementById('hw-cpu-bar');
    if (cpuT) cpuT.textContent = l.cpu_temp != null ? l.cpu_temp + '°C' : '—';
    if (cpuC) cpuC.textContent = (l.cpu_load != null ? l.cpu_load + '% load' : '') + (l.cpu_clock ? ' • ' + l.cpu_clock + ' MHz' : '');
    if (cpuB) { cpuB.style.width = (l.cpu_load || 0) + '%'; cpuB.className = 'stat-bar-fill' + ((l.cpu_temp || 0) > 90 ? ' crit' : (l.cpu_temp || 0) > 80 ? ' warn' : ''); }

    var gpuT = document.getElementById('hw-gpu-temp');
    var gpuC = document.getElementById('hw-gpu-clock');
    var gpuB = document.getElementById('hw-gpu-bar');
    if (gpuT) gpuT.textContent = l.gpu_temp != null ? l.gpu_temp + '°C' : '—';
    if (gpuC) gpuC.textContent = (l.gpu_load != null ? l.gpu_load + '% load' : '') + (l.gpu_clock ? ' • ' + l.gpu_clock + ' MHz' : '');
    if (gpuB) { gpuB.style.width = (l.gpu_load || 0) + '%'; gpuB.className = 'stat-bar-fill' + ((l.gpu_temp || 0) > 85 ? ' crit' : (l.gpu_temp || 0) > 75 ? ' warn' : ''); }

    var ramE = document.getElementById('hw-ram');
    var ramS = document.getElementById('hw-ram-standby');
    var ramB = document.getElementById('hw-ram-bar');
    if (ramE) ramE.textContent = l.ram_pct != null ? l.ram_pct + '%' : '—';
    if (ramS) ramS.textContent = 'Standby: ' + (l.ram_standby_mb || 0) + ' MB';
    if (ramB) { ramB.style.width = (l.ram_pct || 0) + '%'; ramB.className = 'stat-bar-fill' + ((l.ram_pct || 0) > 90 ? ' crit' : (l.ram_pct || 0) > 80 ? ' warn' : ''); }

    var vramE = document.getElementById('hw-vram');
    var vramB = document.getElementById('hw-vram-bar');
    if (vramE) vramE.textContent = l.gpu_vram_pct != null ? l.gpu_vram_pct + '%' : '—';
    if (vramB) { vramB.style.width = (l.gpu_vram_pct || 0) + '%'; }

    // 3.3.2 — relabeled to "DPC Rate" (DPCs/sec — what LatencyMon uses).
    // Reads via NT API SystemInterruptInformation; works even when
    // perflib is corrupt.  Healthy: <5,000/s, concerning: >20,000/s.
    var dpcE = document.getElementById('hw-dpc');
    if (dpcE) {
        if (l.dpc_rate != null) {
            dpcE.textContent = l.dpc_rate.toLocaleString() + ' /s';
        } else if (l.dpc_ok && l.dpc_pct != null) {
            dpcE.textContent = l.dpc_pct.toFixed(2) + ' %';
        } else {
            dpcE.textContent = '—';
        }
    }

    var pingE = document.getElementById('hw-ping');
    if (pingE) pingE.textContent = l.net_ping_ms != null ? l.net_ping_ms + ' ms' : '—';

    // Alerts
    var alertsEl = document.getElementById('hw-alerts');
    if (alertsEl && data.alerts && data.alerts.length > 0) {
        alertsEl.innerHTML = data.alerts.map(function(a) {
            return '<div class="warn-box" style="margin-bottom:4px"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/></svg>' + escHtml(a) + '</div>';
        }).join('');
    } else if (alertsEl) {
        alertsEl.innerHTML = '';
    }
}

async function runBenchmark() {
    var el = document.getElementById('benchmark-results');
    if (el) el.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Running benchmark (30-60 seconds)...</span>';
    var data = await apiPost('/api/monitor/benchmark');
    if (!el) return;
    var html = '<div style="font-size:13px;color:var(--accent);margin-bottom:8px">Composite Score: <strong>' + (data.composite_score || 0) + '</strong></div>';
    if (data.cpu_single) html += '<div style="font-size:11px;color:var(--text-dim)">CPU: ' + data.cpu_single.score + ' (primes in ' + data.cpu_single.elapsed_ms + 'ms)</div>';
    if (data.memory) html += '<div style="font-size:11px;color:var(--text-dim)">Memory: ' + data.memory.bandwidth_mbps + ' MB/s</div>';
    if (data.disk) html += '<div style="font-size:11px;color:var(--text-dim)">Disk: R=' + data.disk.read_mbps + ' MB/s W=' + data.disk.write_mbps + ' MB/s</div>';
    if (data.network) html += '<div style="font-size:11px;color:var(--text-dim)">Ping: ' + (data.network.ping_ms || '?') + 'ms</div>';
    el.innerHTML = html;
}

async function loadTopProcesses() {
    var el = document.getElementById('top-processes');
    if (!el) return;
    el.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Scanning...</span>';
    var data = await apiGet('/api/monitor/processes');
    var procs = data.processes || [];
    if (procs.length === 0) { el.innerHTML = 'No process data'; return; }
    var html = '<div style="display:flex;gap:8px;padding:2px 0;color:var(--accent);font-size:10px;font-weight:600"><span style="flex:2">Process</span><span style="flex:1;text-align:right">CPU (s)</span><span style="flex:1;text-align:right">RAM (MB)</span></div>';
    procs.forEach(function(p) {
        html += '<div style="display:flex;gap:8px;padding:2px 0"><span style="flex:2">' + escHtml(p.ProcessName || '') + '</span><span style="flex:1;text-align:right">' + (p.CPU_s || 0) + '</span><span style="flex:1;text-align:right">' + (p.RAM_MB || 0) + '</span></div>';
    });
    el.innerHTML = html;
}

// Update switchPage to load new pages
var _origSwitchPage = switchPage;
switchPage = function(page) {
    _origSwitchPage(page);
    if (page === 'kernel') loadKernelPage();
    if (page === 'profiles') loadProfilesPage();
    if (page === 'hwmon') loadHWMonPage();

    // v3 — Hardware Monitor auto-runs.  Page entry kicks polling back on,
    // exit pauses it (keeps the backend monitor running so the snapshot is
    // current next time we open the page).
    if (page === 'hwmon') {
        if (!_monitorRunning) {
            apiPost('/api/monitor/start').then(function() {
                _monitorRunning = true;
                if (!_hwmonPollTimer) _hwmonPollTimer = setInterval(function() { if (_pollSkipIfHidden()) return; pollHWMon(); }, 3000);
                pollHWMon();
            });
        } else if (!_hwmonPollTimer) {
            _hwmonPollTimer = setInterval(function() { if (_pollSkipIfHidden()) return; pollHWMon(); }, 3000);
            pollHWMon();
        }
    } else if (_hwmonPollTimer) {
        clearInterval(_hwmonPollTimer);
        _hwmonPollTimer = null;
        // v3 — also pause the perf-monitor poll + release consumer slot
        _stopPerfPoll();
    }

    // Poll profile status periodically
    if (page === 'profiles' && _profileEngineRunning) {
        setTimeout(function() { if (currentPage === 'profiles') loadProfileEngineStatus(); }, 3000);
    }
    if (page !== 'profiles') _stopAdaptivePoll();

    // v3 — GPU live state auto-runs.  Same pattern as hwmon: keep polling
    // while on the GPU page, pause when off.
    if (page === 'gpu') {
        if (!_ocState.liveActive) toggleOcLive();
        // v3 — also poll the temp-ceiling watchdog status so the UI
        // shows current temp / "above ceiling" / cooldown state in real time.
        _startTempCeilingPoll();
        // v3 — load crash-recovery history once on page entry (no need to poll;
        // updates only happen on game exit which is a rare event)
        try { loadCrashRecoveryHistory(); } catch (e) {}
    } else {
        // Pause the live poll when leaving GPU, but only if no in-flight
        // stress test / auto-OC is depending on it.
        if (_ocState.liveActive && !_ocState.stressActive && !_ocState.autoActive) {
            stopOcLive();
        }
        _stopTempCeilingPoll();
        // Abort any in-flight stress / auto-OC so timers and WebGL contexts
        // don't keep running after the user navigates away.
        try {
            if (_ocState.stressActive && typeof abortStabilityTest === 'function') {
                abortStabilityTest();
            }
            if (_ocState.autoActive && typeof cancelAutoOc === 'function') {
                cancelAutoOc();
            }
            if (_ocState.benchmarkActive) {
                _ocState.benchmarkActive = false;
            }
        } catch (e) { console.warn('cleanup on page switch failed', e); }
    }
};

// ═══════════════════════════════════════════════════════════════
// GPU OVERCLOCKING
// ═══════════════════════════════════════════════════════════════
var _ocState = {
    capability: null,
    profile: null,
    liveTimer: null,
    liveActive: false,
    stressActive: false,
    stressWebGL: null,      // { gl, program, canvas, rafId, frameCount, lastTick }
    stressPollTimer: null,
    stressStarted: 0,
    stressDuration: 60,
    stressAborted: false,
    autoActive: false,
    // v2.9.5 — benchmark uses _runAutoStabilityStep() too; without its own
    // flag the loop's `if (!_ocState.autoActive) break` would exit immediately
    // and produce a zero-data verdict (the bug from your screenshot).
    benchmarkActive: false,
    autoLog: [],
};

// Called after loadGpuInfo() finishes. Safe to call multiple times.
async function loadOcCapability() {
    var cap = await apiGet('/api/gpu/oc/capability');
    _ocState.capability = cap;
    var el = document.getElementById('oc-capability');
    if (!el) return;

    if (!cap.ok || cap.vendor !== 'nvidia') {
        // 3.4.0 — clean "not your GPU" messaging instead of a raw error.
        // GhostShell's overclocking is NVIDIA-only for now; everything
        // else on this page (telemetry, driver info) still works.
        el.innerHTML =
            '<div style="color:var(--text-secondary)">Overclocking isn\'t available for your GPU.</div>' +
            '<div style="margin-top:4px;font-size:11px;color:var(--text-tertiary)">' +
              'Vispora\'s OC + Adaptive Tuning are NVIDIA-only right now. ' +
              'Detected: <b>' + escHtml((cap.vendor || 'unknown').toUpperCase()) + '</b> — ' +
              escHtml(cap.name || 'unknown') + '. ' +
              'Temperature, load, and clock telemetry still work for your GPU.' +
            '</div>';
        // Dim the manual card
        var manual = document.getElementById('oc-manual-card');
        var auto = document.getElementById('oc-auto-card');
        if (manual) manual.style.opacity = '0.4';
        if (auto) auto.style.opacity = '0.4';
        // Disable sliders
        ['oc-core-slider','oc-mem-slider','oc-power-slider','auto-oc-start-btn'].forEach(function(id){
            var e = document.getElementById(id);
            if (e) e.disabled = true;
        });
        return;
    }

    var lim = cap.limits || {};
    el.innerHTML =
        '<div style="color:var(--accent)">✓ NVIDIA overclocking available</div>' +
        '<div style="margin-top:4px">' +
            '<span style="margin-right:14px">🎛 Core max: <b>' + (lim.core_max_mhz || '?') + ' MHz</b></span>' +
            '<span style="margin-right:14px">🎛 Memory max: <b>' + (lim.mem_max_mhz || '?') + ' MHz</b></span>' +
            '<span style="margin-right:14px">⚡ Power range: <b>' + (lim.power_min_w || '?') + '–' + (lim.power_max_w || '?') + ' W</b> (default ' + (lim.power_default_w || '?') + ' W)</span>' +
        '</div>';

    // v2.9.9.2 — read max from backend instead of hardcoding 400/2000.
    // Backend reports core_max_offset / mem_max_offset (currently 950 / 4500
    // per MAX_CORE_OFFSET_MHZ in gpu_overclock.py).  Falling back to the HTML
    // attribute means manual edits to the slider tag also survive.
    var coreSlider = document.getElementById('oc-core-slider');
    var memSlider  = document.getElementById('oc-mem-slider');
    var coreMax = (lim.core_max_offset != null) ? lim.core_max_offset : (data.core_max_offset || 950);
    var memMax  = (lim.mem_max_offset  != null) ? lim.mem_max_offset  : (data.mem_max_offset  || 4500);
    if (coreSlider) coreSlider.max = coreMax;
    if (memSlider)  memSlider.max  = memMax;

    // Load current saved profile
    loadOcProfile();
    // Do a one-shot live read
    pollOcLiveOnce();
}

async function loadOcProfile() {
    var p = await apiGet('/api/gpu/oc/profile');
    _ocState.profile = p;
    var el = document.getElementById('oc-profile-display');
    var chk = document.getElementById('oc-apply-on-startup');
    if (!el) return;

    if (!p.exists) {
        el.innerHTML = '<span style="color:var(--text-dim)">No saved profile.</span>';
        return;
    }

    var date = p.saved_at ? new Date(p.saved_at * 1000).toLocaleString() : '';
    var auto = p.created_auto ? ' <span style="color:var(--accent2)">(auto-tuned)</span>' : '';
    el.innerHTML =
        '<div>Core: <b style="color:var(--accent)">+' + p.core_offset_mhz + ' MHz</b></div>' +
        '<div>Memory: <b style="color:var(--accent)">+' + p.mem_offset_mhz + ' MHz</b></div>' +
        '<div>Power: <b style="color:var(--accent)">' + p.power_pct + '%</b></div>' +
        '<div style="font-size:10px;color:var(--text-dim);margin-top:4px">Saved ' + escHtml(date) + auto + '</div>';

    if (chk) chk.checked = !!p.apply_on_startup;

    // Reflect saved values on sliders
    var coreSlider = document.getElementById('oc-core-slider');
    var memSlider = document.getElementById('oc-mem-slider');
    var powerSlider = document.getElementById('oc-power-slider');
    if (coreSlider) { coreSlider.value = p.core_offset_mhz; }
    if (memSlider) { memSlider.value = p.mem_offset_mhz; }
    if (powerSlider) { powerSlider.value = p.power_pct; }
    onOcSliderChange();
}

function onOcSliderChange() {
    var core = parseInt(document.getElementById('oc-core-slider').value);
    var mem = parseInt(document.getElementById('oc-mem-slider').value);
    var power = parseInt(document.getElementById('oc-power-slider').value);
    document.getElementById('oc-core-val').textContent = (core >= 0 ? '+' : '') + core + ' MHz';
    document.getElementById('oc-mem-val').textContent = (mem >= 0 ? '+' : '') + mem + ' MHz';
    document.getElementById('oc-power-val').textContent = power + '%';
}

async function applyOcSliders() {
    // Debounce: ignore extra clicks while a request is in flight.
    if (_ocState.applyInFlight) {
        termWrite('gpu-terminal', '⏳ Apply already in progress — please wait...');
        return;
    }
    var core = parseInt(document.getElementById('oc-core-slider').value);
    var mem = parseInt(document.getElementById('oc-mem-slider').value);
    var power = parseInt(document.getElementById('oc-power-slider').value);

    _ocState.applyInFlight = true;
    var applyBtns = document.querySelectorAll('.btn-apply-oc, [data-oc-apply]');
    applyBtns.forEach(function(b) { b.classList.add('loading'); b.disabled = true; });
    termWrite('gpu-terminal', 'Applying OC: core+' + core + ', mem+' + mem + ', power ' + power + '%...');

    try {
        var r = await apiPost('/api/gpu/oc/apply', {
            core_offset_mhz: core,
            mem_offset_mhz: mem,
            power_pct: power,
        });
        (r.steps || []).forEach(function(s) {
            termWrite('gpu-terminal', '  ' + (s.ok !== false ? '✓' : '✗') + ' ' + s.name);
        });
        if (!r.ok) {
            termWrite('gpu-terminal', '✗ ' + (r.err || 'OC apply failed'), 'error');
        } else {
            termWrite('gpu-terminal', '=== OC applied — auto-verifying that the GPU accepted it... ===');
            await new Promise(function(res) { setTimeout(res, 1500); });
            await verifyAppliedOc();
        }
        pollOcLiveOnce();
    } finally {
        _ocState.applyInFlight = false;
        applyBtns.forEach(function(b) { b.classList.remove('loading'); b.disabled = false; });
    }
}

async function resetOc() {
    termWrite('gpu-terminal', 'Resetting GPU to stock...');
    var r = await apiPost('/api/gpu/oc/reset');
    (r.steps || []).forEach(function(s) {
        termWrite('gpu-terminal', '  ' + (s.ok !== false ? '✓' : '✗') + ' ' + s.name);
    });
    termWrite('gpu-terminal', '=== Reset to stock complete ===');
    // Reset sliders
    var cs = document.getElementById('oc-core-slider'); if (cs) cs.value = 0;
    var ms = document.getElementById('oc-mem-slider'); if (ms) ms.value = 0;
    var ps = document.getElementById('oc-power-slider'); if (ps) ps.value = 100;
    onOcSliderChange();
    pollOcLiveOnce();
}

async function saveCurrentOcProfile() {
    var core = parseInt(document.getElementById('oc-core-slider').value);
    var mem = parseInt(document.getElementById('oc-mem-slider').value);
    var power = parseInt(document.getElementById('oc-power-slider').value);
    var onStartup = document.getElementById('oc-apply-on-startup').checked;
    var r = await apiPost('/api/gpu/oc/profile', {
        core_offset_mhz: core,
        mem_offset_mhz: mem,
        power_pct: power,
        apply_on_startup: onStartup,
    });
    if (r.ok) {
        termWrite('gpu-terminal', '✓ Profile saved: core+' + core + ', mem+' + mem + ', power ' + power + '%');
        loadOcProfile();
    } else {
        termWrite('gpu-terminal', '✗ Save failed: ' + (r.err || 'unknown'), 'error');
    }
}

// ═══ Hard GPU Temp Ceiling ═══
var _tempCeilingPollTimer = null;
var _tempCeilingDirtyTimer = null;

async function loadTempCeilingStatus() {
    var data = await apiGet('/api/gpu/temp-ceiling/status');
    if (!data) return;
    var s = data.settings || {};
    // Settings → controls (only if user isn't actively dragging)
    var slider1 = document.getElementById('temp-ceiling-slider');
    var slider2 = document.getElementById('temp-sustained-slider');
    var actSel  = document.getElementById('temp-ceiling-action');
    var tog     = document.getElementById('toggle-temp-ceiling');
    if (slider1 && document.activeElement !== slider1) slider1.value = s.ceiling_c || 83;
    if (slider2 && document.activeElement !== slider2) slider2.value = s.sustained_seconds || 5;
    if (actSel  && document.activeElement !== actSel)  actSel.value  = s.action || 'reset';
    if (tog) {
        if (s.enabled) tog.classList.add('on'); else tog.classList.remove('on');
    }
    var cv = document.getElementById('temp-ceiling-val');
    var sv = document.getElementById('temp-sustained-val');
    if (cv) cv.textContent = (s.ceiling_c || 83) + ' °C';
    if (sv) sv.textContent = (s.sustained_seconds || 5) + ' s';

    // Live status
    var cur = document.getElementById('temp-ceiling-current');
    var pill = document.getElementById('temp-ceiling-status-pill');
    var sus  = document.getElementById('temp-ceiling-sustained');
    var t = data.current_temp_c;
    if (cur) cur.textContent = (t == null ? '—' : t + '°C');
    if (pill) {
        if (!data.running) {
            pill.className = 'status-badge neutral'; pill.textContent = 'Watchdog off';
        } else if (data.cooldown_remaining_s != null) {
            pill.className = 'status-badge warn'; pill.textContent = 'Cooldown ' + data.cooldown_remaining_s + 's';
        } else if (data.above_ceiling_for_s != null) {
            pill.className = 'status-badge danger'; pill.textContent = 'Above ceiling';
        } else if (t != null && t >= (s.ceiling_c || 83) - 5) {
            pill.className = 'status-badge warn'; pill.textContent = 'Approaching';
        } else {
            pill.className = 'status-badge ok'; pill.textContent = 'Healthy';
        }
    }
    if (sus) {
        if (data.above_ceiling_for_s != null) {
            sus.textContent = '↑ above ceiling for ' + data.above_ceiling_for_s + 's '
                + '(trips this session: ' + (data.tripped_count || 0) + ')';
        } else if (data.tripped_count) {
            sus.textContent = 'Trips this session: ' + data.tripped_count;
        } else {
            sus.textContent = '';
        }
    }
}

function _scheduleTempCeilingSave() {
    // Debounce — wait 400 ms after the last drag tick before POSTing.
    if (_tempCeilingDirtyTimer) clearTimeout(_tempCeilingDirtyTimer);
    _tempCeilingDirtyTimer = setTimeout(async function() {
        var body = {
            ceiling_c:         parseInt(document.getElementById('temp-ceiling-slider').value, 10),
            sustained_seconds: parseInt(document.getElementById('temp-sustained-slider').value, 10),
            action:            document.getElementById('temp-ceiling-action').value,
        };
        await apiPost('/api/gpu/temp-ceiling/settings', body);
    }, 400);
}

function onTempCeilingSliderChange() {
    var c = document.getElementById('temp-ceiling-slider').value;
    var s = document.getElementById('temp-sustained-slider').value;
    document.getElementById('temp-ceiling-val').textContent  = c + ' °C';
    document.getElementById('temp-sustained-val').textContent = s + ' s';
    _scheduleTempCeilingSave();
}

function onTempCeilingActionChange() {
    _scheduleTempCeilingSave();
}

async function toggleTempCeiling(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    await apiPost('/api/gpu/temp-ceiling/settings', { enabled: newOn });
    if (newOn) await apiPost('/api/gpu/temp-ceiling/start');
    else       await apiPost('/api/gpu/temp-ceiling/stop');
    loadTempCeilingStatus();
}

async function testTempCeilingTrip() {
    var r = await apiPost('/api/gpu/temp-ceiling/test');
    if (r && r.ok) {
        termWrite('gpu-terminal',
            '✓ Test trip fired — check the bottom-right corner for the toast notification.');
    }
    loadTempCeilingStatus();
}

async function loadTempCeilingTrips() {
    var data = await apiGet('/api/gpu/temp-ceiling/trips');
    var c = document.getElementById('temp-ceiling-trips');
    if (!c) return;
    var trips = (data && data.trips) || [];
    if (trips.length === 0) {
        c.style.display = 'block';
        c.innerHTML = '<div class="empty-state">No trip events recorded.</div>';
        return;
    }
    var html = '<div style="font-weight:600;color:var(--text-bright);margin-bottom:8px">Recent trips:</div>';
    trips.slice().reverse().forEach(function(t) {
        var d = new Date(t.ts * 1000);
        html += '<div style="padding:6px 0;border-bottom:1px solid var(--border-faint)">'
              + '<span style="color:var(--text-tertiary);font-family:var(--font-mono);font-size:11px">'
              + d.toLocaleString() + '</span> · '
              + '<span style="color:var(--danger);font-weight:600">' + t.temp_c + '°C</span> · '
              + 'ceiling ' + t.ceiling_c + '°C · '
              + 'action: ' + t.action
              + (t.reset_ok === false ? ' <span style="color:var(--danger)">(reset failed)</span>' : '')
              + '</div>';
    });
    c.style.display = 'block';
    c.innerHTML = html;
}

async function clearTempCeilingTrips() {
    if (!confirm('Clear the trip log?')) return;
    await apiDelete('/api/gpu/temp-ceiling/trips');
    loadTempCeilingTrips();
}

function _startTempCeilingPoll() {
    if (_tempCeilingPollTimer) return;
    loadTempCeilingStatus();
    // v3.2.3 — was 2 s.  Bump to 5 s — temp ceiling doesn't change often
    // and the UI is a status indicator, not a chart.
    _tempCeilingPollTimer = setInterval(function() {
        if (_pollSkipIfHidden()) return;
        loadTempCeilingStatus();
    }, 5000);
}

function _stopTempCeilingPoll() {
    if (_tempCeilingPollTimer) {
        clearInterval(_tempCeilingPollTimer);
        _tempCeilingPollTimer = null;
    }
}

// ═══ Crash Recovery ═══
async function loadCrashRecoveryHistory() {
    var data = await apiGet('/api/gpu/crash-recovery/history');
    var history = (data && data.history) || [];
    var c = document.getElementById('crash-recovery-history');
    var countEl = document.getElementById('crash-recovery-count');
    var lastEl = document.getElementById('crash-recovery-last');
    if (countEl) countEl.textContent = history.length;
    if (history.length === 0) {
        if (lastEl) lastEl.textContent = 'No crashes recorded — nice.';
        if (c) c.innerHTML = '<div class="empty-state">No crashes recorded yet. The blacklist is empty too.</div>';
        return;
    }
    var last = history[history.length - 1];
    if (lastEl) lastEl.textContent = 'Last: ' + new Date(last.ts * 1000).toLocaleString();
    var html = '';
    history.slice().reverse().forEach(function(h) {
        var d = new Date(h.ts * 1000);
        var sd = h.step_down
            ? ' → core+' + h.step_down.core + ' / mem+' + h.step_down.mem
            : '';
        var bl = h.blacklisted
            ? '<span class="status-badge danger" style="margin-left:6px">blacklisted</span>'
            : '<span class="status-badge neutral" style="margin-left:6px">no OC</span>';
        html +=
          '<div style="padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-elevated);margin-bottom:6px">' +
            '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
              '<span style="font-weight:600;color:var(--text-bright)">' + escHtml(h.display_name || h.exe || 'unknown game') + '</span>' +
              '<span class="status-badge warn">' + escHtml(h.kind) + '</span>' +
              bl +
              '<span style="margin-left:auto;color:var(--text-tertiary);font-family:var(--font-mono);font-size:11px">' + d.toLocaleString() + '</span>' +
            '</div>' +
            '<div style="margin-top:6px;font-family:var(--font-mono);color:var(--text-secondary);font-size:12px">' +
              'OC at crash: <span style="color:var(--danger)">core+' + h.active_core + ' / mem+' + h.active_mem + '</span>' +
              '<span style="color:var(--accent)">' + sd + '</span>' +
            '</div>' +
            (h.tdr_count > 0 || h.crash_count > 0
              ? '<div style="margin-top:4px;font-size:11px;color:var(--text-tertiary)">' +
                'tdr=' + (h.tdr_count || 0) + ' · driver_crash=' + (h.crash_count || 0) + '</div>'
              : '') +
          '</div>';
    });
    if (c) c.innerHTML = html;
}

async function clearCrashRecoveryHistory() {
    if (!confirm('Clear the crash history? This does NOT clear the blacklist — failed offsets stay banned.')) return;
    await apiDelete('/api/gpu/crash-recovery/history');
    loadCrashRecoveryHistory();
}

async function clearCrashRecoveryBlacklist() {
    if (!confirm('Clear the per-game crash blacklist for ALL games?\n\nVispora will stop avoiding offsets that previously crashed.')) return;
    await apiDelete('/api/gpu/crash-recovery/blacklist');
    termWrite('gpu-terminal', '✓ Crash blacklist cleared.');
    loadCrashRecoveryHistory();
}

async function testCrashRecovery() {
    if (!confirm('Fire a TEST crash-recovery event?\n\nThis writes a fake entry to the blacklist + history and fires the toast notification — but does NOT actually step down your saved profile.')) return;
    // Use core+1/mem+1 sentinel values so the test doesn't touch real blacklist entries.
    var r = await apiPost('/api/gpu/crash-recovery/test',
        { exe: '__test__.exe', display_name: 'Vispora Test', core: 1, mem: 1 });
    if (r && r.crashed) {
        termWrite('gpu-terminal',
            '✓ Test crash-recovery event fired (kind=' + r.kind + '). Toast should appear within 1-2s.');
    } else {
        termWrite('gpu-terminal',
            '⚠ Test ran but no crash signal was found in the event log. ' +
            'On a real crash you would see TDR events here — that means the toast / blacklist / step-down logic is working but there was nothing to react to.');
    }
    loadCrashRecoveryHistory();
}

async function loadAndApplyProfile() {
    if (!_ocState.profile || !_ocState.profile.exists) {
        termWrite('gpu-terminal', '✗ No saved profile to apply');
        return;
    }
    var p = _ocState.profile;
    var cs = document.getElementById('oc-core-slider');
    var ms = document.getElementById('oc-mem-slider');
    var ps = document.getElementById('oc-power-slider');
    if (cs) cs.value = p.core_offset_mhz;
    if (ms) ms.value = p.mem_offset_mhz;
    if (ps) ps.value = p.power_pct;
    onOcSliderChange();
    await applyOcSliders();
}

async function verifyAppliedOc() {
    var core = parseInt(document.getElementById('oc-core-slider').value);
    var mem = parseInt(document.getElementById('oc-mem-slider').value);
    var power = parseInt(document.getElementById('oc-power-slider').value);
    var resultEl = document.getElementById('oc-verify-result');
    resultEl.innerHTML = '<span class="pulse" style="color:var(--text-dim)">Reading back actual clocks...</span>';

    // v2.9.3 — also fetch the conflict scan in parallel so we can render
    // a clear "Afterburner is running" warning if applicable.
    var conflictsPromise = apiGet('/api/gpu/oc/conflicts');

    var v = await apiGet('/api/gpu/oc/verify?core_offset=' + core + '&mem_offset=' + mem + '&power_pct=' + power);
    if (!v || !v.ok) {
        resultEl.innerHTML = '<span style="color:var(--red)">✗ ' + escHtml((v && v.err) || 'Verify failed') + '</span>';
        return;
    }

    // Defensive: backend may short-circuit and return a sparse object.
    var core_ = v.core || {}, mem_ = v.mem || {}, power_ = v.power || {};
    var warnings = Array.isArray(v.warnings) ? v.warnings : [];

    var rowHtml = function(label, ok, expected, actual, msg) {
        var color = ok ? 'var(--accent)' : 'var(--red)';
        var icon = ok ? '✓' : '✗';
        return '<div style="padding:3px 0;color:' + color + '">' + icon + ' <b>' + label + ':</b> requested ' + expected + ', GPU reports ' + actual + ' — ' + escHtml(msg || '') + '</div>';
    };

    // v2.9.3 — render a prominent banner if other OC tools are running.
    // This is the #1 cause of "NVAPI returned OK but clocks didn't move"
    // and the user needs to see it BEFORE the per-field rows.
    var conflictBanner = '';
    try {
        var c = await conflictsPromise;
        if (c && c.conflict_likely && c.tools && c.tools.length) {
            var names = c.tools.map(function(t){ return escHtml(t.name); }).join(', ');
            conflictBanner =
                '<div style="margin-bottom:8px;padding:8px 10px;border:1px solid var(--orange);' +
                'border-radius:4px;background:var(--orange-dim);font-size:11px;color:var(--orange)">' +
                '<b>⚠ Conflicting OC tool detected:</b> ' + names + '<br>' +
                '<span style="color:var(--text-dim);font-size:10px">' +
                'Close it (and disable its "Apply at Windows startup" option) before ' +
                'applying OC in Vispora — it re-asserts its own offsets every few ' +
                'seconds and silently overrides our writes.</span></div>';
        }
    } catch (e) { /* silent */ }

    var html = '<div style="padding:8px;background:var(--bg-void);border:1px solid var(--border);border-radius:4px">';
    html += conflictBanner;
    html += '<div style="font-size:11px;font-weight:600;margin-bottom:6px;color:' + (v.verified ? 'var(--accent)' : 'var(--red)') + '">';
    html += v.verified ? '✓ All values verified — OC is actively applied to the GPU' : '⚠ Some values did not stick — see below';
    html += '</div>';
    html += rowHtml('Core',  core_.ok,
        '+' + (core_.requested_offset || 0) + ' MHz (target ' + (core_.expected_max || '?') + ')',
        (core_.actual_max != null ? core_.actual_max : '?') + ' MHz',  core_.msg);
    html += rowHtml('Memory', mem_.ok,
        '+' + (mem_.requested_offset || 0) + ' MHz (target ' + (mem_.expected_max || '?') + ')',
        (mem_.actual_max != null ? mem_.actual_max : '?') + ' MHz',  mem_.msg);
    html += rowHtml('Power', power_.ok,
        (power_.requested_pct || 100) + '% (' + (power_.requested_w || 0) + 'W)',
        (power_.actual_w != null ? power_.actual_w : '?') + 'W',  power_.msg);
    if (warnings.length > 0) {
        html += '<div style="margin-top:8px;padding-top:6px;border-top:1px dashed var(--border);color:var(--orange);font-size:10px">';
        warnings.forEach(function(w) { html += '⚠ ' + escHtml(w) + '<br>'; });
        html += '</div>';
    }
    html += '</div>';
    resultEl.innerHTML = html;
}

// ─── Benchmark: Stock vs OC ─────────────────────────────────────
async function runBenchmarkComparison() {
    if (_ocState.stressActive || _ocState.autoActive || _ocState.benchmarkActive) {
        showErrorToast('Another GPU test is running. Wait for it to finish.');
        return;
    }
    if (!_ocState.capability || _ocState.capability.vendor !== 'nvidia') {
        showErrorToast('Benchmark requires an NVIDIA GPU.');
        return;
    }

    // Use current slider values as the "OC" config (fallback to saved profile if all zero)
    var core = parseInt(document.getElementById('oc-core-slider').value);
    var mem = parseInt(document.getElementById('oc-mem-slider').value);
    var power = parseInt(document.getElementById('oc-power-slider').value);
    var oc = (core === 0 && mem === 0 && power === 100) ? null : {
        core_offset_mhz: core, mem_offset_mhz: mem, power_pct: power,
    };

    if (!oc && (!_ocState.profile || !_ocState.profile.exists)) {
        showErrorToast('Set an OC via sliders or save a profile before running the benchmark.');
        return;
    }

    if (!confirm('Benchmark will:\n• Reset GPU to stock and measure 30s baseline\n• Apply OC and measure 30s comparison\n• Show FPS / temp / clock deltas\n\nTakes ~70 seconds. Continue?')) return;

    var benchEl = document.getElementById('oc-benchmark-card');
    var contentEl = document.getElementById('oc-benchmark-content');
    benchEl.style.display = 'block';
    contentEl.innerHTML = '<div class="pulse" style="color:var(--text-dim)">▶ Resetting to stock for baseline measurement...</div>';

    termWrite('gpu-terminal', '📊 Benchmark started — stock baseline first');

    // v2.9.5 — gate the stability-step loop with our new benchmark flag so
    // _runAutoStabilityStep() actually iterates instead of bailing on tick 1.
    _ocState.benchmarkActive = true;
    try {
        var startResp = await apiPost('/api/gpu/oc/benchmark/start', { oc: oc });
        if (!startResp.ok) {
            contentEl.innerHTML = '<div style="color:var(--red)">✗ ' + escHtml(startResp.err || 'Benchmark failed to start') + '</div>';
            return;
        }

        // Phase 1: Stock measurement
        contentEl.innerHTML = '<div class="pulse" style="color:var(--accent)">▶ Measuring STOCK performance for ' + startResp.duration_s + 's...</div>';
        var stockVerdict = await _runAutoStabilityStep(startResp.duration_s);
        termWrite('gpu-terminal', '  Stock: ' + (stockVerdict.avg_fps || 0) + ' FPS, max temp ' + (stockVerdict.max_temp_c || 0) + '°C');

        // Submit verdict; backend applies OC and tells us to do phase 2
        var phase2 = await apiPost('/api/gpu/oc/benchmark/record', stockVerdict);
        if (!phase2.ok) {
            contentEl.innerHTML = '<div style="color:var(--red)">✗ ' + escHtml(phase2.err || 'Phase transition failed') + '</div>';
            return;
        }

        // Phase 2: OC measurement
        contentEl.innerHTML = '<div class="pulse" style="color:var(--accent)">▶ Stock: ' + (stockVerdict.avg_fps || 0) + ' FPS — applying OC, measuring ' + phase2.duration_s + 's...</div>';
        var ocVerdict = await _runAutoStabilityStep(phase2.duration_s);
        termWrite('gpu-terminal', '  OC: ' + (ocVerdict.avg_fps || 0) + ' FPS, max temp ' + (ocVerdict.max_temp_c || 0) + '°C');

        var finalResp = await apiPost('/api/gpu/oc/benchmark/record', ocVerdict);
        if (!finalResp.ok || !finalResp.done) {
            contentEl.innerHTML = '<div style="color:var(--red)">✗ Benchmark did not complete</div>';
            return;
        }

        // Render comparison
        _renderBenchmarkComparison(contentEl, finalResp);
        var d = finalResp.delta || {};
        termWrite('gpu-terminal', '═══ Benchmark complete: Δ ' + ((d.fps_delta || 0) >= 0 ? '+' : '') + (d.fps_delta || 0) + ' FPS (' + ((d.fps_pct || 0) >= 0 ? '+' : '') + (d.fps_pct || 0) + '%) ═══');
    } finally {
        _ocState.benchmarkActive = false;
    }
}

function _renderBenchmarkComparison(el, result) {
    var stock = result.stock || {};
    var oc = result.oc || {};
    var delta = result.delta || {};
    var applied = result.applied_oc || {};

    var statRow = function(label, stockVal, ocVal, deltaVal, unit, betterIsHigher) {
        var dColor = 'var(--text-dim)';
        var dIcon = '';
        if (deltaVal !== undefined && deltaVal !== null) {
            var isBetter = betterIsHigher ? deltaVal > 0 : deltaVal < 0;
            var isWorse = betterIsHigher ? deltaVal < 0 : deltaVal > 0;
            if (isBetter) { dColor = 'var(--accent)'; dIcon = '▲ '; }
            else if (isWorse) { dColor = 'var(--red)'; dIcon = '▼ '; }
        }
        return '<tr>' +
            '<td style="padding:6px 12px;color:var(--text-dim)">' + label + '</td>' +
            '<td style="padding:6px 12px;font-family:monospace">' + stockVal + (unit||'') + '</td>' +
            '<td style="padding:6px 12px;font-family:monospace">' + ocVal + (unit||'') + '</td>' +
            '<td style="padding:6px 12px;font-family:monospace;color:' + dColor + '">' + (deltaVal !== undefined ? dIcon + (deltaVal >= 0 ? '+' : '') + deltaVal + (unit||'') : '—') + '</td>' +
        '</tr>';
    };

    var html = '<div style="margin-bottom:10px;font-size:12px">';
    html += '<b style="color:var(--accent)">Applied OC:</b> core +' + applied.core_offset_mhz + ' MHz, memory +' + applied.mem_offset_mhz + ' MHz, power ' + applied.power_pct + '%';
    html += '</div>';

    var fpsPct = delta.fps_pct || 0;
    var summaryColor = fpsPct >= 5 ? 'var(--accent)' : (fpsPct >= 0 ? 'var(--accent2)' : 'var(--red)');
    html += '<div style="font-size:18px;font-weight:600;margin-bottom:14px;color:' + summaryColor + '">';
    html += (fpsPct >= 0 ? '+' : '') + fpsPct + '% FPS gain';
    if (fpsPct >= 5) html += ' 🏆';
    else if (fpsPct >= 0) html += ' ✓';
    else html += ' (regression)';
    html += '</div>';

    html += '<table style="width:100%;border-collapse:collapse;font-size:11px">';
    html += '<thead><tr style="border-bottom:1px solid var(--border);color:var(--text-dim)">' +
        '<th style="padding:6px 12px;text-align:left">Metric</th>' +
        '<th style="padding:6px 12px;text-align:left">Stock</th>' +
        '<th style="padding:6px 12px;text-align:left">OC</th>' +
        '<th style="padding:6px 12px;text-align:left">Δ</th>' +
        '</tr></thead><tbody>';
    html += statRow('Avg FPS', stock.avg_fps, oc.avg_fps, delta.fps_delta, '', true);
    html += statRow('Avg frame time', stock.avg_frame_time_ms, oc.avg_frame_time_ms, delta.frame_time_delta_ms, ' ms', false);
    html += statRow('1% low frame time (p99)', stock.p99_frame_time_ms, oc.p99_frame_time_ms, delta.p99_delta_ms, ' ms', false);
    html += statRow('Frame variance', stock.frame_variance_pct, oc.frame_variance_pct, undefined, '%');
    html += statRow('Max core clock', stock.max_core_mhz || 0, oc.max_core_mhz || 0, delta.core_delta_mhz, ' MHz', true);
    html += statRow('Max memory clock',
        stock.max_mem_mhz || '—',
        oc.max_mem_mhz || '—',
        (stock.max_mem_mhz && oc.max_mem_mhz) ? (oc.max_mem_mhz - stock.max_mem_mhz) : undefined,
        ' MHz', true);
    html += statRow('Max temp', stock.max_temp_c, oc.max_temp_c, delta.temp_delta_c, ' °C', false);
    html += statRow('Stable', stock.stable ? '✓' : '✗', oc.stable ? '✓' : '✗', undefined, '');
    html += '</tbody></table>';

    if (!oc.stable) {
        html += '<div style="margin-top:10px;padding:8px;background:var(--red-dim);border-left:3px solid var(--red);font-size:11px;color:var(--red)">⚠ OC was UNSTABLE during benchmark: ' + escHtml(oc.abort_reason || 'Unknown failure') + '</div>';
    }

    el.innerHTML = html;
}

async function deleteOcProfile() {
    if (!confirm('Delete saved OC profile and reset GPU to stock?')) return;
    var r = await fetch('/api/gpu/oc/profile', { method: 'DELETE' }).then(function(x) { return x.json(); });
    if (r.ok) {
        termWrite('gpu-terminal', '✓ Profile deleted, GPU reset to stock');
        loadOcProfile();
        var cs = document.getElementById('oc-core-slider'); if (cs) cs.value = 0;
        var ms = document.getElementById('oc-mem-slider'); if (ms) ms.value = 0;
        var ps = document.getElementById('oc-power-slider'); if (ps) ps.value = 100;
        onOcSliderChange();
        pollOcLiveOnce();
    }
}

// ─── Live monitoring ───
async function pollOcLiveOnce() {
    var s = await apiGet('/api/gpu/oc/live');
    if (!s.ok) return;
    var setText = function(id, val, warn) {
        var el = document.getElementById(id);
        if (!el) return;
        el.textContent = val;
        if (warn !== undefined) el.style.color = warn ? 'var(--red)' : '';
    };
    setText('oc-live-core', s.core_mhz);
    setText('oc-live-mem', s.mem_mhz);
    setText('oc-live-temp', s.temp_c, s.temp_c >= 80);
    setText('oc-live-power', Math.round(s.power_w));
    var sub = document.getElementById('oc-live-power-sub');
    if (sub) sub.textContent = 'W / ' + Math.round(s.power_limit_w) + 'W limit';
    setText('oc-live-util', s.gpu_util_pct);
    setText('oc-live-fan', s.fan_pct);
}

function toggleOcLive() {
    var lbl = document.getElementById('oc-live-toggle-label');
    if (_ocState.liveActive) {
        stopOcLive();
        if (lbl) lbl.textContent = 'Start Live Monitor';
    } else {
        _ocState.liveActive = true;
        pollOcLiveOnce();
        _ocState.liveTimer = setInterval(pollOcLiveOnce, 1000);
        if (lbl) lbl.textContent = 'Stop Live Monitor';
    }
}

function stopOcLive() {
    if (_ocState.liveTimer) {
        clearInterval(_ocState.liveTimer);
        _ocState.liveTimer = null;
    }
    _ocState.liveActive = false;
}

// ─── WebGL stress test engine v2 ───
// Heavy fragment shader (mandelbrot + noise) that pegs the GPU to 95-100%.
// PLUS a deterministic checksum pattern in the bottom-left 4x4 pixel block —
// JS reads those pixels back periodically and verifies against expected values.
// Mismatch = visual artifacts = unstable OC.
//
// Robustness signals exposed:
//   - state.frameTimes[]    — rolling array of recent frame durations (ms)
//   - state.contextLost     — set true on webglcontextlost event
//   - state.pixelCheckFailed — set true when readback doesn't match expected
var OC_STRESS_VERT = [
    'attribute vec2 a_pos;',
    'void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }',
].join('\n');

// v2.9.9.2 — fragment shader rewritten to MATCH the stress-test.py reference.
// The user's reference benchmark uses an 18-iteration rotating-noise loop +
// 14-iteration fractal-fold loop with hash-based smoothNoise.  This is far
// heavier than the old mandelbrot path and produces the same FPS curve the
// reference script measures, so our scores are directly comparable.
//
// We keep the bottom-left 4x4 deterministic checksum block on top so the
// pixel-correctness GPU-crash detector still works.
var OC_STRESS_FRAG = [
    'precision highp float;',
    'uniform float u_time;',
    'uniform vec2 u_res;',
    '',
    'float hash(vec2 p) {',
    '  p = fract(p * vec2(123.34, 456.21));',
    '  p += dot(p, p + 45.32);',
    '  return fract(p.x * p.y);',
    '}',
    '',
    'float noise(vec2 p) {',
    '  vec2 i = floor(p);',
    '  vec2 f = fract(p);',
    '  f = f * f * (3.0 - 2.0 * f);',
    '  float a = hash(i);',
    '  float b = hash(i + vec2(1.0, 0.0));',
    '  float c = hash(i + vec2(0.0, 1.0));',
    '  float d = hash(i + vec2(1.0, 1.0));',
    '  return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);',
    '}',
    '',
    'mat2 rot(float a) {',
    '  float s = sin(a), c = cos(a);',
    '  return mat2(c, -s, s, c);',
    '}',
    '',
    'void main() {',
    // ── 4x4 checksum block — KEEP, used for pixel-correctness GPU-crash detector
    '  if (gl_FragCoord.x < 4.0 && gl_FragCoord.y < 4.0) {',
    '    float r = 0.5 + 0.5 * sin(u_time * 1.7 + gl_FragCoord.x * 0.3);',
    '    float g = 0.5 + 0.5 * cos(u_time * 1.3 + gl_FragCoord.y * 0.5);',
    '    float b = 0.5 + 0.5 * sin(u_time * 0.9 + (gl_FragCoord.x + gl_FragCoord.y) * 0.2);',
    '    gl_FragColor = vec4(r, g, b, 1.0);',
    '    return;',
    '  }',
    '',
    // ── stress-test.py main shader (verbatim port of the heavy ray-march/fractal-fold) ──
    '  vec2 vUV = gl_FragCoord.xy / u_res;',
    '  vec2 uv = (vUV * 2.0 - 1.0);',
    '  uv.x *= u_res.x / u_res.y;',
    '',
    '  vec3 col = vec3(0.0);',
    '  vec2 p = uv;',
    '',
    // 18-iter rotating noise loop — main GPU stress
    '  for (int i = 0; i < 18; i++) {',
    '    float fi = float(i);',
    '    p *= rot(0.13 + 0.025 * fi + u_time * 0.02);',
    '    p += 0.08 * vec2(',
    '      sin(u_time * (0.7 + fi * 0.03) + p.y * 3.0),',
    '      cos(u_time * (0.6 + fi * 0.02) + p.x * 3.0)',
    '    );',
    '    float l = length(p);',
    '    float n = noise(p * 3.0 + fi * 0.17 + u_time * 0.1);',
    '    col += vec3(',
    '      0.12 + 0.88 * abs(sin(l * 3.0 + u_time + n)),',
    '      0.10 + 0.90 * abs(sin(l * 2.5 + u_time * 1.3 + n * 2.0)),',
    '      0.08 + 0.92 * abs(sin(l * 4.0 + u_time * 0.7 + n * 3.0))',
    '    ) * (0.06 + 0.04 * n);',
    '    p = fract(p * 1.8) - 0.5;',
    '  }',
    '',
    // 14-iter fractal-fold loop — additional GPU work
    '  float v = 0.0;',
    '  vec2 q = uv;',
    '  for (int j = 0; j < 14; j++) {',
    '    float fj = float(j) + 1.0;',
    '    q = abs(q) / dot(q, q + 0.7) - 0.6;',
    '    v += abs(sin(q.x * fj + q.y * 1.7 + u_time * 0.9));',
    '  }',
    '',
    '  col += vec3(v * 0.04);',
    '  col = pow(col, vec3(0.85));',
    '  gl_FragColor = vec4(col, 1.0);',
    '}',
].join('\n');

function _compileShader(gl, type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        console.error('Shader compile:', gl.getShaderInfoLog(s));
        gl.deleteShader(s);
        return null;
    }
    return s;
}

// Compute the EXPECTED checksum pixel value for a given time.
// MUST match the GLSL formula exactly. Returns array of 16 RGBA values (4x4 pixels).
function _expectedChecksumPixels(t) {
    var pixels = [];
    for (var y = 0; y < 4; y++) {
        for (var x = 0; x < 4; x++) {
            // Note: gl_FragCoord is at pixel CENTER (x+0.5, y+0.5)
            var fx = x + 0.5;
            var fy = y + 0.5;
            var r = 0.5 + 0.5 * Math.sin(t * 1.7 + fx * 0.3);
            var g = 0.5 + 0.5 * Math.cos(t * 1.3 + fy * 0.5);
            var b = 0.5 + 0.5 * Math.sin(t * 0.9 + (fx + fy) * 0.2);
            pixels.push(Math.round(r * 255), Math.round(g * 255), Math.round(b * 255), 255);
        }
    }
    return pixels;
}

function startWebGLStress() {
    var canvas = document.getElementById('oc-stress-canvas');
    if (!canvas) return null;
    var gl = canvas.getContext('webgl', { powerPreference: 'high-performance', antialias: false, preserveDrawingBuffer: true });
    if (!gl) {
        termWrite('gpu-terminal', '✗ WebGL not available — stress test cannot run');
        return null;
    }
    var vs = _compileShader(gl, gl.VERTEX_SHADER, OC_STRESS_VERT);
    var fs = _compileShader(gl, gl.FRAGMENT_SHADER, OC_STRESS_FRAG);
    if (!vs || !fs) return null;

    var program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        console.error('Program link:', gl.getProgramInfoLog(program));
        return null;
    }
    gl.useProgram(program);

    // Fullscreen triangle
    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 3,-1, -1,3]), gl.STATIC_DRAW);
    var posLoc = gl.getAttribLocation(program, 'a_pos');
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

    var uTime = gl.getUniformLocation(program, 'u_time');
    var uRes = gl.getUniformLocation(program, 'u_res');
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(uRes, canvas.width, canvas.height);

    var state = {
        gl: gl, program: program, canvas: canvas, uTime: uTime,
        rafId: 0, frameCount: 0, startTime: performance.now(),
        lastFrameMs: performance.now(),
        frameTimes: [],          // ring buffer of last N per-draw times in ms
        // v2.9.9.3 — bumped from 600 to 5000.  With BATCH_SIZE=32 draws per
        // RAF on a fast GPU we generate up to ~960 entries/second; 600 was
        // only ~0.6 s of history, which made the 1%-low metric unreliable.
        // 5000 entries gives us 5–25 s of history depending on GPU speed.
        frameTimesMax: 5000,
        contextLost: false,
        pixelCheckFailed: false,
        pixelCheckCount: 0,
        pixelCheckBadCount: 0,
        currentTime: 0,          // last u_time sent to shader (for verification)
    };

    // v2.9.9.2 — context-loss is the EARLIEST crash signal we can see.
    // Hit the backend's emergency-reset before the next probe tick gets a
    // chance to report it through the slow stability-probe pipeline.
    // We use sendBeacon-style fetch (fire-and-forget) because the WebGL
    // event handler may run before/during a display recovery and we don't
    // want to await anything inside it.
    canvas.addEventListener('webglcontextlost', function(ev) {
        ev.preventDefault();
        state.contextLost = true;
        console.warn('WebGL context lost during stress test — triggering emergency reset');
        try {
            // No await — let it complete in the background.  apiPost
            // already swallows network errors so this can't throw.
            apiPost('/api/gpu/oc/emergency-reset',
                    { reason: 'webgl_context_lost' },
                    { timeoutMs: 5000 });
        } catch (e) { /* never block on a crash recovery path */ }
    }, false);

    var pixBuf  = new Uint8Array(64);  // 4x4 RGBA = 64 bytes (pixel-correctness)
    var syncBuf = new Uint8Array(4);   // v2.9.9.4 — single-pixel sync (see frame())

    // v2.9.9.4 — fix the FPS-measurement-doesn't-actually-measure problem
    // we hit in v2.9.9.3.
    //
    //   What broke in v2.9.9.3:
    //     gl.finish() is supposed to block JS until the GPU completes all
    //     pending work.  In Chromium's WebGL backend (which WebView2 uses)
    //     gl.finish() returns IMMEDIATELY because GPU submission is async
    //     across a renderer / GPU-process boundary.  Result: batch_time =
    //     ~0.001 ms and "FPS" reads 3 million.
    //
    //   The reliable sync primitive is gl.readPixels — it MUST round-trip
    //   to the GPU and pull bytes back, which forces an actual sync.  We
    //   read a single pixel into syncBuf right after the batch.  Cost is
    //   trivial (one pixel) but it forces a real GPU/CPU rendezvous.
    //
    //   Plus a 1-second WARMUP discard at the start of each stress so the
    //   first few RAFs (cold GPU, browser still composing) don't pollute
    //   the per-step average.  Without this the first 200 ms of bogus
    //   "infinity FPS" entries wreck the σ and 1%-low numbers.
    var BATCH_SIZE     = 32;        // big enough that one batch >> vsync budget
    var WARMUP_MS      = 1000;      // discard first second of frame samples
    var MIN_PER_DRAW_MS = 0.05;     // anything faster is bogus (sync didn't work)
    var MAX_PER_DRAW_MS = 500;      // anything slower than this is a hung frame

    // Bump the canvas's internal resolution.  CSS still constrains the
    // displayed size, but the GPU renders 1280×1280 worth of fragments
    // per draw — ~6.4× the old 512×512 workload.  This makes the test
    // GPU-bound on cards from a GTX 1660 upward.
    if (canvas.width < 1280)  canvas.width  = 1280;
    if (canvas.height < 1280) canvas.height = 1280;
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(uRes, canvas.width, canvas.height);

    state.warmupStartMs = performance.now();
    state.bogusReadingsCount = 0;

    function frame() {
        if (state.contextLost) return;  // stop on crash

        var batchStartMs = performance.now();
        var t = (batchStartMs - state.startTime) / 1000;
        state.currentTime = t;
        gl.uniform1f(uTime, t);

        // Submit BATCH_SIZE draws — GPU pipelines them all at once.
        for (var i = 0; i < BATCH_SIZE; i++) {
            gl.drawArrays(gl.TRIANGLES, 0, 3);
        }
        // SYNC: read back a single pixel.  Unlike gl.finish() (which
        // Chromium routinely no-ops), readPixels MUST wait for the GPU
        // to complete the work above before it can return real bytes.
        // This is the documented WebGL synchronization primitive.
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, syncBuf);

        var batchEndMs = performance.now();
        var perDrawMs  = (batchEndMs - batchStartMs) / BATCH_SIZE;

        // Drop bogus values — happens when the sync didn't actually sync
        // (impossibly fast) or when something stalled (impossibly slow).
        // The benchmark verdict treats "no samples this step" as a failure.
        if (perDrawMs < MIN_PER_DRAW_MS || perDrawMs > MAX_PER_DRAW_MS) {
            state.bogusReadingsCount++;
            state.rafId = requestAnimationFrame(frame);
            return;
        }

        // Discard everything during the WARMUP_MS window so cold-cache
        // and first-RAF outliers don't pollute the per-step stats.
        if (batchEndMs - state.warmupStartMs >= WARMUP_MS) {
            for (var j = 0; j < BATCH_SIZE; j++) {
                if (state.frameTimes.length >= state.frameTimesMax) state.frameTimes.shift();
                state.frameTimes.push(perDrawMs);
            }
            state.frameCount += BATCH_SIZE;
        }
        state.lastFrameMs = batchEndMs;

        state.rafId = requestAnimationFrame(frame);
    }
    state.rafId = requestAnimationFrame(frame);

    // Pixel correctness verification — runs inside startWebGLStress's closure
    state.runPixelCheck = function() {
        if (state.contextLost) return false;
        try {
            // Sync with GPU and read back the 4x4 checksum block (bottom-left)
            gl.readPixels(0, 0, 4, 4, gl.RGBA, gl.UNSIGNED_BYTE, pixBuf);
            var expected = _expectedChecksumPixels(state.currentTime);
            // Compare with tolerance — interpolation/rounding can cause ±2 RGB
            var totalDiff = 0;
            var badPixels = 0;
            for (var i = 0; i < 64; i += 4) {
                var dr = Math.abs(pixBuf[i] - expected[i]);
                var dg = Math.abs(pixBuf[i+1] - expected[i+1]);
                var db = Math.abs(pixBuf[i+2] - expected[i+2]);
                var pixelDiff = dr + dg + db;
                totalDiff += pixelDiff;
                if (pixelDiff > 30) badPixels++;
            }
            state.pixelCheckCount++;
            // 2+ pixels with large mismatch = artifact
            if (badPixels >= 2 || totalDiff > 200) {
                state.pixelCheckBadCount++;
                state.pixelCheckFailed = true;
                console.warn('OC pixel check FAILED — totalDiff=' + totalDiff + ' badPixels=' + badPixels);
                return false;
            }
            return true;
        } catch (e) {
            console.warn('Pixel check error:', e);
            return true;  // don't abort on infra errors
        }
    };

    // Get rolling frame time stats
    state.getFrameStats = function() {
        if (state.frameTimes.length < 5) return { avg: 0, p99: 0, count: 0 };
        var sum = 0;
        for (var i = 0; i < state.frameTimes.length; i++) sum += state.frameTimes[i];
        var avg = sum / state.frameTimes.length;
        var sorted = state.frameTimes.slice().sort(function(a, b) { return b - a; });
        var p99 = sorted[Math.floor(sorted.length * 0.01)] || sorted[0];
        return { avg: avg, p99: p99, count: state.frameTimes.length };
    };

    return state;
}

function stopWebGLStress(state) {
    if (!state) return;
    if (state.rafId) cancelAnimationFrame(state.rafId);
    // v2.9.9.5 — DO NOT call WEBGL_lose_context.loseContext() here.
    //
    // The old code destroyed the WebGL context every time a step finished.
    // For the FIRST step that was fine — context dies, GPU resources freed.
    // For the SECOND step (and every subsequent one in benchmark mode), the
    // next startWebGLStress() called canvas.getContext('webgl') and got back
    // either null OR a still-lost context.  Either way no draws actually ran,
    // which is why the user saw "fps 0 / 1%low 0 / σ 0%" on Core +25 MHz
    // even though Core +0 MHz showed real numbers.
    //
    // Just stopping the RAF is enough — the GPU has nothing else to do once
    // we stop submitting draws.  The next startWebGLStress() reuses the same
    // (still-alive) context, compiles fresh shaders into it, and runs cleanly.
    // Releasing GL resources is the browser's job at canvas teardown.
}

// ─── Manual stability test ───
async function runStabilityTest(durationSec) {
    if (_ocState.stressActive) {
        termWrite('gpu-terminal', '⚠ Stability test already running');
        return;
    }
    durationSec = durationSec || 60;
    _ocState.stressActive = true;
    _ocState.stressDuration = durationSec;
    _ocState.stressStarted = Date.now();
    _ocState.stressAborted = false;

    document.getElementById('oc-stress-card').style.display = 'block';
    document.getElementById('oc-stress-title').textContent = 'Running Stability Test (' + durationSec + 's)...';
    document.getElementById('oc-stress-status').textContent = 'Starting WebGL stress workload...';
    document.getElementById('oc-stress-elapsed').textContent = '0s';
    document.getElementById('oc-stress-max-temp').textContent = '—°C';
    document.getElementById('oc-stress-cur-core').textContent = '—';
    document.getElementById('oc-stress-fps').textContent = '—';
    document.getElementById('oc-stress-progress').style.width = '0%';

    termWrite('gpu-terminal', '▶ Starting ' + durationSec + 's robust stability test...');

    // Read live state to get current target boost clock for clock-dip detection
    var liveBefore = await apiGet('/api/gpu/oc/live');
    var expectedCoreMax = liveBefore.ok ? (liveBefore.core_max_mhz || 0) : 0;

    await apiPost('/api/gpu/oc/probe/start', { expected_core_max: expectedCoreMax });
    _ocState.stressWebGL = startWebGLStress();

    var maxTemp = 0;
    var lastFrameCount = 0;
    var lastSecond = Date.now();
    var tickCount = 0;

    _ocState.stressPollTimer = setInterval(async function() {
        if (!_ocState.stressActive) return;
        tickCount++;
        var elapsed = (Date.now() - _ocState.stressStarted) / 1000;
        var pct = Math.min(100, (elapsed / durationSec) * 100);
        document.getElementById('oc-stress-elapsed').textContent = Math.round(elapsed) + 's';
        document.getElementById('oc-stress-progress').style.width = pct + '%';

        // Snapshot WebGL state for the probe
        var webglState = _ocState.stressWebGL;
        var contextLost = false;
        var pixelFailed = false;
        var frameStats = { avg: 0, p99: 0, count: 0 };
        var instantFps = 0;

        if (webglState) {
            contextLost = webglState.contextLost;
            // Run pixel correctness check every 2nd second (after warmup)
            if (tickCount >= 3 && tickCount % 2 === 0) {
                var pixelOK = webglState.runPixelCheck();
                pixelFailed = !pixelOK;
            }
            frameStats = webglState.getFrameStats();
            // Calculate FPS for display
            var now = Date.now();
            var dt = (now - lastSecond) / 1000;
            if (dt > 0) {
                instantFps = Math.round((webglState.frameCount - lastFrameCount) / dt);
                document.getElementById('oc-stress-fps').textContent = instantFps;
                lastFrameCount = webglState.frameCount;
                lastSecond = now;
            }
        }

        // Probe GPU state — sends WebGL signals to backend
        var tick = await apiPost('/api/gpu/oc/probe/tick', {
            frame_time_ms: frameStats.avg || null,
            context_lost: contextLost,
            pixel_check_failed: pixelFailed,
        });
        if (tick.ok && tick.state) {
            maxTemp = Math.max(maxTemp, tick.state.temp_c);
            document.getElementById('oc-stress-max-temp').textContent = maxTemp + '°C';
            document.getElementById('oc-stress-cur-core').textContent = tick.state.core_mhz;
            // Status line shows the most-relevant info
            var status = 'Running — ' + tick.state.gpu_util_pct + '% GPU, ' + tick.state.temp_c + '°C, ' + Math.round(frameStats.avg || 0) + 'ms avg frame';
            if (tick.state.temp_c >= 80) {
                status = '⚠ High temp: ' + tick.state.temp_c + '°C — ' + status;
            }
            document.getElementById('oc-stress-status').textContent = status;
        } else if (tick.abort) {
            termWrite('gpu-terminal', '✗ ABORT: ' + tick.reason + ' (kind=' + (tick.kind || '?') + ')', 'error');
            _ocState.stressAborted = true;
            await finishStabilityTest();
            return;
        } else if (tick.hang) {
            termWrite('gpu-terminal', '✗ DRIVER HANG: nvidia-smi stopped responding', 'error');
            _ocState.stressAborted = true;
            await finishStabilityTest();
            return;
        }

        if (elapsed >= durationSec) {
            await finishStabilityTest();
        }
    }, 1000);
}

async function finishStabilityTest() {
    if (!_ocState.stressActive) return;
    _ocState.stressActive = false;
    if (_ocState.stressPollTimer) {
        clearInterval(_ocState.stressPollTimer);
        _ocState.stressPollTimer = null;
    }
    stopWebGLStress(_ocState.stressWebGL);
    _ocState.stressWebGL = null;

    var verdict = await apiPost('/api/gpu/oc/probe/end');
    document.getElementById('oc-stress-card').style.display = 'none';

    if (verdict.ok) {
        var stable = verdict.stable && !_ocState.stressAborted;
        var line = stable
            ? '✓ STABLE — ' + verdict.duration_s + 's | ' + verdict.avg_fps + ' avg FPS | ' + verdict.frame_variance_pct + '% variance | max ' + verdict.max_temp_c + '°C | core ' + verdict.min_core_mhz + '–' + verdict.max_core_mhz + ' MHz'
            : '✗ UNSTABLE — ' + (verdict.abort_reason || 'Test failed');
        termWrite('gpu-terminal', line, stable ? '' : 'error');
        // Detail breakdown
        if (verdict.thermal_throttle) {
            termWrite('gpu-terminal', '  ⚠ Thermal throttle / clock dips detected — GPU is power/temp limited');
        }
        if (verdict.tdr_count > 0) {
            termWrite('gpu-terminal', '  ✗ ' + verdict.tdr_count + ' driver TDR event(s) logged during test', 'error');
        }
        if (verdict.context_lost) {
            termWrite('gpu-terminal', '  ✗ WebGL context lost = GPU driver crash', 'error');
        }
        if (verdict.pixel_artifacts > 0) {
            termWrite('gpu-terminal', '  ✗ ' + verdict.pixel_artifacts + ' frame(s) with visual artifacts (pixel check failed)', 'error');
        }
        if (verdict.frame_variance_pct > 30 && stable) {
            termWrite('gpu-terminal', '  ⚠ High frame time variance (' + verdict.frame_variance_pct + '%) — borderline stability');
        }
    }
    return verdict;
}

function abortStabilityTest() {
    _ocState.stressAborted = true;
    finishStabilityTest();
    termWrite('gpu-terminal', '⏹ Stability test aborted by user');
}

// ─── Auto-OC loop (binary-search + jump-step v2) ───
async function startAutoOc() {
    if (_ocState.autoActive) return;
    if (!_ocState.capability || _ocState.capability.vendor !== 'nvidia') {
        showErrorToast('Auto-OC requires an NVIDIA GPU.');
        return;
    }
    if (!confirm('Auto-OC v3 — Robust Binary Search\n\n• Maxes power limit FIRST (more clock headroom)\n• Jumps +75 MHz core / +250 MHz memory until first crash\n• Binary searches to within 5 MHz core / 15 MHz mem of true ceiling\n• Stability checked via: driver TDR events, WebGL context loss, pixel artifacts, frame variance, clock dips, thermal throttle\n• Final 60s validation pass\n• ~5-8 minutes total\n\nContinue?')) return;

    var coreStep = parseInt(document.getElementById('auto-core-step').value) || 75;
    var memStep = parseInt(document.getElementById('auto-mem-step').value) || 250;
    var maxSteps = parseInt(document.getElementById('auto-max-steps').value) || 14;

    _ocState.autoActive = true;
    _ocState.autoLog = [];
    document.getElementById('auto-oc-start-btn').style.display = 'none';
    document.getElementById('auto-oc-cancel-btn').style.display = '';
    document.getElementById('oc-auto-progress-bar').style.display = 'block';
    document.getElementById('oc-auto-status').innerHTML = '';
    updateAutoOcStatus('Starting binary-search auto-tune...');

    termWrite('gpu-terminal', '🤖 Auto-OC v2 started: core_jump=' + coreStep + 'MHz, mem_jump=' + memStep + 'MHz, max_iters=' + maxSteps);
    await apiPost('/api/gpu/oc/auto/start', {
        core_step_mhz: coreStep,
        mem_step_mhz: memStep,
        max_steps: maxSteps,
    });

    var prev = null;
    var totalIters = maxSteps + 4;  // generous cap

    for (var iter = 0; iter < totalIters; iter++) {
        if (!_ocState.autoActive) break;

        var body = prev ? Object.assign({ reported: true }, prev) : {};
        var nxt = await apiPost('/api/gpu/oc/auto/next', body);
        if (!nxt.ok) {
            termWrite('gpu-terminal', '✗ Auto-OC error: ' + (nxt.err || 'unknown'), 'error');
            break;
        }
        if (nxt.done) {
            // v2.9.9.7 — handle the case where backend ran out of recovery
            // options (crash at +0 or last-stable also crashed).
            if (nxt.aborted) {
                termWrite('gpu-terminal',
                    '🛑 Auto-OC aborted: ' + (nxt.abort_reason || 'GPU could not be recovered'),
                    'error');
                await _showCrashBanner('Auto-tune aborted',
                    nxt.abort_reason || 'GPU crashed and could not be recovered. Offsets reset to stock.');
                updateAutoOcStatus('🛑 Aborted — see crash banner');
                break;
            }
            var b = nxt.best || {};
            var validatedTag = nxt.validated ? ' ✓ validated' : ' (validation failed; backed off)';
            termWrite('gpu-terminal', '🏆 Auto-OC complete: core+' + b.core_offset_mhz + ' MHz, mem+' + b.mem_offset_mhz + ' MHz' + validatedTag + ' — saved as profile');
            updateAutoOcStatus('✓ Auto-tune complete! Saved: core+' + b.core_offset_mhz + ' / mem+' + b.mem_offset_mhz + validatedTag);
            document.getElementById('oc-auto-progress-fill').style.width = '100%';
            loadOcProfile();
            break;
        }

        var phaseShort = nxt.phase.replace(/_/g, ' ').toUpperCase();
        var stepLabel = '[' + phaseShort + '] ' + (nxt.step_label || ('core+' + nxt.current_core + ' mem+' + nxt.current_mem));
        var dur = nxt.recommended_duration_s || 25;

        termWrite('gpu-terminal', '▶ ' + stepLabel + ' — running ' + dur + 's stability check...');
        updateAutoOcStatus(stepLabel);

        // Progress bar — phase-weighted estimate
        var progressPct = _estimateAutoProgress(nxt);
        document.getElementById('oc-auto-progress-fill').style.width = progressPct + '%';

        // Phase-aware stability test
        var verdict = await _runAutoStabilityStep(dur);
        if (!_ocState.autoActive) break;

        prev = {
            stable: verdict.stable && !verdict.aborted,
            reason: verdict.abort_reason || '',
            // v3.1.1 — forward `kind` so the backend can distinguish a hard
            // crash (TDR / context loss / hang / artifacts) from a soft fail
            // (thermal / variance).  Without this, hard crashes silently
            // got treated as soft, and the binary-search refiner would walk
            // UP past the real crash ceiling probing nearby values.
            kind: verdict.kind || '',
            max_temp_c: verdict.max_temp_c,
        };

        // Append step result to live log
        var entry = document.createElement('div');
        entry.className = 'oc-auto-step ' + (prev.stable ? 'stable' : 'unstable');
        var resultStr = prev.stable ? 'stable' : ('unstable: ' + (prev.reason || 'crash/hang'));
        entry.textContent = (prev.stable ? '✓' : '✗') + ' [' + phaseShort + '] core+' + nxt.current_core + ' mem+' + nxt.current_mem + ' — ' + resultStr + ' (max ' + verdict.max_temp_c + '°C, ' + verdict.duration_s + 's)';
        var statusEl = document.getElementById('oc-auto-status');
        statusEl.appendChild(entry);
        statusEl.scrollTop = statusEl.scrollHeight;

        // v2.9.9.7 — STEP-DOWN on hard crash (don't kill the whole session).
        //
        // Old behaviour (v2.9.9.2 – v2.9.9.6): a single crash anywhere broke
        // the loop, even if a perfectly fine lower offset had already been
        // verified.  E.g. a crash at core+600 with core+550 known-stable
        // would discard the +550 result entirely and never test memory.
        //
        // New behaviour: when a crash happens, give the GPU 5 s to come back
        // from the TDR, then send the crash verdict to the backend.  The
        // backend's state machine notices `kind=context_loss/hang/tdr/...`
        // and either:
        //   (a) locks the last-stable offset on this axis and advances to
        //       the next axis (e.g. core crashed → keep stable_core, start
        //       memory tuning), OR
        //   (b) aborts the whole session if even +0 wasn't stable.
        //
        // Either way, the FRONTEND no longer makes the decision — we just
        // forward the verdict and let the loop continue.  Backend will
        // signal `done: true, aborted: true` if it can't recover, and the
        // top of the loop catches that.
        if (!prev.stable && /context|hang|tdr|crash|artifact/i.test(prev.reason)) {
            termWrite('gpu-terminal',
                '⚠ Crash at ' + stepLabel + ' — GPU recovering, will step down to last stable',
                'error');
            await new Promise(function(r){ setTimeout(r, 5000); });   // GPU TDR settle
            // Do NOT break.  Fall through — next iteration sends `prev`
            // (the crash verdict) to backend, which advances the phase.
        }
    }

    _ocState.autoActive = false;
    document.getElementById('auto-oc-start-btn').style.display = '';
    document.getElementById('auto-oc-cancel-btn').style.display = 'none';
}


// v2.9.9.2 — show a prominent red banner when the GPU crashes.  Stays
// visible until the user dismisses it.  Used by both Quick Tune and
// Benchmark Tune crash handlers.
function _showCrashBanner(title, message) {
    var existing = document.getElementById('gpu-crash-banner');
    if (existing) existing.remove();
    var banner = document.createElement('div');
    banner.id = 'gpu-crash-banner';
    banner.style.cssText =
        'position:fixed;top:48px;left:50%;transform:translateX(-50%);' +
        'z-index:10002;min-width:380px;max-width:560px;padding:14px 44px 14px 18px;' +
        'background:var(--bg-card);border:2px solid var(--red);border-radius:6px;' +
        'box-shadow:0 12px 36px rgba(0,0,0,0.55), 0 0 24px rgba(255,51,102,0.4);' +
        'font-size:12px';
    banner.innerHTML =
        '<div style="font-weight:700;color:var(--red);margin-bottom:4px;letter-spacing:0.5px">⚠ ' + escHtml(title) + '</div>' +
        '<div style="color:var(--text);line-height:1.5">' + escHtml(message) + '</div>' +
        '<button onclick="document.getElementById(\'gpu-crash-banner\').remove()" ' +
            'style="position:absolute;top:6px;right:8px;background:transparent;border:none;' +
            'color:var(--text-dim);font-size:20px;cursor:pointer;padding:2px 6px">×</button>';
    document.body.appendChild(banner);
    return Promise.resolve();
}

// Rough progress estimate based on phase progression.
// core_jump=0-30%, core_refine=30-50%, mem_jump=50-75%, mem_refine=75-90%, final=90-99%
function _estimateAutoProgress(nxt) {
    var p = nxt.phase || '';
    var step = nxt.step || 0;
    var maxStep = nxt.max_step || 14;
    if (p === 'core_jump') return Math.min(30, (step / maxStep) * 60);
    if (p === 'core_refine') return 30 + Math.min(20, (step / maxStep) * 30);
    if (p === 'mem_jump') return 50 + Math.min(25, (step / maxStep) * 40);
    if (p === 'mem_refine') return 75 + Math.min(15, (step / maxStep) * 25);
    if (p === 'final_validation') return 92;
    return Math.min(99, (step / maxStep) * 100);
}

async function _runAutoStabilityStep(durationSec) {
    // Get expected core max for clock-dip detection
    var live = await apiGet('/api/gpu/oc/live');
    var expectedCoreMax = live.ok ? (live.core_max_mhz || 0) : 0;

    await apiPost('/api/gpu/oc/probe/start', { expected_core_max: expectedCoreMax });
    var webgl = startWebGLStress();
    var ticks = 0;
    var startMs = Date.now();
    var aborted = false;

    while ((Date.now() - startMs) / 1000 < durationSec) {
        // v2.9.5 — accept either auto-OC OR benchmark as the active driver
        // of this step.  Earlier code only checked autoActive, which made
        // benchmarks return immediately with all zeros.
        if (!_ocState.autoActive && !_ocState.benchmarkActive) break;
        await new Promise(function(r) { setTimeout(r, 1000); });
        ticks++;

        // Snapshot WebGL signals
        var contextLost = webgl ? webgl.contextLost : false;
        var pixelFailed = false;
        var frameStats = { avg: 0 };
        if (webgl) {
            // Pixel check every 2nd tick (after warmup)
            if (ticks >= 3 && ticks % 2 === 0) {
                pixelFailed = !webgl.runPixelCheck();
            }
            frameStats = webgl.getFrameStats();
        }

        var tick = await apiPost('/api/gpu/oc/probe/tick', {
            frame_time_ms: frameStats.avg || null,
            context_lost: contextLost,
            pixel_check_failed: pixelFailed,
        });
        if (!tick.ok && (tick.abort || tick.hang)) {
            aborted = true;
            break;
        }
        // Early-exit on WebGL context loss to avoid wasting time
        if (contextLost) {
            aborted = true;
            break;
        }
    }
    stopWebGLStress(webgl);
    var verdict = await apiPost('/api/gpu/oc/probe/end');
    return verdict;
}

async function cancelAutoOc() {
    if (!_ocState.autoActive) return;
    _ocState.autoActive = false;
    // v2.9.9.0 — both modes share this cancel handler.  Try both endpoints
    // since we don't track which was started — whichever isn't active
    // returns ok:false harmlessly.
    await Promise.all([
        apiPost('/api/gpu/oc/auto/cancel'),
        apiPost('/api/gpu/oc/benchmark-tune/cancel'),
    ]);
    document.getElementById('auto-oc-start-btn').style.display = '';
    var benchBtn = document.getElementById('bench-tune-start-btn');
    if (benchBtn) benchBtn.style.display = '';
    document.getElementById('auto-oc-cancel-btn').style.display = 'none';
    updateAutoOcStatus('⏹ Auto-tune cancelled, reset to stock');
    termWrite('gpu-terminal', '⏹ Auto-OC cancelled');
    loadOcProfile();
    pollOcLiveOnce();
}


// ═══════════════════════════════════════════════════════════════
// v2.9.9.0 — BENCHMARK TUNE
// Walks an offset ladder, benchmarks each step (FPS + 1%-low + frametime),
// picks the offset with the best score.  Slower but smarter than Quick Tune
// — finds the actual performance peak rather than the stability ceiling.
// ═══════════════════════════════════════════════════════════════
async function startBenchmarkTune() {
    if (_ocState.autoActive) return;
    if (!_ocState.capability || _ocState.capability.vendor !== 'nvidia') {
        showErrorToast('Benchmark Tune requires an NVIDIA GPU.');
        return;
    }
    // v2.9.9.1 — finer steps + much longer per-step measurement to find the
    // actual performance peak instead of just the rough one Quick Tune finds.
    if (!confirm('Benchmark Tune — Performance-Optimised Auto-OC\n\n' +
        '• Maxes power limit FIRST\n' +
        '• Walks the CORE ladder in 25 MHz steps (0, 25, 50 ... up to 600 MHz)\n' +
        '   Per step: 90s WebGL benchmark → records FPS, 1% lows, frametime σ, temp\n' +
        '   Picks the offset with the BEST score\n' +
        '• Repeats for MEMORY in 100 MHz steps (0, 100, 200 ... up to 2500 MHz)\n' +
        '• Final 120s validation pass with both winners\n' +
        '• Saves the winning combo to a profile\n\n' +
        'Runtime: 45-90 minutes (stops early if your GPU crashes at a low offset).\n' +
        'Use Quick Tune if you just want the maximum stable OC.\n' +
        'Use this when you want the offset with the best ACTUAL performance.\n\n' +
        'Continue?')) return;

    _ocState.autoActive = true;
    _ocState.autoLog = [];
    document.getElementById('auto-oc-start-btn').style.display = 'none';
    document.getElementById('bench-tune-start-btn').style.display = 'none';
    document.getElementById('auto-oc-cancel-btn').style.display = '';
    document.getElementById('oc-auto-progress-bar').style.display = 'block';
    document.getElementById('oc-auto-status').innerHTML = '';
    updateAutoOcStatus('▶ Starting Benchmark Tune...');

    termWrite('gpu-terminal', '🏁 Benchmark Tune started — measures real performance per offset');

    var startResp = await apiPost('/api/gpu/oc/benchmark-tune/start', {
        core_max_offset: 600,    // v2.9.9.1 — was 300 (matches new defaults)
        mem_max_offset:  2500,   // v2.9.9.1 — was 1500
        max_power: true,
    });
    if (!startResp || !startResp.ok) {
        termWrite('gpu-terminal', '✗ Benchmark Tune failed to start: ' + ((startResp && startResp.err) || 'unknown'), 'error');
        cancelAutoOc();
        return;
    }
    var totalSteps = startResp.total_steps || (startResp.estimated_steps || 14);
    termWrite('gpu-terminal', '   Core ladder: ' + JSON.stringify(startResp.core_ladder));
    termWrite('gpu-terminal', '   Mem ladder:  ' + JSON.stringify(startResp.mem_ladder));

    var prev = null;
    var safetyCap = (startResp.core_ladder.length + startResp.mem_ladder.length + 5);

    for (var iter = 0; iter < safetyCap; iter++) {
        if (!_ocState.autoActive) break;

        var body = prev ? Object.assign({ reported: true }, prev) : {};
        var nxt = await apiPost('/api/gpu/oc/benchmark-tune/next', body);
        if (!nxt || !nxt.ok) {
            termWrite('gpu-terminal', '✗ Benchmark step error: ' + ((nxt && nxt.err) || 'unknown'), 'error');
            break;
        }
        if (nxt.done) {
            var b = nxt.best || {};
            termWrite('gpu-terminal', '🏆 Benchmark Tune complete!');
            termWrite('gpu-terminal', '   Winner: core+' + b.core_offset_mhz + ' MHz / mem+' + b.mem_offset_mhz + ' MHz');
            termWrite('gpu-terminal', '   Score: ' + b.best_score + ' (gain: ' + (b.gain_pct >= 0 ? '+' : '') + b.gain_pct + '% vs stock)');
            termWrite('gpu-terminal', '   FPS: ' + b.avg_fps + ' avg, ' + b.min_fps + ' 1% low, frametime σ: ' + b.frametime_std_ms + ' ms');
            updateAutoOcStatus('✓ Benchmark Tune complete — saved: core+' + b.core_offset_mhz + ' / mem+' + b.mem_offset_mhz +
                ' (+' + b.gain_pct + '% vs stock)');
            document.getElementById('oc-auto-progress-fill').style.width = '100%';
            _renderBenchmarkLadder(nxt.core_results || [], nxt.mem_results || [], b);
            loadOcProfile();
            break;
        }

        var dur = nxt.recommended_duration_s || 23;
        var label = nxt.current_label || (nxt.phase + ' step ' + nxt.step_idx);
        termWrite('gpu-terminal', '▶ [' + nxt.phase.toUpperCase() + ' ' + nxt.step_idx + '/' + totalSteps + '] ' + label + ' — benchmarking ' + dur + 's...');
        updateAutoOcStatus('[' + nxt.phase + '] ' + label);

        var pct = Math.min(99, Math.floor((nxt.step_idx / totalSteps) * 100));
        document.getElementById('oc-auto-progress-fill').style.width = pct + '%';

        // Apply this step's offset BEFORE measuring
        await apiPost('/api/gpu/oc/apply', {
            core_offset_mhz: nxt.apply_core,
            mem_offset_mhz: nxt.apply_mem,
            power_pct: 100,
        });
        // v2.9.9.4 — bumped from 1.5s to 3s.  When the previous offset
        // was hammering the GPU at full bore, NVAPI applying a new offset
        // can take a moment to actually take effect (boost curve transition,
        // thermal sensor settle, driver clock-domain switch).  3s + the
        // 1s WebGL warmup discard inside _runAutoStabilityStep gives a
        // total 4s settle before any frame data is recorded.
        await new Promise(function(r) { setTimeout(r, 3000); });

        // Re-use the auto-stability step (same WebGL stress + verdict pipeline)
        var verdict = await _runAutoStabilityStep(dur);
        if (!_ocState.autoActive) break;

        // v2.9.9.4 — clamp the verdict values before they get displayed
        // OR scored.  If something slipped through the per-draw filter,
        // we don't want a 3-million-FPS row in the results table.
        var _clampFps = function(v) {
            var n = Number(v);
            if (!isFinite(n) || n < 0) return 0;
            return Math.min(n, 5000);
        };
        prev = {
            stable:             verdict.stable && !verdict.aborted,
            aborted:            !!verdict.aborted,
            abort_reason:       verdict.abort_reason || '',
            // v3.1.2 — forward `kind` so the benchmark backend can detect
            // hard crashes (context loss / TDR / etc) for retroactive
            // invalidation of previously-scored stable rungs.
            kind:               verdict.kind || '',
            avg_fps:            _clampFps(verdict.avg_fps),
            avg_frame_time_ms:  verdict.avg_frame_time_ms,
            p99_frame_time_ms:  verdict.p99_frame_time_ms,
            frame_variance_pct: verdict.frame_variance_pct,
            max_temp_c:         verdict.max_temp_c,
            max_core_mhz:       verdict.max_core_mhz,
            max_mem_mhz:        verdict.max_mem_mhz,
        };

        var entry = document.createElement('div');
        entry.className = 'oc-auto-step ' + (prev.stable ? 'stable' : 'unstable');
        entry.textContent = (prev.stable ? '✓' : '✗') + ' ' + label +
            ' — fps ' + (prev.avg_fps || 0) + ' / 1%low ' +
            (prev.p99_frame_time_ms ? Math.round(1000 / prev.p99_frame_time_ms) : 0) +
            ' / σ ' + (prev.frame_variance_pct || 0) + '% / ' + (prev.max_temp_c || 0) + '°C';
        var statusEl = document.getElementById('oc-auto-status');
        statusEl.appendChild(entry);
        statusEl.scrollTop = statusEl.scrollHeight;

        // v2.9.9.2 — same hard-stop as Quick Tune.  In benchmark mode the
        // backend uses the crash signal to mark this axis "stop", but we
        // also break the JS loop so we don't sit there waiting through the
        // mem-axis steps when a core-axis offset just black-screened the
        // whole machine.  Send the user back to safety.
        if (!prev.stable && /context|hang|tdr|crash/i.test(prev.abort_reason)) {
            termWrite('gpu-terminal', '🛑 Hard crash during benchmark — stopping (GPU recovering)', 'error');
            await _showCrashBanner('Benchmark Tune stopped',
                'GPU crash at ' + label + ': ' + (prev.abort_reason || 'unknown') +
                '. Offsets reset to stock to let the GPU recover. ' +
                'Re-run Benchmark Tune with a lower core/mem ceiling if this keeps happening.');
            await new Promise(function(r){ setTimeout(r, 4000); });   // GPU settle
            break;
        }
    }

    _ocState.autoActive = false;
    document.getElementById('auto-oc-start-btn').style.display = '';
    document.getElementById('bench-tune-start-btn').style.display = '';
    document.getElementById('auto-oc-cancel-btn').style.display = 'none';
}


// Render a ranked-results table inside #oc-auto-status when Benchmark Tune
// finishes — helps the user see WHY the chosen offset won.
function _renderBenchmarkLadder(coreResults, memResults, best) {
    var statusEl = document.getElementById('oc-auto-status');
    if (!statusEl) return;

    var rowFor = function(r, axis) {
        var marker = '';
        if (best && axis === 'core' && r.offset_core === best.core_offset_mhz) marker = ' ← winner';
        if (best && axis === 'mem'  && r.offset_mem  === best.mem_offset_mhz)  marker = ' ← winner';
        var color = r.stable ? (marker ? 'var(--accent)' : 'var(--text)') : 'var(--red)';
        var label = (axis === 'core' ? 'core+' + r.offset_core : 'mem+' + r.offset_mem);
        return '<tr style="color:' + color + '">' +
            '<td style="padding:2px 8px">' + label + '</td>' +
            '<td style="padding:2px 8px">' + r.score + '</td>' +
            '<td style="padding:2px 8px">' + (r.avg_fps || 0) + '</td>' +
            '<td style="padding:2px 8px">' + (r.min_fps || 0) + '</td>' +
            '<td style="padding:2px 8px">' + (r.frametime_std_ms || 0) + '</td>' +
            '<td style="padding:2px 8px">' + (r.max_temp_c || 0) + '°C</td>' +
            '<td style="padding:2px 8px">' + (r.stable ? '✓' : '✗ ' + (r.abort_reason || 'crash')) + '</td>' +
            '<td style="padding:2px 8px;color:var(--accent)">' + marker + '</td>' +
            '</tr>';
    };

    var headHtml = '<thead><tr style="color:var(--text-dim);font-size:10px;text-transform:uppercase">' +
        '<th style="padding:2px 8px;text-align:left">Offset</th>' +
        '<th style="padding:2px 8px;text-align:left">Score</th>' +
        '<th style="padding:2px 8px;text-align:left">Avg FPS</th>' +
        '<th style="padding:2px 8px;text-align:left">1% Low</th>' +
        '<th style="padding:2px 8px;text-align:left">FT σ ms</th>' +
        '<th style="padding:2px 8px;text-align:left">Temp</th>' +
        '<th style="padding:2px 8px;text-align:left">Stable</th>' +
        '<th style="padding:2px 8px;text-align:left"></th>' +
        '</tr></thead>';

    var coreRows = coreResults.map(function(r){ return rowFor(r, 'core'); }).join('');
    var memRows  = memResults.map(function(r){ return rowFor(r, 'mem'); }).join('');

    var html =
        '<div style="margin-top:10px;padding:8px;background:var(--bg-void);border:1px solid var(--border);border-radius:4px">' +
        '<div style="font-size:11px;color:var(--accent);margin-bottom:6px;font-weight:600">CORE LADDER RESULTS</div>' +
        '<table style="width:100%;border-collapse:collapse;font-size:11px;font-family:var(--mono)">' + headHtml +
        '<tbody>' + coreRows + '</tbody></table>' +
        '<div style="font-size:11px;color:var(--accent);margin:10px 0 6px;font-weight:600">MEMORY LADDER RESULTS</div>' +
        '<table style="width:100%;border-collapse:collapse;font-size:11px;font-family:var(--mono)">' + headHtml +
        '<tbody>' + memRows + '</tbody></table>' +
        '</div>';
    statusEl.insertAdjacentHTML('beforeend', html);
}

function updateAutoOcStatus(msg) {
    var el = document.getElementById('oc-auto-status');
    if (!el) return;
    // Only update the header (first text node), keep the accumulated step log
    if (el.firstChild && el.firstChild.nodeType === Node.TEXT_NODE) {
        el.firstChild.textContent = msg + '\n';
    } else {
        el.insertBefore(document.createTextNode(msg + '\n'), el.firstChild);
    }
}


// ═══════════════════════════════════════════════════════════════════
// v3.2-beta.7 — Dependency manager banner
// ═══════════════════════════════════════════════════════════════════
// Polls /api/dependencies/status on a 3 s interval.  Surfaces THREE
// states at the top of the page (above the active page content):
//   1. Install batch in progress — show progress bar + current step
//   2. Restart pending after install — show "Restart now / Later"
//   3. Manual deps missing (RTSS) — show one-line "Install" prompt
// Hidden entirely when there's nothing to say.

var _depPollTimer = null;
var _depRestartCountdown = null;
var _depPollMode = 'idle';     // 'idle' (30s) | 'active' (1.5s)

function startDependencyPolling() {
    if (_depPollTimer) clearInterval(_depPollTimer);
    _refreshDependencyBanner();
    // v3.2.3 — adaptive polling.  When nothing is happening (no install
    // batch, no pending restart) we slow down to 30 s so we're not
    // hammering Flask 1200×/hour for status that almost never changes.
    // The poll function bumps itself up to 1.5 s while an install batch
    // is running, then drops back to 30 s when it finishes.
    _depPollTimer = setInterval(_refreshDependencyBanner, 30000);
}

function _setDepPollMode(mode) {
    if (mode === _depPollMode) return;
    _depPollMode = mode;
    if (_depPollTimer) clearInterval(_depPollTimer);
    var interval = (mode === 'active') ? 1500 : 30000;
    _depPollTimer = setInterval(_refreshDependencyBanner, interval);
}

async function _refreshDependencyBanner() {
    if (_pollSkipIfHidden()) return;
    var d = await apiGet('/api/dependencies/status');
    var banner = document.getElementById('dep-banner');
    var content = document.getElementById('dep-banner-content');
    if (!banner || !content || !d) return;

    var prog    = d.progress || {};
    var restart = d.pending_restart || {};
    var deps    = d.deps || [];
    var missingManual = deps.filter(function(x) { return !x.installed && x.browser_only; });

    // v3.2.3 — adaptive polling: only run the 1.5 s tight loop while an
    // install is actively running.  Otherwise drop back to 30 s.
    _setDepPollMode(prog.running ? 'active' : 'idle');

    // ── State 1: install batch running ─────────────────────────────
    if (prog.running) {
        var pct = prog.total > 0
            ? Math.min(100, Math.round((prog.downloaded / prog.total) * 100))
            : 0;
        var dlMb = (prog.downloaded || 0) / (1024 * 1024);
        var totMb = (prog.total || 0) / (1024 * 1024);
        banner.style.display = '';
        banner.style.borderLeft = '3px solid var(--accent)';
        content.innerHTML =
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">' +
              '<span class="pulse" style="color:var(--accent);font-size:11px;letter-spacing:0.05em;text-transform:uppercase;font-weight:600">Installing optional features…</span>' +
              '<span style="color:var(--text-tertiary);font-size:12px;margin-left:auto">' +
                (prog.completed || []).length + ' done · ' + (prog.failed || []).length + ' failed' +
              '</span>' +
            '</div>' +
            '<div style="color:var(--text-bright);font-size:12.5px">' + escHtml(prog.current_step || 'preparing…') + '</div>' +
            (prog.total > 0
                ? '<div style="margin-top:6px;display:flex;align-items:center;gap:10px">' +
                    '<div style="flex:1;height:4px;background:var(--bg-elevated);border-radius:2px;overflow:hidden">' +
                      '<div style="width:' + pct + '%;height:100%;background:var(--accent);transition:width 0.3s"></div>' +
                    '</div>' +
                    '<span style="color:var(--text-tertiary);font-family:var(--font-mono);font-size:11px">' +
                      dlMb.toFixed(1) + ' / ' + totMb.toFixed(1) + ' MB' +
                    '</span>' +
                  '</div>'
                : '');
        return;
    }

    // ── State 2: restart pending ───────────────────────────────────
    if (restart && restart.pending) {
        var sources = [];
        if (restart.by_ghostshell) sources.push('Vispora drivers');
        if (restart.by_windows)    sources.push('Windows updates');
        banner.style.display = '';
        banner.style.borderLeft = '3px solid var(--warning)';
        content.innerHTML =
            '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">' +
              '<span style="color:var(--warning);font-weight:600">⟳ Restart needed</span>' +
              '<span style="color:var(--text-secondary);font-size:12px;flex:1">' +
                'New drivers were installed (' + escHtml(sources.join(', ')) + '). ' +
                'A restart finishes the install — your virtual gamepad / frame telemetry won\'t work fully until then.' +
              '</span>' +
              '<button class="btn btn-sm btn-primary" onclick="restartNowAfterDeps()">Restart now (10 s)</button>' +
              '<button class="btn btn-sm" onclick="restartLater()">Later</button>' +
            '</div>';
        return;
    }

    // beta.15 — "Install RTSS for broader frame coverage" banner removed.
    // RTSS is no longer a managed GhostShell dependency (see core/
    // dependencies.py).  PresentMon 1.x covers D3D and PresentMon 2.x
    // adds OpenGL + Vulkan, so the historic RTSS recommendation isn't
    // relevant.  The dep-banner only has state 1 (auto deps installing)
    // and state 2 (post-install restart pending) now.

    // ── Otherwise: hidden ──────────────────────────────────────────
    banner.style.display = 'none';
}

async function restartNowAfterDeps() {
    if (!confirm('Restart Windows now in 10 seconds?\n\nYou can abort with "shutdown /a" in a command prompt.')) return;
    var r = await apiPost('/api/dependencies/restart-now', {});
    if (r && r.ok) {
        addLog('Windows restart initiated (' + (r.in_seconds || 10) + 's)');
    } else {
        addLog('Restart failed: ' + ((r && r.err) || 'unknown'));
    }
}

async function restartLater() {
    var r = await apiPost('/api/dependencies/restart-later', {});
    if (r && r.ok) {
        addLog('Restart deferred — will re-prompt on next Vispora launch');
        _refreshDependencyBanner();
    }
}

function dismissDepBanner() {
    sessionStorage.setItem('dep-manual-dismissed', '1');
    var b = document.getElementById('dep-banner');
    if (b) b.style.display = 'none';
}

document.addEventListener('DOMContentLoaded', function() {
    // Stagger the start so we don't compete with the boot-splash + initial
    // page-data fetches for the first ~2 s.
    setTimeout(startDependencyPolling, 2500);
    // v3.3.1-beta.9 — check for a legacy-bad-tweaks recovery report.
    // Boot's auto_recover.recover_legacy_bad_tweaks() may have
    // reverted things; if so it wrote a report and we show the user
    // an apology toast with a "view details" button.
    setTimeout(_checkRecoveryReport, 3500);
});

async function _checkRecoveryReport() {
    try {
        var r = await apiGet('/api/recover/last-report');
        if (!r || !r.pending || !(r.reverted && r.reverted.length)) return;
        _showRecoveryApologyToast(r);
    } catch (e) {}
}

function _showRecoveryApologyToast(report) {
    var n = report.reverted.length;
    var existing = document.getElementById('recover-toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.id = 'recover-toast';
    toast.className = 'post-install-toast ok';
    toast.style.borderLeft = '4px solid var(--accent)';
    toast.innerHTML =
        '<div class="post-install-toast-title">Vispora auto-fixed ' + n + ' legacy tweak' + (n !== 1 ? 's' : '') + '</div>' +
        '<div class="post-install-toast-msg">' +
        'An earlier version of Vispora applied ' + n + ' setting' + (n !== 1 ? 's' : '') +
        ' that turned out to cause problems (Bluetooth audio glitches, SSD wear, broken Outlook search, etc.).  ' +
        'Sorry — we\'ve reverted them on your system.  ' +
        '<a href="#" onclick="_showRecoveryDetailsModal();return false;" style="color:var(--accent-bright);text-decoration:underline">' +
        'See what changed →</a>' +
        '</div>' +
        '<button class="post-install-toast-close" onclick="_dismissRecoveryToast()">×</button>';
    document.body.appendChild(toast);
    requestAnimationFrame(function() { toast.classList.add('show'); });
}

function _dismissRecoveryToast() {
    var t = document.getElementById('recover-toast');
    if (t) {
        t.classList.remove('show');
        setTimeout(function() {
            var t2 = document.getElementById('recover-toast');
            if (t2) t2.remove();
        }, 400);
    }
    // Mark seen so we don't re-show on next boot
    apiPost('/api/recover/acknowledge', {}).catch(function() {});
}

function _showRecoveryDetailsModal() {
    apiGet('/api/recover/last-report').then(function(r) {
        if (!r || !r.reverted) return;
        var html =
            '<div style="padding:18px 22px;max-width:680px">' +
            '<h2 style="margin:0 0 8px;color:var(--text-bright);font-size:18px">A quick apology + what we changed</h2>' +
            '<p style="color:var(--text-secondary);font-size:13px;line-height:1.55;margin-bottom:14px">' +
            'A pre-stable audit of Vispora\'s optimizer caught several tweaks that older builds applied which actively cause problems on modern Windows.  We removed them from Apply All — and for users like you who already ran an older build, Vispora scanned the registry on startup and reverted whatever it found.  Below is the full list of what changed on your machine.' +
            '</p>' +
            '<div style="background:var(--bg-overlay);border-radius:6px;padding:6px 0;margin-bottom:14px">';
        r.reverted.forEach(function(item) {
            html +=
                '<div style="padding:10px 14px;border-bottom:1px solid var(--border)">' +
                '<div style="color:var(--accent);font-weight:600;font-size:13px;margin-bottom:4px">' +
                  '● ' + escHtml(item.label || item.id) + '</div>' +
                '<div style="color:var(--text-secondary);font-size:12px;line-height:1.5;margin-bottom:6px">' +
                  escHtml(item.explanation || '') + '</div>' +
                '<div style="color:var(--text-tertiary);font-family:var(--font-mono);font-size:11px">' +
                  escHtml(item.action || '') + '</div>' +
                '</div>';
        });
        html += '</div>' +
            '<p style="color:var(--text-tertiary);font-size:12px;line-height:1.5;margin-bottom:14px">' +
            'No action needed on your end.  These are already reverted.  ' +
            'For context on what we changed in the optimizer itself, see release notes for v3.3.1-beta.7 and beta.8.' +
            '</p>' +
            '<div style="display:flex;justify-content:flex-end;gap:8px">' +
            '<button class="btn btn-primary" onclick="closeModal();_dismissRecoveryToast()">Got it, thanks</button>' +
            '</div>' +
            '</div>';
        openModal(html);
    });
}

// v3.2.3 — when the window comes back from minimized / tabbed away,
// fire one immediate refresh of the always-on polls so the user
// doesn't stare at stale state for 30 s waiting for the next tick.
_onVisible(function() {
    try { _refreshDependencyBanner(); } catch (e) {}
    try { if (typeof _refreshAdaptiveLive === 'function') _refreshAdaptiveLive(); } catch (e) {}
});


// ═══════════════════════════════════════════════════════════════════
// v3.3 — Capture page (clipper + screenshots + hotkey settings)
// ═══════════════════════════════════════════════════════════════════
var _capturePollTimer = null;

async function loadCaptureStatus() {
    var d = await apiGet('/api/capture/status');
    if (!d) return;
    var c = d.clipper || {};
    _renderClipperStatus(c);
    // OBS backend status (v3.3.0-beta.15+).  Independent endpoint —
    // OBS detection is cached for 60 s on the backend so a per-tick
    // call here is cheap, but we still piggyback the request on the
    // capture-status poll cadence rather than a separate timer.
    try {
        var obs = await apiGet('/api/obs/status');
        if (obs) _renderObsStatus(obs);
    } catch (e) {}
    // v3.3.1-beta.1 — Stream Helper status (auto OBS config).  Same
    // piggyback strategy; cheap, single endpoint, all settings in
    // one response.
    try {
        var sh = await apiGet('/api/stream-helper/status');
        if (sh) _renderStreamHelperStatus(sh);
    } catch (e) {}
    // v3.3.0-beta.3 fix: monitors + audio devices MUST populate before
    // _renderClipperSettings runs, because that's the function that
    // sets <select>.value — and a <select> can't be set to a value
    // for an <option> that doesn't exist in its DOM yet (the browser
    // silently keeps whatever was selected before, which made
    // "monitor:0" revert to "auto-follow" every time).
    if (c.monitors && c.monitors.length) _populateMonitorOptions(c.monitors);
    if (!_audioDevicesLoaded) {
        _audioDevicesLoaded = true;
        try {
            var a = await apiGet('/api/clipper/audio-devices');
            if (a && a.ok) _populateAudioDevices(a.audio || []);
        } catch (e) {}
    }
    // NOW it's safe to set the dropdown values from settings.
    _renderClipperSettings(c.settings || {});
    _renderTargetStatus(c);
    _renderSystemAudioInfo(c);
    _renderHotkeyStatus(d.hotkeys || {});
    _renderRecentClips(d.recent_clips || []);
    _renderRecentScreenshots(d.recent_screenshots || []);
}
var _audioDevicesLoaded = false;

function _renderClipperStatus(c) {
    var box = document.getElementById('clipper-status');
    if (!box) return;
    var toggleEl = document.getElementById('toggle-clipper-enabled');
    if (toggleEl) {
        if (c.settings && c.settings.enabled) toggleEl.classList.add('on');
        else toggleEl.classList.remove('on');
    }
    var lbl = document.getElementById('clip-buffer-label');
    if (lbl && c.settings) lbl.textContent = (c.settings.buffer_seconds || 30) + ' seconds';

    var pieces = [];
    if (!c.ffmpeg_installed) {
        pieces.push('<span style="color:var(--warning)">⚠ FFmpeg not installed</span> — open the Dependencies banner to auto-install.');
    } else if (c.running) {
        var enc = c.encoder_used || 'unknown';
        var up = Math.round(c.uptime_s || 0);
        pieces.push('<span style="color:var(--accent);font-weight:600">● Buffering</span>' +
                    ' · encoder <span style="font-family:var(--font-mono);color:var(--accent-bright)">' + escHtml(enc) + '</span>' +
                    ' · uptime ' + up + ' s' +
                    ' · PID ' + (c.ffmpeg_pid || '?'));
        if (c.foreground_game && c.foreground_game.exe) {
            pieces.push('<br>Foreground game: <span style="color:var(--text-bright);font-weight:600">' + escHtml(c.foreground_game.name || c.foreground_game.exe) + '</span>');
        }
    } else {
        pieces.push('<span style="color:var(--text-tertiary)">○ Buffer not running</span> — turn it on below to start capturing.');
        if (c.last_err) {
            pieces.push('<br><span style="color:var(--danger)">' + escHtml(c.last_err) + '</span>');
        }
    }
    box.innerHTML = '<div style="padding:10px 12px;background:var(--bg-overlay);border-radius:var(--radius-sm)">' + pieces.join('') + '</div>';
}

function _renderClipperSettings(s) {
    if (!s) return;
    var $ = function(id) { return document.getElementById(id); };
    if ($('clip-duration')   && s.buffer_seconds)     $('clip-duration').value     = s.buffer_seconds;
    if ($('clip-max-buffer') && s.max_buffer_seconds) $('clip-max-buffer').value   = s.max_buffer_seconds;
    if ($('clip-fps')        && s.fps)                $('clip-fps').value          = s.fps;
    if ($('clip-encoder')    && s.encoder)            $('clip-encoder').value      = s.encoder;
    if ($('clip-bitrate')    && s.bitrate_kbps)       $('clip-bitrate').value      = s.bitrate_kbps;
    if ($('clip-target')     && s.capture_target)     $('clip-target').value       = s.capture_target;
    // Hotkey button labels
    if ($('hotkey-save-btn') && s.save_hotkey)       $('hotkey-save-btn').textContent = s.save_hotkey || '(unbound)';
    if ($('hotkey-ss-btn')   && s.screenshot_hotkey) $('hotkey-ss-btn').textContent   = s.screenshot_hotkey || '(unbound)';
    // Audio toggles
    var mt = $('toggle-mic-enabled');
    if (mt) { if (s.mic_enabled) mt.classList.add('on'); else mt.classList.remove('on'); }
    var st = $('toggle-sysaudio-enabled');
    if (st) { if (s.system_audio_enabled) st.classList.add('on'); else st.classList.remove('on'); }
    // Audio device selections (only when not focused — don't clobber user).
    // v3.3.0-beta.4 — the system-audio device is auto-detected now, so
    // no dropdown to sync.  _renderSystemAudioInfo() shows what got picked.
    var mic = $('clip-mic-device');
    if (mic && document.activeElement !== mic) mic.value = s.mic_device || '';
}

function _renderTargetStatus(c) {
    var box = document.getElementById('target-status');
    if (!box) return;
    var pieces = [];
    if (c.target_used)  pieces.push('current: ' + escHtml(c.target_used));
    if (c.target_rect)  pieces.push('rect ' + c.target_rect.join(', '));
    if (c.target_title) pieces.push('title "' + escHtml(c.target_title) + '"');
    // v3.3.0-beta.7 — surface which screen-capture engine is actually
    // in use.  ddagrab = GPU-accelerated (DXGI Desktop Duplication);
    // gdigrab = CPU-side GDI fallback (window-title mode or virtual
    // desktop, or FFmpeg lacks ddagrab support).
    if (c.running && c.capture_method) {
        var tag = (c.capture_method === 'ddagrab')
            ? '<span style="color:var(--accent);font-weight:600">ddagrab</span> ' +
              '<span style="color:var(--text-tertiary)">(GPU)</span>'
            : '<span style="color:var(--text-bright);font-weight:600">gdigrab</span> ' +
              '<span style="color:var(--text-tertiary)">(CPU fallback)</span>';
        pieces.push('engine: ' + tag);
    } else if (!c.running && c.ddagrab_avail) {
        pieces.push('<span style="color:var(--text-tertiary)">' +
                    'ddagrab available — will use GPU capture when running</span>');
    }
    if (c.buffered_seconds != null && c.running) {
        var max = (c.settings && c.settings.max_buffer_seconds) || 60;
        pieces.push('buffered ' + c.buffered_seconds.toFixed(1) + ' / ' + max + ' s');
    }
    box.innerHTML = pieces.join(' · ') || '—';
}

function _populateMonitorOptions(monitors) {
    // v3.3.0-beta.12: dropdown shows ONLY monitor entries — auto-follow /
    // game-window / virtual-desktop modes were error-prone (the user
    // could end up capturing nothing meaningful when foreground was
    // something other than their game) and the user explicitly asked
    // for the simpler picker.
    //
    // Labels use physical_w / physical_h so a 4K display at 150 %
    // scaling shows as "3840×2160" — what users see in Windows
    // Display Settings — instead of the DPI-unaware "2560×1440" that
    // would otherwise confuse anyone who knows their monitor is 4K.
    var sel = document.getElementById('clip-target');
    if (!sel) return;
    sel.innerHTML = '';
    (monitors || []).forEach(function(m, i) {
        var opt = document.createElement('option');
        opt.value = 'monitor:' + i;
        var w = m.physical_w || m.w;
        var h = m.physical_h || m.h;
        opt.textContent = 'Monitor ' + (i + 1) + ' — ' + w + '×' + h +
                          (m.primary ? ' (primary)' : '');
        sel.appendChild(opt);
    });
    // If no monitor matches the saved value, default to primary
    // (monitor:0) and persist that on the next settings write so the
    // setting can't get stuck on a legacy "auto-follow" string.
    if (sel.selectedIndex < 0 && sel.options.length > 0) {
        sel.selectedIndex = 0;
    }
}

function _populateAudioDevices(devices) {
    // v3.3.0-beta.4 — only the microphone dropdown is user-facing now.
    // System audio is auto-detected (see _renderSystemAudioInfo).
    //
    // We still filter loopback-style devices out of the mic dropdown
    // because they aren't real microphones — listing CABLE Output as
    // an option for "Microphone" is just confusing.
    var mic = document.getElementById('clip-mic-device');
    if (!mic) return;

    var loopbackTokens = [
        'stereo mix', 'what u hear', 'what you hear', 'wave out mix',
        'voicemeeter out', 'voicemeeter aux', 'voicemeeter vaio',
        'cable output', 'cable-output', 'cable a output', 'cable b output',
        'cable c output', 'vb-cable', 'vb-audio',
        'virtual-audio-capturer', 'virtual audio capturer',
        'loopback',
    ];
    function isLoopback(name) {
        var n = String(name || '').toLowerCase();
        return loopbackTokens.some(function(tok) { return n.indexOf(tok) >= 0; });
    }

    var realInputs = (devices || []).filter(function(d) { return !isLoopback(d.name); });

    var micWas = mic.value;
    mic.innerHTML = '<option value="">— pick microphone —</option>';
    realInputs.forEach(function(d) {
        var opt = document.createElement('option');
        opt.value = d.name;
        opt.textContent = d.name;
        mic.appendChild(opt);
    });
    if (micWas) mic.value = micWas;
}

function _renderSystemAudioInfo(c) {
    // v3.3.0-beta.6 — system audio now uses native WASAPI loopback
    // (no third-party drivers required).  Status surface:
    //   • toggle off                                → grey "Toggle on to capture…"
    //   • toggle on + buffer running + ok           → green "Recording <device> · <rate> Hz × <ch>"
    //   • toggle on + buffer stopped + probe ok     → blue  "Will record from <device> when buffer starts"
    //   • toggle on + WASAPI runtime missing        → red   "Native WASAPI not available, error …"
    //   • toggle on + probe failed                  → amber "<device>: <error>"
    var box = document.getElementById('clip-sysaudio-info');
    if (!box) return;
    var s = c.settings || {};
    var sa = c.system_audio || {};
    if (!s.system_audio_enabled) {
        box.style.color = 'var(--text-tertiary)';
        box.innerHTML = 'Toggle on to capture whatever\'s playing through your default Windows output (speakers / headphones / HDMI).  ' +
                        'Uses native WASAPI loopback — no VB-CABLE / Voicemeeter / Stereo Mix needed.';
        return;
    }
    // Buffer running with sys audio actually flowing — best case
    if (sa.active && sa.active_device) {
        box.style.color = 'var(--text-secondary)';
        box.innerHTML = '<span style="color:var(--accent);font-weight:600">● Recording</span> ' +
                        '<span style="font-family:var(--font-mono);color:var(--text-bright)">' +
                        escHtml(sa.active_device) + '</span>' +
                        ' <span style="color:var(--text-tertiary)">· ' +
                        escHtml(sa.active_format || '') + ' · WASAPI loopback</span>';
        return;
    }
    // PyAudioWPatch failed to import (bundle missing the .pyd /
    // PortAudio dll) — hard error, nothing the user can do
    if (sa.import_err) {
        box.style.color = 'var(--danger, #e0584e)';
        box.innerHTML = '<span style="color:var(--danger, #e0584e);font-weight:600">⚠</span> ' +
                        'Native WASAPI runtime not available in this build.  ' +
                        '<span style="color:var(--text-tertiary);font-family:var(--font-mono);font-size:11px">' +
                        escHtml(sa.import_err) + '</span>';
        return;
    }
    // Probe succeeded but buffer not running yet
    if (sa.probe_ok && sa.probe_name) {
        box.style.color = 'var(--text-secondary)';
        box.innerHTML = '<span style="color:var(--accent-bright);font-weight:600">○</span> ' +
                        'Will record from <span style="font-family:var(--font-mono);color:var(--text-bright)">' +
                        escHtml(sa.probe_name) + '</span>' +
                        ' <span style="color:var(--text-tertiary)">· ' + sa.probe_rate + ' Hz × ' +
                        sa.probe_channels + ' ch · WASAPI loopback (starts when buffer turns on)</span>';
        return;
    }
    // Probe failed — usually means there's no default playback device
    // configured in Windows, or the user's only output device doesn't
    // support shared-mode loopback (very rare)
    box.style.color = 'var(--warning, #e0b850)';
    var err = sa.active_err || sa.probe_err || 'unknown';
    box.innerHTML = '<span style="color:var(--warning, #e0b850);font-weight:600">⚠</span> ' +
                    'Couldn\'t open the default Windows playback device for loopback capture.<br>' +
                    '<span style="color:var(--text-tertiary);font-size:11px;font-family:var(--font-mono)">' +
                    escHtml(err) + '</span><br>' +
                    '<span style="color:var(--text-tertiary);font-size:11.5px">' +
                    'Set a default playback device in Windows Sound settings, then toggle the buffer off/on.</span>';
}

// ─── OBS backend card (v3.3.0-beta.15+) ─────────────────────────
//
// Status box visual matrix (v3.3.0-beta.16):
//
//   detected.ok    version_ok    toggle_on    connection.connected   → State
//   ────────────   ───────────   ──────────   ────────────────────     ─────
//   false          —             —            —                        Red    "OBS not installed"
//   true           false         —            —                        Amber  "OBS too old"
//   true           true          false        —                        White  "OBS detected (toggle off)"
//   true           true          true         false                    Amber  "Toggle on, not connected — click Connect"
//   true           true          true         true                     Green  "Connected · OBS X.Y.Z · ws vN"

function _renderObsStatus(d) {
    var box = document.getElementById('obs-status-box');
    if (!box) return;
    var det  = d.detected   || {};
    var s    = d.settings   || {};
    var conn = d.connection || {};

    // Sync toggle visual
    var tog = document.getElementById('toggle-obs-enabled');
    if (tog) {
        if (s.enabled) tog.classList.add('on');
        else tog.classList.remove('on');
    }
    // Sync password field (don't clobber while user is typing)
    var pw = document.getElementById('obs-ws-password');
    if (pw && document.activeElement !== pw && (pw.value || '') !== (s.websocket_password || '')) {
        pw.value = s.websocket_password || '';
    }

    if (!det.ok) {
        box.style.color = 'var(--danger, #e0584e)';
        box.innerHTML = '<span style="color:var(--danger, #e0584e);font-weight:600">⚠</span> ' +
                        'OBS Studio not found.  ' +
                        '<span style="color:var(--text-tertiary)">Click "Install OBS" below to download from obsproject.com, then hit "Re-detect".</span>';
        return;
    }
    if (!det.version_ok) {
        box.style.color = 'var(--warning, #e0b850)';
        box.innerHTML = '<span style="color:var(--warning, #e0b850);font-weight:600">⚠</span> ' +
                        'OBS at <span style="font-family:var(--font-mono);color:var(--text-bright)">' +
                        escHtml(det.path || '') + '</span>' +
                        (det.version ? ' (v' + escHtml(det.version) + ')' : '') +
                        ' is too old.<br><span style="color:var(--text-tertiary)">' +
                        escHtml(det.err || 'Update to OBS 28+ for the built-in WebSocket plugin') +
                        '</span>';
        return;
    }
    if (!s.enabled) {
        box.style.color = 'var(--text-secondary)';
        box.innerHTML = '<span style="color:var(--accent-bright);font-weight:600">○</span> ' +
                        '<span style="color:var(--text-bright)">OBS ' +
                        escHtml(det.version || '') + ' detected</span> ' +
                        '<span style="color:var(--text-tertiary)">at <span style="font-family:var(--font-mono);font-size:11px">' +
                        escHtml(det.path || '') + '</span></span><br>' +
                        '<span style="color:var(--text-tertiary)">Toggle on to switch Vispora\'s recorder backend from FFmpeg to OBS.</span>';
        return;
    }
    // Toggle is ON — check connection
    if (conn.connected) {
        box.style.color = 'var(--text-secondary)';
        var plat = conn.platform_desc || conn.platform || 'unknown';
        // v3.3.0-beta.18: surface scene + buffer state too
        var rec = d.recording || {};
        var recLine;
        if (rec.scene_ready && rec.buffer_running) {
            recLine = '<span style="color:var(--accent);font-weight:600">● Replay buffer active</span>' +
                      ' <span style="color:var(--text-tertiary)">— save-clip hotkey saves from OBS.</span>';
        } else if (rec.scene_ready) {
            recLine = '<span style="color:var(--accent-bright);font-weight:600">○</span> ' +
                      '<span style="color:var(--text-bright)">Scene ready</span>' +
                      ' <span style="color:var(--text-tertiary)">but replay buffer not running. ' +
                      'Click "Start replay buffer" or turn the Vispora buffer toggle on.</span>';
        } else {
            recLine = '<span style="color:var(--text-tertiary)">' +
                      'No Vispora scene found in OBS yet. ' +
                      'Click "Setup scene" to create it (Display Capture + audio sources).</span>';
        }
        box.innerHTML = '<span style="color:var(--accent);font-weight:600">● Connected</span> ' +
                        '<span style="color:var(--text-tertiary)">to OBS </span>' +
                        '<span style="font-family:var(--font-mono);color:var(--text-bright)">' +
                        escHtml(conn.obs_version || '?') + '</span>' +
                        '<span style="color:var(--text-tertiary)"> · WebSocket v</span>' +
                        '<span style="font-family:var(--font-mono);color:var(--text-bright)">' +
                        escHtml(conn.ws_version || '?') + '</span>' +
                        '<span style="color:var(--text-tertiary)"> · ' + escHtml(plat) + '</span><br>' +
                        recLine;
    } else {
        box.style.color = 'var(--warning, #e0b850)';
        var err = conn.connect_err || 'Not connected yet — click "Connect to OBS" below.';
        box.innerHTML = '<span style="color:var(--warning, #e0b850);font-weight:600">⚠</span> ' +
                        '<span style="color:var(--text-bright)">Toggle on, but no WebSocket connection.</span><br>' +
                        '<span style="color:var(--text-tertiary);font-size:11px">' +
                        escHtml(err) + '</span>';
    }
}

async function toggleObsBackend(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    var r = await apiPost('/api/obs/settings', { enabled: newOn });
    if (r && r.ok) {
        addLog('OBS backend ' + (newOn ? 'enabled' : 'disabled'));
        // Auto-connect when toggling on so the user gets immediate
        // feedback about whether the connection works.
        if (newOn) {
            try {
                await apiPost('/api/obs/connect', {});
            } catch (e) {}
        }
    } else {
        addLog('OBS toggle failed: ' + ((r && r.err) || 'unknown'));
        el.classList.toggle('on');
    }
    try {
        var d = await apiGet('/api/obs/status');
        if (d) _renderObsStatus(d);
    } catch (e) {}
}

async function redetectObs() {
    addLog('Re-detecting OBS Studio install…');
    var r = await apiPost('/api/obs/detect', {});
    if (r && r.ok) {
        addLog('OBS found: ' + (r.version ? 'v' + r.version : 'unknown') + ' at ' + (r.path || '?'));
    } else if (r) {
        addLog('OBS not found: ' + (r.err || 'no obs64.exe in standard paths'));
    }
    try {
        var d = await apiGet('/api/obs/status');
        if (d) _renderObsStatus(d);
    } catch (e) {}
}

async function saveObsPassword(value) {
    var r = await apiPost('/api/obs/settings', { websocket_password: value || '' });
    if (r && r.ok) {
        addLog('OBS WebSocket password saved');
    } else {
        addLog('OBS password save failed: ' + ((r && r.err) || 'unknown'));
    }
}

async function connectObs() {
    addLog('Connecting to OBS WebSocket…');
    var r = await apiPost('/api/obs/connect', { force: true });
    if (r && r.connected) {
        addLog('OBS connected: v' + (r.obs_version || '?') + ' (ws v' + (r.ws_version || '?') + ')');
    } else if (r) {
        addLog('OBS connect failed: ' + (r.connect_err || 'unknown'));
    }
    try {
        var d = await apiGet('/api/obs/status');
        if (d) _renderObsStatus(d);
    } catch (e) {}
}

async function disconnectObs() {
    var r = await apiPost('/api/obs/disconnect', {});
    addLog('OBS disconnected');
    try {
        var d = await apiGet('/api/obs/status');
        if (d) _renderObsStatus(d);
    } catch (e) {}
}

async function launchObs() {
    addLog('Launching OBS…');
    var r = await apiPost('/api/obs/launch', {});
    if (r && r.ok) {
        addLog('OBS spawned — give it 3-5 seconds to come online, then click Connect');
    } else if (r) {
        addLog('OBS launch failed: ' + (r.err || 'unknown'));
    }
}

function openObsDownload() {
    apiPost('/api/util/open-path', { path: 'https://obsproject.com/download' });
}

async function setupObsScene() {
    addLog('Setting up Vispora scene in OBS…');
    var r = await apiPost('/api/obs/setup-scene', {});
    if (r && r.ok) {
        var created = (r.sources_created || []).join(', ') || 'nothing new';
        addLog('OBS scene ready (' + created + ')');
    } else if (r) {
        addLog('OBS scene setup failed: ' + (r.err || 'unknown'));
    }
    try {
        var d = await apiGet('/api/obs/status');
        if (d) _renderObsStatus(d);
    } catch (e) {}
}

async function startObsBuffer() {
    addLog('Starting OBS replay buffer…');
    var r = await apiPost('/api/obs/start-buffer', {});
    if (r && r.ok) {
        addLog('OBS replay buffer running (' + (r.buffer_seconds || '?') + 's)');
    } else if (r) {
        addLog('OBS replay buffer start failed: ' + (r.err || 'unknown'));
    }
    try {
        var d = await apiGet('/api/obs/status');
        if (d) _renderObsStatus(d);
    } catch (e) {}
}

async function stopObsBuffer() {
    var r = await apiPost('/api/obs/stop-buffer', {});
    addLog('OBS replay buffer stopped');
    try {
        var d = await apiGet('/api/obs/status');
        if (d) _renderObsStatus(d);
    } catch (e) {}
}

async function testObsSave() {
    addLog('Testing OBS save (calling SaveReplayBuffer)…');
    var r = await apiPost('/api/obs/save-replay', {});
    if (r && r.ok) {
        addLog('OBS clip saved: ' + (r.path || '?'));
        // Refresh recent-clips list so the new file shows up
        try { loadCaptureStatus(); } catch (e) {}
    } else if (r) {
        addLog('OBS save failed: ' + (r.err || 'unknown'));
    }
}

// ─── Stream Helper card (v3.3.1-beta.1+) ─────────────────────────
// When the master toggle is ON, the legacy Capture controls below
// (Clipper, audio sources, monitor picker, hotkey settings) collapse
// to a single status block — GhostShell handles all setup via OBS.
function _renderStreamHelperStatus(d) {
    if (!d) return;
    var s = d.settings || {};
    var box = document.getElementById('stream-helper-status');
    var tog = document.getElementById('toggle-stream-helper-enabled');
    if (tog) {
        if (s.enabled) tog.classList.add('on');
        else tog.classList.remove('on');
    }

    // Collapse / expand the legacy Capture UI based on the toggle.
    // We hide the "Clipper" / "Audio" / "Hotkeys" sections + dividers
    // below the Stream Helper card; the Stream Helper card itself
    // remains the only thing visible when on.
    var hideIds = [
        'legacy-capture-divider', 'clipper-card',
    ];
    // Also hide following section-dividers/cards by walking sibling
    // elements after legacy-capture-divider.  Simpler: just toggle
    // a class on the parent <div class="page"> that hides everything
    // matching a `data-legacy-capture` attribute.
    var legacyEls = document.querySelectorAll(
        '#page-capture .section-divider:not(:first-of-type), ' +
        '#page-capture .card:not(#stream-helper-card)'
    );
    legacyEls.forEach(function(el) {
        el.style.display = s.enabled ? 'none' : '';
    });

    if (!box) return;
    if (!s.enabled) {
        box.style.color = 'var(--text-tertiary)';
        box.innerHTML = 'Off — flip the toggle to let Vispora take over OBS setup.';
        return;
    }
    if (!d.obs_connected) {
        box.style.color = 'var(--warning, #e0b850)';
        box.innerHTML = '<span style="color:var(--warning, #e0b850);font-weight:600">⚠</span> ' +
                        'Stream Helper is on, but OBS Studio isn\'t connected.<br>' +
                        '<span style="color:var(--text-tertiary)">Open OBS, enable WebSocket Server in Tools settings, paste the password into the OBS Studio backend card below, and flip its toggle on.</span>';
        return;
    }
    var active = d.active_game || '';
    var watcher = d.watcher_running ? 'watching' : 'stopped';
    var bullets = [];
    if (s.capture_game_audio)    bullets.push('Game audio');
    if (s.capture_discord_audio) bullets.push('Discord audio');
    if (s.capture_mic)           bullets.push('Mic');
    // v3.3.1-beta.3: warn if the active game is one of the known
    // anti-cheats that blocks OBS Game Capture hooks.  Roblox uses
    // Hyperion/Byfron, Valorant uses Vanguard — both will leave
    // Game Capture's preview black no matter what we do.  Streamers
    // covering those games typically fall back to Window Capture
    // or Display Capture in OBS.
    var blockedAntiCheatGames = ['robloxplayerbeta.exe', 'valorant.exe', 'valorant-win64-shipping.exe'];
    var anticheatNote = '';
    if (active && blockedAntiCheatGames.indexOf(String(active).toLowerCase()) >= 0) {
        anticheatNote = '<br><span style="color:var(--warning, #e0b850);font-size:11px">' +
                        '⚠ Heads up: ' + escHtml(active) + '\'s anti-cheat (Byfron / Vanguard) blocks OBS Game Capture hooks.  ' +
                        'The Game Capture source will show a black preview regardless of what Vispora sets.  ' +
                        'For these games, manually swap to Window Capture or Display Capture inside OBS.' +
                        '</span>';
    }
    box.style.color = 'var(--text-secondary)';
    box.innerHTML = '<span style="color:var(--accent);font-weight:600">● Active</span> ' +
                    '<span style="color:var(--text-tertiary)">— foreground game tracker is ' +
                    escHtml(watcher) + '.</span><br>' +
                    '<span style="color:var(--text-bright)">Currently following:</span> ' +
                    (active
                        ? '<span style="font-family:var(--font-mono);color:var(--accent-bright)">' + escHtml(active) + '</span>'
                        : '<span style="color:var(--text-tertiary)">no known game running</span>') +
                    '<br><span style="color:var(--text-tertiary)">Audio sources: </span>' +
                    '<span style="color:var(--text-bright)">' + (bullets.join(' · ') || 'none') + '</span>' +
                    anticheatNote;
}

async function toggleStreamHelper(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    var r = await apiPost('/api/stream-helper/settings', { enabled: newOn });
    if (r && r.ok) {
        addLog('Stream Helper ' + (newOn ? 'enabled' : 'disabled'));
        // When turning on, immediately set up the scene so the user
        // sees the active state without waiting for the watcher tick.
        if (newOn) {
            try {
                await apiPost('/api/stream-helper/setup', {});
            } catch (e) {}
        }
    } else {
        addLog('Stream Helper toggle failed: ' + ((r && r.err) || 'unknown'));
        el.classList.toggle('on');     // revert visual
    }
    try {
        var d = await apiGet('/api/stream-helper/status');
        if (d) _renderStreamHelperStatus(d);
    } catch (e) {}
}

async function streamHelperSetup() {
    addLog('Setting up Stream Helper scene in OBS…');
    var r = await apiPost('/api/stream-helper/setup', {});
    if (r && r.ok) {
        var created = (r.sources_created || []).join(', ') || 'nothing new';
        addLog('Stream Helper scene ready (' + created + ')');
    } else if (r) {
        addLog('Stream Helper setup failed: ' + (r.err || 'unknown'));
    }
    try {
        var d = await apiGet('/api/stream-helper/status');
        if (d) _renderStreamHelperStatus(d);
    } catch (e) {}
}


function _renderHotkeyStatus(h) {
    var box = document.getElementById('hotkey-status');
    if (!box) return;
    if (!h.running) {
        box.innerHTML = '<span style="color:var(--text-tertiary)">Hotkey listener not running</span>';
        return;
    }
    var bound = (h.bindings || []).map(function(b) {
        return '<span style="color:var(--accent-bright);font-family:var(--font-mono)">' + escHtml(b.name) + '</span> ' + escHtml(b.hotkey);
    }).join(' · ');
    var err = h.last_err ? '<br><span style="color:var(--warning)">' + escHtml(h.last_err) + '</span>' : '';
    box.innerHTML = '<span style="color:var(--text-tertiary)">Registered: </span>' + (bound || '(none)') + err;
}

// v3.3.0-beta.3 — index media files in a global registry so onclick
// handlers can refer to them by index (no fragile path-quoting in
// HTML attributes).  Previous approach inlined the path into the
// onclick string with backslash escaping that occasionally lost a
// quote and broke the click.
var _captureMediaRegistry = { clips: [], screenshots: [] };
// v3.3.0-beta.5: cache the last-rendered signature for each bucket so
// the 6 s status poll doesn't blow away and rebuild the entire DOM
// when nothing actually changed.  Capture status fires every 6 s; if
// the user never saves a clip, every one of those polls was tearing
// down + recreating the recent-clips card.  Diff first, render only
// on real changes.
var _captureRenderSig = { clips: '', screenshots: '' };

function _mediaSignature(items) {
    if (!items || !items.length) return '';
    // length + first item's mtime/size is enough — any new save lands
    // at the top with a fresh mtime, deletions drop the length.
    var first = items[0] || {};
    return items.length + '|' + (first.mtime || 0) + '|' + (first.size || 0) + '|' + (first.name || '');
}

function _renderRecentClips(clips) {
    _captureMediaRegistry.clips = clips || [];
    var box = document.getElementById('recent-clips-list');
    if (!box) return;
    var sig = _mediaSignature(clips);
    if (sig === _captureRenderSig.clips) return;  // no change — skip DOM work
    _captureRenderSig.clips = sig;
    if (!clips.length) {
        box.innerHTML = '<div class="empty-state" style="padding:14px">No clips saved yet — turn on the buffer, play, hit your hotkey.</div>';
        return;
    }
    var html = '';
    clips.forEach(function(c, i) {
        var date = new Date((c.mtime || 0) * 1000);
        var mb = ((c.size || 0) / (1024 * 1024)).toFixed(1);
        html += '<div style="padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-elevated)">' +
                  '<div style="font-family:var(--font-mono);font-size:12px;color:var(--text-bright);word-break:break-all">' + escHtml(c.name) + '</div>' +
                  '<div style="margin-top:6px;font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono)">' + mb + ' MB · ' + date.toLocaleString() + '</div>' +
                  '<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">' +
                    '<button class="btn btn-sm" onclick="captureMediaAction(\'clips\',' + i + ',\'launch\')">▶ Play</button>' +
                    '<button class="btn btn-sm" onclick="captureMediaAction(\'clips\',' + i + ',\'reveal\')">Show in folder</button>' +
                    '<button class="btn btn-sm btn-danger" style="margin-left:auto" onclick="captureMediaAction(\'clips\',' + i + ',\'delete\')">Delete</button>' +
                  '</div>' +
                '</div>';
    });
    box.innerHTML = html;
}

function _renderRecentScreenshots(shots) {
    _captureMediaRegistry.screenshots = shots || [];
    var box = document.getElementById('recent-screenshots-list');
    if (!box) return;
    var sig = _mediaSignature(shots);
    if (sig === _captureRenderSig.screenshots) return;  // no change — skip DOM work
    _captureRenderSig.screenshots = sig;
    if (!shots.length) {
        box.innerHTML = '<div class="empty-state" style="padding:14px">No screenshots taken yet.</div>';
        return;
    }
    var html = '';
    shots.forEach(function(s, i) {
        var date = new Date((s.mtime || 0) * 1000);
        var kb = Math.round((s.size || 0) / 1024);
        html += '<div style="padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-elevated)">' +
                  '<div style="font-family:var(--font-mono);font-size:12px;color:var(--text-bright);word-break:break-all">' + escHtml(s.name) + '</div>' +
                  '<div style="margin-top:6px;font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono)">' + kb + ' KB · ' + date.toLocaleString() + '</div>' +
                  '<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">' +
                    '<button class="btn btn-sm" onclick="captureMediaAction(\'screenshots\',' + i + ',\'launch\')">🖼 Open</button>' +
                    '<button class="btn btn-sm" onclick="captureMediaAction(\'screenshots\',' + i + ',\'reveal\')">Show in folder</button>' +
                    '<button class="btn btn-sm btn-danger" style="margin-left:auto" onclick="captureMediaAction(\'screenshots\',' + i + ',\'delete\')">Delete</button>' +
                  '</div>' +
                '</div>';
    });
    box.innerHTML = html;
}

async function captureMediaAction(bucket, idx, action) {
    var item = (_captureMediaRegistry[bucket] || [])[idx];
    if (!item || !item.path) {
        addLog('Capture action failed: item ' + bucket + '[' + idx + '] not in registry');
        return;
    }
    var path = item.path;
    try {
        if (action === 'delete') {
            if (!confirm('Delete ' + path + '?')) return;
            var ep = (bucket === 'clips') ? '/api/clipper/delete' : '/api/screenshot/delete';
            var r = await apiPost(ep, { path: path });
            if (r && r.ok) {
                addLog('Deleted ' + path);
                loadCaptureStatus();
            } else {
                addLog('Delete failed: ' + ((r && r.err) || 'unknown'));
            }
            return;
        }
        // 'launch' opens in the default app; 'reveal' opens explorer at parent
        var r = await apiPost('/api/util/open-path', {
            path: path,
            mode: action,
        });
        if (!r || !r.ok) {
            addLog('Open failed: ' + ((r && r.err) || 'unknown'));
        }
    } catch (e) {
        addLog('Capture action error: ' + (e && e.message || e));
    }
}

async function toggleClipperEnabled(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    await apiPost('/api/clipper/settings', { enabled: newOn });
    if (newOn) {
        var r = await apiPost('/api/clipper/start', {});
        if (r && !r.ok) addLog('Clipper start failed: ' + (r.err || 'unknown'));
    } else {
        await apiPost('/api/clipper/stop', {});
    }
    setTimeout(loadCaptureStatus, 500);
}

async function saveClipNow() {
    var r = await apiPost('/api/clipper/save', {});
    if (r && r.ok) {
        addLog('Clip saved: ' + (r.path || 'unknown') + ' (' + Math.round((r.size || 0) / (1024*1024)) + ' MB)');
        loadCaptureStatus();
    } else {
        addLog('Save clip failed: ' + ((r && r.err) || 'unknown'));
    }
}

async function takeScreenshotNow(target) {
    var r = await apiPost('/api/screenshot/take', { target: target || 'foreground' });
    if (r && r.ok) {
        addLog('Screenshot saved: ' + (r.path || ''));
        loadCaptureStatus();
    } else {
        addLog('Screenshot failed: ' + ((r && r.err) || 'unknown'));
    }
}

async function saveClipperSettings(extra) {
    var $ = function(id) { return document.getElementById(id); };
    var body = {
        max_buffer_seconds:   parseInt($('clip-max-buffer').value, 10) || 60,
        buffer_seconds:       parseInt($('clip-duration').value, 10) || 30,
        fps:                  parseInt($('clip-fps').value, 10) || 30,
        encoder:              $('clip-encoder').value || 'auto',
        bitrate_kbps:         parseInt($('clip-bitrate').value, 10) || 12000,
        capture_target:       $('clip-target').value || 'auto-follow',
        mic_device:           ($('clip-mic-device') ? $('clip-mic-device').value : '') || '',
        // v3.3.0-beta.4: system_audio_device removed from UI — auto-detected
        // on every clipper start (Voicemeeter > VB-CABLE > virtual-audio-
        // capturer > Stereo Mix priority).  Power users can still override
        // by editing clipper_settings.json's system_audio_device field.
    };
    // Merge any overrides (e.g. from toggles passing { mic_enabled: true })
    if (extra) Object.keys(extra).forEach(function(k) { body[k] = extra[k]; });
    var r = await apiPost('/api/clipper/settings', body);
    if (r && r.ok) {
        if (r.hotkey_warning) addLog('Hotkey warning: ' + r.hotkey_warning);
        // If the buffer is running and the user changed capture target /
        // resolution / audio, the backend won't auto-restart — we ping
        // a restart cycle so settings take effect without UI fiddling.
        if (r.settings && r.settings.enabled) {
            // soft refresh
            setTimeout(loadCaptureStatus, 300);
        } else {
            loadCaptureStatus();
        }
    } else {
        addLog('Settings save failed: ' + ((r && r.err) || 'unknown'));
    }
}

async function toggleClipperMic(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    await saveClipperSettings({ mic_enabled: newOn });
}

async function toggleClipperSysAudio(el) {
    var newOn = !el.classList.contains('on');
    el.classList.toggle('on');
    await saveClipperSettings({ system_audio_enabled: newOn });
}

// ─── Click-to-bind hotkey UI ──────────────────────────────────────
// User clicks a button; we listen for the next keydown event and
// translate it to a "Ctrl+Shift+F10"-style combo string the backend
// can register with Win32 RegisterHotKey.  Esc cancels, Backspace
// clears the binding entirely.

var _hotkeyCapture = null;

function startCaptureHotkey(name) {
    if (_hotkeyCapture) {
        document.removeEventListener('keydown', _hotkeyCapture, true);
        _hotkeyCapture = null;
    }
    var modal = document.getElementById('hotkey-modal');
    var label = document.getElementById('hotkey-modal-target');
    var preview = document.getElementById('hotkey-modal-preview');
    if (label) label.textContent = (name === 'save_clip') ? 'Save clip' : 'Screenshot';
    if (preview) preview.textContent = '—';
    if (modal) {
        modal.style.display = 'flex';
        modal.style.alignItems = 'center';
        modal.style.justifyContent = 'center';
    }

    _hotkeyCapture = function(e) {
        e.preventDefault();
        e.stopPropagation();
        // Ignore lone modifier presses — wait for the real key
        if (['Control', 'Alt', 'Shift', 'Meta', 'OS'].indexOf(e.key) >= 0) {
            // Show the modifier in preview so user knows we registered it
            if (preview) preview.textContent = _formatHotkey(e, true);
            return;
        }
        if (e.key === 'Escape') {
            _closeHotkeyModal();
            return;
        }
        if (e.key === 'Backspace') {
            // Clear binding
            _closeHotkeyModal();
            var body = {};
            body[name === 'save_clip' ? 'save_hotkey' : 'screenshot_hotkey'] = '';
            apiPost('/api/clipper/settings', body).then(function() {
                loadCaptureStatus();
            });
            return;
        }
        var combo = _formatHotkey(e, false);
        if (!combo) return;
        _closeHotkeyModal();
        // Save the new binding
        var body = {};
        body[name === 'save_clip' ? 'save_hotkey' : 'screenshot_hotkey'] = combo;
        apiPost('/api/clipper/settings', body).then(function(r) {
            if (r && r.ok) {
                addLog('Hotkey for ' + name + ' set to ' + combo);
                if (r.hotkey_warning) addLog('⚠ ' + r.hotkey_warning);
            } else {
                addLog('Hotkey save failed: ' + ((r && r.err) || 'unknown'));
            }
            loadCaptureStatus();
        });
    };
    document.addEventListener('keydown', _hotkeyCapture, true);
}

function _formatHotkey(e, partial) {
    var parts = [];
    if (e.ctrlKey)  parts.push('Ctrl');
    if (e.altKey)   parts.push('Alt');
    if (e.shiftKey) parts.push('Shift');
    if (e.metaKey)  parts.push('Win');
    if (partial) return parts.join('+') + (parts.length ? '+…' : '…');

    var key = e.key;
    if (['Control','Alt','Shift','Meta','OS'].indexOf(key) >= 0) return '';
    // Normalise to canonical names
    if (key === ' ')           key = 'Space';
    else if (key === 'Enter')  key = 'Enter';
    else if (key === 'Tab')    key = 'Tab';
    else if (key === 'ArrowLeft')  key = 'Left';
    else if (key === 'ArrowRight') key = 'Right';
    else if (key === 'ArrowUp')    key = 'Up';
    else if (key === 'ArrowDown')  key = 'Down';
    else if (key.startsWith('F') && key.length <= 3) key = key.toUpperCase();
    else if (key.length === 1) key = key.toUpperCase();

    parts.push(key);
    return parts.join('+');
}

function _closeHotkeyModal() {
    var modal = document.getElementById('hotkey-modal');
    if (modal) modal.style.display = 'none';
    if (_hotkeyCapture) {
        document.removeEventListener('keydown', _hotkeyCapture, true);
        _hotkeyCapture = null;
    }
}

async function clearCaptureHotkey(name) {
    var body = {};
    body[name === 'save_clip' ? 'save_hotkey' : 'screenshot_hotkey'] = '';
    var r = await apiPost('/api/clipper/settings', body);
    if (r && r.ok) {
        addLog('Hotkey for ' + name + ' cleared');
        loadCaptureStatus();
    }
}

async function deleteClip(path) {
    if (!confirm('Delete ' + path + '?')) return;
    await apiPost('/api/clipper/delete', { path: path });
    loadCaptureStatus();
}

async function deleteScreenshot(path) {
    if (!confirm('Delete ' + path + '?')) return;
    await apiPost('/api/screenshot/delete', { path: path });
    loadCaptureStatus();
}

async function openInExplorer(path) {
    // Hand off to the Flask backend which uses explorer /select for
    // files or os.startfile for folders.  v3.3.0-beta.3: was firing
    // and forgetting — silent failures gave the impression nothing
    // was happening.  Now we log success / failure into the in-app
    // log so the user can see what's going on.
    if (!path) {
        addLog('Open failed: no path');
        return;
    }
    try {
        var r = await apiPost('/api/util/open-path', { path: path });
        if (r && r.ok) {
            addLog('Opened ' + path);
        } else {
            addLog('Open failed: ' + ((r && r.err) || 'unknown'));
        }
    } catch (e) {
        addLog('Open failed: ' + (e && e.message || e));
    }
}

// Wire up the capture page to start polling when navigated to.
//
// v3.3.0-beta.2 bugfix: this used to call its captured ref
// `_origSwitchPage`, but the existing switchPage wrapper around line
// 7628 *also* uses that exact variable name with `var`.  Because var
// declarations share a single module-scope binding, my reassignment
// of `_origSwitchPage = switchPage` was overwriting the original
// wrapper's saved reference — which meant when the page-7628 wrapper
// called `_origSwitchPage(page)`, it called ITSELF, blowing the stack
// with "Maximum call stack size exceeded".  Different name = different
// binding = no recursion.
var _captureWrapPriorSwitchPage = (typeof switchPage === 'function') ? switchPage : null;
if (_captureWrapPriorSwitchPage) {
    switchPage = function(name) {
        _captureWrapPriorSwitchPage(name);
        if (name === 'capture') {
            loadCaptureStatus();
            if (!_capturePollTimer) {
                // v3.3.0-beta.5: was 4 s.  Bumped to 6 s — capture
                // status is a "is the buffer still rolling" indicator,
                // not a chart; the user perceives no difference at 6 s
                // and we cut Flask hits by 33%.  Also: clear interval
                // when the user navigates away (not just no-op the
                // callback) so we stop firing wakeups entirely.
                _capturePollTimer = setInterval(function() {
                    if (_pollSkipIfHidden()) return;
                    if (currentPage !== 'capture') {
                        clearInterval(_capturePollTimer);
                        _capturePollTimer = null;
                        return;
                    }
                    loadCaptureStatus();
                }, 6000);
            }
        }
    };
}

// ═══════════════════════════════════════════════════════════════
// beta.17 — Disk Analyzer (WizTree-style)
// ═══════════════════════════════════════════════════════════════
// Backend lives in core/disk_analyzer.py and exposes:
//   POST /api/cleaner/disk/scan       — start scan
//   GET  /api/cleaner/disk/status     — progress + final summary
//   GET  /api/cleaner/disk/result     — drill-down tree (path + depth)
//   POST /api/cleaner/disk/cancel     — abort
//   POST /api/cleaner/disk/delete     — delete a list of paths
//   GET  /api/cleaner/disk/drives     — drive list
//
// UI state in `_da` — drive list, current path, last node, selected
// paths.  Treemap is squarified (Bruls/Huijbregts/van Wijk).

var _da = {
    drives:        [],
    scanId:        null,
    scanRunning:   false,
    pollTimer:     null,
    currentPath:   '',
    currentNode:   null,
    pathStack:     [],
    tab:           'folders',
    selected:      {},
    initialDriveTotalUsedGB: 0,
};

async function diskAnalyzerRefresh() {
    var sel = document.getElementById('da-drive-select');
    if (!sel) return;
    try {
        var r = await apiGet('/api/cleaner/disk/drives');
        if (!r || !r.ok) return;
        _da.drives = r.drives || [];
        sel.innerHTML = '';
        _da.drives.forEach(function(d) {
            var opt = document.createElement('option');
            opt.value = d.drive;
            opt.textContent = d.drive
                + (d.label ? '  (' + d.label + ')' : '')
                + ' — ' + d.free_gb + '/' + d.total_gb + ' GB free'
                + ' · ' + d.used_pct + '% used';
            sel.appendChild(opt);
        });
    } catch (e) { /* offline */ }
}

async function diskAnalyzerStart() {
    var sel = document.getElementById('da-drive-select');
    if (!sel || !sel.value) { showWarnToast('Pick a drive first'); return; }
    var drive = sel.value;
    var driveInfo = _da.drives.find(function(d){ return d.drive === drive; });
    _da.initialDriveTotalUsedGB = driveInfo
        ? (driveInfo.total_gb - driveInfo.free_gb) : 0;
    _daResetSelection();
    var r = await apiPost('/api/cleaner/disk/scan', { drive: drive });
    if (!r || !r.ok) {
        showErrorToast('Could not start scan: ' + ((r && r.err) || 'unknown'));
        return;
    }
    _da.scanId = r.scan_id;
    _da.scanRunning = true;
    document.getElementById('da-progress').style.display = '';
    document.getElementById('da-result').style.display   = 'none';
    document.getElementById('da-cancel-btn').style.display = '';
    _daSetProgress({label: 'Scanning ' + drive + '…', counters: '0 files · 0 GB'});
    _da.pollTimer = setInterval(_daPoll, 500);
}

async function diskAnalyzerCancel() {
    await apiPost('/api/cleaner/disk/cancel', {});
}

async function _daPoll() {
    var s;
    try { s = await apiGet('/api/cleaner/disk/status'); }
    catch (e) { return; }
    if (!s) return;
    var p = s.progress || {};
    var bytesGB = (p.bytes || 0) / (1024 * 1024 * 1024);
    _daSetProgress({
        label:    s.state === 'cancelling' ? 'Cancelling…' : 'Scanning ' + (s.drive || '') + '…',
        counters: (p.files || 0).toLocaleString() + ' files · '
                  + bytesGB.toFixed(2) + ' GB',
        current:  p.current || '',
        bytesGB:  bytesGB,
    });
    if (s.state === 'done') {
        _daScanFinished(true);
    } else if (s.state === 'cancelled') {
        _daScanFinished(false);
        addLog('Disk scan cancelled.');
    } else if (s.state === 'error') {
        _daScanFinished(false);
        showErrorToast('Disk scan failed: ' + (s.error || 'unknown'));
    }
}

function _daScanFinished(ok) {
    if (_da.pollTimer) { clearInterval(_da.pollTimer); _da.pollTimer = null; }
    _da.scanRunning = false;
    document.getElementById('da-progress').style.display    = 'none';
    document.getElementById('da-cancel-btn').style.display  = 'none';
    _da.pathStack = [];
    if (!ok) return;
    diskAnalyzerOpen('');
}

function _daSetProgress(o) {
    var l = document.getElementById('da-progress-label');
    var c = document.getElementById('da-progress-counters');
    var p = document.getElementById('da-progress-current');
    var bar = document.getElementById('da-progress-fill');
    if (l && o.label) l.textContent = o.label;
    if (c && o.counters) c.textContent = o.counters;
    if (p) p.textContent = o.current || '';
    if (bar) {
        var pct = 50;
        if (_da.initialDriveTotalUsedGB > 0 && o.bytesGB) {
            pct = Math.min(95, Math.max(5, (o.bytesGB / _da.initialDriveTotalUsedGB) * 100));
        }
        bar.style.width = pct + '%';
    }
}

async function diskAnalyzerOpen(path) {
    var url = '/api/cleaner/disk/result?depth=2'
              + (path ? '&path=' + encodeURIComponent(path) : '');
    var r = await apiGet(url);
    if (!r || !r.ok) {
        showErrorToast('Could not load result: ' + ((r && r.err) || 'unknown'));
        return;
    }
    _da.currentPath = (r.node && r.node.path) || path;
    _da.currentNode = r;
    document.getElementById('da-result').style.display = '';
    var bc = document.getElementById('da-breadcrumb');
    bc.textContent = _da.currentPath || '(drive root)';
    var stats = document.getElementById('da-result-stats');
    var n = r.node || {};
    stats.textContent =
        (n.file_count || 0).toLocaleString() + ' files · '
        + (n.subdir_count || 0).toLocaleString() + ' subdirs · '
        + _daFormatBytes(n.size || 0) + ' total'
        + (r.scan_seconds ? '  (scanned in ' + r.scan_seconds + 's)' : '');
    document.getElementById('da-up-btn').style.display =
        (_da.pathStack.length > 0) ? '' : 'none';
    _daRenderTreemap(n);
    _daRenderTable();
}

// beta.20 — curated 12-color palette.  Each top-level folder gets a
// stable hue based on a name hash, so the same folder shows the same
// color across drill-down levels.  Lightness ramps with size rank
// (larger = darker base, lighter shimmer on top) so the eye can
// compare without reading numbers.
var _DA_PALETTE = [
    [ 90, 145, 200],   // muted azure
    [110, 175, 130],   // sage green
    [200, 140,  95],   // burnt amber
    [170, 120, 190],   // dusty violet
    [205, 120, 120],   // rose
    [195, 180,  95],   // mustard
    [105, 195, 175],   // teal
    [140, 150, 200],   // periwinkle
    [180, 140, 110],   // tan
    [150, 155, 165],   // graphite
    [200, 145, 175],   // mauve
    [140, 180, 110],   // olive
];

function _daHashHue(name) {
    var h = 0;
    var s = String(name || '');
    for (var i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    return Math.abs(h) % _DA_PALETTE.length;
}

function _daColorFor(name, idx, total, depth) {
    var base = _DA_PALETTE[_daHashHue(name)];
    // Larger rect (lower idx) → darker.  Smaller → lighter.  Scale
    // narrow so colors stay readable in dark mode.
    var lightShift = (idx / Math.max(1, total - 1)) * 18 - 8;  // -8..+10
    var depthShift = (depth || 0) * -4;
    function clamp(n) { return Math.max(10, Math.min(245, Math.round(n))); }
    var r = clamp(base[0] + lightShift + depthShift);
    var g = clamp(base[1] + lightShift + depthShift);
    var b = clamp(base[2] + lightShift + depthShift);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
}

function _daRenderTreemap(node) {
    var svg = document.getElementById('da-treemap');
    var tip = document.getElementById('da-treemap-tooltip');
    if (!svg || !node || !node.children) {
        if (svg) svg.innerHTML = '';
        return;
    }
    var wrap = document.getElementById('da-treemap-wrap');
    var W = wrap.clientWidth || 800;
    var H = wrap.clientHeight || 340;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    // Inject SVG <defs> for filters used by every cell — done once
    // and reused via filter URL refs (so we don't bloat the DOM with
    // hundreds of copies).
    svg.innerHTML =
        '<defs>'
        + '<filter id="da-cell-shadow" x="-5%" y="-5%" width="110%" height="110%">'
            + '<feDropShadow dx="0" dy="1" stdDeviation="1.2" flood-opacity="0.45"/>'
        + '</filter>'
        + '<linearGradient id="da-cell-gloss" x1="0%" y1="0%" x2="0%" y2="100%">'
            + '<stop offset="0%" stop-color="rgba(255,255,255,0.14)"/>'
            + '<stop offset="50%" stop-color="rgba(255,255,255,0.02)"/>'
            + '<stop offset="100%" stop-color="rgba(0,0,0,0.18)"/>'
        + '</linearGradient>'
        + '</defs>';
    var entries = (node.children || []).filter(function(c){ return c.size > 0; });
    if (!entries.length) {
        var em = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        em.setAttribute('x', W/2); em.setAttribute('y', H/2);
        em.setAttribute('fill', '#7a7a82');
        em.setAttribute('font-size', '13');
        em.setAttribute('text-anchor', 'middle');
        em.setAttribute('font-family', 'Segoe UI, system-ui, sans-serif');
        em.textContent = '(empty)';
        svg.appendChild(em);
        return;
    }
    var total = entries.reduce(function(a,b){ return a + b.size; }, 0);
    if (total <= 0) return;
    var rects = _daSquarify(entries.map(function(c){
        return { ref: c, size: c.size };
    }), { x: 0, y: 0, w: W, h: H }, total);

    rects.forEach(function(r, i) {
        var c = r.ref;
        var pct = (c.size / total) * 100;
        var fill = _daColorFor(c.name, i, rects.length, 0);
        var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.setAttribute('class', 'da-cell');
        g.style.cursor = 'pointer';
        g.onclick = function() { _daDrillInto(c); };
        g.onmouseenter = function() {
            tip.style.display = 'block';
            tip.innerHTML = '<b>' + _daEsc(c.name) + '</b><br>'
                + _daFormatBytes(c.size) + ' &nbsp;·&nbsp; ' + pct.toFixed(1) + '% of ' + _daEsc(node.name || 'this folder');
            highlight.setAttribute('opacity', '1');
        };
        g.onmousemove = function(e) {
            var bbox = wrap.getBoundingClientRect();
            var x = e.clientX - bbox.left + 12, y = e.clientY - bbox.top + 12;
            if (x + 220 > W) x = W - 220;
            if (y + 60  > H) y = H - 60;
            tip.style.left = x + 'px'; tip.style.top = y + 'px';
        };
        g.onmouseleave = function() {
            tip.style.display = 'none';
            highlight.setAttribute('opacity', '0');
        };

        // Base rect: rounded corners + soft drop shadow
        var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        var pad = 1.5;     // gap between cells for that "tiled" look
        var rx  = Math.min(4, Math.min(r.w, r.h) / 6);
        rect.setAttribute('x', r.x + pad);
        rect.setAttribute('y', r.y + pad);
        rect.setAttribute('width',  Math.max(0, r.w - pad*2));
        rect.setAttribute('height', Math.max(0, r.h - pad*2));
        rect.setAttribute('rx', rx);
        rect.setAttribute('ry', rx);
        rect.setAttribute('fill', fill);
        rect.setAttribute('stroke', 'rgba(0,0,0,0.35)');
        rect.setAttribute('stroke-width', '0.5');
        rect.setAttribute('filter', 'url(#da-cell-shadow)');
        g.appendChild(rect);

        // Gloss overlay — subtle top-light / bottom-dark gradient.
        var gloss = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        gloss.setAttribute('x', r.x + pad);
        gloss.setAttribute('y', r.y + pad);
        gloss.setAttribute('width',  Math.max(0, r.w - pad*2));
        gloss.setAttribute('height', Math.max(0, r.h - pad*2));
        gloss.setAttribute('rx', rx);
        gloss.setAttribute('ry', rx);
        gloss.setAttribute('fill', 'url(#da-cell-gloss)');
        gloss.setAttribute('pointer-events', 'none');
        g.appendChild(gloss);

        // Hover highlight — appears when you mouse over, fades out.
        var highlight = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        highlight.setAttribute('x', r.x + pad);
        highlight.setAttribute('y', r.y + pad);
        highlight.setAttribute('width',  Math.max(0, r.w - pad*2));
        highlight.setAttribute('height', Math.max(0, r.h - pad*2));
        highlight.setAttribute('rx', rx);
        highlight.setAttribute('ry', rx);
        highlight.setAttribute('fill', 'none');
        highlight.setAttribute('stroke', 'rgba(255,255,255,0.55)');
        highlight.setAttribute('stroke-width', '1.5');
        highlight.setAttribute('opacity', '0');
        highlight.setAttribute('pointer-events', 'none');
        highlight.style.transition = 'opacity 120ms ease';
        g.appendChild(highlight);

        // Labels.  Two lines: name + size + pct, scaled to cell size.
        if (r.w > 56 && r.h > 22) {
            var nameTxt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            var nameSize = Math.min(15, Math.max(10, Math.floor(r.w / 14)));
            nameTxt.setAttribute('x', r.x + 8);
            nameTxt.setAttribute('y', r.y + nameSize + 4);
            nameTxt.setAttribute('fill', '#ffffff');
            nameTxt.setAttribute('font-size', nameSize);
            nameTxt.setAttribute('font-family', 'Segoe UI, system-ui, sans-serif');
            nameTxt.setAttribute('font-weight', '600');
            nameTxt.style.pointerEvents = 'none';
            nameTxt.style.textShadow = '0 1px 2px rgba(0,0,0,0.6)';
            var maxChars = Math.floor((r.w - 16) / (nameSize * 0.55));
            nameTxt.textContent = c.name.length > maxChars
                ? c.name.slice(0, Math.max(2, maxChars - 1)) + '…'
                : c.name;
            g.appendChild(nameTxt);
            if (r.h > nameSize + 18) {
                var subSize = Math.min(12, Math.max(9, nameSize - 3));
                var subTxt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                subTxt.setAttribute('x', r.x + 8);
                subTxt.setAttribute('y', r.y + nameSize + subSize + 8);
                subTxt.setAttribute('fill', 'rgba(255,255,255,0.85)');
                subTxt.setAttribute('font-size', subSize);
                subTxt.setAttribute('font-family', 'Consolas, "JetBrains Mono", monospace');
                subTxt.style.pointerEvents = 'none';
                subTxt.textContent = _daFormatBytes(c.size) + '  ·  ' + pct.toFixed(1) + '%';
                g.appendChild(subTxt);
            }
        }
        svg.appendChild(g);
    });
}

// Squarified treemap (Bruls/Huijbregts/van Wijk 1999).  Produces
// rectangles with near-square aspect ratios — much easier to read
// than a strip-and-slice layout.
function _daSquarify(items, rect, total) {
    var out = [];
    var sumArea = rect.w * rect.h;
    var scale = sumArea / total;
    var pending = items.slice();
    var row = [];
    var x = rect.x, y = rect.y, w = rect.w, h = rect.h;

    function worst(rowItems, length) {
        var sizes = rowItems.map(function(it){ return it.size * scale; });
        var rsum = sizes.reduce(function(a, b){ return a + b; }, 0);
        var rmax = Math.max.apply(null, sizes);
        var rmin = Math.min.apply(null, sizes);
        var l2 = length * length;
        return Math.max((l2 * rmax) / (rsum * rsum),
                        (rsum * rsum) / (l2 * rmin));
    }

    function layoutRow(rowItems, length, isHorizontal) {
        var rsum = rowItems.reduce(function(a, b){ return a + b.size * scale; }, 0);
        var thickness = rsum / length;
        var cx = x, cy = y;
        for (var i = 0; i < rowItems.length; i++) {
            var area = rowItems[i].size * scale;
            var side = area / thickness;
            if (isHorizontal) {
                out.push({ ref: rowItems[i].ref, size: rowItems[i].size,
                            x: cx, y: cy, w: side, h: thickness });
                cx += side;
            } else {
                out.push({ ref: rowItems[i].ref, size: rowItems[i].size,
                            x: cx, y: cy, w: thickness, h: side });
                cy += side;
            }
        }
        return thickness;
    }

    while (pending.length) {
        var length = Math.min(w, h);
        var isHorizontal = (h <= w);
        if (!row.length) {
            row.push(pending.shift());
            continue;
        }
        var trial = row.concat([pending[0]]);
        if (worst(row, length) >= worst(trial, length)) {
            row = trial;
            pending.shift();
        } else {
            var thickness = layoutRow(row, length, isHorizontal);
            if (isHorizontal) { y += thickness; h -= thickness; }
            else              { x += thickness; w -= thickness; }
            row = [];
        }
    }
    if (row.length) {
        layoutRow(row, Math.min(w, h), (h <= w));
    }
    return out;
}

function _daDrillInto(child) {
    if (!child || !child.path) return;
    if (_da.currentPath) _da.pathStack.push(_da.currentPath);
    diskAnalyzerOpen(child.path);
}

function diskAnalyzerUp() {
    var prev = _da.pathStack.pop();
    diskAnalyzerOpen(prev || '');
}

function diskAnalyzerSwitchTab(tab) {
    _da.tab = tab;
    document.querySelectorAll('#da-tabs .btn').forEach(function(b){
        if (b.getAttribute('data-da-tab') === tab) b.classList.add('active');
        else b.classList.remove('active');
    });
    _daRenderTable();
}

function _daRenderTable() {
    var head = document.getElementById('da-table-head');
    var body = document.getElementById('da-table-body');
    if (!head || !body) return;
    head.innerHTML = ''; body.innerHTML = '';
    var rows = [];
    var totalForPct = (_da.currentNode && _da.currentNode.node)
        ? _da.currentNode.node.size : 0;
    if (_da.tab === 'folders') {
        head.innerHTML = '<th style="width:32px"></th><th>Folder</th>'
                       + '<th style="width:120px;text-align:right">Size</th>'
                       + '<th style="width:90px;text-align:right">% of node</th>'
                       + '<th style="width:90px;text-align:right">Files</th>'
                       + '<th style="width:90px"></th>';
        var n = (_da.currentNode && _da.currentNode.node) || {};
        rows = (n.children || []).map(function(c){
            return { kind: 'folder', path: c.path, name: c.name,
                     size: c.size, file_count: c.file_count };
        });
    } else if (_da.tab === 'files') {
        head.innerHTML = '<th style="width:32px"></th><th>File</th>'
                       + '<th style="width:120px;text-align:right">Size</th>'
                       + '<th style="width:90px;text-align:right">Ext</th>'
                       + '<th style="width:90px"></th>';
        rows = ((_da.currentNode && _da.currentNode.top_files) || []).slice(0, 100)
            .map(function(f){
                return { kind: 'file', path: f.path,
                         name: (f.path.split(/[\\\\/]/).pop()),
                         size: f.size, ext: f.ext || '' };
            });
    } else {
        head.innerHTML = '<th>Extension</th>'
                       + '<th style="width:120px;text-align:right">Size</th>'
                       + '<th style="width:90px;text-align:right">% of node</th>';
        rows = ((_da.currentNode && _da.currentNode.extensions) || []).map(function(e){
            return { kind: 'ext', name: e.ext, size: e.bytes };
        });
    }
    if (!rows.length) {
        body.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-tertiary);padding:24px">No entries</td></tr>';
        return;
    }
    rows.forEach(function(r) {
        var tr = document.createElement('tr');
        var pct = totalForPct ? (r.size / totalForPct * 100) : 0;
        var checked = !!_da.selected[r.path];
        if (_da.tab === 'ext') {
            tr.innerHTML = '<td>' + _daEsc(r.name || '(no ext)') + '</td>'
                         + '<td style="text-align:right;font-family:var(--font-mono)">' + _daFormatBytes(r.size) + '</td>'
                         + '<td style="text-align:right;font-family:var(--font-mono)">' + pct.toFixed(1) + '%</td>';
        } else {
            var nameCell;
            if (r.kind === 'folder') {
                var idx = body.children.length;
                nameCell = '<a href="#" data-da-folder-idx="' + idx + '">' + _daEsc(r.name) + '</a>';
            } else {
                nameCell = _daEsc(r.name);
            }
            tr.innerHTML =
                '<td><input type="checkbox" data-da-row="' + _daEsc(r.path) + '" '
                  + 'data-da-size="' + r.size + '" data-da-kind="' + r.kind + '" '
                  + (checked ? 'checked' : '') + '></td>'
                + '<td style="font-family:var(--font-mono);max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" '
                  + 'title="' + _daEsc(r.path) + '">' + nameCell + '</td>'
                + '<td style="text-align:right;font-family:var(--font-mono)">' + _daFormatBytes(r.size) + '</td>';
            if (_da.tab === 'folders') {
                tr.innerHTML += '<td style="text-align:right;font-family:var(--font-mono)">' + pct.toFixed(1) + '%</td>'
                              + '<td style="text-align:right;font-family:var(--font-mono)">' + (r.file_count || 0).toLocaleString() + '</td>';
            } else {
                tr.innerHTML += '<td style="text-align:right;font-family:var(--font-mono)">' + _daEsc(r.ext || '') + '</td>';
            }
            tr.innerHTML += '<td><button class="btn btn-sm" data-da-open-path="' + _daEsc(r.path) + '">Open</button></td>';
            tr.setAttribute('data-da-path', r.path);
        }
        body.appendChild(tr);
    });
    // Wire row events (use closures so paths with quotes / backslashes
    // don't blow up inline onclick handlers).
    body.querySelectorAll('input[type=checkbox][data-da-row]').forEach(function(cb) {
        cb.addEventListener('change', _daOnRowChecked);
    });
    body.querySelectorAll('a[data-da-folder-idx]').forEach(function(a) {
        a.addEventListener('click', function(ev) {
            ev.preventDefault();
            var tr = ev.target.closest('tr');
            var p = tr && tr.getAttribute('data-da-path');
            if (p) _daDrillInto({ path: p });
        });
    });
    body.querySelectorAll('button[data-da-open-path]').forEach(function(btn) {
        btn.addEventListener('click', function(ev) {
            ev.preventDefault();
            var p = btn.getAttribute('data-da-open-path');
            if (p) _daRevealInExplorer(p);
        });
    });
    _daUpdateDeleteBar();
}

function _daOnRowChecked(e) {
    var cb = e.target;
    var path = cb.getAttribute('data-da-row');
    var size = parseInt(cb.getAttribute('data-da-size'), 10) || 0;
    var kind = cb.getAttribute('data-da-kind');
    if (cb.checked) {
        _da.selected[path] = { size: size, kind: kind };
    } else {
        delete _da.selected[path];
    }
    _daUpdateDeleteBar();
}

function _daUpdateDeleteBar() {
    var bar = document.getElementById('da-delete-bar');
    var sumEl = document.getElementById('da-delete-summary');
    if (!bar) return;
    var keys = Object.keys(_da.selected);
    if (!keys.length) { bar.style.display = 'none'; return; }
    bar.style.display = '';
    var total = 0;
    keys.forEach(function(k){ total += _da.selected[k].size || 0; });
    // beta.20 — only the path-count cap remains.  Total-size cap removed.
    var overCap = (keys.length > 100);
    sumEl.innerHTML = '<b>' + keys.length + '</b> selected · '
        + '<b>' + _daFormatBytes(total) + '</b> total'
        + (overCap
            ? ' <span style="color:#f0888c">— exceeds cap (max 100 paths per call)</span>'
            : '');
}

function diskAnalyzerClearSelection() {
    _daResetSelection();
    _daRenderTable();
}

function _daResetSelection() {
    _da.selected = {};
    _daUpdateDeleteBar();
}

async function diskAnalyzerDeleteSelected() {
    var keys = Object.keys(_da.selected);
    if (!keys.length) return;
    var total = 0;
    keys.forEach(function(k){ total += _da.selected[k].size || 0; });
    var toRecycle = document.getElementById('da-to-recycle').checked;
    if (keys.length > 100) {
        showWarnToast('Too many selected (max 100 per delete call — split into batches).');
        return;
    }
    // beta.20 — 50 GB total cap removed.  User asked for "any size";
    // games can be 100-300 GB.  Confirm dialog still shows the total
    // so a misclick is visible before commit.
    var word = toRecycle ? 'Recycle Bin (reversible)' : 'PERMANENTLY DELETE';
    if (!confirm('Delete ' + keys.length + ' item(s) totalling '
                 + _daFormatBytes(total) + '?\n\nDestination: ' + word
                 + '\n\nCore Windows files are blocked even if checked.')) return;
    var r = await apiPost('/api/cleaner/disk/delete',
        { paths: keys, to_recycle_bin: toRecycle });
    if (!r || !r.ok) {
        // beta.19 — when EVERY path got rejected, surface the actual
        // reasons so the user knows whether they tripped a real
        // safeguard or a false positive.  Also carry the rejection
        // JSON in the auto-submitted error detail so future reports
        // for this codepath include the path that caused it.
        var msg = (r && r.err) || 'unknown';
        var rej = (r && r.rejected) || [];
        var detail = '';
        if (rej.length) {
            detail = 'Rejected paths:\n' + rej.slice(0, 10).map(function(x) {
                return '  ' + (x.path || '?') + '  →  ' + (x.reason || '?');
            }).join('\n') + (rej.length > 10 ? '\n  ...(' + (rej.length - 10) + ' more)' : '');
        }
        showErrorToast('Delete failed: ' + msg, {
            kind:   'disk analyzer delete failed',
            detail: detail,
        });
        // Follow-up warn toast with the first-few rejection reasons —
        // user sees exactly what the safety filter caught.
        if (rej.length) {
            var top = rej.slice(0, 3).map(function(x){
                var p = (x.path || '?');
                var shortP = p.length > 60 ? '…' + p.slice(-58) : p;
                return shortP + '  (' + (x.reason || '?') + ')';
            }).join('\n');
            showWarnToast(top + (rej.length > 3 ? '\n…and ' + (rej.length - 3) + ' more' : ''),
                          { title: 'Why was the delete rejected?',
                            timeoutMs: 16000 });
        }
        return;
    }
    showInfoToast('Deleted ' + r.deleted + ' item(s), freed '
                  + _daFormatBytes(r.bytes_freed)
                  + (r.errors && r.errors.length ? ' (' + r.errors.length + ' error(s))' : '')
                  + (r.rejected && r.rejected.length ? ' — ' + r.rejected.length + ' rejected by safety filter' : ''),
                  { title: 'Disk analyzer', timeoutMs: 9000 });
    if (r.rejected && r.rejected.length) {
        var first = r.rejected[0];
        showWarnToast('Some paths rejected: ' + (first.reason || 'unknown')
                      + ' (and ' + (r.rejected.length-1) + ' more)',
                      { timeoutMs: 8000 });
    }
    _daResetSelection();
    diskAnalyzerOpen(_da.currentPath);
}

function _daRevealInExplorer(path) {
    apiPost('/api/util/open-path', { path: path, mode: 'reveal' })
        .catch(function(){});
}

function _daEsc(s) {
    if (typeof s !== 'string') s = String(s == null ? '' : s);
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
            .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _daFormatBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n/1048576).toFixed(1) + ' MB';
    if (n < 1099511627776) return (n/1073741824).toFixed(2) + ' GB';
    return (n/1099511627776).toFixed(2) + ' TB';
}
