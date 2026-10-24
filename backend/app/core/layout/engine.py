"""Laying text out, and judging a design -- shared with the editor.

`layout_text` is the same algorithm the browser runs, so a line that wraps
on screen wraps on paper. `layout_problems` is what makes a design wrong:
one readable QR where one is needed, known tokens, nothing off the ticket.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

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
from app.core.layout.tokens import at_wall_clock, day_key, format_agenda_day, format_event_time, parse_ticket_template

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


# ─── Agenda layout (shared with the editor) ────────────────────────────────


@dataclass
class AgendaEntry:
    starts_at: datetime
    ends_at: Optional[datetime]
    title: str
    speaker: Optional[str] = None
    location: Optional[str] = None


@dataclass
class AgendaPiece:
    """One string to draw: the caller maps the role to a font and a colour."""

    role: str  # time | title | detail | day | more
    text: str
    x: float
    y: float  # baseline, from the layer's top
    size: float
    width: float


@dataclass
class LaidOutAgenda:
    pieces: list
    dropped: int  # sessions that did not fit even after shrinking
    size: float  # title size after shrinking
    height: float


def agenda_weights(font: str) -> dict:
    """The weights the agenda draws its three roles in, for a given family."""
    w = TICKET_FONTS[font]["weights"]
    return {"title": 600 if 600 in w else 700 if 700 in w else w[0], "detail": 400 if 400 in w else w[0]}


def layout_agenda(layer, entries: list, tz: str, measure: dict, hour12: bool = False) -> LaidOutAgenda:
    """Lays the run of show into the layer's box. Left: time in a column, title
    beside it, detail under the title. Right: the mirror. Centre: time, title
    and detail stacked. Columns fill top to bottom, then left to right. If the
    whole thing will not fit, the type shrinks (to half size) before sessions
    are dropped, and a last line says how many were.

    `measure` holds a function per role (title, detail, time); the same
    algorithm runs in the browser with the same font files."""
    weights = agenda_weights(layer.font)
    stem = TICKET_FONTS[layer.font]["stem"]
    title_file = f"{stem}-{weights['title']}"
    detail_file = f"{stem}-{weights['detail']}"
    metrics = FONT_METRICS.get(title_file) or {"unitsPerEm": 1000, "ascent": 950, "descent": -250}
    col_gap = layer.size * 1.5
    col_width = (layer.width - col_gap * (layer.columns - 1)) / layer.columns
    multi_day = layer.show_day_headings and len({day_key(e.starts_at, tz) for e in entries}) > 1

    def time_of(e: AgendaEntry) -> str:
        t = format_event_time(e.starts_at, tz, hour12)
        return f"{t} – {format_event_time(e.ends_at, tz, hour12)}" if e.ends_at else t

    def block(role, lines, size, advance, ascent, indent, line_height_px) -> dict:
        return {"role": role, "lines": lines, "size": size, "advance": advance, "ascent": ascent, "indent": indent, "line_height_px": line_height_px}

    def build(size: float) -> list:
        """Every session as a stack of text blocks at a title size; heights follow from that."""
        time_size = size * 0.78
        detail_size = size * 0.72
        day_size = size * 0.7

        def asc(sz):
            return metrics["ascent"] / metrics["unitsPerEm"] * sz

        def desc(sz):
            return -metrics["descent"] / metrics["unitsPerEm"] * sz

        # The time column is as wide as the widest time it holds, so start-only times do not leave a gap.
        time_w = max([measure["time"]("00:00", time_size)] + [measure["time"](time_of(e), time_size) for e in entries])
        gutter = size * 0.9
        title_width = col_width if layer.align == "center" else max(20, col_width - time_w - gutter)
        items = []
        last_day = None
        for e in entries:
            key = day_key(e.starts_at, tz)
            if multi_day and key != last_day:
                last_day = key
                text = format_agenda_day(e.starts_at, tz).upper()
                items.append({
                    "blocks": [block("day", [{"text": text, "width": measure["time"](text, day_size)}], day_size, day_size * LINE_HEIGHT, asc(day_size), 0, day_size * LINE_HEIGHT)],
                    "height": day_size * LINE_HEIGHT + layer.row_gap * 0.5,
                })
            time = time_of(e)
            title = layout_text(e.title, width=title_width, size=size, max_lines=2, line_spacing=0, letter_spacing=0, fit="shrink", font_file=title_file, measure=measure["title"])
            detail_text = " · ".join(t for t in [e.speaker if layer.show_speaker else None, e.location if layer.show_location else None] if t)
            detail = (
                layout_text(detail_text, width=title_width, size=detail_size, max_lines=1, line_spacing=0, letter_spacing=0, fit="truncate", font_file=detail_file, measure=measure["detail"])
                if detail_text else None
            )
            # Left and right both keep a time column; centre stacks the time above the title instead.
            indent = 0 if layer.align == "center" else time_w + gutter
            blocks = []
            h = 0.0
            if layer.align == "center":
                blocks.append(block("time", [{"text": time, "width": measure["time"](time, time_size)}], time_size, time_size * LINE_HEIGHT, asc(time_size), 0, time_size * LINE_HEIGHT))
                h += time_size * LINE_HEIGHT
            blocks.append(block("title", title.lines, title.size, title.size * LINE_HEIGHT, asc(title.size), indent, title.size * LINE_HEIGHT))
            h += len(title.lines) * title.size * LINE_HEIGHT
            if detail is not None:
                blocks.append(block("detail", detail.lines, detail.size, detail.size * LINE_HEIGHT, asc(detail.size), indent, detail.size * LINE_HEIGHT))
                h += detail.size * LINE_HEIGHT
            if layer.align != "center":
                # The time sits on the title's first line, in its own column.
                blocks.insert(0, block("time", [{"text": time, "width": measure["time"](time, time_size)}], time_size, 0, asc(title.size), 0, 0))
            items.append({"blocks": blocks, "height": h + desc(size) * 0.3 + layer.row_gap})
        return items

    def fits(items: list) -> bool:
        return sum(it["height"] for it in items) <= layer.height * layer.columns + layer.row_gap

    size = layer.size
    items = build(size)
    while not fits(items) and size > layer.size * MIN_SCALE:
        size = max(layer.size * MIN_SCALE, size * 0.92)
        items = build(size)

    # Fill columns; anything past the last column is dropped and counted. When
    # everything fits, the columns are balanced, so a short programme does not
    # sit in the first column with the rest of the page empty.
    more_line = size * 0.72 * LINE_HEIGHT

    def is_day(i: int) -> bool:
        return i < len(items) and items[i]["blocks"][0]["role"] == "day"

    def place(target: float) -> tuple:
        pieces: list = []
        col = 0
        y = 0.0
        tallest = 0.0
        dropped = 0
        for i, it in enumerate(items):
            remaining = len(items) - i
            need_more = more_line if remaining > 1 else 0
            # A day heading never ends a column: it moves with the session under it.
            need = it["height"] + items[i + 1]["height"] if is_day(i) and i + 1 < len(items) else it["height"]
            overflows = y + need - layer.row_gap > layer.height + 0.5
            balanced = y > 0 and col < layer.columns - 1 and y + need / 2 > target
            if overflows or balanced:
                col += 1
                y = 0.0
                if col >= layer.columns:
                    dropped = remaining
                    break
            # The last thing in the last column must leave room for the "+N more" line if anything follows.
            if col == layer.columns - 1 and y + it["height"] - layer.row_gap + need_more > layer.height + 0.5 and remaining > 1:
                dropped = remaining
                break
            col_x = col * (col_width + col_gap)
            cursor = y
            for b in it["blocks"]:
                if b["role"] == "time" and layer.align != "center":
                    # Beside the title's first line: right-aligned mirrors the column to the far side.
                    w = b["lines"][0]["width"]
                    x = col_x + col_width - w if layer.align == "right" else col_x
                    pieces.append(AgendaPiece("time", b["lines"][0]["text"], x, cursor + b["ascent"], b["size"], w))
                    continue
                for li, line in enumerate(b["lines"]):
                    if layer.align == "center":
                        x = col_x + (col_width - line["width"]) / 2
                    elif layer.align == "right":
                        x = col_x + col_width - b["indent"] - line["width"]
                    else:
                        x = col_x + b["indent"]
                    pieces.append(AgendaPiece(b["role"], line["text"], x, cursor + li * b["advance"] + b["ascent"], b["size"], line["width"]))
                cursor += len(b["lines"]) * b["line_height_px"]
            y += it["height"]
            tallest = max(tallest, y - layer.row_gap)
        if dropped > 0:
            text = f"+ {dropped} more session{'' if dropped == 1 else 's'}"
            sz = size * 0.72
            w = measure["detail"](text, sz)
            col_x = min(col, layer.columns - 1) * (col_width + col_gap)
            if layer.align == "center":
                x = col_x + (col_width - w) / 2
            elif layer.align == "right":
                x = col_x + col_width - w
            else:
                x = col_x
            pieces.append(AgendaPiece("more", text, x, min(y, layer.height - sz * 0.3) + metrics["ascent"] / metrics["unitsPerEm"] * sz, sz, w))
        return pieces, dropped, min(layer.height, tallest)

    total = sum(it["height"] for it in items)
    pieces, dropped, height = place(float("inf"))
    if layer.columns > 1 and dropped == 0:
        target = max([total / layer.columns] + [it["height"] for it in items])
        even = place(target)
        # Balancing can, rarely, push the last session past the last column; then the plain fill stands.
        if even[1] == 0:
            pieces, dropped, height = even
    return LaidOutAgenda(pieces=pieces, dropped=dropped, size=size, height=height)


def sample_agenda(starts_at: datetime, tz: str = "UTC") -> list:
    """Sessions for the editor when the event has none yet, on the event's day in its own timezone."""

    def at(h: int, m: int = 0) -> datetime:
        return at_wall_clock(starts_at, tz, h, m)

    return [
        AgendaEntry(at(9, 0), at(9, 30), "Registration and coffee", None, "Foyer"),
        AgendaEntry(at(9, 30), at(10, 15), "Opening keynote — the year ahead", "Ananya Iyer", "Main hall"),
        AgendaEntry(at(10, 30), at(11, 30), "Workshop: designing for the door", "Arjun Sharma", "Studio 2"),
        AgendaEntry(at(12, 0), at(13, 0), "Lunch", None, "Terrace"),
        AgendaEntry(at(13, 0), at(14, 0), "Panel: what volunteers see", "Kavya Reddy, Imran Khan", "Main hall"),
        AgendaEntry(at(14, 15), at(15, 0), "Closing remarks and certificates", "Ananya Iyer", "Main hall"),
    ]


def agenda_entries(items: list) -> list:
    """Entries from rows or dicts carrying starts_at/ends_at/title/speaker/location."""
    out = []
    for it in items:
        get = (lambda k, it=it: it.get(k)) if isinstance(it, dict) else (lambda k, it=it: getattr(it, k, None))
        out.append(AgendaEntry(get("starts_at"), get("ends_at"), get("title") or "", get("speaker"), get("location")))
    return out


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
    agendas = [i for i, l in enumerate(layers) if l.kind == "agenda"]
    if len(agendas) > 1:
        out.append(Problem(["layers", agendas[1]], "A design lists the run of show once."))
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
        elif l.kind == "agenda":
            if off_ticket(l, l.width, l.height):
                at("x", "this agenda sits entirely outside the page.")
    return out


def problems_as_fields(problems: list) -> dict:
    """The first problem per field path, as the API reports them."""
    fields: dict = {}
    for p in problems:
        fields.setdefault(".".join(map(str, p.path)) or "layers", p.message)
    return fields
