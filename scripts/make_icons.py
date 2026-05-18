"""Generate on-brand PWA icons into frontend/icons/.

BUILD-TIME tool only — requires Pillow (`pip install pillow`). NOT a runtime
dependency, so it is intentionally NOT in requirements.txt. The generated PNGs
are committed to the repo; this script exists so they can be regenerated.

Design: solid brand background (#2f6bff) with a centered bold WHITE Hangul
"장" (from 장바구니). If a CJK font can't be loaded, fall back to drawing a
simple white shopping-basket so the script never crashes.

Deterministic and re-runnable. Prints every file written.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Brand palette (matches frontend theme_color / CSS --accent).
BRAND_BG = (47, 107, 255)  # #2f6bff
WHITE = (255, 255, 255)
GLYPH = "장"

# Candidate CJK fonts (Windows dev env), most-preferred first.
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\malgunbd.ttf",
    r"C:\Windows\Fonts\malgun.ttf",
)

_ICONS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "icons"


def _load_font(px: int) -> ImageFont.FreeTypeFont | None:
    """Try the CJK candidates at the requested pixel size; None if all fail."""
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, px)
        except (OSError, ValueError):
            continue
    return None


def _draw_basket(draw: ImageDraw.ImageDraw, size: int) -> None:
    """Fallback mark: a simple white shopping basket (trapezoid + handle)."""
    cx = size / 2
    # Handle: an arc above the basket body.
    handle_w = size * 0.34
    handle_top = size * 0.20
    handle_bottom = size * 0.46
    stroke = max(2, int(size * 0.045))
    draw.arc(
        [cx - handle_w / 2, handle_top, cx + handle_w / 2, handle_bottom],
        start=180,
        end=360,
        fill=WHITE,
        width=stroke,
    )
    # Body: a downward-narrowing trapezoid.
    top_y = size * 0.42
    bot_y = size * 0.78
    top_half = size * 0.30
    bot_half = size * 0.22
    draw.polygon(
        [
            (cx - top_half, top_y),
            (cx + top_half, top_y),
            (cx + bot_half, bot_y),
            (cx - bot_half, bot_y),
        ],
        fill=WHITE,
    )


def _make_icon(size: int, out: Path, glyph_ratio: float) -> None:
    """Render one square icon: brand bg + centered white glyph (or basket)."""
    img = Image.new("RGB", (size, size), BRAND_BG)
    draw = ImageDraw.Draw(img)

    target = size * glyph_ratio
    font = _load_font(int(target))
    if font is None:
        _draw_basket(draw, size)
    else:
        # Measure the actual glyph box and center it precisely.
        bbox = draw.textbbox((0, 0), GLYPH, font=font)
        gw = bbox[2] - bbox[0]
        gh = bbox[3] - bbox[1]
        x = (size - gw) / 2 - bbox[0]
        y = (size - gh) / 2 - bbox[1]
        draw.text((x, y), GLYPH, font=font, fill=WHITE)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG")
    print(f"wrote {out} ({size}x{size})")


def main() -> None:
    # (filename, size, glyph_ratio). Maskable uses a smaller glyph so the mark
    # stays inside Android's circular safe zone. apple-touch is opaque (RGB).
    specs = (
        ("icon-192.png", 192, 0.60),
        ("icon-512.png", 512, 0.60),
        ("maskable-512.png", 512, 0.40),
        ("apple-touch-icon.png", 180, 0.60),
    )
    for name, size, ratio in specs:
        _make_icon(size, _ICONS_DIR / name, ratio)


if __name__ == "__main__":
    main()
