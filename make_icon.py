"""Generate assets/vispora.ico — the Vispora "Velocity V" brand mark.

Renders the iridescent (Nacre) Velocity V — two tapered blades forming a
sharp V with a diagonal mint -> periwinkle -> lilac -> rose sweep — on a
dark rounded tile, at every standard Windows icon size, packed into one
multi-resolution .ico.

Run:
    python make_icon.py
Re-run any time the brand colours / geometry change.
"""
import os
from PIL import Image, ImageDraw

# ─── Tokens (Vispora Nacre) ───────────────────────────────────────────────
BG     = (10, 11, 18, 255)            # near-black tile (#0a0b12)
# Diagonal gradient stops (top-left -> bottom-right): mint, periwinkle, lilac, rose
GRAD_STOPS = [
    (0.00, (167, 240, 228)),   # #a7f0e4 mint
    (0.40, (184, 200, 255)),   # #b8c8ff periwinkle
    (0.70, (217, 184, 255)),   # #d9b8ff lilac
    (1.00, (255, 196, 230)),   # #ffc4e6 rose
]

# Velocity V geometry in a 96x96 design space (two tapered blades).
V_LEFT  = [(22, 28), (38, 28), (49, 57), (43, 70)]
V_RIGHT = [(49, 57), (58, 28), (74, 28), (55, 70)]

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "vispora.ico")
SIZES = [256, 128, 64, 48, 32, 24, 16]


def _gradient_rgba(n: int) -> Image.Image:
    """An n x n RGBA image holding the diagonal Nacre gradient."""
    # The colour depends only on x + y, so compute one pixel per diagonal and
    # give each row an n-pixel window of that strip (Pillow only, no numpy).
    diag = bytearray()
    for s in range(2 * n - 1):
        t = s / (2 * (n - 1)) if n > 1 else 0.0
        rgb = GRAD_STOPS[0][1]
        for (t0, c0), (t1, c1) in zip(GRAD_STOPS, GRAD_STOPS[1:]):
            if t0 <= t <= t1:            # a later stop wins on a shared edge
                f = (t - t0) / (t1 - t0)
                rgb = tuple(int(a + (b - a) * f) for a, b in zip(c0, c1))
        diag += bytes(rgb) + b"\xff"
    rows = b"".join(bytes(diag[4 * y:4 * (y + n)]) for y in range(n))
    return Image.frombytes("RGBA", (n, n), rows)


def _render(size: int) -> Image.Image:
    """Render one icon at `size`, super-sampled 4x then downsampled."""
    SS = 4
    n = size * SS
    canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    radius = int(n * 0.22)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=radius, fill=BG)
    if size >= 48:   # subtle Win11 mica rim, large sizes only
        d.rounded_rectangle((1, 1, n - 2, n - 2), radius=radius - 1,
                            outline=(255, 255, 255, 14), width=max(1, n // 64))

    # Velocity V — fill the two blades with the diagonal gradient via a mask.
    sc = n / 96.0
    scale = lambda pts: [(x * sc, y * sc) for x, y in pts]
    mask = Image.new("L", (n, n), 0)
    md = ImageDraw.Draw(mask)
    md.polygon(scale(V_LEFT), fill=255)
    md.polygon(scale(V_RIGHT), fill=255)
    canvas.paste(_gradient_rgba(n), (0, 0), mask)

    if size <= 24:
        return canvas.resize((size, size), Image.Resampling.BILINEAR)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    images = [_render(s) for s in SIZES]
    images[0].save(
        OUT_PATH, format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=images[1:],
    )
    sz = os.path.getsize(OUT_PATH)
    print(f"Wrote {OUT_PATH}  ({sz} bytes, {len(SIZES)} resolutions)")


if __name__ == "__main__":
    main()
