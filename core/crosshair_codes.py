"""Crosshair share codes: Vispora's own, plus imports from CS2 and Valorant.

Pure Python. A Vispora code is "VXH1-" and the base64url (no padding) of the
design's compact JSON, so it round-trips exactly. CS2 and Valorant codes are
decoded from their byte and key layouts and mapped to the nearest Vispora
design. Neither game publishes how its numbers become screen pixels, so those
imports are always marked approximate.
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import math
import re

from core.crosshair_render import DEFAULT_DESIGN, normalize_design

CODE_PREFIX = "VXH1-"
MAX_CODE_BYTES = 2048

CS_ALPHABET = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhijkmnopqrstuvwxyz23456789"
_CS_INDEX = {ch: i for i, ch in enumerate(CS_ALPHABET)}
_OUTLINE_NAMES = ("none", "full", "half")

# CS2 itself refuses these codes and there is no converter, so the advice can't
# be "open it in CS2": the only ways on are a current code or the Designer.
LEGACY_V1_ERR = ("This is an old-format crosshair code. CS2 no longer accepts these, and Vispora can't "
                 "convert this one. Paste a current CS2 code (it starts with CS and has no dashes) "
                 "or build the crosshair in the Designer.")
IMAGE_WARNING = "Image crosshairs can't be shared as a code."
GAP_WARNING = "Gap is tighter than Vispora can draw, so it was set to 0."
VERTICAL_WARNING = "Vertical and horizontal lengths differ; Vispora uses one length."
OUTER_WARNING = "Outer lines aren't supported and were left out."
TOO_BIG_WARNING = "Some sizes are bigger than Vispora allows, so they were made smaller."

_ERR_EMPTY = "Paste a crosshair code first."
_ERR_TOO_LONG = "That's too long to be a crosshair code."
_ERR_UNKNOWN = "That doesn't look like a Vispora, CS2 or Valorant crosshair code."
_ERR_VISPORA = "This Vispora code is damaged. Copy it again."
_ERR_UNREADABLE = "That code couldn't be read."
_ERR_CS_NOT = "That isn't a CS2 crosshair code."
# "CS code" where the text can come from a current CS2 code or an old CSGO- one.
_ERR_CS_LENGTH = "This CS code is the wrong length. Copy it again."
_ERR_CS_CHAR = "This CS code has a character that can't be in a crosshair code. Copy it again."
_ERR_CS_DAMAGED = "This isn't a crosshair code, or it's damaged. Copy it again."
_ERR_CS_VERSION = "This CS2 code uses a format Vispora can't read yet."     # current CS2 codes only
_ERR_CS_HEIGHT = "This CS code is damaged: its screen height is 0."
_ERR_CS_EMPTY = "This CS crosshair has no lines or dot, so there's nothing to import."
_ERR_VAL_NOT = "That isn't a Valorant crosshair code."
_ERR_VAL_SHORT = "This Valorant code is cut short. Copy it again."
_ERR_VAL_VALUE = "This Valorant code has a value Vispora can't read. Copy it again."
_ERR_VAL_EMPTY = "This Valorant crosshair has no lines or dot, so there's nothing to import."

# Valorant colour index 0-7 (8 means "custom").
VALORANT_COLOURS = ("#FFFFFF", "#00FF00", "#7FFF00", "#DFFF00", "#FFFF00", "#00FFFF", "#FF00FF", "#FF0000")
_VAL_GLOBAL = {"p": 1, "c": 0, "s": 0}
_VAL_AIM = {"c": 0, "u": None, "b": 0, "h": 1, "t": 1, "o": 0.5, "d": 0, "z": 2, "a": 1, "m": 0}
# Line keys, prefixed "0" for the inner lines and "1" for the outer: (inner default, outer default).
_VAL_LINES = {"b": (1, 1), "t": (2, 2), "l": (6, 2), "v": (6, 2), "g": (0, 0), "o": (3, 10),
              "a": (0.8, 0.35), "m": (0, 1), "s": (1, 1), "f": (1, 1), "e": (1, 1)}
_VAL_SNIPER = {"d": 1, "b": 0, "c": 7, "t": None, "s": 1, "o": 0.75}
_VAL_HEX_KEYS = {"P": ("u",), "A": ("u",), "S": ("t",)}
_VAL_HEX_RE = re.compile(r"[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?")
_VAL_NUM_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")


def _err(msg: str) -> dict:
    return {"ok": False, "err": msg}


def _round(x: float) -> int:
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _hex_colour(r, g, b) -> str:
    return "#%02X%02X%02X" % (r, g, b)


# ── Vispora ───────────────────────────────────────────────────────────
def export_code(design) -> dict:
    """{"ok": True, "code": "VXH1-...", "warnings": [...]}. An image design has
    no portable form, so it exports as the default crosshair, with a warning."""
    try:
        d = normalize_design(design)
        warnings = []
        if d["kind"] == "image":
            d = copy.deepcopy(DEFAULT_DESIGN)
            warnings.append(IMAGE_WARNING)
        raw = json.dumps(d, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        code = CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return {"ok": True, "code": code, "warnings": warnings}
    except Exception:
        return _err("This crosshair couldn't be turned into a code.")


def _from_vispora(body: str) -> dict:
    if not _B64URL_RE.fullmatch(body):
        return _err(_ERR_VISPORA)
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        obj = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, RecursionError):
        return _err(_ERR_VISPORA)
    if not isinstance(obj, dict):
        return _err(_ERR_VISPORA)
    design = normalize_design(obj)
    warnings = []
    if design["kind"] == "image":
        design["kind"] = "shape"
        design["image"] = copy.deepcopy(DEFAULT_DESIGN["image"])
        warnings.append(IMAGE_WARNING)
    return {"ok": True, "source": "vispora", "design": design, "approximate": False,
            "name": "Shared crosshair", "warnings": warnings}


# ── CS2 ───────────────────────────────────────────────────────────────
def _cs_bytes(text: str):
    """(bytes, kind) from a CS share code, or (None, error)."""
    if text.startswith("CSGO-"):
        groups = text[5:].split("-")
        if len(groups) != 5 or any(len(g) != 5 for g in groups):
            return None, _ERR_CS_LENGTH
        body, size, kind = "".join(groups), 18, "legacy"
    elif text.startswith("CS"):
        body, size, kind = text[2:], 32, "cs2"
        if len(body) != 44:
            return None, _ERR_CS_LENGTH
    else:
        return None, _ERR_CS_NOT
    value = 0
    for ch in reversed(body):               # the first character is the least significant digit
        idx = _CS_INDEX.get(ch)
        if idx is None:
            return None, _ERR_CS_CHAR
        value = value * 57 + idx
    if value >> (8 * size):
        return None, _ERR_CS_DAMAGED
    data = value.to_bytes(size, "big")
    if data[0] != sum(data[1:]) % 256:
        return None, _ERR_CS_DAMAGED
    return data, kind


def _decode_cs2_v1(b: bytes) -> dict:
    bits = int.from_bytes(b[18:22], "little")
    mode = min(b[13] >> 6, 2)
    gap = int.from_bytes(b[14:16], "little", signed=True)
    return {
        "ok": True, "kind": "cs2", "version": 1,
        "style": min(b[4] & 0x1F, 9),
        "dot": bool(b[4] & 0x40),
        "t_style": bool(b[4] & 0x80),
        "follow_recoil": bool(b[4] & 0x20),
        "outline_on": mode != 0,
        "outline_mode": mode,
        "outline": _OUTLINE_NAMES[mode],
        "rgba": list(b[5:9]),
        "outline_rgba": list(b[9:13]),
        "gap": max(-3840, min(3840, gap)),
        "length": b[16],
        "thickness": min(b[13] & 0x3F, 32),
        "spread_limit": b[17],
        "split_distance": bits & 0x7F,
        "inner_alpha": min((bits >> 7) & 0x7F, 100) / 100,
        "outer_alpha": (min((bits >> 14) & 0x7F, 70) + 30) / 100,
        "split_ratio": min((bits >> 21) & 0x7F, 100) / 100,
        "scope_dot_uses_color": bool((bits >> 28) & 1),
        "scope_dot_scale": min((b[22] + 10) / 100, 2.0),
        "screen_height": b[2] | (b[3] << 8),
    }


def _decode_legacy(b: bytes, version: int) -> dict:
    bits = int.from_bytes(b[10:14], "little")
    if version == 4:
        mode = min((bits >> 28) & 3, 2)
    else:
        mode = 1 if b[2] & 0x20 else 0      # v3 only says on or off
    return {
        "ok": True, "kind": "legacy", "version": version,
        "style": b[2] & 0x0F,
        "dot": bool(b[2] & 0x40),
        "t_style": bool(b[2] & 0x80),
        "follow_recoil": bool(b[2] & 0x10),
        "outline_on": mode != 0,
        "outline_mode": mode,
        "outline": _OUTLINE_NAMES[mode],
        "rgba": list(b[3:7]),
        "outline_rgba": None,
        "gap": b[7],
        "length": b[8],
        "thickness": (bits >> 23) & 0x1F,
        "spread_limit": b[9],
        "split_distance": bits & 0x7F,
        "inner_alpha": ((bits >> 7) & 0x1F) / 20,
        "outer_alpha": (((bits >> 12) & 0xF) + 6) / 20,
        "split_ratio": ((bits >> 16) & 0x7F) / 100,
        "scope_dot_uses_color": None,
        "scope_dot_scale": None,
        "screen_height": b[14] | (b[15] << 8),
    }


def decode_cs(code) -> dict:
    """Raw fields of a CS share code ("CS" + 44 characters, or a legacy
    "CSGO-" code of version 3 or 4), or {"ok": False, "err": ...}."""
    try:
        if not isinstance(code, str):
            return _err(_ERR_CS_NOT)
        data, kind = _cs_bytes(code.strip())
        if data is None:
            return _err(kind)
        version = data[1]
        if kind == "cs2":
            if version != 1:
                return _err(_ERR_CS_VERSION)
            out = _decode_cs2_v1(data)
        else:
            if version == 1:
                return _err(LEGACY_V1_ERR)
            if version not in (3, 4):
                # The old format is finished (there is no v2), so any other
                # version is damage or foreign data, not a format to wait for.
                return _err(_ERR_CS_DAMAGED)
            out = _decode_legacy(data, version)
        if out["screen_height"] == 0:
            return _err(_ERR_CS_HEIGHT)
        return out
    except Exception:
        return _err(_ERR_UNREADABLE)


def _from_cs(dec: dict) -> dict:
    # The raw length, not the scaled one: a 1 px line on a 2400 px screen
    # scales to 0 but the game still draws it.
    if dec["length"] == 0 and not dec["dot"]:
        return _err(_ERR_CS_EMPTY)
    warnings = []
    f = 1080 / max(240, dec["screen_height"])     # these formats are in pixels at screen_height
    r, g, b, a = dec["rgba"]
    colour, opacity = _hex_colour(r, g, b), _round(a * 100 / 255)
    thickness = max(1, _round(dec["thickness"] * f))
    length = _round(dec["length"] * f)
    if dec["gap"] < 0:
        gap = 0
        warnings.append(GAP_WARNING)
    else:
        gap = _round(dec["gap"] * f)
    if thickness > 24 or length > 120 or gap > 60:
        warnings.append(TOO_BIG_WARNING)
    outline = dec["outline_rgba"] or [0, 0, 0, 255]
    d = copy.deepcopy(DEFAULT_DESIGN)
    d.update(shape="cross", color=colour, opacity=opacity, length=max(1, length), thickness=thickness, gap=gap)
    d["arms"]["top"] = not dec["t_style"]
    d["dot"] = {"on": dec["dot"], "size": thickness, "color": colour, "opacity": opacity}
    d["outline"] = {"on": dec["outline_on"], "thickness": max(1, _round(f)),
                    "color": _hex_colour(*outline[:3]), "opacity": _round(outline[3] * 100 / 255)}
    if length < 1 and dec["dot"]:
        d["shape"] = "dot"                  # no lines, only the centre dot
        d["dot"]["on"] = False
    name = "CS2 crosshair" if dec["kind"] == "cs2" else "CS crosshair"
    return {"ok": True, "source": "cs2", "design": normalize_design(d), "approximate": True,
            "name": name, "warnings": warnings}


# ── Valorant ──────────────────────────────────────────────────────────
def _val_defaults(section: str) -> dict:
    if section == "global":
        return dict(_VAL_GLOBAL)
    if section == "S":
        return dict(_VAL_SNIPER)
    out = dict(_VAL_AIM)
    if section == "P":
        out.update(f=1, s=1)
    for key, (inner, outer) in _VAL_LINES.items():
        out["0" + key] = inner
        out["1" + key] = outer
    return out


def _val_number(raw: str):
    if not _VAL_NUM_RE.fullmatch(raw):
        return None
    f = float(raw)
    return int(f) if f.is_integer() else f


def decode_valorant(code) -> dict:
    """Raw settings of a Valorant crosshair code with every key filled in:
    {"ok": True, "global": {...}, "primary": {...}, "ads": {...}, "sniper": {...}}
    or {"ok": False, "err": ...}. Unknown keys are ignored."""
    try:
        if not isinstance(code, str):
            return _err(_ERR_VAL_NOT)
        tokens = code.strip().split(";")
        if len(tokens) > 1 and tokens[-1] == "":
            tokens.pop()
        if tokens[0] != "0":
            return _err(_ERR_VAL_NOT)
        sections = {name: _val_defaults(name) for name in ("global", "P", "A", "S")}
        current = "global"
        i = 1
        while i < len(tokens):
            key = tokens[i]
            if key in ("P", "A", "S"):
                current = key
                i += 1
                continue
            if i + 1 >= len(tokens):
                return _err(_ERR_VAL_SHORT)
            raw = tokens[i + 1]
            i += 2
            table = sections[current]
            if key not in table:
                continue
            if key in _VAL_HEX_KEYS.get(current, ()):
                if not _VAL_HEX_RE.fullmatch(raw):
                    return _err(_ERR_VAL_VALUE)
                table[key] = raw.upper()
            else:
                value = _val_number(raw)
                if value is None:
                    return _err(_ERR_VAL_VALUE)
                table[key] = value
        return {"ok": True, "global": sections["global"], "primary": sections["P"],
                "ads": sections["A"], "sniper": sections["S"]}
    except Exception:
        return _err(_ERR_UNREADABLE)


def _pct(fraction) -> int:
    return max(0, min(100, _round(fraction * 100)))


def _valorant_colour(p: dict) -> str:
    if (p["c"] == 8 or p["b"] == 1) and p["u"]:
        return "#" + p["u"][:6]
    idx = p["c"]
    if isinstance(idx, int) and 0 <= idx < len(VALORANT_COLOURS):
        return VALORANT_COLOURS[idx]
    return "#FFFFFF"


def _from_valorant(dec: dict) -> dict:
    p = dec["primary"]
    warnings = []
    colour = _valorant_colour(p)
    # A part counts only if it can be seen: hidden by its toggle, a zero size or
    # an opacity that rounds to 0%. Otherwise "lines at opacity 0" would win
    # over the dot-only and outer-only cases and import an invisible crosshair.
    inner = p["0b"] == 1 and p["0l"] > 0 and p["0t"] > 0 and _pct(p["0a"]) > 0
    outer = p["1b"] == 1 and p["1l"] > 0 and p["1t"] > 0 and _pct(p["1a"]) > 0
    dot_on = p["d"] == 1 and _pct(p["a"]) > 0
    d = copy.deepcopy(DEFAULT_DESIGN)
    d["color"] = colour
    d["scale_with_resolution"] = False          # Valorant sizes are screen pixels at any resolution
    d["outline"] = {"on": p["h"] == 1, "thickness": p["t"], "color": "#000000", "opacity": _pct(p["o"])}
    d["dot"] = {"on": dot_on, "size": p["z"], "color": colour, "opacity": _pct(p["a"])}
    if inner:
        d.update(shape="cross", length=p["0l"], thickness=p["0t"], gap=p["0o"], opacity=_pct(p["0a"]))
        if p["0g"] == 1 and p["0v"] != p["0l"]:
            warnings.append(VERTICAL_WARNING)
        if outer:
            warnings.append(OUTER_WARNING)
    elif dot_on:
        d.update(shape="dot", thickness=p["z"], opacity=_pct(p["a"]))
        d["dot"]["on"] = False
        if outer:
            warnings.append(OUTER_WARNING)
    elif outer:
        # Only the outer lines show: draw them, since leaving them out leaves nothing.
        d.update(shape="cross", length=p["1l"], thickness=p["1t"], gap=p["1o"], opacity=_pct(p["1a"]))
        warnings.append("Only the outer lines show in this code, so Vispora drew them as its lines.")
        if p["1g"] == 1 and p["1v"] != p["1l"]:
            warnings.append(VERTICAL_WARNING)
    else:
        return _err(_ERR_VAL_EMPTY)
    return {"ok": True, "source": "valorant", "design": normalize_design(d), "approximate": True,
            "name": "Valorant crosshair", "warnings": warnings}


# ── import ────────────────────────────────────────────────────────────
def _looks_like_code(tok: str) -> bool:
    return tok.upper().startswith(CODE_PREFIX) or tok.startswith("CS") or tok == "0" or tok.startswith("0;")


def _unwrap(text: str) -> str:
    """The code itself from a pasted line: surrounding quotes go, and from
    something like `apply_crosshair_code CS...` the last code-like word is kept."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    words = text.split()
    if len(words) > 1:
        for word in reversed(words):
            word = word.strip("\"'`")
            if _looks_like_code(word):
                return word
    return text


def import_code(text) -> dict:
    """A design from a Vispora, CS2 or Valorant code:
    {"ok": True, "source", "design", "approximate", "name", "warnings"} or
    {"ok": False, "err": a plain sentence}. Never raises."""
    try:
        if not isinstance(text, str):
            return _err(_ERR_EMPTY)
        if len(text) > 8 * MAX_CODE_BYTES:
            return _err(_ERR_TOO_LONG)
        code = text.strip()
        if not code:
            return _err(_ERR_EMPTY)
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            return _err(_ERR_TOO_LONG)
        code = _unwrap(code)
        if code.upper().startswith(CODE_PREFIX):
            return _from_vispora(code[len(CODE_PREFIX):])
        if code.startswith("CS"):
            dec = decode_cs(code)
            return _from_cs(dec) if dec.get("ok") else dec
        if code == "0" or code.startswith("0;"):
            dec = decode_valorant(code)
            return _from_valorant(dec) if dec.get("ok") else dec
        return _err(_ERR_UNKNOWN)
    except Exception:
        return _err(_ERR_UNREADABLE)
