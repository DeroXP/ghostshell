# GhostShell v2.9.8.0

Two small but visible changes.

## 🔢 4-component versioning (`2.9.8.0`)

Switching from `MAJOR.MINOR.PATCH` (`2.9.7`) to `MAJOR.MINOR.PATCH.BUILD`
(`2.9.8.0`).  The 4-component scheme matches what most Windows apps and
GPU drivers use, gives us a dedicated build digit for hotfixes, and lines
up neatly with the NVIDIA driver versioning we display in the Settings
panel.

**Comparison logic was hardened to handle the transition cleanly.**  Both
the app updater (`updater._parse_version`) and the GPU driver updater
(`driver_updater._version_tuple`) now always pad parsed versions to
exactly 4 components, so:

| Current | Latest | Result |
|---|---|---|
| `2.9.7`     | `2.9.8.0` | update available ✓ |
| `2.9.8`     | `2.9.8.0` | up to date — **no false update** ✓ |
| `2.9.8.0`   | `2.9.8.0` | up to date ✓ |
| `2.9.8.0`   | `2.9.8.1` | update available ✓ |
| `2.9.8.1`   | `2.9.8.0` | up to date — no downgrade ✓ |

Without the padding, Python's tuple comparison would treat `(2,9,8) <
(2,9,8,0)` (shorter < longer with matching prefix), which would
incorrectly tell users on `2.9.8` that `2.9.8.0` is "newer".

All nine edge cases verified passing.

## 🧹 Network page UI compaction

The Network page used to render two separate cards stacked vertically:

> **LATENCY OPTIMIZATION**
> Disable Nagle's Algorithm, network throttling, Large Send Offload, adapter power management.
> ⬜ Disable IPv6
> [APPLY ALL NETWORK TWEAKS] [PING TEST]
>
> **ADVANCED NETWORK TWEAKS**
> Global TCP stack tuning (auto-tuning, RSS, RSC, heuristics, SYN retransmissions), …
> [NETSH TCP STACK] [DISABLE NETBIOS/TEREDO] [ENABLE DOH] [GAME PORT QOS]

Two cards, two titles, two paragraphs of explainer, two `.btn-group`s —
for closely-related network knobs.  v2.9.8.0 merges them into a single
**Latency & Network Tweaks** card with one description and one combined
button row.  Saves ~80 px of vertical real estate and reads cleaner.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ in the title bar to
check immediately. After install you'll see the new compact Network
card and the version chip in the title bar will read `v2.9.8.0`.
