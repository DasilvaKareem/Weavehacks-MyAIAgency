"""Global font system — gives the whole game a crisp 90s CRT-terminal look.

The game draws ~470 strings through raylib's built-in 10px bitmap font, scaled
up to 14-28px, which looks blocky. Instead of editing every call site, this
module loads one real TTF (VT323, an SIL-OFL DEC-VT320 terminal font) at a high
base size and *wraps* ``pr.draw_text`` / ``pr.measure_text`` so every existing
call renders with it automatically.

Call :func:`init` once, right after ``init_window`` (the GL context must exist
before a font/texture can be uploaded). Swapping the look later is a one-line
change to ``FONT_PATH``.
"""

from __future__ import annotations

import os

import pyray as pr

FONT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "fonts", "VT323-Regular.ttf",
)

# Rasterize the atlas big so glyphs stay sharp when scaled down to any UI size.
# With HIGHDPI a 28px logical glyph is ~56 physical px, so the atlas must be
# comfortably larger than that — 96 gives clean downscale headroom at every size.
# Bilinear filtering then smooths it into a soft CRT glow rather than the hard
# stairstepping of the old upscaled bitmap font.
_BASE_SIZE = 96

# Codepoints to bake into the atlas: ASCII + Latin-1 (é, í, ±, ×, –, "" …) plus a
# few typographic extras the game actually uses. Glyphs VT323 lacks (arrows,
# box-drawing, emoji) simply come out blank — exactly as they did with the old
# default font, so this is parity, not a regression.
_EXTRA = [
    0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026,
    0x2212, 0x2248, 0x2264, 0x2265,
]
_CODEPOINTS = list(range(32, 127)) + list(range(160, 256)) + _EXTRA

_font: "pr.Font | None" = None
_orig_draw_text = pr.draw_text
_orig_measure_text = pr.measure_text


def _spacing(font_size: float) -> float:
    """Per-glyph spacing scaled to the draw size (VT323 is tight by default)."""
    return max(1.0, font_size / 20.0)


def _draw_text(text, pos_x, pos_y, font_size, color):
    if _font is None:
        return _orig_draw_text(text, int(pos_x), int(pos_y), int(font_size), color)
    pr.draw_text_ex(
        _font, str(text), pr.Vector2(float(pos_x), float(pos_y)),
        float(font_size), _spacing(font_size), color,
    )


def _measure_text(text, font_size) -> int:
    if _font is None:
        return _orig_measure_text(text, int(font_size))
    return int(pr.measure_text_ex(
        _font, str(text), float(font_size), _spacing(font_size),
    ).x)


def init() -> bool:
    """Load VT323 and route the whole game's text through it. Idempotent.

    Returns True if the custom font loaded; False (and leaves the stock font in
    place) if the TTF is missing or fails to load, so the game still runs.
    """
    global _font
    if _font is not None:
        return True
    if not os.path.exists(FONT_PATH):
        print(f"[fonts] {FONT_PATH} not found — using default font")
        return False
    try:
        # Keep the int[] buffer in a named var for the duration of the call —
        # casting an inline ffi.new() lets its owner be freed, so load_font_ex
        # would read garbage codepoints and bake tiny/broken glyphs.
        cp_buf = pr.ffi.new("int[]", _CODEPOINTS)
        font = pr.load_font_ex(
            FONT_PATH, _BASE_SIZE, pr.ffi.cast("int *", cp_buf), len(_CODEPOINTS),
        )
        if not pr.is_font_valid(font):
            print("[fonts] VT323 failed to load — using default font")
            return False
        pr.set_texture_filter(font.texture, pr.TEXTURE_FILTER_BILINEAR)
        _font = font
    except Exception as exc:  # never let a font hiccup take down the game
        print(f"[fonts] load error ({exc}) — using default font")
        return False

    # Redirect every existing pr.draw_text / pr.measure_text call site.
    pr.draw_text = _draw_text
    pr.measure_text = _measure_text
    print("[fonts] VT323 loaded — CRT terminal mode")
    return True


def font() -> "pr.Font | None":
    """The loaded VT323 Font (for code that wants draw_text_ex directly), or None."""
    return _font
