"""Crosshair drawing (core/crosshair_render.py): the design format and how it
is cleaned up, exact pixels for straight shapes, the supersampled shapes and
rotation, scaling with the screen, the image kind, the bytes handed to
UpdateLayeredWindow, the built-in presets and how long a render may take."""
import sandbox as m

import base64
import io
import time

from PIL import Image, ImageChops

R = m.app("core.crosshair_render")

IMG_ID = "0123456789abcdef0123456789abcdef"
CAP = 511                   # the spec's limit for either side


# ── helpers ───────────────────────────────────────────────────────────
def _grid(img):
    """{(dx, dy): (r, g, b, a)} with (0, 0) the centre pixel."""
    w, h = img.size
    px = img.load()
    return {(x - w // 2, y - h // 2): px[x, y] for y in range(h) for x in range(w)}


def _same(a, b):
    return a.size == b.size and ImageChops.difference(a, b).getbbox() is None


def _rect(x0, y0, x1, y1):
    return {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


def _ring_around(fill, o):
    """Pixels within o (Chebyshev) of the fill, not in it: where an outline o thick goes."""
    out = set()
    for x, y in fill:
        for dx in range(-o, o + 1):
            for dy in range(-o, o + 1):
                out.add((x + dx, y + dy))
    return out - fill


def _cross_fill(t, length, gap, arms=("top", "bottom", "left", "right")):
    lo = -((t - 1) // 2)
    hi = lo + t - 1
    near = gap + (1 - t % 2)
    far = near + length - 1
    fill = set()
    if "right" in arms:
        fill |= _rect(near, lo, far, hi)
    if "left" in arms:
        fill |= _rect(-(gap + length - 1), lo, -gap, hi)
    if "bottom" in arms:
        fill |= _rect(lo, near, hi, far)
    if "top" in arms:
        fill |= _rect(lo, -(gap + length - 1), hi, -gap)
    return fill


def _assert_exact(img, fill, colour, o):
    """Fill pixels are the colour at full alpha, the outline is black around
    them, everything else is fully transparent."""
    grid = _grid(img)
    outline = _ring_around(fill, o) if o else set()
    for p, v in grid.items():
        if p in fill:
            assert v == colour + (255,), f"{p} should be the fill colour, is {v}"
        elif p in outline:
            assert v == (0, 0, 0, 255), f"{p} should be outline, is {v}"
        else:
            assert v[3] == 0, f"{p} should be transparent, is {v}"
    assert fill <= set(grid), "the image is too small for the shape"


def _asymmetry(img):
    """Largest channel difference between the image and its left-right and
    top-bottom mirrors."""
    lr = ImageChops.difference(img, img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)).getextrema()
    ud = ImageChops.difference(img, img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)).getextrema()
    return max(hi for _, hi in lr), max(hi for _, hi in ud)


def _alpha_sum(img):
    return sum(v * n for v, n in enumerate(img.getchannel("A").histogram()))


# ── the design format ─────────────────────────────────────────────────
def test_the_default_design_is_the_documented_one():
    d = R.DEFAULT_DESIGN
    assert (d["kind"], d["shape"], d["color"], d["opacity"]) == ("shape", "cross", "#00FFFF", 100)
    assert (d["length"], d["thickness"], d["gap"]) == (6, 2, 3)
    assert d["arms"] == {"top": True, "bottom": True, "left": True, "right": True}
    assert d["dot"] == {"on": False, "size": 2, "color": "#00FFFF", "opacity": 100}
    assert (d["ring_radius"], d["ring_thickness"]) == (8, 2)
    assert d["outline"] == {"on": True, "thickness": 1, "color": "#000000", "opacity": 100}
    assert (d["rotation"], d["scale_with_resolution"]) == (0, True)
    assert d["image"] == {"id": "", "scale": 100, "opacity": 100}
    assert R.normalize_design(d) == d


def test_normalize_clamps_coerces_and_drops_junk():
    for junk in (None, [], "cross", 42, 3.5, b"{}", {"kind"}):
        out = R.normalize_design(junk)
        assert out == R.DEFAULT_DESIGN, junk
        out["arms"]["top"] = False
        assert R.DEFAULT_DESIGN["arms"]["top"] is True, "the default must be copied, not shared"

    d = R.normalize_design({
        "kind": "video", "shape": "star", "color": "red", "opacity": 250, "length": -5, "thickness": " 7 ",
        "gap": 3.6, "rotation": -45, "ring_radius": 1, "ring_thickness": float("inf"),
        "arms": {"top": "off", "left": 0, "right": "maybe"},
        "dot": {"on": "yes", "size": 99, "color": "#abc", "opacity": True, "glow": 1},
        "outline": {"thickness": 0, "color": "12345G", "opacity": "nan"},
        "image": {"id": "0123456789ABCDEF0123456789ABCDEF", "scale": 5000, "opacity": -3},
        "scale_with_resolution": "false", "extra": {"x": 1}, "__proto__": 1,
    })
    assert set(d) == set(R.DEFAULT_DESIGN), "unknown keys are dropped"
    for sub in ("arms", "dot", "outline", "image"):
        assert set(d[sub]) == set(R.DEFAULT_DESIGN[sub]), sub
    assert (d["kind"], d["shape"], d["color"]) == ("shape", "cross", "#00FFFF"), "bad enums and colours fall back"
    assert (d["opacity"], d["length"], d["thickness"], d["gap"]) == (100, 1, 7, 4)
    assert d["rotation"] == 315, "rotation wraps round"
    assert (d["ring_radius"], d["ring_thickness"]) == (2, 2), "1 clamps up to 2; infinity is not a number"
    assert d["arms"] == {"top": False, "bottom": True, "left": False, "right": True}
    assert d["dot"] == {"on": True, "size": 24, "color": "#AABBCC", "opacity": 100}, "booleans are not numbers"
    assert d["outline"] == {"on": True, "thickness": 1, "color": "#000000", "opacity": 100}
    assert d["image"] == {"id": IMG_ID, "scale": 400, "opacity": 0}
    assert d["scale_with_resolution"] is False

    assert R.normalize_design({"shape": "Circle-Dot"})["shape"] == "circle_dot"
    assert R.normalize_design({"color": "0f0"})["color"] == "#00FF00"
    assert R.normalize_design({"color": "#00ff7f"})["color"] == "#00FF7F"
    assert R.normalize_design({"color": "#00ff7f80"})["color"] == "#00FFFF", "6 or 3 digits only"
    assert R.normalize_design({"rotation": 360})["rotation"] == 0
    assert R.normalize_design({"rotation": "90.4"})["rotation"] == 90
    assert R.normalize_design({"opacity": 10 ** 400})["opacity"] == 100, "a number too big for a float"
    assert R.normalize_design({"image": {"id": "../../evil"}})["image"]["id"] == ""
    assert R.normalize_design({"image": {"id": IMG_ID + "0"}})["image"]["id"] == ""

    class Hostile(dict):
        def get(self, *a):
            raise RuntimeError("no")
    assert R.normalize_design(Hostile(shape="dot")) == R.DEFAULT_DESIGN


def test_scaled_converts_1080p_sizes_for_the_monitor():
    d = R.DEFAULT_DESIGN
    s = R.scaled(d, 1440)
    assert (s["length"], s["thickness"], s["gap"], s["outline"]["thickness"]) == (8, 3, 4, 1)
    assert (s["ring_radius"], s["ring_thickness"], s["dot"]["size"]) == (11, 3, 3)
    s = R.scaled(d, 2160)
    assert (s["length"], s["thickness"], s["gap"], s["outline"]["thickness"]) == (12, 4, 6, 2)
    assert (s["ring_radius"], s["ring_thickness"], s["dot"]["size"]) == (16, 4, 4)
    s = R.scaled(d, 720)
    assert (s["length"], s["thickness"], s["gap"], s["outline"]["thickness"]) == (4, 1, 2, 1)
    s = R.scaled({"length": 1, "thickness": 1, "gap": 1, "ring_radius": 2, "ring_thickness": 1,
                  "dot": {"size": 1}}, 240)
    assert (s["length"], s["thickness"], s["gap"], s["ring_radius"], s["ring_thickness"], s["dot"]["size"]) == \
        (1, 1, 0, 2, 1, 1), "a size that starts at 1 never drops to 0; gap may"
    assert R.scaled(d, 1080) == d
    assert R.scaled(dict(d, scale_with_resolution=False), 2160)["length"] == 6
    for bad in (None, 0, -1440, "tall", float("nan")):
        assert R.scaled(d, bad) == d, bad
    original = R.normalize_design({"length": 10})
    R.scaled(original, 2160)
    assert original["length"] == 10, "the caller's design is not changed"


# ── pixels ────────────────────────────────────────────────────────────
def test_images_are_odd_sized_rgba_with_the_aim_point_in_the_middle():
    for shape in R.SHAPES:
        for t in (1, 2, 3, 4):
            for rot in (0, 30, 90):
                img = R.render({"shape": shape, "thickness": t, "rotation": rot, "dot": {"on": True, "size": t}})
                w, h = img.size
                assert img.mode == "RGBA" and w % 2 == 1 and h % 2 == 1, (shape, t, rot, img.size)
    img = R.render({"shape": "dot", "thickness": 1, "outline": {"on": False}, "color": "#FF0000"})
    lit = {p for p, v in _grid(img).items() if v[3]}
    assert lit == {(0, 0)}, "a 1 px dot is exactly the centre pixel"
    img = R.render({"thickness": 1, "gap": 0, "outline": {"on": False}})
    assert _grid(img)[(0, 0)][3] == 255, "with no gap the arms meet on the centre pixel"


def test_cross_arms_gap_and_outline_are_pixel_exact():
    img = R.render({"thickness": 3, "length": 4, "gap": 3, "color": "#FF0000"})
    fill = _cross_fill(3, 4, 3)
    assert (3, 0) in fill and (6, 0) in fill and (7, 0) not in fill and (2, 0) not in fill
    _assert_exact(img, fill, (255, 0, 0), 1)
    grid = _grid(img)
    for p in _rect(-1, -1, 1, 1):
        assert grid[p][3] == 0, f"the gap at {p} must be see-through"
    img = R.render({"thickness": 1, "length": 5, "gap": 2, "color": "#FFFF00",
                    "outline": {"on": True, "thickness": 3}})
    _assert_exact(img, _cross_fill(1, 5, 2), (255, 255, 0), 3)
    img = R.render({"thickness": 3, "length": 4, "gap": 3, "color": "#FF0000", "outline": {"on": False}})
    _assert_exact(img, _cross_fill(3, 4, 3), (255, 0, 0), 0)


def test_even_thickness_puts_the_extra_pixel_right_and_down():
    for t in (2, 4, 6):
        img = R.render({"thickness": t, "length": 3, "gap": 2, "color": "#00FF00", "outline": {"on": False}})
        fill = _cross_fill(t, 3, 2)
        lo, hi = -((t - 1) // 2), t // 2
        assert {y for x, y in fill if x == 4} == set(range(lo, hi + 1)), "rows of the right arm"
        assert hi == lo + t - 1 and hi > -lo, "the extra pixel is below / right of the centre"
        _assert_exact(img, fill, (0, 255, 0), 0)
    # The arms mirror about the band's centre (a pixel corner), so the gaps match.
    grid = _grid(R.render({"thickness": 2, "length": 3, "gap": 2, "outline": {"on": False}}))
    assert grid[(-2, 0)][3] == 255 and grid[(-1, 0)][3] == 0, "left arm ends 2 px left of the centre pixel"
    assert grid[(3, 0)][3] == 255 and grid[(2, 0)][3] == 0, "right arm starts 2 px right of the next one"
    img = R.render({"shape": "dot", "thickness": 2, "outline": {"on": False}, "color": "#FFFFFF"})
    _assert_exact(img, _rect(0, 0, 1, 1), (255, 255, 255), 0)


def test_t_shape_has_no_top_arm():
    img = R.render({"arms": {"top": False}, "thickness": 2, "length": 6, "gap": 3, "color": "#00FFFF"})
    _assert_exact(img, _cross_fill(2, 6, 3, arms=("bottom", "left", "right")), (0, 255, 255), 1)
    lit_above = [p for p, v in _grid(img).items() if v[3] and p[1] < -1]
    assert lit_above == [], "nothing above the horizontal arms and their outline"
    preset = R.render(R.builtin_preset("builtin:t-shape")["design"])
    assert not [p for p, v in _grid(preset).items() if v[3] and p[1] < -1]


def test_box_and_dot_are_pixel_exact():
    img = R.render({"shape": "box", "length": 4, "thickness": 1, "color": "#FF00FF"})
    fill = _rect(-4, -4, 4, 4) - _rect(-3, -3, 3, 3)
    _assert_exact(img, fill, (255, 0, 255), 1)
    img = R.render({"shape": "box", "length": 5, "thickness": 2, "color": "#FF00FF", "outline": {"on": False}})
    _assert_exact(img, _rect(-5, -5, 5, 5) - _rect(-3, -3, 3, 3), (255, 0, 255), 0)
    img = R.render({"shape": "box", "length": 2, "thickness": 3, "color": "#FF00FF", "outline": {"on": False}})
    _assert_exact(img, _rect(-2, -2, 2, 2), (255, 0, 255), 0)
    img = R.render({"shape": "dot", "thickness": 3, "color": "#FFFFFF", "outline": {"on": True, "thickness": 2}})
    _assert_exact(img, _rect(-1, -1, 1, 1), (255, 255, 255), 2)


def test_every_shape_draws_something_and_odd_sizes_are_symmetric():
    base = {"thickness": 3, "length": 5, "gap": 2, "ring_thickness": 3, "ring_radius": 9,
            "dot": {"on": True, "size": 3}, "outline": {"on": True, "thickness": 2}}
    for shape in R.SHAPES:
        for rot in (0, 90, 180, 270):
            img = R.render(dict(base, shape=shape, rotation=rot))
            assert img.getchannel("A").getextrema()[1] == 255, (shape, rot)
            lr, ud = _asymmetry(img)
            if shape == "chevron":                  # mirror-symmetric across one axis only
                lr, ud = (lr, 0) if rot in (0, 180) else (0, ud)
            assert lr == 0 and ud == 0, (shape, rot, lr, ud)
        for rot in (45, 135, 315):                  # supersampled, yet still exactly symmetric
            img = R.render(dict(base, shape=shape, rotation=rot))
            lr, ud = _asymmetry(img)
            if shape == "chevron":
                continue
            assert lr == 0 and ud == 0, (shape, rot, lr, ud)


def test_the_fields_a_shape_does_not_use_change_nothing():
    dot = {"shape": "dot", "thickness": 3}
    assert _same(R.render(dot), R.render(dict(dot, length=120, gap=60, ring_radius=50, arms={"top": False})))
    assert _same(R.render(dot), R.render(dict(dot, dot={"on": True, "size": 9})))
    ring = {"shape": "circle", "ring_radius": 7}
    assert _same(R.render(ring), R.render(dict(ring, length=40, thickness=9, gap=20)))
    builtin_dot = R.render({"shape": "circle_dot", "dot": {"on": False}})
    assert _same(builtin_dot, R.render({"shape": "circle_dot", "dot": {"on": True}})), "circle_dot always has its dot"
    img = R.render({"thickness": 3, "gap": 0, "outline": {"on": False}, "color": "#FFFFFF",
                    "dot": {"on": True, "size": 3, "color": "#FF0000"}})
    assert _grid(img)[(0, 0)] == (255, 0, 0, 255) and _grid(img)[(3, 0)] == (255, 255, 255, 255), \
        "the extra dot is drawn on top, in its own colour"


def test_margin_is_see_through_and_fits_the_outline():
    for d in ({}, {"shape": "box"}, {"shape": "dot", "thickness": 9}, {"outline": {"on": True, "thickness": 6}},
              {"shape": "circle", "outline": {"on": True, "thickness": 4}}, {"shape": "chevron"},
              {"rotation": 45, "length": 30}, {"shape": "cross_circle", "rotation": 20}):
        img = R.render(d)
        w, h = img.size
        a = img.getchannel("A")
        edges = [a.getpixel((x, y)) for x in range(w) for y in (0, h - 1)] + \
                [a.getpixel((x, y)) for y in range(h) for x in (0, w - 1)]
        assert max(edges) == 0, (d, "the outermost pixels must be transparent")
    o = 4
    img = R.render({"color": "#FF0000", "outline": {"on": True, "thickness": o}})
    fill = [p for p, v in _grid(img).items() if v == (255, 0, 0, 255)]
    w, h = img.size
    assert min(w // 2 - abs(x) for x, _ in fill) >= o + 1 and min(h // 2 - abs(y) for _, y in fill) >= o + 1


def test_rotation():
    t90 = R.render({"arms": {"top": False}, "thickness": 3, "rotation": 90, "outline": {"on": False},
                    "color": "#FFFFFF"})
    _assert_exact(t90, _cross_fill(3, 6, 3, arms=("top", "bottom", "left")), (255, 255, 255), 0)
    t180 = R.render({"arms": {"top": False}, "thickness": 3, "rotation": 180, "outline": {"on": False},
                     "color": "#FFFFFF"})
    _assert_exact(t180, _cross_fill(3, 6, 3, arms=("top", "left", "right")), (255, 255, 255), 0)
    even = R.render({"arms": {"top": False}, "thickness": 2, "rotation": 90, "outline": {"on": False},
                     "color": "#FFFFFF"})
    _assert_exact(even, _cross_fill(2, 6, 3, arms=("top", "bottom", "left")), (255, 255, 255), 0)

    plain = {"thickness": 3, "length": 10, "gap": 3, "outline": {"on": False}}
    diag = _grid(R.render(dict(plain, rotation=45)))
    assert diag[(7, 7)][3] > 200 and diag[(-7, 7)][3] > 200, "arms now run along the diagonals"
    assert diag[(9, 0)][3] == 0 and diag[(0, 9)][3] == 0
    assert any(0 < v[3] < 255 for v in diag.values()), "rotated edges are anti-aliased"
    area = _alpha_sum(R.render(plain))
    for rot in (10, 30, 45, 77):
        turned = _alpha_sum(R.render(dict(plain, rotation=rot)))
        assert abs(turned - area) < 0.06 * area, (rot, turned, area)

    big = {"shape": "cross_circle", "length": 120, "gap": 60, "thickness": 24, "ring_radius": 120,
           "ring_thickness": 24, "outline": {"on": True, "thickness": 6}, "dot": {"on": True, "size": 24}}
    for rot in range(0, 360, 30):
        for height in (1080, 2160, 4320):
            w, h = R.render(dict(big, rotation=rot), height).size
            assert w <= CAP and h <= CAP and w % 2 and h % 2, (rot, height, w, h)


def test_render_at_1440_and_2160():
    grid = _grid(R.render({"color": "#FFFFFF"}, 1440))      # length 8, thickness 3, gap 4, outline 1
    right = sorted(x for (x, y), v in grid.items() if y == 0 and v == (255, 255, 255, 255) and x > 0)
    assert right == list(range(4, 12)), right
    column = sorted(y for (x, y), v in grid.items() if x == 6 and v == (255, 255, 255, 255))
    assert column == [-1, 0, 1]
    grid = _grid(R.render({"color": "#FFFFFF"}, 2160))      # length 12, thickness 4, gap 6, outline 2
    right = sorted(x for (x, y), v in grid.items() if y == 0 and v == (255, 255, 255, 255) and x > 0)
    assert right == list(range(7, 19)), right
    column = sorted(y for (x, y), v in grid.items() if x == 10 and v[3])
    assert column == list(range(-3, 5)), "4 px of arm plus a 2 px outline on each side"
    assert _same(R.render({"scale_with_resolution": False}, 2160), R.render({}))


def test_opacity_multiplies_alpha():
    grid = _grid(R.render({"color": "#E69F00", "opacity": 50, "outline": {"on": True, "opacity": 25},
                           "dot": {"on": True, "opacity": 40, "color": "#FFFFFF"}, "gap": 4}))
    arm, edge, dot = grid[(6, 0)], grid[(6, -1)], grid[(0, 0)]
    assert arm[3] == 128 and all(abs(c - e) <= 1 for c, e in zip(arm[:3], (0xE6, 0x9F, 0x00))), arm
    assert edge[3] == 64 and edge[:3] == (0, 0, 0), edge
    assert dot[3] == 102 and dot[:3] == (255, 255, 255), dot
    hollow = _grid(R.render({"opacity": 0, "gap": 3}))
    assert hollow[(6, 0)][3] == 0, "a see-through shape leaves just its outline"
    assert hollow[(6, -1)] == (0, 0, 0, 255) and hollow[(3, 0)] == (0, 0, 0, 255)
    assert R.render({"opacity": 0, "outline": {"on": False}}).getchannel("A").getextrema() == (0, 0)


def test_premultiplied_bytes():
    img = Image.new("RGBA", (2, 2))
    pixels = [(200, 100, 50, 128), (255, 255, 255, 0), (10, 20, 30, 255), (255, 0, 0, 1)]
    for i, p in enumerate(pixels):
        img.putpixel((i % 2, i // 2), p)
    want = []
    for r, g, b, a in pixels:
        want += [round(b * a / 255), round(g * a / 255), round(r * a / 255), a]
    data = R.to_premultiplied_bgra(img)
    assert list(data) == want, "top-down rows of B, G, R, A, colour times alpha"
    assert list(data[:4]) == [25, 50, 100, 128]
    assert R.to_premultiplied_bgra(img.convert("RGB"))[3] == 255, "any mode is accepted"

    shot = R.render({"color": "#E69F00", "opacity": 50, "shape": "circle"})
    data = R.to_premultiplied_bgra(shot)
    w, h = shot.size
    assert len(data) == w * h * 4
    px = shot.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            i = (y * w + x) * 4
            assert tuple(data[i:i + 4]) == (round(b * a / 255), round(g * a / 255), round(r * a / 255), a), (x, y)


def test_image_kind():
    red = Image.new("RGBA", (10, 6), (255, 0, 0, 255))
    pal = Image.new("P", (8, 8), 3)
    images = {IMG_ID: red, "f" * 32: pal, "e" * 32: Image.new("RGBA", (600, 600), (0, 0, 255, 255))}
    design = {"kind": "image", "image": {"id": IMG_ID, "scale": 100, "opacity": 100}}

    img = R.render(design, image_loader=images.get)
    assert img.size == (13, 9) and img.mode == "RGBA" and "warning" not in img.info
    assert _grid(img)[(0, 0)] == (255, 0, 0, 255)
    assert img.getchannel("A").getbbox() == (1, 1, 11, 7), "a transparent margin around the image"
    img = R.render(dict(design, image={"id": IMG_ID, "scale": 200}), image_loader=images.get)
    assert img.size == (23, 15)
    img = R.render(dict(design, image={"id": IMG_ID, "opacity": 50}), image_loader=images.get)
    assert img.getchannel("A").getextrema() == (0, 128)
    img = R.render(dict(design, rotation=90), image_loader=images.get)
    w, h = img.size
    assert w % 2 and h % 2 and h > w, "turned on its side"
    img = R.render(dict(design, rotation=30), image_loader=images.get)
    assert img.size[0] % 2 and img.size[1] % 2
    img = R.render({"kind": "image", "image": {"id": "e" * 32, "scale": 400}, "rotation": 45},
                   image_loader=images.get)
    assert max(img.size) <= CAP and img.size[0] % 2 and img.size[1] % 2
    img = R.render({"kind": "image", "image": {"id": "f" * 32}}, image_loader=images.get)
    assert img.size == (11, 11) and "warning" not in img.info
    assert red.size == (10, 6) and red.mode == "RGBA" and pal.mode == "P", "the loader's images are untouched"
    assert img.size == R.render(dict(design, image={"id": "f" * 32}, length=99), image_loader=images.get).size, \
        "shape fields don't matter for an image"

    default = R.render(R.DEFAULT_DESIGN)

    def boom(_):
        raise OSError("gone")
    for loader, iid in ((images.get, "a" * 32), (boom, IMG_ID), (None, IMG_ID), (images.get, ""),
                        (lambda _: "not an image", IMG_ID), (lambda _: Image.new("RGBA", (0, 0)), IMG_ID)):
        img = R.render({"kind": "image", "image": {"id": iid}, "shape": "box"}, image_loader=loader)
        assert img.info.get("warning") == "image missing", (loader, iid)
        assert _same(img, default), "a missing image draws the default crosshair"
    img = R.render({"kind": "image", "image": {"id": "a" * 32}}, 2160, image_loader=images.get)
    assert _same(img, R.render(R.DEFAULT_DESIGN, 2160)) and img.info["warning"] == "image missing"


def test_image_centre_pixel_stays_on_the_aim_point():
    def centroid(img):
        """Alpha-weighted centre relative to the aim pixel (w // 2, h // 2)."""
        w, h = img.size
        px = img.getchannel("A").load()
        total = sx = sy = 0
        for y in range(h):
            for x in range(w):
                total += px[x, y]
                sx += px[x, y] * x
                sy += px[x, y] * y
        return sx / total - w // 2, sy / total - h // 2

    # A 31 x 31 pixel-art cross, 1 px arms through its middle pixel. Scales
    # that would make it even (50%, 200%, 400%) used to put it half a pixel
    # up and left, and smear the 1 px arms over two pixels.
    n = 31
    cross = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    for i in range(n):
        cross.putpixel((n // 2, i), (255, 255, 255, 255))
        cross.putpixel((i, n // 2), (255, 255, 255, 255))
    loader = {IMG_ID: cross}.get
    for rotation in (0, 90, 180, 270, 30):
        for scale in (10, 25, 50, 75, 100, 150, 200, 300, 400):
            img = R.render({"kind": "image", "image": {"id": IMG_ID, "scale": scale}, "rotation": rotation},
                           image_loader=loader)
            assert img.size[0] % 2 and img.size[1] % 2, (rotation, scale, img.size)
            cx, cy = centroid(img)
            assert abs(cx) < 0.01 and abs(cy) < 0.01, (rotation, scale, cx, cy)
    for scale in (50, 200, 400):
        img = R.render({"kind": "image", "image": {"id": IMG_ID, "scale": scale}}, image_loader=loader)
        w, h = img.size
        row = [img.getpixel((x, 2))[3] for x in range(w)]       # a row through the top arm
        assert row == row[::-1] and row[w // 2] == max(row) > 0, (scale, row)

    # Up to 509 px fits at 100% with its 1 px margin (511), so it is pasted
    # as it is, not resampled; a right-angle turn is an exact transpose.
    big = Image.new("RGBA", (509, 301), (0, 0, 0, 0))
    for xy, colour in (((254, 150), (255, 0, 0, 255)), ((0, 0), (0, 255, 0, 255)), ((508, 300), (0, 0, 255, 255)),
                       ((100, 7), (255, 255, 0, 255))):
        big.putpixel(xy, colour)
    loader = {IMG_ID: big}.get
    img = R.render({"kind": "image", "image": {"id": IMG_ID}}, image_loader=loader)
    assert img.size == (CAP, 303) and _same(img.crop((1, 1, 510, 302)), big)
    assert _grid(img)[(0, 0)] == (255, 0, 0, 255)
    img = R.render({"kind": "image", "image": {"id": IMG_ID}, "rotation": 90}, image_loader=loader)
    assert img.size == (303, CAP) and _same(img.crop((1, 1, 302, 510)), big.transpose(Image.Transpose.ROTATE_270))
    assert _grid(img)[(0, 0)] == (255, 0, 0, 255)
    for side in (508, 510, 512):
        img = R.render({"kind": "image", "image": {"id": IMG_ID}},
                       image_loader={IMG_ID: Image.new("RGBA", (side, side), (9, 9, 9, 255))}.get)
        assert img.size == (CAP, CAP), (side, img.size)


def test_png_data_uri_and_design_hash():
    img = R.render(R.DEFAULT_DESIGN)
    uri = R.to_png_data_uri(img)
    assert uri.startswith("data:image/png;base64,")
    back = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
    assert back.format == "PNG" and _same(back.convert("RGBA"), img)

    h = R.design_hash(R.DEFAULT_DESIGN)
    assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)
    assert R.design_hash({}) == h and R.design_hash({"length": "6", "junk": 1}) == h, "hashes the normalized design"
    assert R.design_hash({"length": 7}) != h and R.design_hash({"outline": {"on": False}}) != h
    assert R.design_hash(None) == h


def test_render_never_raises_on_junk():
    for d in (None, [], "cross", {"kind": "image"}, {"shape": 5, "length": "long"}, {"dot": "yes"}):
        for height in (1080, None, "4k", -1, 10 ** 9):
            img = R.render(d, height)
            assert img.mode == "RGBA" and img.size[0] % 2 and max(img.size) <= CAP, (d, height)


def test_builtin_presets():
    ids = ["builtin:classic", "builtin:dot", "builtin:cross-dot", "builtin:t-shape", "builtin:ring",
           "builtin:ring-dot", "builtin:crossed-circle", "builtin:chevron", "builtin:box", "builtin:thin-cross",
           "builtin:orange", "builtin:diagonal"]
    names = ["Classic", "Dot", "Cross and dot", "T-shape", "Ring", "Ring and dot", "Crossed circle", "Chevron",
             "Box", "Thin cross", "Orange", "Diagonal"]
    assert [p["id"] for p in R.BUILTIN_PRESETS] == ids and list(R.BUILTIN_IDS) == ids
    assert len(set(ids)) == 12
    assert [p["name"] for p in R.BUILTIN_PRESETS] == names
    by_id = {p["id"]: p["design"] for p in R.BUILTIN_PRESETS}
    for pid, d in by_id.items():
        assert R.normalize_design(d) == d, pid
        img = R.render(d)
        assert img.size[0] % 2 and img.size[1] % 2 and img.getchannel("A").getextrema()[1] == 255, pid
    hashes = {R.design_hash(d) for d in by_id.values()}
    assert len(hashes) == 12, "every preset is a different design"
    assert by_id["builtin:classic"] == R.DEFAULT_DESIGN
    assert by_id["builtin:dot"]["shape"] == "dot"
    assert by_id["builtin:cross-dot"]["shape"] == "cross" and by_id["builtin:cross-dot"]["dot"]["on"]
    assert by_id["builtin:t-shape"]["arms"] == {"top": False, "bottom": True, "left": True, "right": True}
    assert by_id["builtin:ring"]["shape"] == "circle" and by_id["builtin:ring-dot"]["shape"] == "circle_dot"
    assert by_id["builtin:crossed-circle"]["shape"] == "cross_circle"
    assert by_id["builtin:chevron"]["shape"] == "chevron" and by_id["builtin:box"]["shape"] == "box"
    thin = by_id["builtin:thin-cross"]
    assert (thin["shape"], thin["thickness"], thin["length"]) == ("cross", 1, 10)
    assert (by_id["builtin:orange"]["shape"], by_id["builtin:orange"]["color"]) == ("cross", "#E69F00")
    assert (by_id["builtin:diagonal"]["shape"], by_id["builtin:diagonal"]["rotation"]) == ("cross", 45)
    copy = R.builtin_preset("builtin:box")
    copy["design"]["length"] = 99
    assert R.builtin_preset("builtin:box")["design"]["length"] != 99, "builtin_preset hands out copies"
    assert R.builtin_preset("user:12345678") is None


def test_render_budget():
    worst = {"shape": "cross_circle", "ring_radius": 120, "ring_thickness": 24, "length": 120, "thickness": 24,
             "gap": 60, "rotation": 45, "outline": {"on": True, "thickness": 6}, "dot": {"on": True, "size": 24}}
    ring = {"shape": "circle", "ring_radius": 120, "ring_thickness": 24, "rotation": 45,
            "outline": {"on": True, "thickness": 6}}
    for d in (worst, ring):
        R.render(d, 2160)                          # warm up
        best = min(_timed(d) for _ in range(3))
        assert best < 0.4, f"{d['shape']} took {best * 1000:.0f} ms"


def _timed(d):
    t0 = time.perf_counter()
    img = R.render(d, 2160)
    took = time.perf_counter() - t0
    assert max(img.size) <= CAP
    return took
