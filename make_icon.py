"""Generate assets/ghostshell.ico from the brand mark.

Renders the same stacked-layers ghost mark used in the titlebar + boot
splash at every standard Windows icon size (16/24/32/48/64/128/256),
then packs them into one multi-resolution .ico file.

Run:
    python make_icon.py

Re-run any time the brand colours / geometry change.
"""
import math
import os
from PIL import Image, ImageDraw

# ─── Tokens (match style.css) ─────────────────────────────────────────────
BG          = (10, 11, 13, 255)      # #0a0b0d
BG_INNER    = (6,  7,  8,  255)      # slight gradient feel
ACCENT      = (94, 221, 162, 255)    # #5edda2
ACCENT_DEEP = (46, 168, 100, 255)    # #2ea864 — for the layered look
ACCENT_DIM  = (94, 221, 162, 110)    # for the bottom-layer alpha
SHADOW      = (0, 0, 0, 90)

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "ghostshell.ico")
SIZES = [256, 128, 64, 48, 32, 24, 16]


def _stacked_layers_path(size: int):
    """Return the three diamond-row polygons that make the mark.

    The brand SVG is the Lucide 'layers' glyph — three stacked
    parallelograms.  We translate that to pixel coords for a given size.
    """
    s = size
    # Geometry from the SVG (24x24 viewBox), scaled to pixel size.
    # Top diamond:    (12,2)-(2,7)-(12,12)-(22,7)
    # Middle:         (2,12)-(12,17)-(22,12)
    # Bottom:         (2,17)-(12,22)-(22,17)
    def p(x, y):
        return (x / 24 * s, y / 24 * s)

    top    = [p(12, 2),  p(2, 7),   p(12, 12), p(22, 7)]
    middle = [p(2, 12),  p(12, 17), p(22, 12)]
    bottom = [p(2, 17),  p(12, 22), p(22, 17)]
    return top, middle, bottom


def _render(size: int) -> Image.Image:
    """Render a single icon at the requested edge size, with anti-alias.

    Strategy: render at 4× then downsample to keep edges crisp at small
    sizes (Windows uses the pre-rendered 16/24/32 directly without
    further filtering).
    """
    SS = 4
    canvas = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    # Rounded square background.  Win11 icon shape is squircle-ish, but
    # we use a simple rounded-rect — Windows clips it to the taskbar
    # shape automatically when displayed on the taskbar.
    radius = int(size * SS * 0.22)
    d.rounded_rectangle(
        (0, 0, size * SS - 1, size * SS - 1),
        radius=radius,
        fill=BG,
    )

    # Subtle inner highlight rim (Win11 mica feel) — only at large sizes
    if size >= 48:
        rim_color = (255, 255, 255, 14)
        d.rounded_rectangle(
            (1, 1, size * SS - 2, size * SS - 2),
            radius=radius - 1,
            outline=rim_color,
            width=max(1, size * SS // 64),
        )

    # Stacked layers — translate to a centered mini-canvas
    inner = int(size * SS * 0.62)
    pad = (size * SS - inner) // 2
    top, middle, bottom = _stacked_layers_path(inner)
    top    = [(x + pad, y + pad) for x, y in top]
    middle = [(x + pad, y + pad) for x, y in middle]
    bottom = [(x + pad, y + pad) for x, y in bottom]

    # Bottom layer — most translucent
    d.polygon(bottom, fill=ACCENT_DIM)
    # Middle layer — medium accent
    d.polygon(middle, fill=ACCENT_DEEP)
    # Top layer — full accent (the "lit" surface)
    d.polygon(top, fill=ACCENT)

    # Tiny outline on the top layer to crisp it against the bg
    line_w = max(1, size * SS // 96)
    d.polygon(top, outline=(255, 255, 255, 25), width=line_w)

    # Downsample with bicubic for smooth edges (except at tiny sizes
    # where bicubic blurs too much — use bilinear for 16/24).
    if size <= 24:
        return canvas.resize((size, size), Image.Resampling.BILINEAR)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    images = [_render(s) for s in SIZES]
    # Pillow's .ico writer takes the largest image first and an
    # explicit `sizes` list of the resolutions to include.
    images[0].save(
        OUT_PATH,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=images[1:],
    )
    sz = os.path.getsize(OUT_PATH)
    print(f"Wrote {OUT_PATH}  ({sz} bytes, {len(SIZES)} resolutions)")


if __name__ == "__main__":
    main()
