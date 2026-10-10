"""The ticket fonts: the same files the editor loads.

Widths are measured with reportlab's own TTFont, which is the font the PDF
is drawn with. The browser measures the same file with kerning and
ligatures switched off, so both sides sum plain glyph advances and a line
that fits on screen fits on paper.
"""

import os
from functools import lru_cache
from typing import Callable

from fontTools.ttLib import TTFont as FTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "assets",
    "fonts",
)

FALLBACK = "inter-400"

# Mirrors TICKET_FONTS in the TypeScript contract.
TICKET_FONTS: dict[str, dict] = {
    "inter": {"label": "Inter", "note": "Clean sans — the default", "stem": "inter", "weights": [400, 500, 600, 700]},
    "google_sans": {"label": "Google Sans Flex", "note": "Google events · the 24pt optical size", "stem": "google-sans", "weights": [400, 500, 600, 700]},
    "mono": {"label": "Roboto Mono", "note": "Codes, numbers, seats", "stem": "roboto-mono", "weights": [400, 700]},
    "dm_serif": {"label": "DM Serif Display", "note": "Invitations and galas · Latin only", "stem": "dm-serif", "weights": [400]},
    "bebas": {"label": "Bebas Neue", "note": "Tall capitals for headlines · Latin only", "stem": "bebas", "weights": [400]},
}

# Vertical metrics of each file, read once. Baselines are placed with these
# on both sides, so a line sits at the same height on screen and on paper.
FONT_METRICS: dict[str, dict] = {
    "bebas-400": {"unitsPerEm": 1000, "ascent": 900, "descent": -300},
    "dm-serif-400": {"unitsPerEm": 1000, "ascent": 1036, "descent": -335},
    "google-sans-400": {"unitsPerEm": 2000, "ascent": 1932, "descent": -572},
    "google-sans-500": {"unitsPerEm": 2000, "ascent": 1932, "descent": -572},
    "google-sans-600": {"unitsPerEm": 2000, "ascent": 1932, "descent": -572},
    "google-sans-700": {"unitsPerEm": 2000, "ascent": 1932, "descent": -572},
    "inter-400": {"unitsPerEm": 2048, "ascent": 1984, "descent": -494},
    "inter-500": {"unitsPerEm": 2048, "ascent": 1984, "descent": -494},
    "inter-600": {"unitsPerEm": 2048, "ascent": 1984, "descent": -494},
    "inter-700": {"unitsPerEm": 2048, "ascent": 1984, "descent": -494},
    "roboto-mono-400": {"unitsPerEm": 2048, "ascent": 2146, "descent": -555},
    "roboto-mono-700": {"unitsPerEm": 2048, "ascent": 2146, "descent": -555},
}

LINE_HEIGHT = 1.2


def font_weight_of(font: str, weight: int) -> int:
    """The nearest weight a family actually has."""
    weights = TICKET_FONTS[font]["weights"]
    return min(weights, key=lambda w: abs(w - weight))


def font_file_of(font: str, weight: int) -> str:
    """File stem for a family and weight: inter-600."""
    return f"{TICKET_FONTS[font]['stem']}-{font_weight_of(font, weight)}"


def font_path(name: str) -> str:
    known = name if name in FONT_METRICS else FALLBACK
    return os.path.join(FONT_DIR, f"{known}.ttf")


@lru_cache(maxsize=None)
def register(name: str) -> str:
    """Register a file with reportlab once; returns the name to draw with."""
    known = name if name in FONT_METRICS else FALLBACK
    if known not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(known, font_path(known)))
    return known


@lru_cache(maxsize=None)
def _cmap(name: str) -> dict:
    with FTFont(font_path(name), lazy=True) as f:
        return dict(f.getBestCmap() or {})


def covers(name: str, text: str) -> bool:
    """Whether a family can draw every character of a string."""
    cmap = _cmap(name)
    return all(ord(ch) <= 32 or ord(ch) in cmap for ch in text)


def pick(name: str, text: str, safe: bool = False) -> str:
    """The family to draw a string in: the chosen one, or Inter when the chosen one lacks some of its characters."""
    if safe or name not in FONT_METRICS:
        return FALLBACK
    return name if covers(name, text) else FALLBACK


MeasureText = Callable[[str, float], float]


def measure_for(name: str) -> MeasureText:
    """Width of a string at a size, without letter spacing, in the embedded font."""
    registered = register(name)

    def measure(text: str, size: float) -> float:
        return pdfmetrics.stringWidth(text, registered, size)

    return measure
