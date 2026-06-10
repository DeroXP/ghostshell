# GhostShell v2.9.9.8

User-facing error reporting.  When something breaks, you now see what
broke and a one-click path to file it on GitHub.

## ⚠ What's new

### Automatic error capture

Three classes of error now surface in a clear modal instead of disappearing
into the log buffer:

1. **Uncaught JavaScript exceptions** — anything thrown by frontend code
   that wasn't caught by a `try/catch` block.
2. **Unhandled promise rejections** — async errors that nobody awaited.
3. **Server errors** — when an API call returns HTTP 5xx (Flask hit an
   unhandled exception in a route) or the backend isn't reachable at all.

Each surfaces as a modal:

```
┌──────────────────────────────────────────────────────────┐
│  ⚠ Server error                                           │
│                                                           │
│  ╭─────────────────────────────────────────────────╮    │
│  │ 500 on /api/gpu/oc/apply                        │    │
│  ╰─────────────────────────────────────────────────╯    │
│                                                           │
│  ▸ Technical detail (click to expand)                    │
│                                                           │
│  GhostShell hit an unexpected error.  If this keeps      │
│  happening, please report it so we can fix it — the      │
│  report includes your hardware, recent logs, and the     │
│  error detail above (no personal data beyond what's      │
│  already in your file paths).                            │
│                                                           │
│       [Dismiss]  [Copy details]  [Report on GitHub →]    │
└──────────────────────────────────────────────────────────┘
```

### One-click GitHub issue with pre-filled body

Clicking **Report on GitHub** opens your default browser with a new-issue
form on the GhostShell repo, body pre-populated with:

- The error kind, message, and stack/detail
- GhostShell version
- OS version + build
- GPU + CPU model
- Whether running as administrator
- The last 30 log lines

…so the report is actionable without you having to type anything except
*"this is what I was doing when it crashed"*.

If the URL would exceed GitHub's ~7 KB limit (long stack traces), the body
gets truncated in the URL but the **full** body is also copied to your
clipboard automatically — paste it into the issue body once the form
opens.

### Manual reporting

Open **Settings → REPORT A PROBLEM** anytime to file an issue without
needing GhostShell to actually error first.  Useful for "this feature
behaves weirdly" reports that wouldn't trigger an exception.

### Smart de-duping

If the same error fires repeatedly within 30 seconds, only the first one
shows the modal — no spam if a tight loop keeps tripping the same bug.
The first 1.5 seconds after page load are also suppressed, so transient
"backend not ready yet" hiccups don't generate spurious modals.

## 🔌 New endpoint

`GET /api/diagnostics/snapshot` — returns the bundled context attached to
each report:
```json
{
  "app":      { "version", "name", "admin", "frozen" },
  "os":       { "platform", "release", "version", "build" },
  "hardware": { "cpu", "gpu", "ram_gb", "drives" },
  "gpu_oc":   { "current_offsets", "live", "stock_baseline", "crash_state" },
  "logs":     [...last 80 entries...]
}
```

Useful even outside of error reporting — power users / scripts can poll
this endpoint to programmatically check GhostShell's state.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.9 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.

After upgrading: nothing is visible until something errors.  When it
does, you'll see the new modal.  Or hit **Settings → REPORT A PROBLEM**
to test it manually.
