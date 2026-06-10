# GhostShell v2.9.9.7

Two important changes that pave the road for the v3 development cycle.

## 🛡 Auto-OC now steps down on crash instead of giving up

**Old behaviour:** a single hard crash anywhere during Quick Tune killed the
entire session.  If your card crashed at +600 MHz core but +550 had already
been verified stable, the +550 result was discarded and memory tuning never
ran.

**New behaviour:** on a hard crash (TDR / WebGL context loss / driver hang
/ pixel artifacts) the algorithm now:

1. Records the crashing offset as the upper bound for the axis
2. Locks the last-known-stable offset as the axis winner
3. Waits 5 seconds for the GPU to recover from the TDR
4. Re-applies the locked offset and runs a 15 s sanity probe
5. **Advances to the next axis** if the sanity probe passes
6. Aborts the entire session only if EVEN the last-stable offset crashes
   during the sanity probe (genuinely unrecoverable)

So:

```
Old              New
───              ───
+0  → stable     +0  → stable
+50 → stable     +50 → stable
+100→ stable     +100→ stable
+150→ stable     +150→ stable
+200→ CRASH      +200→ CRASH
                       ↓ wait 5 s, re-apply +150, sanity probe → stable
[STOP — done]    [Lock core+150, advance to memory axis]
                 ...continues with memory tuning at core+150...
```

If you hit "GPU crash at last-stable also crashes" — extremely rare but
possible if the driver got wedged — the whole session aborts cleanly with
a clear banner explaining what happened.  No half-applied state.

This applies to **Quick Tune** in this release; **Benchmark Tune** already
walks each ladder step independently and only stops the current axis on a
crash, which is the same behaviour.

## 📡 Update channel filter

A new **Update channel** dropdown in **Settings → AUTO-UPDATE**:

- **Stable** (default — what existing v2 users will be on after upgrading)
  Only sees v2.x.x.x non-prerelease releases.  v3 alpha / beta builds are
  ignored even when their semver tag is higher.

- **Insiders** (opt-in only)
  Sees everything, including v3 prereleases.

This matters because v3 is being built as a major architectural change
(new dashboard UI, Railway-backed cloud sync, per-game profiles, RTSS
overlay, CPU OC + UV, Adaptive Tuning, etc.).  v3 alphas/betas will be
tagged as GitHub prereleases.  Existing v2 users on Stable will not be
auto-upgraded into them — your install stays on v2 until v3 ships out of
beta.  At that point the `major >= 3` filter gets relaxed and Stable
users will be offered the v3 final.

If you want to follow v3 development as it happens, switch to Insiders in
Settings.  You can switch back to Stable any time.

This release also belt-and-braces: even a v3 release that's accidentally
NOT marked prerelease will still be filtered out for Stable channel users
because of the `major >= 3` rule.  Multiple safety nets so a tag mistake
can't drag everyone into a beta.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.9 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.

After upgrading:
- If your auto-tune crashes mid-session, you'll see *"Crash at core+X — GPU
  recovering, will step down to last stable"* in the log instead of *"hard
  crash detected — auto-tune stopped"*.
- Open **Settings → AUTO-UPDATE** to see the new **Update channel** dropdown.
  It's set to *Stable* by default — leave it there unless you want to
  follow v3 betas.

## 👀 What's next

This is the last v2.9.x feature release.  v3 development starts immediately
on a separate branch and will be tagged as `v3.0.0-beta.1`, `v3.0.0-beta.2`,
etc., as Insiders-channel prereleases.  v2.x users will stay on this
branch and only see future v2.x bugfix releases (if any) until v3 final
ships.
