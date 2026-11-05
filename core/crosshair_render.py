"""Crosshair bitmaps: a design dict in, an RGBA image out.

Pure Pillow and no Win32, so the overlay thread, Flask handlers and tests can
all import it. Sizes in a design are pixels at 1080p (scaled() converts them
for a monitor). Straight, axis-aligned parts are drawn pixel-exact at 1x;
rings, the chevron and rotated parts are drawn at 4x and scaled down with
LANCZOS. Every image has odd sides, and its middle pixel is the aim point.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import re

from PIL import Image, ImageChops, ImageDraw

SHAPES = ("cross", "dot", "circle", "circle_dot", "cross_circle", "chevron", "box")
KINDS = ("shape", "image")
BASE_HEIGHT = 1080
MAX_SIDE = 511                  # odd; caps the layered window whatever a design asks for
SUPERSAMPLE = 4
_LANCZOS_REACH = 3              # px a LANCZOS downscale can spread past an edge

DEFAULT_DESIGN = {
    "kind": "shape",
    "shape": "cross",
    "color": "#00FFFF",
    "opacity": 100,
    "length": 6,
    "thickness": 2,
    "gap": 3,
    "arms": {"top": True, "bottom": True, "left": True, "right": True},
    "dot": {"on": False, "size": 2, "color": "#00FFFF", "opacity": 100},
    "ring_radius": 8,
    "ring_thickness": 2,
    "outline": {"on": True, "thickness": 1, "color": "#000000", "opacity": 100},
    "rotation": 0,
    "scale_with_resolution": True,
    "image": {"id": "", "scale": 100, "opacity": 100},
}

_RANGES = {"opacity": (0, 100), "length": (1, 120), "thickness": (1, 24), "gap": (0, 60),
           "ring_radius": (2, 120), "ring_thickness": (1, 24)}
_SUB_RANGES = {"dot": {"size": (1, 24), "opacity": (0, 100)},
               "outline": {"thickness": (1, 6), "opacity": (0, 100)},
               "image": {"scale": (10, 400), "opacity": (0, 100)}}
# Field, smallest value after scaling.
_SCALED_FIELDS = (("length", 1), ("thickness", 1), ("gap", 0), ("ring_radius", 2), ("ring_thickness", 1))
_HEX_RE = re.compile(r"[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}")
_IMAGE_ID_RE = re.compile(r"[0-9a-f]{32}")

# Layers, bottom to top: every outline, then the shape, then the centre dot.
_OUTLINE, _FILL, _DOT = 0, 1, 2


# ── normalising ───────────────────────────────────────────────────────
def _round(x: float) -> int:
    """Half away from zero: 2.5 px becomes 3 px (round() would give 2)."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _num(v):
    """A finite float from an int, a float or a numeric string; None otherwise.
    Booleans are not numbers here."""
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, (int, float)):
            f = float(v)
        elif isinstance(v, str):
            f = float(v.strip())
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _int(v, lo, hi, default):
    f = _num(v)
    return default if f is None else max(lo, min(hi, _round(f)))


def _bool(v, default):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return default if v != v else v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    return default


def _hex(v, default):
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("#"):
            s = s[1:]
        if _HEX_RE.fullmatch(s):
            if len(s) == 3:
                s = "".join(ch * 2 for ch in s)
            return "#" + s.upper()
    return default


def _enum(v, allowed, default):
    if isinstance(v, str):
        s = v.strip().lower().replace("-", "_").replace(" ", "_")
        if s in allowed:
            return s
    return default


def _merge(out: dict, d: dict) -> None:
    out["kind"] = _enum(d.get("kind"), KINDS, out["kind"])
    out["shape"] = _enum(d.get("shape"), SHAPES, out["shape"])
    out["color"] = _hex(d.get("color"), out["color"])
    for key, (lo, hi) in _RANGES.items():
        out[key] = _int(d.get(key), lo, hi, out[key])
    # Rotation wraps instead of clamping: -45 means 315, 360 means 0.
    out["rotation"] = _int(d.get("rotation"), -10 ** 9, 10 ** 9, out["rotation"]) % 360
    out["scale_with_resolution"] = _bool(d.get("scale_with_resolution"), out["scale_with_resolution"])
    arms = d.get("arms")
    if isinstance(arms, dict):
        for side in out["arms"]:
            out["arms"][side] = _bool(arms.get(side), out["arms"][side])
    for sub, ranges in _SUB_RANGES.items():
        src = d.get(sub)
        if not isinstance(src, dict):
            continue
        dst = out[sub]
        for key, (lo, hi) in ranges.items():
            dst[key] = _int(src.get(key), lo, hi, dst[key])
        if "on" in dst:
            dst["on"] = _bool(src.get("on"), dst["on"])
        if "color" in dst:
            dst["color"] = _hex(src.get("color"), dst["color"])
        if sub == "image":
            iid = src.get("id")
            iid = iid.strip().lower() if isinstance(iid, str) else ""
            dst["id"] = iid if _IMAGE_ID_RE.fullmatch(iid) else ""


def normalize_design(d) -> dict:
    """A complete, valid design from anything: missing keys take the default,
    numbers are coerced and clamped, enums and colours are checked, unknown
    keys are dropped. Never raises."""
    out = copy.deepcopy(DEFAULT_DESIGN)
    if not isinstance(d, dict):
        return out
    try:
        _merge(out, d)
    except Exception:           # a dict subclass with a hostile get(), say
        return copy.deepcopy(DEFAULT_DESIGN)
    return out


def _target_height(h) -> int:
    f = _num(h)
    return BASE_HEIGHT if f is None or f < 1 else min(16384, _round(f))


def scaled(design, target_height) -> dict:
    """The design with its pixel sizes converted from 1080p to target_height,
    when scale_with_resolution is on. Whole pixels; sizes that start at 1 stay
    at least 1."""
    d = normalize_design(design)
    if not d["scale_with_resolution"]:
        return d
    f = _target_height(target_height) / BASE_HEIGHT
    if f == 1.0:
        return d
    for key, lo in _SCALED_FIELDS:
        d[key] = max(lo, _round(d[key] * f))
    d["dot"]["size"] = max(1, _round(d["dot"]["size"] * f))
    d["outline"]["thickness"] = max(1, _round(d["outline"]["thickness"] * f))
    return d


# ── geometry ──────────────────────────────────────────────────────────
# Coordinates are continuous and relative to the centre pixel, which covers
# [0, 1) x [0, 1); y grows downwards. A part is either
#   ("box", layer, cx, cy, half_w, half_h, angle_deg)   a rectangle turned by angle
#   ("ring", layer, cx, cy, outer_radius, width)

def _cos_sin(deg):
    """cos and sin of whole degrees, exact at right angles, and computed from
    the angle's distance to the x axis so that mirrored angles (a, 180 - a,
    -a) give exactly mirrored values and mirrored designs stay symmetric."""
    d = deg % 360
    exact = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}
    if d in exact:
        return exact[d]
    ref = min(d % 180, 180 - d % 180)
    if ref == 45:
        c = s = math.sqrt(0.5)
    else:
        c, s = math.cos(math.radians(ref)), math.sin(math.radians(ref))
    return (c if d < 90 or d > 270 else -c), (s if d < 180 else -s)


def _snap(v: float):
    """Whole subpixel coordinates for v: one, or both neighbours when v is
    exactly halfway, so a vertex on an axis of symmetry stays symmetric."""
    lo = math.floor(v)
    if abs(v - lo - 0.5) < 1e-6:
        return [lo, lo + 1]
    return [int(round(v))]


def _polygon(corners):
    """Vertices for ImageDraw.polygon, which takes whole numbers. A vertex
    halfway between two subpixels becomes a one-subpixel flat edge."""
    pts = []
    for i, (x, y) in enumerate(corners):
        cand = [(a, b) for a in _snap(x) for b in _snap(y)]
        if len(cand) == 2:
            px, py = corners[i - 1]
            cand.sort(key=lambda q: (q[0] - px) ** 2 + (q[1] - py) ** 2)
        elif len(cand) == 4:            # halfway on both axes: rare enough to take one corner
            cand = cand[:1]
        pts.extend(cand)
    return pts


def _band(t: int):
    """Pixel range of a band t wide centred on pixel 0: an even t puts the
    extra pixel right / down."""
    lo = -((t - 1) // 2)
    return lo, lo + t - 1


def _band_centre(t: int) -> float:
    return 0.5 if t % 2 else 1.0


def _rect(layer, x0, y0, x1, y1):
    """A box from an inclusive pixel range."""
    return ("box", layer, (x0 + x1 + 1) / 2, (y0 + y1 + 1) / 2, (x1 - x0 + 1) / 2, (y1 - y0 + 1) / 2, 0)


def _with_outline(parts, rects, layer, o):
    """Pixel rectangles for one layer, each with its outline: the same
    rectangle grown by o on every side."""
    for x0, y0, x1, y1 in rects:
        if o:
            parts.append(_rect(_OUTLINE, x0 - o, y0 - o, x1 + o, y1 + o))
        parts.append(_rect(layer, x0, y0, x1, y1))


def _cross_rects(s):
    t, length, gap = s["thickness"], s["length"], s["gap"]
    b0, b1 = _band(t)
    near = gap + (1 - t % 2)    # even bands centre on a pixel corner; mirror the arms about it
    far = near + length - 1
    rects = []
    arms = s["arms"]
    if arms["right"]:
        rects.append((near, b0, far, b1))
    if arms["left"]:
        rects.append((-(gap + length - 1), b0, -gap, b1))
    if arms["bottom"]:
        rects.append((b0, near, b1, far))
    if arms["top"]:
        rects.append((b0, -(gap + length - 1), b1, -gap))
    return rects


def _box_rects(s):
    half, t = s["length"], s["thickness"]
    if 2 * t >= 2 * half + 1:
        return [(-half, -half, half, half)]
    # The bars overlap at the corners rather than meet: two supersampled
    # edges that only touch leave a faint seam once the box is turned.
    return [(-half, -half, half, -half + t - 1),            # top
            (-half, half - t + 1, half, half),              # bottom
            (-half, -half, -half + t - 1, half),            # left
            (half - t + 1, -half, half, half)]              # right


def _ring(parts, layer, centre, radius, width, o):
    outer = radius + width / 2
    if o:
        parts.append(("ring", _OUTLINE, centre, centre, outer + o, width + 2 * o))
    parts.append(("ring", layer, centre, centre, outer, width))


def _chevron(parts, s, centre, o):
    """A V pointing up, 90 degrees between the arms, apex `gap` below centre:
    two arm rectangles plus a square turned 45 degrees at the apex, which
    together make a mitred stroke. The outline is the same stroke made 2*o
    wider and o longer at the free ends."""
    length, t = s["length"], s["thickness"]
    ax, ay = centre, centre + s["gap"]
    for ang in (45, 135):
        c, si = _cos_sin(ang)
        if o:
            reach = length + o
            parts.append(("box", _OUTLINE, ax + c * reach / 2, ay + si * reach / 2, reach / 2, t / 2 + o, ang))
        parts.append(("box", _FILL, ax + c * length / 2, ay + si * length / 2, length / 2, t / 2, ang))
    if o:
        parts.append(("box", _OUTLINE, ax, ay, t / 2 + o, t / 2 + o, 45))
    parts.append(("box", _FILL, ax, ay, t / 2, t / 2, 45))


def _parts(s):
    """Every part of a (scaled) shape design, and the point it turns about."""
    shape = s["shape"]
    o = s["outline"]["thickness"] if s["outline"]["on"] else 0
    dot_size = s["dot"]["size"]
    has_dot = shape == "circle_dot" or (s["dot"]["on"] and shape != "dot")
    if shape in ("cross", "cross_circle", "dot"):
        centre = _band_centre(s["thickness"])
    elif has_dot and shape != "box":
        centre = _band_centre(dot_size)
    else:
        centre = 0.5
    parts = []
    if shape in ("cross", "cross_circle"):
        _with_outline(parts, _cross_rects(s), _FILL, o)
    if shape in ("circle", "circle_dot", "cross_circle"):
        _ring(parts, _FILL, centre, s["ring_radius"], s["ring_thickness"], o)
    if shape == "dot":
        b0, b1 = _band(s["thickness"])
        _with_outline(parts, [(b0, b0, b1, b1)], _FILL, o)
    elif shape == "chevron":
        _chevron(parts, s, centre, o)
    elif shape == "box":
        _with_outline(parts, _box_rects(s), _FILL, o)
    if has_dot:
        b0, b1 = _band(dot_size)
        _with_outline(parts, [(b0, b0, b1, b1)], _DOT, o)
    return parts, centre


def _corners(p):
    _, _, cx, cy, hw, hh, ang = p
    c, s = _cos_sin(ang)
    return [(cx + u * c - v * s, cy + u * s + v * c) for u, v in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def _rotate(p, deg, centre):
    c, s = _cos_sin(deg)
    dx, dy = p[2] - centre, p[3] - centre
    cx, cy = centre + dx * c - dy * s, centre + dx * s + dy * c
    if p[0] == "box":
        return ("box", p[1], cx, cy, p[4], p[5], (p[6] + deg) % 360)
    return ("ring", p[1], cx, cy, p[4], p[5])


def _shrink(p, f):
    """Scale a part by f about the middle of the centre pixel."""
    cx, cy = 0.5 + (p[2] - 0.5) * f, 0.5 + (p[3] - 0.5) * f
    if p[0] == "box":
        return ("box", p[1], cx, cy, p[4] * f, p[5] * f, p[6])
    return ("ring", p[1], cx, cy, p[4] * f, p[5] * f)


def _exact_rect(p):
    """The inclusive pixel range of an axis-aligned box on whole pixels, else None."""
    if p[0] != "box" or p[6] % 90:
        return None
    xs, ys = zip(*_corners(p))
    edges = (min(xs), min(ys), max(xs), max(ys))
    if any(abs(v - round(v)) > 1e-6 for v in edges):
        return None
    x0, y0, x1, y1 = (int(round(v)) for v in edges)
    return x0, y0, x1 - 1, y1 - 1


def _extent(parts):
    """Furthest reach from the middle of the centre pixel, per axis."""
    ex = ey = 0.0
    for p in parts:
        if p[0] == "box":
            for x, y in _corners(p):
                ex, ey = max(ex, abs(x - 0.5)), max(ey, abs(y - 0.5))
        else:
            ex = max(ex, abs(p[2] - 0.5) + p[4])
            ey = max(ey, abs(p[3] - 0.5) + p[4])
    return ex, ey


def _rasterize(parts, w, h, hx, hy):
    """One coverage mask (mode L) per layer, or None for an empty layer."""
    k = SUPERSAMPLE
    exact = [None, None, None]
    fine = [None, None, None]
    for p in parts:
        layer = p[1]
        r = _exact_rect(p)
        if r is not None:
            if exact[layer] is None:
                exact[layer] = Image.new("L", (w, h), 0)
            x0, y0, x1, y1 = r
            ImageDraw.Draw(exact[layer]).rectangle((x0 + hx, y0 + hy, x1 + hx, y1 + hy), fill=255)
            continue
        if fine[layer] is None:
            fine[layer] = Image.new("L", (w * k, h * k), 0)
        draw = ImageDraw.Draw(fine[layer])
        cx, cy = (p[2] + hx) * k, (p[3] + hy) * k
        if p[0] == "box":
            # Pillow puts pixel i's centre at coordinate i, floors vertices and
            # fills edge pixels too. So: pull every side in by half a
            # subpixel, move to its centre-based coordinates, and round the
            # vertices ourselves (flooring would push every shape up-left).
            q = ("box", layer, cx, cy, max(p[4] * k - 0.5, 0.0), max(p[5] * k - 0.5, 0.0), p[6])
            draw.polygon(_polygon([(x - 0.5, y - 0.5) for x, y in _corners(q)]), fill=255)
        else:
            radius, width = p[4] * k, p[5] * k
            bbox = (_round(cx - radius), _round(cy - radius), _round(cx + radius) - 1, _round(cy + radius) - 1)
            wpx = max(1, _round(width))
            if wpx >= radius:
                draw.ellipse(bbox, fill=255)
            else:
                draw.ellipse(bbox, outline=255, width=wpx)
    masks = []
    for layer in range(3):
        m1, m4 = exact[layer], fine[layer]
        if m4 is not None:
            m4 = m4.resize((w, h), Image.Resampling.LANCZOS)
            m1 = m4 if m1 is None else ImageChops.lighter(m1, m4)
        masks.append(m1)
    return masks


def _rgb(colour):
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def _compose(w, h, layers):
    """Stack the layers, each replacing what is under it in proportion to its
    coverage. Done per band in premultiplied form, which is what makes the
    soft edges of rings and rotated parts come out right."""
    bands = [Image.new("L", (w, h), 0) for _ in range(4)]
    for mask, colour, opacity in layers:
        if mask is None:
            continue
        a = _round(opacity * 255 / 100)
        values = [(c * a + 127) // 255 for c in _rgb(colour)] + [a]
        for band, v in zip(bands, values):
            band.paste(v, None, mask)
    return Image.merge("RGBa", bands).convert("RGBA")


def _render_shape(s) -> Image.Image:
    parts, centre = _parts(s)
    if s["rotation"]:
        parts = [_rotate(p, s["rotation"], centre) for p in parts]
    exact = all(_exact_rect(p) is not None for p in parts)
    pad = 1 if exact else 1 + _LANCZOS_REACH
    ex, ey = _extent(parts)
    limit = MAX_SIDE // 2
    if max(math.ceil(ex - 0.5 - 1e-9), math.ceil(ey - 0.5 - 1e-9)) + pad > limit:
        pad = 1 + _LANCZOS_REACH
        f = (limit + 0.5 - pad) / max(ex, ey)
        parts = [_shrink(p, f) for p in parts]
        ex, ey = _extent(parts)
    hx = max(0, math.ceil(ex - 0.5 - 1e-9)) + pad
    hy = max(0, math.ceil(ey - 0.5 - 1e-9)) + pad
    w, h = 2 * hx + 1, 2 * hy + 1
    masks = _rasterize(parts, w, h, hx, hy)
    return _compose(w, h, [(masks[_OUTLINE], s["outline"]["color"], s["outline"]["opacity"]),
                           (masks[_FILL], s["color"], s["opacity"]),
                           (masks[_DOT], s["dot"]["color"], s["dot"]["opacity"])])


# ── images ────────────────────────────────────────────────────────────
def _odd(x: float) -> int:
    return max(1, min(MAX_SIDE, int(x) | 1))


def _fit_cap(img: Image.Image) -> Image.Image:
    w, h = img.size
    if max(w, h) <= MAX_SIDE:
        return img
    f = MAX_SIDE / max(w, h)
    return img.resize((_odd(w * f), _odd(h * f)), Image.Resampling.LANCZOS)


def _render_image(d, image_loader):
    iid = d["image"]["id"]
    if not iid or not callable(image_loader):
        return None
    try:
        src = image_loader(iid)
        if not isinstance(src, Image.Image) or min(src.size) < 1:
            return None
        im = src.convert("RGBA")                    # always a copy: the caller's image stays as it was
        w, h = im.size
        f = d["image"]["scale"] / 100
        c, s = (abs(v) for v in _cos_sin(d["rotation"]))
        span = max(w * c + h * s, w * s + h * c) * f
        # A 1 px margin each side. Right angles are exact transposes, so only
        # other angles need slack for rotate(); without that a 509 px image at
        # 100% would be resampled (and blurred) for no reason.
        room = MAX_SIDE - (2 if d["rotation"] % 90 == 0 else 4)
        if span > room:
            f *= room / span

        def _scaled(n):
            # An odd side keeps a centre pixel, so it stays odd: the nearest odd
            # size. Free rounding would make 31 px at 200% 62 px and put the
            # image half a pixel up and left of the aim point. room is odd, so
            # this never goes past it.
            x = n * f
            return max(1, 2 * _round((x - 1) / 2) + 1) if n % 2 else max(1, _round(x))
        size = (_scaled(w), _scaled(h))
        if size != im.size:
            im = im.resize(size, Image.Resampling.LANCZOS)
        opacity = d["image"]["opacity"]
        if opacity < 100:
            im.putalpha(im.getchannel("A").point([_round(v * opacity / 100) for v in range(256)]))
        if d["rotation"]:
            im = im.convert("RGBa").rotate(-d["rotation"], resample=Image.Resampling.BICUBIC,
                                           expand=True).convert("RGBA")
        w, h = im.size
        cw, ch = (w + 2) | 1, (h + 2) | 1
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        canvas.paste(im, ((cw - w) // 2, (ch - h) // 2))
        return _fit_cap(canvas)
    except Exception:
        return None


# ── public ────────────────────────────────────────────────────────────
def render(design, target_height=BASE_HEIGHT, image_loader=None) -> Image.Image:
    """The crosshair as an RGBA image with odd width and height; pixel
    (w // 2, h // 2) is the aim point. Never larger than 511 x 511.

    kind "image" asks image_loader(id) for a PIL image; when that fails the
    default crosshair is drawn instead, with img.info["warning"] set to
    "image missing"."""
    d = normalize_design(design)
    if d["kind"] == "image":
        img = _render_image(d, image_loader)
        if img is not None:
            return img
        img = _render_shape(scaled(DEFAULT_DESIGN, target_height))
        img.info["warning"] = "image missing"
        return img
    return _render_shape(scaled(d, target_height))


def to_premultiplied_bgra(img: Image.Image) -> bytes:
    """Top-down rows of B, G, R, A with the colour already multiplied by
    alpha (round(c * a / 255)): what UpdateLayeredWindow expects."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    return img.convert("RGBa").tobytes("raw", "BGRa")


def to_png_data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def design_hash(design) -> str:
    """Stable short hash of the normalized design, for caching thumbnails."""
    text = json.dumps(normalize_design(design), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _preset(pid, name, **changes):
    d = copy.deepcopy(DEFAULT_DESIGN)
    for key, value in changes.items():
        if isinstance(value, dict):
            d[key].update(value)
        else:
            d[key] = value
    return {"id": pid, "name": name, "design": normalize_design(d)}


BUILTIN_PRESETS = (
    _preset("builtin:classic", "Classic"),
    _preset("builtin:dot", "Dot", shape="dot", thickness=4),
    _preset("builtin:cross-dot", "Cross and dot", length=5, gap=4, dot={"on": True, "size": 2}),
    _preset("builtin:t-shape", "T-shape", arms={"top": False}),
    _preset("builtin:ring", "Ring", shape="circle", ring_radius=10, ring_thickness=2),
    _preset("builtin:ring-dot", "Ring and dot", shape="circle_dot", ring_radius=10, ring_thickness=2,
            dot={"size": 2}),
    _preset("builtin:crossed-circle", "Crossed circle", shape="cross_circle", length=8, gap=6,
            ring_radius=10, ring_thickness=2),
    _preset("builtin:chevron", "Chevron", shape="chevron", length=8, thickness=2, gap=2),
    _preset("builtin:box", "Box", shape="box", length=7, thickness=2),
    _preset("builtin:thin-cross", "Thin cross", thickness=1, length=10),
    _preset("builtin:orange", "Orange", color="#E69F00", dot={"color": "#E69F00"}),
    _preset("builtin:diagonal", "Diagonal", rotation=45),
)
BUILTIN_IDS = tuple(p["id"] for p in BUILTIN_PRESETS)


def builtin_preset(preset_id):
    """A copy of the built-in preset with this id ({"id", "name", "design"}), or None."""
    for p in BUILTIN_PRESETS:
        if p["id"] == preset_id:
            return copy.deepcopy(p)
    return None
