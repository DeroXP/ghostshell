// Updater UI controller — polls /api/status every 500 ms, renders the
// mode-aware UI (install vs update), drives the pipeline through REST
// calls, and exposes the browse/rescan helpers.

let _pollTimer = null;
let _lastPhase = null;
let _autoConfirmed = false;
let _dirPickerForced = false;   // user clicked "Change…" — keep picker open
// v1.3.0 — the install pipeline launches GhostShell at step 6.  Once
// phase=done lands, the updater has nothing left to do — close
// immediately rather than counting down 3 seconds.  The brief "Update
// complete" banner still flashes for the ~250ms transition before the
// window tears itself down, which is enough for the user to register
// "it finished".  `_autoCloseFired` keeps the call idempotent across
// the 500ms poll loop.

async function api(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    return r.json();
}

function setError(msg) {
    const box = document.getElementById('error-box');
    if (msg) {
        box.textContent = msg;
        box.style.display = '';
    } else {
        box.style.display = 'none';
    }
}

const STEP_ICONS = {
    pending: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/></svg>',
    active:  '<span class="icon spinner"></span>',
    done:    '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>',
    error:   '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>',
    skipped: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><line x1="8" y1="12" x2="16" y2="12"/></svg>',
};

function renderSteps(steps, mode) {
    const ul = document.getElementById('steps');
    ul.innerHTML = '';
    // In install mode the "close" step is irrelevant — hide it.
    for (const s of steps) {
        if (mode === 'install' && s.key === 'close') continue;
        const li = document.createElement('li');
        li.className = s.status;
        li.innerHTML =
            (STEP_ICONS[s.status] || STEP_ICONS.pending) +
            '<div class="body">' +
                '<div class="label">' + escapeHtml(s.label) + '</div>' +
                (s.detail ? '<div class="detail">' + escapeHtml(s.detail) + '</div>' : '') +
            '</div>';
        ul.appendChild(li);
    }
}

function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, m => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[m]);
}

function renderHeader(st) {
    // Title flips between Install / Update modes
    const title = document.getElementById('page-title');
    const appTitle = document.getElementById('app-title');
    if (st.mode === 'install') {
        title.textContent = 'Install Vispora';
        appTitle.textContent = 'Vispora Installer';
    } else {
        title.textContent = 'Updating Vispora';
        appTitle.textContent = 'Vispora Updater';
    }
    document.getElementById('channel-tag').textContent =
        'channel: ' + (st.channel || '— pick —');
    document.getElementById('updater-version-tag').textContent =
        'v' + (st.updater_version || '?');
}

function renderVersions(st) {
    const row = document.getElementById('versions-row');
    if (st.mode === 'install') {
        // Hide the from/to row in install mode — there's no "from".
        row.style.display = 'none';
        return;
    }
    row.style.display = '';
    document.getElementById('from-version').textContent =
        st.current_version ? 'v' + st.current_version : '— current —';
    const to = document.getElementById('to-version');
    if (st.target_version) {
        to.textContent = 'v' + st.target_version;
    } else if (st.channel_check && st.channel_check.version) {
        to.textContent = 'v' + st.channel_check.version;
    } else {
        to.textContent = 'latest on ' + (st.channel || 'channel');
    }
}

function renderInstallBanner(st) {
    const banner = document.getElementById('install-banner');
    if (st.mode === 'install') {
        banner.style.display = '';
        const detail = document.getElementById('install-banner-detail');
        if (st.scan_result && (st.scan_result.candidates || []).length) {
            detail.textContent =
                'No existing Vispora found across ' +
                st.scan_result.candidates.length +
                ' standard locations.  Pick where to install it below.';
        }
    } else {
        banner.style.display = 'none';
    }
}

function renderControls(st) {
    const running = st.phase === 'downloading' || st.phase === 'closing'
                  || st.phase === 'verifying'  || st.phase === 'installing'
                  || st.phase === 'launching';

    // Channel selector — show until we have a channel.  Hide during install.
    const chCtrl = document.getElementById('controls-channel');
    const chSel  = document.getElementById('channel-select');
    if (st.channel && chSel.value !== st.channel) {
        chSel.value = st.channel;
    }
    chCtrl.style.display = running ? 'none' : 'flex';

    // Target-dir picker — visible in install mode OR when user clicked
    // Change… in update mode.
    const dirCtrl   = document.getElementById('controls-targetdir');
    const dirInp    = document.getElementById('target-dir-input');
    const dirRO     = document.getElementById('controls-targetdir-readonly');
    const readonly  = document.getElementById('readonly-path');

    const showPicker = (st.mode === 'install' || _dirPickerForced) && !running;
    if (showPicker) {
        dirCtrl.style.display = 'flex';
        // Keep the input in sync with state (but don't clobber what the
        // user is currently typing).  Compare against last server value.
        if (!dirInp.dataset.userEdited) {
            dirInp.value = st.target_dir || '';
        }
        dirRO.style.display = 'none';
    } else if (st.mode === 'update' && st.target_dir && !running) {
        dirCtrl.style.display = 'none';
        dirRO.style.display = 'flex';
        readonly.textContent = st.target_dir;
        readonly.title = st.target_exe || st.target_dir;
    } else {
        dirCtrl.style.display = 'none';
        dirRO.style.display = 'none';
    }
}

function renderButtons(st) {
    const btnStart  = document.getElementById('btn-start');
    const btnCancel = document.getElementById('btn-cancel');
    const btnClose  = document.getElementById('btn-close');

    const running = st.phase === 'downloading' || st.phase === 'closing'
                  || st.phase === 'verifying'  || st.phase === 'installing'
                  || st.phase === 'launching';

    if (st.phase === 'done') {
        // v1.2.0 — pipeline already launched GhostShell at step 6.
        // Only show the "Close now" button so users who want to dismiss
        // immediately can; the auto-close countdown handles the rest.
        btnStart.style.display  = 'none';
        btnCancel.style.display = 'none';
        btnClose.style.display  = '';
    } else if (st.phase === 'error') {
        btnStart.style.display  = '';
        btnStart.textContent    = 'Retry';
        btnStart.disabled       = false;
        btnCancel.style.display = '';
        btnCancel.textContent   = 'Close';
        btnClose.style.display  = 'none';
    } else if (running) {
        btnStart.style.display  = '';
        btnStart.disabled       = true;
        btnCancel.style.display = '';
        btnCancel.textContent   = 'Cancel';
        btnClose.style.display  = 'none';
    } else {
        btnStart.disabled       = !(st.target_dir && st.channel);
        btnStart.style.display  = '';
        btnStart.textContent    = st.mode === 'install' ? 'Install Vispora' : 'Install Update';
        btnCancel.style.display = '';
        btnCancel.textContent   = 'Cancel';
        btnClose.style.display  = 'none';
    }
}

async function pollStatus() {
    let st;
    try { st = await api('GET', '/api/status'); }
    catch (e) { setError('Cannot reach updater backend: ' + e.message); return; }
    // v1.2.0 — once we've flipped into the success-countdown state, don't
    // let the 500ms poll loop blow it away with setError('').  The
    // countdown's setInterval re-paints the message every second.
    if (st.phase !== 'done') {
        setError(st.error || '');
    }
    renderHeader(st);
    renderInstallBanner(st);
    renderVersions(st);
    renderControls(st);
    renderSteps(st.steps, st.mode);
    renderButtons(st);
    document.getElementById('progress-fill').style.width = (st.progress || 0) + '%';

    // Auto-confirm: skip the "click Install" gate when GhostShell told
    // us to install immediately.  Only relevant in update mode — fresh
    // installs always need the user to confirm the directory.
    if (st.auto_confirm && !_autoConfirmed
        && st.mode === 'update'
        && st.target_dir && st.channel
        && st.phase === 'ready') {
        _autoConfirmed = true;
        onStart();
    }

    // v1.3.0 — install pipeline reached step 6 (launch new GhostShell)
    // and succeeded.  Tear down the updater immediately — no point
    // counting down with the user staring at a redundant window after
    // GhostShell has already taken focus.
    if (st.phase === 'done' && !_autoCloseFired) {
        _autoCloseFired = true;
        _closeUpdaterNow();
    }

    _lastPhase = st.phase;
}

function _closeUpdaterNow() {
    // v1.3.0 — drop the 3-second countdown.  Paint the success banner
    // briefly so the user sees confirmation in the transition frame,
    // then fire /api/quit.  No setInterval; no countdown text.
    const box = document.getElementById('error-box');
    if (box) {
        box.classList.add('success');
        box.innerHTML =
            '<svg class="icon" viewBox="0 0 24 24" fill="none" ' +
              'stroke="currentColor" stroke-width="3" ' +
              'style="vertical-align:middle;width:14px;height:14px;margin-right:6px">' +
              '<polyline points="20 6 9 17 4 12"/></svg>' +
            'Update complete — Vispora is launching.';
        box.style.display = '';
    }
    // Small delay so the banner has a frame to render before the
    // backend process exits underneath us.  150ms is imperceptible to
    // the user but enough for one paint cycle.
    setTimeout(function() {
        api('POST', '/api/quit', {}).catch(function() {});
    }, 150);
}

async function onStart() {
    // Make sure the user's typed install dir is committed before we
    // start the pipeline.  No-op if the input is empty / unchanged.
    const dirInp = document.getElementById('target-dir-input');
    if (dirInp && dirInp.value && dirInp.dataset.userEdited) {
        await setTargetDir();
    }
    await api('POST', '/api/start', {});
}

async function onCancel() {
    const st = await api('GET', '/api/status');
    if (st.phase === 'error' || st.phase === 'ready' || st.phase === 'done') {
        await api('POST', '/api/quit', {});
    } else {
        await api('POST', '/api/cancel', {});
    }
}

// v1.2.0 — onLaunch() removed.  GhostShell is now auto-launched by
// installer.py step 6, so there's no manual button to wire up.  The
// /api/launch-app endpoint stays as a fallback for headless scripting
// but the UI no longer surfaces it.
async function onClose() { await api('POST', '/api/quit', {}); }

async function setTargetDir() {
    const inp = document.getElementById('target-dir-input');
    const v = (inp.value || '').trim();
    if (!v) { setError('Please enter an install path'); return; }
    const r = await api('POST', '/api/set-target-dir', { target_dir: v });
    if (!r || !r.ok) {
        setError((r && r.err) || 'invalid path');
    } else {
        setError('');
        inp.dataset.userEdited = '';
    }
}

async function browseFolder() {
    const r = await api('POST', '/api/browse-folder', {});
    if (r && r.ok && r.path) {
        const inp = document.getElementById('target-dir-input');
        inp.value = r.path;
        inp.dataset.userEdited = '';
        // Server already updated its state in api/browse-folder
        pollStatus();
    } else if (r && !r.ok) {
        setError('Browse failed: ' + (r.err || 'unknown'));
    }
}

async function rescan() {
    await api('POST', '/api/rescan', {});
    _dirPickerForced = false;
    pollStatus();
}

function showDirPicker() {
    _dirPickerForced = true;
    pollStatus();
}

document.getElementById('channel-select').addEventListener('change', async (e) => {
    const v = e.target.value;
    if (!v) return;
    await api('POST', '/api/set-channel', { channel: v });
});

// Track manual edits to the target-dir input so we don't overwrite
// the user's in-progress typing with stale server state.
const _dirInputEl = document.getElementById('target-dir-input');
if (_dirInputEl) {
    _dirInputEl.addEventListener('input', () => {
        _dirInputEl.dataset.userEdited = '1';
    });
    _dirInputEl.addEventListener('blur', () => {
        // Commit edits on blur — feels natural and avoids the user
        // having to click Browse just to set a typed path.
        if (_dirInputEl.dataset.userEdited && _dirInputEl.value.trim()) {
            setTargetDir();
        }
    });
    _dirInputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            setTargetDir();
        }
    });
}

// Kick off polling
pollStatus();
_pollTimer = setInterval(pollStatus, 500);
