"""Crosshair share codes (core/crosshair_codes.py): Vispora's own codes round
trip, CS2 and Valorant codes decode field by field against published
examples, damaged or foreign codes are refused with a plain sentence, and the
mapping to a Vispora design warns about what it can't carry over."""
import sandbox as m

import base64
import json

C = m.app("core.crosshair_codes")
R = m.app("core.crosshair_render")

# Worked examples (research features.json findings[17]).
LEGACY_V1 = ("CSGO-Cn37R-YE7vo-pLCAL-aURmZ-z6zkG", "CSGO-LibdP-VCVEd-ESayK-rSivi-2UBtG")
CS_VECTORS = {
    "CSGO-MnUCC-89iG7-2cVar-wy7Yn-amCpF": dict(
        kind="legacy", version=3, style=6, outline_on=True, dot=True, rgba=[252, 15, 192, 255], gap=4, length=8,
        thickness=3, spread_limit=255, split_distance=7, inner_alpha=1.0, outer_alpha=0.45, split_ratio=0.3,
        screen_height=1080),
    "CSGO-G8oAC-RyvWc-Hi3CZ-voSwn-QJbfE": dict(
        kind="legacy", version=4, style=8, outline_mode=1, outline_on=True, outline="full", dot=True,
        rgba=[124, 57, 57, 255], gap=25, length=7, thickness=20, spread_limit=181, split_distance=3,
        outer_alpha=0.35, screen_height=768),
    "CSvbPubOq37zTGqtsPTP5QTrp5CB4xFXiKRLfzJsm49ZRe": dict(
        kind="cs2", version=1, style=2, dot=True, outline_on=False, outline_mode=0, rgba=[255, 0, 0, 255],
        outline_rgba=[0, 0, 0, 255], gap=0, length=5, thickness=1, spread_limit=181, split_distance=3,
        inner_alpha=1.0, outer_alpha=0.35, screen_height=768, scope_dot_scale=1.0),
    "CSAB6Oby2JUDOrBRy4jRcsR6JOQdhbszKF9GuKQGhKVwAc": dict(
        kind="cs2", version=1, style=9, follow_recoil=True, t_style=True, outline_mode=2, outline_on=True,
        outline="half", rgba=[10, 20, 30, 200], outline_rgba=[40, 50, 60, 128], gap=-7, length=255, thickness=12,
        spread_limit=64, split_distance=127, inner_alpha=0.29, outer_alpha=0.97, split_ratio=0.63,
        screen_height=1440, scope_dot_scale=2.0, scope_dot_uses_color=True),
    "CSjNcfcGjo8LLy2pDpCynr5e5efCGW6yXuf7B6aQ7wkWmU": dict(       # csxhair's README example
        kind="cs2", version=1, screen_height=1080, style=4, rgba=[0, 255, 0, 255], outline_rgba=[0, 0, 0, 220],
        outline_mode=0, outline_on=False, outline="none", thickness=1, gap=2, length=2, follow_recoil=False,
        scope_dot_uses_color=True, scope_dot_scale=1.0),
    "CSpQo2QSOSE2jXKTFHKei7T4mK5fznKeT2ziT8qXK9uN3Z": dict(       # the same, re-encoded: length 7, follow recoil
        kind="cs2", version=1, screen_height=1080, style=4, rgba=[0, 255, 0, 255], outline_rgba=[0, 0, 0, 220],
        outline_mode=0, thickness=1, gap=2, length=7, follow_recoil=True, scope_dot_uses_color=True,
        scope_dot_scale=1.0),
}
BAD_CHECKSUM = "CSkSCHQKunGr2yPsJr2U3RTd8ycMA2pEs9jWnxK5KjMmWL"
LEGACY_V2 = "CSGO-eRqP8-AkrwM-3Wsqh-pKDTh-eAPtQ"
MATCH_CODE = "CSGO-L9spZ-ihuov-cyhtE-kxbqa-FkBAA"      # a match share code: same container, no checksum

# Worked examples (research features.json findings[14]).
VAL_CYAN = "0;P;c;5;h;0;0l;4;0o;2;0a;1;0f;0;1b;0"
VAL_NATS = "0;P;c;1;o;1.000;0a;1.000;0l;2;0t;1;0o;2;0f;0;1b;0"
VAL_KYEDAE = "0;P;c;8;u;FFFFCCFF;h;0;b;1;f;0;0l;4;0v;4;0o;0;0a;1;0f;0;1b;0"
VAL_ASPAS = "0;s;1;P;c;5;o;1;0l;4;0v;3;0g;1;0o;0;0a;1;0f;0;1b;0;S;c;0;s;1.2;o;1"


def _cs_encode(data, legacy=False):
    """Test-only encoder: checksum byte, then base 57, least significant first."""
    data = bytes([sum(data[1:]) % 256]) + bytes(data[1:])
    value = int.from_bytes(data, "big")
    chars = []
    for _ in range(25 if legacy else 44):
        value, digit = divmod(value, 57)
        chars.append(C.CS_ALPHABET[digit])
    body = "".join(chars)
    if legacy:
        return "CSGO-" + "-".join(body[i:i + 5] for i in range(0, 25, 5))
    return "CS" + body


def _bytes(code):
    data, kind = C._cs_bytes(code)
    assert data is not None, kind
    return bytearray(data)


def _cs2_with(**changes):
    """A valid CS2 code: the csxhair example with some bytes changed."""
    b = _bytes("CSjNcfcGjo8LLy2pDpCynr5e5efCGW6yXuf7B6aQ7wkWmU")
    for index, value in changes.items():
        b[int(index[1:])] = value
    return _cs_encode(b)


# ── CS2 ───────────────────────────────────────────────────────────────
def test_cs_examples_decode_field_by_field():
    for code, want in CS_VECTORS.items():
        got = C.decode_cs(code)
        assert got["ok"] is True, (code, got)
        for key, value in want.items():
            assert got[key] == value, f"{code} {key}: {got[key]!r} != {value!r}"
        for key in ("style", "dot", "t_style", "follow_recoil", "outline_on", "outline_mode", "outline", "rgba",
                    "outline_rgba", "gap", "length", "thickness", "spread_limit", "split_distance", "inner_alpha",
                    "outer_alpha", "split_ratio", "screen_height"):
            assert key in got, (code, key)
    assert C.decode_cs("  " + next(iter(CS_VECTORS)) + "\n")["ok"], "surrounding space is trimmed"


def test_old_csgo_codes_are_refused_with_advice():
    for code in LEGACY_V1:
        assert C.decode_cs(code) == {"ok": False, "err": C.LEGACY_V1_ERR}
        assert C.import_code(code) == C.decode_cs(code)
    # CS2 refuses these codes too (research findings[15]), so the advice must
    # not send the user there: it points at a current code or the Designer.
    err = C.LEGACY_V1_ERR
    assert "Open it in CS2" not in err and "no longer accepts" in err, err
    assert "current CS2 code" in err and "Designer" in err and err.endswith("."), err
    assert chr(0x2014) not in err


def test_cs_container_round_trips():
    for code in list(CS_VECTORS) + list(LEGACY_V1) + [LEGACY_V2]:
        assert _cs_encode(_bytes(code), legacy=code.startswith("CSGO-")) == code
    b = _bytes("CSjNcfcGjo8LLy2pDpCynr5e5efCGW6yXuf7B6aQ7wkWmU")
    b[16] = 7
    b[4] |= 0x20
    assert _cs_encode(b) == "CSpQo2QSOSE2jXKTFHKei7T4mK5fznKeT2ziT8qXK9uN3Z", "csxhair's re-encoded example"


def test_cs_codes_that_are_refused():
    def refused(code, why=None):
        got = C.decode_cs(code)
        assert got["ok"] is False and isinstance(got["err"], str) and got["err"].endswith("."), (code, got)
        if why:
            assert why in got["err"], (code, got["err"])
        imported = C.import_code(code)
        assert imported["ok"] is False, (code, imported)
        return got["err"]

    refused(BAD_CHECKSUM, "damaged")
    refused(MATCH_CODE, "damaged")
    # The old format has no v2 and will never get a new version, so an unknown
    # legacy version is damage, not a format "Vispora can't read yet".
    assert refused(LEGACY_V2, "damaged") == C._ERR_CS_DAMAGED
    legacy = _bytes("CSGO-MnUCC-89iG7-2cVar-wy7Yn-amCpF")
    for version in (0, 2, 5, 9, 255):
        legacy[1] = version
        assert refused(_cs_encode(legacy, legacy=True)) == C._ERR_CS_DAMAGED, version
    assert "CS2" in refused(_cs2_with(b1=7), "format"), "only a current CS2 code can be a format to wait for"
    good = "CSvbPubOq37zTGqtsPTP5QTrp5CB4xFXiKRLfzJsm49ZRe"
    for bad_char in ("0", "1", "I", "l", "g", "-", " ", "é"):
        refused(good[:10] + bad_char + good[11:], "character")
    refused(good[:-1], "length")
    refused(good + "A", "length")
    refused("CSGO-MnUCC-89iG7-2cVar-wy7Yn", "length")
    refused("CSGO-MnUCC-89iG7-2cVar-wy7Yn-amCp", "length")
    refused("CSGO-MnUCC89iG7-2cVar-wy7Yn-amCpF", "length")
    refused("CS" + "9" * 44, "damaged")                     # more than 32 bytes' worth
    refused(_cs2_with(b2=0, b3=0), "screen height is 0")
    refused(_cs2_with(b1=2), "format")
    v3 = _bytes("CSGO-MnUCC-89iG7-2cVar-wy7Yn-amCpF")
    v3[14] = v3[15] = 0
    refused(_cs_encode(v3, legacy=True), "screen height is 0")
    # Old CSGO- codes reach the shared length, character and height checks;
    # none of those texts may call them CS2 codes.
    for code, why in (("CSGO-MnUCC-89iG7-2cVar-wy7Yn", "length"), ("CSGO-abc", "length"),
                      ("CSGO-MnUCC-89iG7-2cVar-wy7Yn-amCp0", "character"),
                      (_cs_encode(v3, legacy=True), "screen height is 0")):
        assert "CS2" not in refused(code, why), code
    for junk in (None, 7, b"CS", ["CS"], "", "cs" + good[2:], "XYZ"):
        got = C.decode_cs(junk)
        assert got["ok"] is False and got["err"], junk


# ── Valorant ──────────────────────────────────────────────────────────
def test_valorant_examples_decode():
    p = C.decode_valorant(VAL_CYAN)["primary"]
    assert (p["c"], p["h"], p["d"], p["0l"], p["0t"], p["0o"], p["0a"], p["0f"], p["1b"]) == (5, 0, 0, 4, 2, 2, 1, 0, 0)
    assert p["0g"] == 0, "no separate vertical length, so the vertical is 0l too"

    p = C.decode_valorant(VAL_NATS)["primary"]
    assert (p["c"], p["h"], p["t"], p["o"], p["0a"], p["0l"], p["0t"], p["0o"], p["0f"], p["1b"]) == \
        (1, 1, 1, 1, 1, 2, 1, 2, 0, 0)

    p = C.decode_valorant(VAL_KYEDAE)["primary"]
    assert (p["c"], p["u"], p["h"], p["b"], p["f"], p["0l"], p["0v"], p["0g"], p["0t"], p["0o"], p["0a"],
            p["0f"], p["1b"]) == (8, "FFFFCCFF", 0, 1, 0, 4, 4, 0, 2, 0, 1, 0, 0)

    dec = C.decode_valorant(VAL_ASPAS)
    p = dec["primary"]
    assert dec["global"]["s"] == 1, "advanced options on"
    assert (p["c"], p["h"], p["t"], p["o"], p["0l"], p["0v"], p["0g"], p["0t"], p["0o"], p["0a"], p["0f"],
            p["1b"]) == (5, 1, 1, 1, 4, 3, 1, 2, 0, 1, 0, 0)
    assert (dec["sniper"]["c"], dec["sniper"]["s"], dec["sniper"]["o"], dec["sniper"]["d"]) == (0, 1.2, 1, 1)

    dec = C.decode_valorant("0")
    assert dec["ok"] and dec["global"] == {"p": 1, "c": 0, "s": 0}
    p = dec["primary"]
    assert (p["c"], p["u"], p["b"], p["h"], p["t"], p["o"], p["d"], p["z"], p["a"], p["m"], p["f"], p["s"]) == \
        (0, None, 0, 1, 1, 0.5, 0, 2, 1, 0, 1, 1)
    assert [p["0" + k] for k in "btlvgoamsfe"] == [1, 2, 6, 6, 0, 3, 0.8, 0, 1, 1, 1]
    assert [p["1" + k] for k in "btlvgoamsfe"] == [1, 2, 2, 2, 0, 10, 0.35, 1, 1, 1, 1]
    assert dec["sniper"] == {"d": 1, "b": 0, "c": 7, "t": None, "s": 1, "o": 0.75}
    assert "f" not in dec["ads"] and "s" not in dec["ads"], "fade and spectate are primary-only keys"

    assert C.decode_valorant(VAL_CYAN + ";")["primary"] == C.decode_valorant(VAL_CYAN)["primary"], "trailing ;"
    assert C.decode_valorant("0;P;q;7;c;1")["primary"]["c"] == 1, "unknown keys are skipped with their value"
    assert C.decode_valorant("0;P;u;00ff7f")["primary"]["u"] == "00FF7F"


def test_valorant_codes_that_are_refused():
    for code in ("1;P;c;5", "P;c;5", "", "  ", None, 5, "0;P;c", "0;P;c;five", "0;P;c;nan", "0;P;0l;1e3",
                 "0;P;u;XYZ", "0;P;u;FFFFF", "0;P;c;;h;0"):
        got = C.decode_valorant(code)
        assert got["ok"] is False and got["err"].endswith("."), (code, got)


# ── Vispora ───────────────────────────────────────────────────────────
def test_vispora_codes_round_trip():
    designs = [R.DEFAULT_DESIGN, {"shape": "chevron", "color": "#f0a", "rotation": 33, "opacity": 41},
               {"shape": "box", "outline": {"on": False}, "dot": {"on": True, "size": 5, "color": "#123456"}},
               {"shape": "circle_dot", "ring_radius": 120, "ring_thickness": 24, "scale_with_resolution": False},
               {"arms": {"top": False}, "thickness": 1, "gap": 0}] + [p["design"] for p in R.BUILTIN_PRESETS]
    for d in designs:
        out = C.export_code(d)
        assert out["ok"] and out["warnings"] == []
        code = out["code"]
        body = code[len("VXH1-"):]
        assert code.startswith("VXH1-") and "=" not in code
        assert set(body) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("ascii")
        assert raw == json.dumps(R.normalize_design(d), separators=(",", ":")), "compact JSON of the design"
        back = C.import_code(code)
        assert back["ok"] and back["source"] == "vispora" and back["approximate"] is False
        assert back["design"] == R.normalize_design(d) and back["warnings"] == []
        assert 1 <= len(back["name"]) <= 40
        assert C.import_code("\n  " + code + "  \n")["design"] == back["design"]
        assert len(code.encode()) < C.MAX_CODE_BYTES


def test_image_designs_export_as_the_default_with_a_warning():
    out = C.export_code({"kind": "image", "image": {"id": "ab" * 16, "scale": 50}, "rotation": 90, "shape": "box"})
    assert out["ok"] and out["warnings"] == ["Image crosshairs can't be shared as a code."]
    back = C.import_code(out["code"])
    assert back["design"] == R.DEFAULT_DESIGN and back["design"]["kind"] == "shape"
    hand_made = "VXH1-" + base64.urlsafe_b64encode(json.dumps(
        {"kind": "image", "image": {"id": "ab" * 16}, "shape": "dot"}).encode()).decode().rstrip("=")
    got = C.import_code(hand_made)
    assert got["ok"] and got["design"]["kind"] == "shape" and got["design"]["shape"] == "dot"
    assert got["design"]["image"]["id"] == "" and got["warnings"] == ["Image crosshairs can't be shared as a code."]


def test_import_refuses_junk_with_a_plain_sentence():
    def b64(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    cases = [None, 12, {"code": "x"}, b"VXH1-", "", "   \n", "hello there", "x" * 10240, "VXH1-" + "A" * 10000,
             "VXH1-", "VXH1-***", "VXH1-" + b64(b"not json"), "VXH1-" + b64(b"[1, 2]"), "VXH1-" + b64(b"\xff\xfe"),
             "VXH1-A", "VXH1-" + b64(b"[" * 600 + b"]" * 600), "CS", "0;;", "0;P;c", "C" * 3000,
             "VXH1-" + b64(json.dumps({"kind": "shape"}).encode()) + "\x00"]
    for text in cases:
        got = C.import_code(text)
        assert got["ok"] is False, (str(text)[:40], got)
        assert isinstance(got["err"], str) and got["err"].endswith(".") and chr(0x2014) not in got["err"], got
    assert C.import_code("x" * 10240)["err"] == "That's too long to be a crosshair code."
    assert C.import_code("")["err"] == "Paste a crosshair code first."


def test_pasted_console_commands_and_quotes_still_import():
    code = "CSvbPubOq37zTGqtsPTP5QTrp5CB4xFXiKRLfzJsm49ZRe"
    assert C.import_code("apply_crosshair_code " + code)["ok"]
    assert C.import_code('"' + code + '"')["ok"]
    vxh = C.export_code({"shape": "dot"})["code"]
    assert C.import_code("'" + vxh + "'")["design"]["shape"] == "dot"
    assert C.import_code("vxh1-" + vxh[5:])["ok"], "the prefix is not case-sensitive"


# ── mapping to a Vispora design ───────────────────────────────────────
def test_cs2_codes_map_to_an_approximate_design():
    got = C.import_code("CSvbPubOq37zTGqtsPTP5QTrp5CB4xFXiKRLfzJsm49ZRe")    # 768p: sizes x 1080/768
    assert got["ok"] and got["source"] == "cs2" and got["approximate"] is True and got["warnings"] == []
    d = got["design"]
    assert (d["kind"], d["shape"], d["color"], d["opacity"]) == ("shape", "cross", "#FF0000", 100)
    assert (d["length"], d["thickness"], d["gap"]) == (7, 1, 0)
    assert d["dot"]["on"] is True and d["dot"]["color"] == "#FF0000"
    assert d["outline"]["on"] is False and all(d["arms"].values())
    assert d == R.normalize_design(d) and 1 <= len(got["name"]) <= 40

    got = C.import_code("CSAB6Oby2JUDOrBRy4jRcsR6JOQdhbszKF9GuKQGhKVwAc")    # 1440p, T style, gap -7
    d = got["design"]
    assert d["arms"]["top"] is False and d["arms"]["bottom"] is True
    assert (d["color"], d["opacity"], d["gap"], d["thickness"], d["length"]) == ("#0A141E", 78, 0, 9, 120)
    assert d["outline"] == {"on": True, "thickness": 1, "color": "#28323C", "opacity": 50}
    assert "Gap is tighter than Vispora can draw, so it was set to 0." in got["warnings"]
    assert any("smaller" in w for w in got["warnings"]), "length 191 was cut to 120"

    got = C.import_code("CSGO-MnUCC-89iG7-2cVar-wy7Yn-amCpF")                # legacy v3 at 1080p
    d = got["design"]
    assert got["ok"] and (d["color"], d["length"], d["thickness"], d["gap"]) == ("#FC0FC0", 8, 3, 4)
    assert d["outline"]["on"] is True and d["outline"]["color"] == "#000000" and d["dot"]["on"] is True

    tiny = C.import_code(_cs2_with(b2=100, b3=0, b16=10))                   # 100 px tall counts as 240
    assert tiny["design"]["length"] == 45, "10 px at 240p is 45 px at 1080p"

    dot_only = C.import_code(_cs2_with(b16=0, b4=0x40 | 4, b13=3))
    assert dot_only["design"]["shape"] == "dot" and dot_only["design"]["thickness"] == 3

    # Length 0 and no dot: the game draws nothing, so there is nothing to
    # import (not four 1 px specks with no warning).
    assert C.import_code(_cs2_with(b16=0)) == {"ok": False, "err": C._ERR_CS_EMPTY}
    assert C.import_code(_cs2_with(b16=0, b13=0x40 | 1)) == {"ok": False, "err": C._ERR_CS_EMPTY}, "outline too"
    legacy = _bytes("CSGO-MnUCC-89iG7-2cVar-wy7Yn-amCpF")
    legacy[8], legacy[2] = 0, legacy[2] & ~0x40 & 0xFF
    assert C.import_code(_cs_encode(legacy, legacy=True)) == {"ok": False, "err": C._ERR_CS_EMPTY}
    assert C._ERR_CS_EMPTY.endswith(".") and chr(0x2014) not in C._ERR_CS_EMPTY
    # The raw length decides, not the scaled one: 1 px at 2400p scales to 0
    # but the game still draws a line.
    tall = C.import_code(_cs2_with(b16=1, b2=2400 & 255, b3=2400 >> 8))
    assert tall["ok"] is True and tall["design"]["shape"] == "cross" and tall["design"]["length"] == 1


def test_valorant_codes_map_to_an_approximate_design():
    got = C.import_code(VAL_CYAN)
    assert got["ok"] and got["source"] == "valorant" and got["approximate"] is True and got["warnings"] == []
    d = got["design"]
    assert (d["shape"], d["color"], d["length"], d["thickness"], d["gap"], d["opacity"]) == \
        ("cross", "#00FFFF", 4, 2, 2, 100)
    assert d["outline"]["on"] is False and d["dot"]["on"] is False
    assert d["scale_with_resolution"] is False, "Valorant sizes are pixels at any resolution"
    assert d == R.normalize_design(d)

    d = C.import_code(VAL_NATS)["design"]
    assert (d["color"], d["length"], d["thickness"], d["gap"]) == ("#00FF00", 2, 1, 2)
    assert d["outline"] == {"on": True, "thickness": 1, "color": "#000000", "opacity": 100}

    assert C.import_code(VAL_KYEDAE)["design"]["color"] == "#FFFFCC"

    got = C.import_code(VAL_ASPAS)
    assert got["warnings"] == ["Vertical and horizontal lengths differ; Vispora uses one length."]
    assert got["design"]["length"] == 4

    got = C.import_code("0")
    d = got["design"]
    assert got["warnings"] == ["Outer lines aren't supported and were left out."]
    assert (d["color"], d["length"], d["thickness"], d["gap"], d["opacity"]) == ("#FFFFFF", 6, 2, 3, 80)
    assert d["outline"] == {"on": True, "thickness": 1, "color": "#000000", "opacity": 50}

    def colour(code):
        return C.import_code(code)["design"]["color"]
    assert colour("0;P;c;8;u;00FF7F;1b;0") == "#00FF7F", "6 hex digits work too"
    assert colour("0;P;c;5;u;000000FF;1b;0") == "#00FFFF", "u alone means nothing"
    assert colour("0;P;b;1;c;5;u;FF000080;1b;0") == "#FF0000", "b;1 switches to the custom colour"
    assert colour("0;P;c;8;1b;0") == "#FFFFFF", "custom with no colour given"
    assert colour("0;P;c;12;1b;0") == "#FFFFFF", "out of range"
    assert [colour("0;P;c;%d;1b;0" % i) for i in range(8)] == list(C.VALORANT_COLOURS)

    d = C.import_code("0;P;d;1;z;4;a;0.5;0b;0;1b;0")["design"]
    assert (d["shape"], d["thickness"], d["opacity"]) == ("dot", 4, 50), "only the centre dot shows"
    assert C.import_code("0;P;d;1;z;3;0l;0;1b;0")["design"]["shape"] == "dot"
    d = C.import_code("0;P;d;1;z;3;0b;1;0l;5;1b;0")["design"]
    assert d["shape"] == "cross" and d["dot"] == {"on": True, "size": 3, "color": "#FFFFFF", "opacity": 100}

    got = C.import_code("0;P;0b;0;1l;5;1o;8;1a;1;1t;3")
    assert got["ok"] and (got["design"]["length"], got["design"]["gap"], got["design"]["thickness"]) == (5, 8, 3)
    assert len(got["warnings"]) == 1, "only the outer lines show, so they are drawn"
    got = C.import_code("0;P;0b;0;1b;0;d;0")
    assert got["ok"] is False and got["err"].endswith(".")


def test_valorant_parts_hidden_by_opacity_count_as_hidden():
    # Inner lines at opacity 0 with the outer lines on: draw the outer lines
    # (Valorant's defaults: length 2, thickness 2, offset 10, opacity 0.35).
    got = C.import_code("0;P;0a;0;1b;1;h;0")
    d = got["design"]
    assert got["ok"] and (d["shape"], d["length"], d["thickness"], d["gap"], d["opacity"]) == ("cross", 2, 2, 10, 35)
    assert got["warnings"] == ["Only the outer lines show in this code, so Vispora drew them as its lines."]
    assert R.render(d).getchannel("A").getextrema()[1] > 0, "something is drawn"

    # Inner lines at opacity 0 with the dot on: only the dot shows.
    d = C.import_code("0;P;0a;0;d;1;z;3;1b;0")["design"]
    assert (d["shape"], d["thickness"], d["opacity"]) == ("dot", 3, 100) and d["dot"]["on"] is False

    # A dot at opacity 0 next to visible lines is not drawn.
    d = C.import_code("0;P;d;1;a;0;1b;0")["design"]
    assert d["shape"] == "cross" and d["dot"]["on"] is False

    # Nothing visible at all, including an opacity that rounds to 0%.
    for code in ("0;P;0a;0;h;0;1b;0", "0;P;0a;0.004;h;0;1b;0", "0;P;0a;0;d;1;a;0;1b;0;h;0",
                 "0;P;0a;0;1a;0.004;h;0"):
        assert C.import_code(code) == {"ok": False, "err": C._ERR_VAL_EMPTY}, code
