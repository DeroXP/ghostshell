"""Competitive Game Advisor (beta.6).

Detects which competitive games you actually have installed (via the existing
library_scanner) and gives the research-backed, per-game latency checklist for
the IN-GAME settings Vispora cannot set for you — Reflex, Frame-Gen, display
mode, and your display's exact FPS cap.  Honest: it never claims to toggle
these; it tells you what to set and why, and is conservative about per-title
Reflex support ("enable if present") rather than asserting facts it can't verify.
"""
from core.utils import get_logger
from core import library_scanner, competitive_latency

log = get_logger("advisor")

# (substring match on the game title) -> short, high-confidence note.
# Reflex support is framed conservatively; only flagged where well-documented.
_COMPETITIVE = [
    ("valorant",        "shooter", "Has NVIDIA Reflex — turn it on. Usually CPU-bound at high FPS, so the cap matters most."),
    ("counter-strike",  "shooter", "No native Reflex (Source 2) — but it's CPU-bound, so Reflex wouldn't help anyway. Focus on the FPS cap + low settings."),
    ("cs2",             "shooter", "No native Reflex (Source 2) — CPU-bound, so the cap + low settings are your levers."),
    ("apex legends",    "shooter", "Has Reflex — enable it; often GPU-bound, so it's a real win here."),
    ("fortnite",        "shooter", "Has Reflex — enable it (bigger win at higher settings / with DLSS)."),
    ("overwatch",       "shooter", "Has Reflex — enable it."),
    ("call of duty",    "shooter", "Has Reflex — enable it."),
    ("warzone",         "shooter", "Has Reflex — enable it."),
    ("the finals",      "shooter", "Has Reflex — enable it (GPU-heavy, so it helps)."),
    ("marvel rivals",   "shooter", "Has Reflex — enable it (GPU-heavy)."),
    ("rainbow six",     "shooter", "Reflex available in recent versions — enable if present; very CPU-bound otherwise."),
    ("pubg",            "shooter", "Reflex available — enable if present."),
    ("destiny 2",       "shooter", "Reflex available — enable if present."),
    ("battlefield",     "shooter", "Enable Reflex if present."),
    ("delta force",     "shooter", "Enable Reflex if present."),
    ("xdefiant",        "shooter", "Enable Reflex if present."),
    ("splitgate",       "shooter", "Enable Reflex if present."),
    ("deadlock",        "shooter", "Source 2 — likely no Reflex; treat like CS2 (CPU-bound)."),
    ("rocket league",   "other",   "No Reflex, CPU-bound — the in-game FPS cap is your lever."),
    ("dota 2",          "moba",    "No Reflex, CPU-bound — cap + low settings."),
    ("league of legends","moba",   "No Reflex, CPU-bound — cap + low settings."),
]


def _checklist(hz, cap):
    capval = f"{cap} fps" if cap else "a few below your refresh"
    hztxt = f"your {hz} Hz" if hz else "your refresh"
    return [
        {"setting": "NVIDIA Reflex / Low Latency", "do": "On (or On + Boost)",
         "why": "The single biggest lever when GPU-bound. Enable it in the game's Video settings if present."},
        {"setting": "Frame Generation / DLSS-FG / Multi-Frame Gen", "do": "OFF",
         "why": "Adds latency — it's a smoothness feature, not a latency one. Off for competitive."},
        {"setting": "Display mode", "do": "Exclusive fullscreen or borderless",
         "why": "On Win11 (flip-model) they're equal latency; avoid plain windowed."},
        {"setting": "In-game FPS cap", "do": capval,
         "why": f"Just below {hztxt}. Use the GAME's limiter (beats a driver/RTSS cap). Only helps if you actually sustain it."},
        {"setting": "V-Sync", "do": "Off — or On only with G-Sync + the cap above",
         "why": "Off avoids buffered lag; with G-Sync, V-Sync On + the cap is the lowest-lag combo."},
        {"setting": "Graphics preset", "do": "Low / competitive",
         "why": "Staying CPU-bound = lowest latency, and higher FPS gets you to the cap."},
    ]


def advise() -> dict:
    try:
        data = library_scanner.get_cached()
        if not data.get("games"):
            data = library_scanner.scan_all()
    except Exception as e:
        log.warning(f"library scan failed: {e}")
        data = {"games": [], "total": 0}

    detected = []
    seen_pat = set()   # dedupe by franchise pattern (one "Call of Duty", not three)
    for g in data.get("games", []):
        title = (g.get("title") or "").strip()
        tl = title.lower()
        for pat, gtype, note in _COMPETITIVE:
            if pat in tl and pat not in seen_pat:
                detected.append({"title": title, "type": gtype, "note": note})
                seen_pat.add(pat)
                break

    hz = None
    cap = None
    try:
        hz = competitive_latency._primary_refresh_hz()
        cap = competitive_latency.recommended_fps_cap(hz)
    except Exception:
        pass

    return {
        "detected": detected,
        "total_installed": data.get("total", 0),
        "refresh_hz": hz,
        "fps_cap": cap,
        "checklist": _checklist(hz, cap),
    }
