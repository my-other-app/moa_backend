"""Drawing a layered design: the PDF with reportlab, the PNG with pdfium.

The ticket area maps to the page; the rest of the canvas is the artboard
around it and is never printed. Layers draw in order, back to front. Text
goes through the same layout function the editor uses, measured with the
embedded font itself, so a line that fits on screen fits on paper.

Mirrors Muster's pdf-lib renderer. Where the two libraries differ -- how a
quadratic curve is drawn, how an image is clipped -- the difference is noted
at the spot.
"""

import io
import re
from dataclasses import dataclass, field
from typing import Optional

import qrcode
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

from app.core.layout.engine import layout_text, line_x
from app.core.layout.fonts import font_file_of, measure_for, pick, register
from app.core.layout.model import (
    SHAPE_PATH_BOX,
    PageSpec,
    TicketCanvas,
    luminance,
    page_size_pt,
    resolve_color,
    scale_shape_path,
    ticket_area,
)
from app.core.layout.tokens import carries_per_guest_token, render_ticket_template

MM = 72 / 25.4


@dataclass
class RenderInput:
    title: str
    canvas: TicketCanvas
    layers: list
    page: PageSpec
    values: dict
    qr_value: str
    colors: dict  # primary, secondary, ticketType
    fit: str = "stretch"  # or "contain"
    template: Optional[bytes] = None
    images: dict = field(default_factory=dict)  # imageKey -> bytes
    org_logo: Optional[bytes] = None
    event_icon: Optional[bytes] = None
    guest_photo: Optional[bytes] = None
    skip_preprinted: bool = False
    monochrome: bool = False


def _rgb(hex_color: str) -> tuple:
    m = re.match(r"^#?([0-9a-fA-F]{6})$", hex_color.strip())
    n = int(m.group(1), 16) if m else 0x141614
    return ((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255


def _initials(name: str) -> str:
    parts = [p for p in name.strip().split() if p]
    return "".join(p[0].upper() for p in parts[:2])


def _rounded_rect(path, x, y, w, h, r):
    """A rounded rectangle, bottom-left origin, on a reportlab path."""
    rr = max(0, min(r, w / 2, h / 2))
    if rr == 0:
        path.rect(x, y, w, h)
    else:
        path.roundRect(x, y, w, h, rr)


def _svg_path(path, d: str, x0: float, y0: float):
    """An M/L/C/Q/Z outline, already scaled to page units, drawn from (x0, y0)
    top-left with y running down -- so every y is subtracted."""
    tokens = re.findall(r"[MLCQZ]|-?\d*\.?\d+(?:e-?\d+)?", d, re.I)
    i = 0
    cmd = None
    cur = (x0, y0)
    start = cur

    def num():
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    while i < len(tokens):
        t = tokens[i]
        if t.upper() in "MLCQZ":
            cmd = t.upper()
            i += 1
            if cmd == "Z":
                path.close()
                cur = start
            continue
        if cmd == "M":
            cur = (x0 + num(), y0 - num())
            path.moveTo(*cur)
            start = cur
            cmd = "L"  # Subsequent pairs after M are implicit lineTos.
        elif cmd == "L":
            cur = (x0 + num(), y0 - num())
            path.lineTo(*cur)
        elif cmd == "C":
            c1 = (x0 + num(), y0 - num())
            c2 = (x0 + num(), y0 - num())
            cur = (x0 + num(), y0 - num())
            path.curveTo(c1[0], c1[1], c2[0], c2[1], cur[0], cur[1])
        elif cmd == "Q":
            # reportlab has no quadratic: raise it to the equivalent cubic.
            q = (x0 + num(), y0 - num())
            end = (x0 + num(), y0 - num())
            c1 = (cur[0] + 2 / 3 * (q[0] - cur[0]), cur[1] + 2 / 3 * (q[1] - cur[1]))
            c2 = (end[0] + 2 / 3 * (q[0] - end[0]), end[1] + 2 / 3 * (q[1] - end[1]))
            path.curveTo(c1[0], c1[1], c2[0], c2[1], end[0], end[1])
            cur = end
        else:
            i += 1


def _image_reader(data: Optional[bytes]) -> Optional[ImageReader]:
    if not data:
        return None
    try:
        return ImageReader(io.BytesIO(data))
    except Exception:
        return None


def render_pdf(inp: RenderInput) -> bytes:
    """A font file that trips the PDF library must never cost a guest their
    ticket: the render is retried with every line in Inter."""
    try:
        return _draw(inp, safe_fonts=False)
    except Exception:
        return _draw(inp, safe_fonts=True)


def _draw(inp: RenderInput, safe_fonts: bool) -> bytes:
    cv = inp.canvas
    area = ticket_area(cv)
    page_w, page_h = page_size_pt(inp.page, area)
    contain = inp.fit == "contain" and bool(inp.page.height_mm)
    k = min(page_w / area.width, page_h / area.height) if contain else page_w / area.width
    ky = k if (contain or not inp.page.height_mm) else page_h / area.height
    ox = (page_w - area.width * k) / 2 if contain else 0
    oy = (page_h - area.height * k) / 2 if contain else 0

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle(inp.title)
    c.setCreator("My Other App")
    mono = inp.monochrome

    def color(ref: str) -> tuple:
        hx = resolve_color(ref, inp.colors)
        if mono:
            hx = "#FFFFFF" if luminance(hx) > 0.45 else "#000000"
        return _rgb(hx)

    def X(x: float) -> float:
        return ox + (x - area.x) * k

    def Y(y: float) -> float:
        return page_h - oy - (y - area.y) * ky

    rounded = area.radius > 0 and not contain
    if rounded:
        c.saveState()
        p = c.beginPath()
        _rounded_rect(p, 0, 0, page_w, page_h, area.radius * k)
        c.clipPath(p, stroke=0, fill=0)

    if not inp.skip_preprinted:
        c.setFillColorRGB(*_rgb("#FFFFFF" if mono else cv.background))
        c.rect(0, 0, page_w, page_h, stroke=0, fill=1)

    template = None if (inp.skip_preprinted or cv.template_preprinted) else _image_reader(inp.template)
    if template:
        c.drawImage(template, X(0), Y(cv.height), width=cv.width * k, height=cv.height * ky, mask="auto")

    def on_stock(l) -> bool:
        return l.preprinted and not (l.kind == "text" and carries_per_guest_token(l.text))

    visible = [l for l in inp.layers if not l.hidden and not (inp.skip_preprinted and on_stock(l))]

    for l in visible:
        if l.kind == "shape":
            fill = color(l.fill) if l.fill else None
            stroke = color(l.stroke) if (l.stroke and l.stroke_width > 0) else None
            c.saveState()
            c.setFillAlpha(l.opacity)
            c.setStrokeAlpha(l.opacity)
            if fill:
                c.setFillColorRGB(*fill)
            if stroke:
                c.setStrokeColorRGB(*stroke)
                c.setLineWidth(l.stroke_width * k)
            p = c.beginPath()
            if l.path:
                d = scale_shape_path(l.path, l.width * k / SHAPE_PATH_BOX, l.height * ky / SHAPE_PATH_BOX)
                _svg_path(p, d, X(l.x), Y(l.y))
            else:
                _rounded_rect(p, X(l.x), Y(l.y) - l.height * ky, l.width * k, l.height * ky, l.radius * k)
            c.drawPath(p, stroke=1 if stroke else 0, fill=1 if fill else 0)
            c.restoreState()

        elif l.kind == "image":
            data = (
                inp.org_logo if l.source == "org_logo"
                else inp.event_icon if l.source == "event_icon"
                else inp.guest_photo if l.source == "guest_photo"
                else inp.images.get(l.image_key) if l.image_key else None
            )
            box = (X(l.x), Y(l.y + l.height), l.width * k, l.height * ky)
            img = _image_reader(data)
            if img is None:
                if l.source == "guest_photo":
                    # No photo on file: a tinted tile with their initials.
                    name = inp.values.get("attendee_name", "")
                    c.saveState()
                    c.setFillAlpha(1 if mono else 0.16)
                    c.setFillColorRGB(*_rgb("#DDDDDD" if mono else resolve_color("brand.primary", inp.colors)))
                    p = c.beginPath()
                    _rounded_rect(p, box[0], box[1], box[2], box[3], l.radius * k)
                    c.drawPath(p, stroke=0, fill=1)
                    c.restoreState()
                    text = _initials(name) or "•"
                    size = min(box[2], box[3]) * 0.42
                    font = register(pick(font_file_of("inter", 700), text, safe_fonts))
                    c.saveState()
                    c.setFillAlpha(l.opacity)
                    c.setFillColorRGB(*(_rgb("#000000") if mono else color("brand.primary")))
                    c.setFont(font, size)
                    w = c.stringWidth(text, font, size)
                    c.drawString(box[0] + (box[2] - w) / 2, box[1] + (box[3] - size * 0.72) / 2, text)
                    c.restoreState()
                continue
            iw, ih = img.getSize()
            s = max(box[2] / iw, box[3] / ih) if l.fit == "cover" else min(box[2] / iw, box[3] / ih)
            dw, dh = iw * s, ih * s
            ax = box[0] if l.align == "left" else box[0] + box[2] - dw if l.align == "right" else box[0] + (box[2] - dw) / 2
            ay = box[1] + (box[3] - dh) / 2
            c.saveState()
            c.setFillAlpha(l.opacity)
            if l.fit == "cover" or l.radius > 0:
                p = c.beginPath()
                _rounded_rect(p, box[0], box[1], box[2], box[3], l.radius * k)
                c.clipPath(p, stroke=0, fill=0)
            c.drawImage(img, ax, ay, width=dw, height=dh, mask="auto")
            c.restoreState()

        elif l.kind == "qr":
            _draw_qr(c, inp.qr_value, X(l.x), Y(l.y), l.size * k, l.quiet_zone, _rgb(l.dark),
                     None if l.light == "transparent" else _rgb(l.light))

        elif l.kind == "text":
            text = render_ticket_template(l.text, inp.values)
            if not text.strip():
                continue
            file = pick(font_file_of(l.font, l.weight), text, safe_fonts)
            font = register(file)
            laid = layout_text(
                text, width=l.width, size=l.size, max_lines=l.max_lines, line_spacing=l.line_spacing,
                letter_spacing=l.letter_spacing, fit=l.fit, font_file=file, measure=measure_for(file),
            )
            c.saveState()
            c.setFillAlpha(l.opacity)
            c.setFillColorRGB(*color(l.color))
            for line, baseline in zip(laid.lines, laid.baselines):
                t = c.beginText()
                t.setFont(font, laid.size * k)
                if laid.letter_spacing:
                    t.setCharSpace(laid.letter_spacing * k)
                t.setTextOrigin(X(line_x(l, line["width"])), Y(l.y + baseline))
                t.textOut(line["text"])
                c.drawText(t)
            c.restoreState()

    if rounded:
        c.restoreState()
    c.showPage()
    c.save()
    return buf.getvalue()


def _draw_qr(c, value: str, x: float, top: float, size: float, quiet: int, dark: tuple, light: Optional[tuple]):
    """Vector modules, row runs merged. The quiet zone is inside the layer's square."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    count = len(matrix)
    cell = size / (count + quiet * 2)
    bottom = top - size
    c.saveState()
    if light:
        c.setFillColorRGB(*light)
        c.rect(x, bottom, size, size, stroke=0, fill=1)
    c.setFillColorRGB(*dark)
    for row in range(count):
        run = -1
        for col in range(count + 1):
            on = col < count and matrix[row][col]
            if on and run < 0:
                run = col
            if not on and run >= 0:
                c.rect(x + (run + quiet) * cell, top - (row + quiet + 1) * cell, (col - run) * cell + 0.02, cell + 0.02, stroke=0, fill=1)
                run = -1
    c.restoreState()


def pdf_to_png(pdf: bytes, *, width_px: int) -> bytes:
    """The first page as a PNG, `width_px` wide -- the preview the designer shows."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    page = doc[0]
    scale = width_px / page.get_width()
    bitmap = page.render(scale=scale)
    image = bitmap.to_pil()
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def pdf_text(pdf: bytes) -> str:
    """Every string the first page draws, for tests that check what printed."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    return doc[0].get_textpage().get_text_range()


def blank_page_pdf(message: str) -> bytes:
    """A one-line PDF, for a download that would otherwise be empty."""
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(320, 200))
    c.setFont("Helvetica", 12)
    c.drawString(24, 100, message)
    c.showPage()
    c.save()
    return buf.getvalue()
