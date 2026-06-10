# GhostShell v2.9.7

Targeted fix for the autostart implementation introduced in 2.9.5/2.9.6.

## 🐛 What was broken

The earlier autostart used a Run-key entry that ran a hidden PowerShell process to `Start-Sleep` for the configured delay, then launch GhostShell. Two real problems:

1. **PowerShell sat in Task Manager** for the entire delay window. It was hidden from the user's view (no taskbar entry), but a security-conscious user would see `powershell.exe` with no parent app and right-click → End Task. **Killing it kills GhostShell's launch.**
2. **UAC prompt at every login.** Run-key entries inherit the launching user's privileges. Since GhostShell.exe is marked `uac_admin=true`, every login fired a UAC dialog — exactly the kind of repeated yes-clicking that makes UAC useless.

## ✨ The fix

v2.9.7 switches autostart to a **Windows Scheduled Task** — the same mechanism Steam, Discord, OneDrive, and most modern desktop apps use.

| | Old (v2.9.5/v2.9.6) | New (v2.9.7) |
|---|---|---|
| Storage | `HKCU\…\Run` registry value | Task Scheduler entry `\GhostShell-Autostart` |
| Delay handling | Hidden PowerShell `Start-Sleep` | Native `<Delay>PT30S</Delay>` in task XML |
| User-visible processes during wait | `powershell.exe` (killable) | None |
| UAC prompt at login | **Yes, every login** | **No** (`<RunLevel>HighestAvailable</RunLevel>`) |
| Failure resilience | Hung if PS killed | `<RestartOnFailure>` triggers retry x3 |
| Console window flash | Possible | Never |

When the user toggles autostart on:
1. We generate a Task Scheduler XML with the user's SID, the configured delay, `<Hidden>true</Hidden>`, and the action `GhostShell.exe --from-autostart`.
2. `schtasks /Create /XML` imports it.
3. The Task Scheduler service handles the wait silently — no user-visible process exists during the delay.

## 🔁 Migration

Anyone who enabled autostart on v2.9.5/v2.9.6 has a leftover entry in `HKCU\…\Run`. v2.9.7 handles this two ways:

- **On startup:** if `autostart_settings.json` says `enabled: true` but no scheduled task is registered yet, we auto-migrate by creating the task and removing the legacy Run-key entry. Zero user action required.
- **Whenever you toggle autostart in Settings:** both `enable()` and `disable()` always purge any leftover Run-key entry as a side effect, so a one-click toggle cleans everything up too.

The Settings page also surfaces a one-line warning if a stray Run-key entry is detected, in case auto-migration somehow misfires.

## 🧪 Verified live

End-to-end test on this build:

```
Pre-test  → no task exists
Enable    → {ok:true, method:"scheduled_task"}
Verify    → schtasks /Query shows the task ✓
Run-key   → automatically purged ✓
Disable   → {ok:true}
Verify    → schtasks /Query returns "cannot find" ✓
```

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |
