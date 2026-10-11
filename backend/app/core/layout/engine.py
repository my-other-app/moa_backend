"""Laying text out, and judging a design -- shared with the editor.

`layout_text` is the same algorithm the browser runs, so a line that wraps
on screen wraps on paper. `layout_problems` is what makes a design wrong:
one readable QR where one is needed, known tokens, nothing off the ticket.
"""

import secrets
import time
from dataclasses import dataclass, field

from app.core.layout.fonts import FONT_METRICS, LINE_HEIGHT, TICKET_FONTS, MeasureText
from app.core.layout.model import (
    PageSpec,
    TicketCanvas,
    TicketLayer,
    contrast_ratio,
    luminance,
    min_qr_size,
    ticket_area,
)
from app.core.layout.tokens import parse_ticket_template

MIN_SCALE = 0.5


@dataclass
class LaidOutText:
    size: float
    letter_spacing: float
    lines: list  # [{"text", "width"}]
    baselines: list  # distance from the layer's top to each baseline
    height: float
    truncated: bool


def layout_text(
    text: str,
    *,
    width: float,
    size: float,
    max_lines: int,
    line_spacing: float,
    letter_spacing: float,
    fit: str,
    font_file: str,
    measure: MeasureText,
) -> LaidOutText:
    """Wrap, shrink to half size if asked, then cut with an ellipsis."""
    metrics = FONT_METRICS.get(font_file) or {"unitsPerEm": 1000, "ascent": 950, "descent": -250}

    def width_at(s: str, sz: float, ls: float) -> float:
        return measure(s, sz) + ls * len(s)

    def wrap(sz: float, ls: float) -> list:
        lines = []
        for paragraph in text.split("\n"):
            words = paragraph.split(" ")
            # JS split(/ +/) collapses runs of spaces; so does this.
            words = [w for w in words if w != ""] or [""]
            current = ""
            for word in words:
                candidate = f"{current} {word}" if current else word
                if width_at(candidate, sz, ls) <= width or not current:
                    # A single word wider than the box is broken by character.
                    if not current and width_at(word, sz, ls) > width:
                        chunk = ""
                        for ch in word:
                            if chunk and width_at(chunk + ch, sz, ls) > width:
                                lines.append(chunk)
                                chunk = ch
                            else:
                                chunk += ch
                        current = chunk
                    else:
                        current = candidate
                else:
                    lines.append(current)
                    current = word
                    if width_at(word, sz, ls) > width:
                        chunk = ""
                        for ch in word:
                            if chunk and width_at(chunk + ch, sz, ls) > width:
                                lines.append(chunk)
                                chunk = ch
                            else:
                                chunk += ch
                        current = chunk
            lines.append(current)
        return lines

    sz = float(size)
    ls = float(letter_spacing)
    lines = wrap(sz, ls) if text else []
    if fit == "shrink":
        while len(lines) > max_lines and sz > size * MIN_SCALE:
            sz = max(size * MIN_SCALE, sz * 0.95)
            ls = letter_spacing * (sz / size)
            lines = wrap(sz, ls)
    truncated = False
    if len(lines) > max_lines:
        truncated = True
        lines = lines[:max_lines]
        last = lines[-1]
        while last and width_at(f"{last}…", sz, ls) > width:
            last = last[:-1]
        lines[-1] = f"{last.rstrip()}…"
    ascent = metrics["ascent"] / metrics["unitsPerEm"] * sz
    descent = -metrics["descent"] / metrics["unitsPerEm"] * sz
    advance = sz * LINE_HEIGHT + line_spacing
    baselines = [ascent + i * advance for i in range(len(lines))]
    return LaidOutText(
        size=sz,
        letter_spacing=ls,
        lines=[{"text": l, "width": width_at(l, sz, ls)} for l in lines],
        baselines=baselines,
        height=(ascent + descent + (len(lines) - 1) * advance) if lines else ascent + descent,
        truncated=truncated,
    )


def line_x(layer, line_width: float) -> float:
    """Where a line starts, given its width and the layer's alignment."""
    if layer.align == "center":
        return layer.x + (layer.width - line_width) / 2
    if layer.align == "right":
        return layer.x + layer.width - line_width
    return layer.x


# ─── Validation ────────────────────────────────────────────────────────────


@dataclass
class Problem:
    path: list
    message: str


def layout_problems(canvas: TicketCanvas, layers: list, page: PageSpec, *, qr_required: bool = True) -> list:
    """Problems that make a ticket wrong or unscannable, each with the field to fix."""
    out: list = []
    area = ticket_area(canvas)
    if page.height_mm:
        want = page.width_mm / page.height_mm
        have = area.width / area.height
        if abs(want - have) / want > 0.015:
            out.append(Problem(["canvas", "area"], f"The {'ticket area' if canvas.area else 'canvas'} is {round(area.width)} × {round(area.height)}, which is not the shape of a {page.width_mm:g} × {page.height_mm:g} mm page. Resize it, or draw a frame of the right shape."))
    if area.x < 0 or area.y < 0 or area.x + area.width > canvas.width + 0.5 or area.y + area.height > canvas.height + 0.5:
        out.append(Problem(["canvas", "area"], "The ticket area must sit inside the template."))
    right = area.x + area.width
    bottom = area.y + area.height

    def off_ticket(l, w, h) -> bool:
        return l.x >= right or l.y >= bottom or l.x + w <= area.x or l.y + h <= area.y

    qrs = [i for i, l in enumerate(layers) if l.kind == "qr"]
    if not qrs and qr_required:
        out.append(Problem(["layers"], "Every ticket needs its QR code — it is what the gate scans."))
    if len(qrs) > 1:
        out.append(Problem(["layers", qrs[1]], "A ticket has exactly one QR code."))
    ids: set = set()
    for i, l in enumerate(layers):
        if l.id in ids:
            out.append(Problem(["layers", i, "id"], f'Two layers share the id "{l.id}".'))
        ids.add(l.id)

        def at(field_name: str, message: str, i=i, l=l):
            out.append(Problem(["layers", i, field_name], f"{l.name}: {message}"))

        if l.kind == "qr":
            mn = min_qr_size(canvas, page)
            if l.hidden:
                at("hidden", "the QR code cannot be hidden.")
            if l.size < mn:
                at("size", f"at least {mn} px on this canvas, or phones struggle to read it.")
            if l.x < area.x or l.y < area.y or l.x + l.size > right + 0.5 or l.y + l.size > bottom + 0.5:
                at("x", "the whole QR code must sit inside the ticket area.")
            if l.light != "transparent":
                if contrast_ratio(l.dark, l.light) < 4:
                    at("dark", "the two QR colours are too close for a scanner to tell apart.")
                elif luminance(l.dark) > luminance(l.light):
                    at("dark", "the dark modules must be darker than the background — inverted codes do not scan everywhere.")
        elif l.kind == "text":
            for e in parse_ticket_template(l.text).errors:
                at("text", e)
            weights = TICKET_FONTS[l.font]["weights"]
            if l.weight not in weights:
                at("weight", f"{TICKET_FONTS[l.font]['label']} has weights {', '.join(map(str, weights))}.")
            if off_ticket(l, l.width, l.size * LINE_HEIGHT):
                at("x", "this text sits entirely outside the ticket area.")
        elif l.kind == "image":
            if l.source == "upload" and not l.image_key and not l.hidden:
                at("imageKey", "choose an image to show.")
            if l.source == "guest_photo" and l.preprinted:
                at("preprinted", "a guest photo cannot be on the card stock.")
            if off_ticket(l, l.width, l.height):
                at("x", "this image sits entirely outside the ticket area.")
        elif l.kind == "shape":
            if not l.fill and not (l.stroke and l.stroke_width > 0):
                at("fill", "a shape needs a fill or an outline.")
            if off_ticket(l, l.width, l.height):
                at("x", "this shape sits entirely outside the ticket area.")
    return out


def problems_as_fields(problems: list) -> dict:
    """The first problem per field path, as the API reports them."""
    fields: dict = {}
    for p in problems:
        fields.setdefault(".".join(map(str, p.path)) or "layers", p.message)
    return fields


_seq = 0


def new_layer_id(kind: str) -> str:
    global _seq
    _seq += 1
    return f"{kind}-{format(int(time.time() * 1000), 'x')}{_seq}{secrets.token_hex(2)}"
