"""Shrinking text to fit the box the designer drew.

Artwork does not reflow. A layer is a fixed box on someone's design, and a
name longer than the box was built for has to give somewhere: it can be cut
off, or it can be set smaller. Cutting is what the renderer did, which is how
"Lakshminarayanan Venkataraman" arrived on a certificate as "Lakshminaraya".

Setting it smaller needs the rendered width of a string, and the renderer is
wkhtmltopdf -- there is no font metric available here, and asking for one
would mean shipping the font files and a shaping engine. So this estimates
width from a per-character table. It is approximate, and deliberately
conservative: the `max_lines` clip stays in the template as the backstop, so
a bad estimate still cannot push text outside the artwork.

The estimate only ever reduces a font size, never raises it, so a layer that
already fits renders at exactly the size the designer chose.
"""

import math
from typing import Optional

# Width of each character as a fraction of the font size, for the sans-serif
# faces the renderer resolves. Measured roughly from Inter at 400; the point
# is the relative spread, since 'i' and 'W' differ by more than 3x and a flat
# average puts a name of either kind badly wrong.
_NARROW = set("iljtfIr.,;:'\"`!|()[]{}/\\ ")
_WIDE = set("mwMW@%&")
_UPPER = set("ABCDEFGHJKLNOPQRSTUVXYZ")
_DIGITS = set("0123456789")

_W_NARROW = 0.30
_W_WIDE = 0.92
_W_UPPER = 0.67
_W_DIGIT = 0.56
_W_DEFAULT = 0.52

# A bold face is wider at the same size. Applied as a flat multiplier because
# the per-character spread is roughly preserved between weights.
_BOLD_FACTOR = 1.06

# Never shrink past this, as a fraction of the size the designer chose. Below
# it the text is unreadable and clipping is the more honest failure -- a club
# should see that the box is too small rather than find 6px type on a ticket.
_FLOOR_RATIO = 0.45
_ABSOLUTE_FLOOR = 8


def estimate_text_width(text: str, font_size: float, *, bold: bool = False,
                        letter_spacing: float = 0) -> float:
    """Approximate rendered width of `text`, in pixels."""
    units = 0.0
    for char in text:
        if char in _NARROW:
            units += _W_NARROW
        elif char in _WIDE:
            units += _W_WIDE
        elif char in _UPPER:
            units += _W_UPPER
        elif char in _DIGITS:
            units += _W_DIGIT
        else:
            units += _W_DEFAULT

    width = units * font_size
    if bold:
        width *= _BOLD_FACTOR
    # Spacing is added between characters, so one fewer gap than characters.
    if letter_spacing and len(text) > 1:
        width += letter_spacing * (len(text) - 1)
    return width


def lines_needed(text: str, box_width: float, font_size: float, *,
                 bold: bool = False, letter_spacing: float = 0) -> int:
    """How many lines `text` wraps onto in a box this wide.

    Greedy word wrap, matching how a browser breaks a paragraph. A single
    word wider than the box counts as one line rather than being broken,
    because the renderer does not break words either -- it overflows, and
    the clip catches it.
    """
    if box_width <= 0:
        return 1

    def width(value: str) -> float:
        return estimate_text_width(
            value, font_size, bold=bold, letter_spacing=letter_spacing
        )

    lines = 1
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and width(candidate) > box_width:
            lines += 1
            current = word
        else:
            current = candidate
    return lines


def fit_font_size(
    text: str,
    *,
    font_size: int,
    box_width: Optional[float],
    max_lines: int = 1,
    bold: bool = False,
    letter_spacing: float = 0,
) -> int:
    """The largest size at or below `font_size` where `text` fits.

    Returns `font_size` unchanged when the text already fits, when there is
    no box to fit it into, or when there is no text -- so this is a no-op for
    every layer that was already fine.
    """
    if not text or not text.strip():
        return font_size
    if not box_width or box_width <= 0:
        return font_size

    if lines_needed(
        text, box_width, font_size, bold=bold, letter_spacing=letter_spacing
    ) <= max_lines:
        return font_size

    floor = max(_ABSOLUTE_FLOOR, math.ceil(font_size * _FLOOR_RATIO))
    # Walk down rather than binary search: the range is at most a couple of
    # hundred integers, it runs once per layer per document, and the
    # monotonicity a binary search would rely on is only approximate here.
    for candidate in range(font_size - 1, floor - 1, -1):
        if lines_needed(
            text, box_width, candidate, bold=bold, letter_spacing=letter_spacing
        ) <= max_lines:
            return candidate
    return floor
