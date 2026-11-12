"""A v2 layout as one HTML page (layout-v2 contract, builder rules).

The same rules run in TypeScript (moa_web packages/layout/src/v2), and the
conformance fixtures (backend/tests/fixtures/layout_v2/) hold both to them:
`build_body` is the part they compare, the `<div class="moa-doc">`.

**The body.** The root is `<div class="moa-doc" style="position:relative;
width:Wpx;height:Hpx;overflow:hidden[;background:F]">`; then each layer
that is drawn, in order, one per line, as `<div data-layer="id"
data-kind="kind" style="position:absolute;left:Xpx;top:Ypx;width:Wpx;
height:Hpx[;transform:rotate(Rdeg);transform-origin:center][;opacity:O]
[kind's own]">…</div>`. Left out: hidden layers, layers whose showIf is
not met, text that fills to nothing, an image with no picture and no
initials tile, and (printing on pre-printed stock) preprinted layers.

Numbers are written by `num`: rounded half up to 3 decimals, no trailing
zeros, never "-0". Colours: #RRGGBB / #RRGGBBAA upper case; var:accent is
vars.accent (#000000 when unset); brand.primary, brand.secondary and
ticket_type come from the context. Fills: solid = the colour; linear =
`linear-gradient(Adeg, C1 P1%, …)`; radial = `radial-gradient(circle at
X% Y%, C1 P1%, …)`. Shadows: `Xpx Ypx Bpx Spx C` (inset first when inset),
joined with ", ".

**Text.** `<div class="moa-t" style="display:flex;flex-direction:column;
justify-content:J;box-sizing:border-box;width:100%;height:100%;padding:T R B
L[;background:F][;border-radius:…]">` holding `<div data-fit="FIT"
data-min="MIN" data-lines="N" style="font-family:'F';font-weight:W;
font-style:S;font-size:Zpx;line-height:L;letter-spacing:Eem;color:C;
text-align:A;text-transform:T;white-space:pre-wrap;overflow-wrap:anywhere
[;text-wrap:balance|pretty][;text-shadow:…]">` and the text: the template
filled, HTML-escaped (& < > " '), or each span as `<span style="…">`
with only what it sets (its size in em of the layer's). Nothing clips the
glyphs: the fit script (doc_designs/fit.js) shrinks the text 1 px at a time
to MIN while it has more than N lines or overflows the box, then clamps it
to N lines with an ellipsis (fit "ellipsis": the clamp alone).

**Shape.** rect / ellipse without a dash: CSS on the layer itself
(`box-sizing:border-box[;background][;border:Wpx solid C]
[;border-radius:…|50%][;box-shadow]`). A path, or a dashed stroke: an
inline `<svg viewBox="0 0 W H" width="100%" height="100%"
preserveAspectRatio="none" style="display:block;overflow:visible">`
(gradients as <defs> in the layer box; a shadow as drop-shadow).

**SVG.** The sanitised markup, its #RRGGBB literals recoloured, every id
(and url(#…), href="#…") prefixed with "<layer id>-", and
`preserveAspectRatio="xMidYMid meet|slice|none" width="100%" height="100%"
style="display:block"` put first on its <svg>.

**Image.** `<img src="data:…" alt="" style="display:block;width:100%;
height:100%;box-sizing:border-box;object-fit:F;object-position:X% Y%…">`,
or without a picture (a guest photo with placeholder "initials") an
initials tile.

**QR.** `<div class="moa-qr" …>` with the QR of the context's payload as
black-on-white SVG, quietZone modules round it (doc_designs/qr.py).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from app.core.doc_designs import fonts as doc_fonts
from app.core.doc_designs import qr as doc_qr
from app.core.layout.tokens import carries_per_guest_token
from app.core.layout_v2 import fonts as v2_fonts
from app.core.layout_v2 import vocabulary as tk
from app.core.layout_v2.context import Context
from app.core.layout_v2.sanitize import sanitize
from app.core.layout_v2.schema import (
    ImageLayer,
    LayoutV2,
    LinearFill,
    QrLayer,
    RadialFill,
    ShapeLayer,
    SolidFill,
    SvgLayer,
    TextLayer,
    parse,
)

HERE = Path(__file__).resolve().parent
DOC_DESIGNS = HERE.parent / "doc_designs"
_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$")
PX_PER_MM = 96 / 25.4


# ─── Small things both builders do the same way ─────────────────────────────


def num(v: float) -> str:
    """A number as the page writes it: half up to 3 decimals, no trailing zeros."""
    r = math.floor(float(v) * 1000 + 0.5) / 1000
    if r == 0:
        return "0"
    if r == int(r):
        return str(int(r))
    return repr(r)


def esc(text: str) -> str:
    return _html.escape(text, quote=True)


def _upper_hex(c: str) -> str:
    return c.upper()


class _Paint:
    def __init__(self, layout: LayoutV2, ctx: Context):
        self.accent = layout.vars.accent or "#000000"
        self.ctx = ctx

    def color(self, ref: str) -> str:
        if ref == "var:accent":
            return self.accent
        if ref == "brand.primary":
            return _upper_hex(self.ctx.colors.get("primary", "#000000"))
        if ref == "brand.secondary":
            return _upper_hex(self.ctx.colors.get("secondary", "#000000"))
        if ref == "ticket_type":
            return _upper_hex(self.ctx.colors.get("ticketType", self.ctx.colors.get("primary", "#000000")))
        return _upper_hex(ref)

    def stops(self, stops) -> str:
        return ", ".join(f"{self.color(s.color)} {num(s.offset * 100)}%" for s in stops)

    def fill(self, f) -> str:
        if isinstance(f, SolidFill):
            return self.color(f.color)
        if isinstance(f, LinearFill):
            return f"linear-gradient({num(f.angle)}deg, {self.stops(f.stops)})"
        if isinstance(f, RadialFill):
            return f"radial-gradient(circle at {num(f.cx * 100)}% {num(f.cy * 100)}%, {self.stops(f.stops)})"
        raise TypeError(f)

    def shadows(self, layer, text: bool = False) -> str:
        parts = []
        for s in layer.shadows():
            if text:
                parts.append(f"{num(s.x)}px {num(s.y)}px {num(s.blur)}px {self.color(s.color)}")
            else:
                parts.append(f"{'inset ' if s.inset else ''}{num(s.x)}px {num(s.y)}px {num(s.blur)}px "
                             f"{num(s.spread)}px {self.color(s.color)}")
        return ", ".join(parts)

    def drop_shadows(self, layer) -> str:
        # drop-shadow's length is the blur's standard deviation: half a box-shadow's blur.
        return " ".join(f"drop-shadow({num(s.x)}px {num(s.y)}px {num(s.blur / 2)}px {self.color(s.color)})"
                        for s in layer.shadows() if not s.inset)


def _radius(r: list[float]) -> Optional[str]:
    if not any(r):
        return None
    if len(set(r)) == 1:
        return f"{num(r[0])}px"
    return " ".join(f"{num(x)}px" for x in r)


def _outer(layer, extra: str = "") -> str:
    s = (f"position:absolute;left:{num(layer.x)}px;top:{num(layer.y)}px;"
         f"width:{num(layer.width)}px;height:{num(layer.height)}px")
    if layer.rotation:
        s += f";transform:rotate({num(layer.rotation)}deg);transform-origin:center"
    if layer.opacity != 1:
        s += f";opacity:{num(layer.opacity)}"
    return s + extra


def _open(layer, style: str) -> str:
    return f'<div data-layer="{esc(layer.id)}" data-kind="{layer.kind}" style="{style}">'


# ─── Text ───────────────────────────────────────────────────────────────────

_JUSTIFY = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}
_TRANSFORM = {"none": "none", "upper": "uppercase", "lower": "lowercase"}


def _initials(values: Mapping[str, Any]) -> str:
    given = " ".join(str(values.get("initials") or "").split())
    if given:
        return given[:3].upper()
    first = " ".join(str(values.get("first_name") or "").split())
    last = " ".join(str(values.get("last_name") or "").split())
    if not (first or last):
        for key in ("full_name", "attendee_name", "recipient_name"):
            parts = str(values.get(key) or "").split()
            if parts:
                first, last = parts[0], (parts[-1] if len(parts) > 1 else "")
                break
    return ((first[:1] + last[:1]) or "?").upper()


def _text(layer: TextLayer, values, kind: str, paint: _Paint) -> Optional[str]:
    if layer.spans:
        pieces = [(s, tk.fill(s.text, values, kind)) for s in layer.spans]
        if not "".join(t for _, t in pieces).strip():
            return None
        content = "".join(_span(layer, s, t, paint) for s, t in pieces)
    else:
        filled = tk.fill(layer.text, values, kind)
        if not filled.strip():
            return None
        content = esc(filled)
    p = layer.padding
    box = (f"display:flex;flex-direction:column;justify-content:{_JUSTIFY[layer.valign]};box-sizing:border-box;"
           f"width:100%;height:100%;padding:{num(p[0])}px {num(p[1])}px {num(p[2])}px {num(p[3])}px")
    if layer.background is not None:
        box += f";background:{paint.fill(layer.background)}"
    r = _radius(layer.radius)
    if r:
        box += f";border-radius:{r}"
    f = layer.font
    style = (f"font-family:'{f.family}';font-weight:{f.weight};font-style:{f.style};font-size:{num(layer.size)}px;"
             f"line-height:{num(layer.line_height)};letter-spacing:{num(layer.letter_spacing)}em;"
             f"color:{paint.color(layer.color)};text-align:{layer.align};"
             f"text-transform:{_TRANSFORM[layer.transform]};white-space:pre-wrap;overflow-wrap:anywhere")
    if layer.text_wrap != "normal":
        style += f";text-wrap:{layer.text_wrap}"
    shadows = paint.shadows(layer, text=True)
    if shadows:
        style += f";text-shadow:{shadows}"
    minimum = layer.floor() if layer.fit == "shrink" else layer.size
    return (_open(layer, _outer(layer)) + f'<div class="moa-t" style="{box}">'
            + f'<div data-fit="{layer.fit}" data-min="{num(minimum)}" data-lines="{layer.max_lines}" style="{style}">'
            + content + "</div></div></div>")


def _span(layer: TextLayer, s, text: str, paint: _Paint) -> str:
    parts = []
    if s.family is not None:
        parts.append(f"font-family:'{s.family}'")
    if s.weight is not None:
        parts.append(f"font-weight:{s.weight}")
    if s.style is not None:
        parts.append(f"font-style:{s.style}")
    if s.size is not None:
        parts.append(f"font-size:{num(s.size / layer.size)}em")
    if s.color is not None:
        parts.append(f"color:{paint.color(s.color)}")
    if s.letter_spacing is not None:
        parts.append(f"letter-spacing:{num(s.letter_spacing)}em")
    if s.transform is not None:
        parts.append(f"text-transform:{_TRANSFORM[s.transform]}")
    if not parts:
        return f"<span>{esc(text)}</span>"
    return f'<span style="{";".join(parts)}">{esc(text)}</span>'


# ─── Shapes ─────────────────────────────────────────────────────────────────


def clamp_radii(r: list[float], w: float, h: float) -> list[float]:
    """CSS's rule: when two corners on a side ask for more than the side, all
    four shrink by the same factor."""
    r = [max(0.0, v) for v in r]
    sides = ((r[0] + r[1], w), (r[1] + r[2], h), (r[2] + r[3], w), (r[3] + r[0], h))
    f = min([length / total for total, length in sides if total > 0] + [1.0])
    return [v * f for v in r] if f < 1 else r


def _rounded_rect_path(x: float, y: float, w: float, h: float, r: list[float]) -> str:
    tl, tr, br, bl = clamp_radii(list(r), w, h)
    return (f"M{num(x + tl)} {num(y)}H{num(x + w - tr)}"
            + (f"A{num(tr)} {num(tr)} 0 0 1 {num(x + w)} {num(y + tr)}" if tr else "")
            + f"V{num(y + h - br)}"
            + (f"A{num(br)} {num(br)} 0 0 1 {num(x + w - br)} {num(y + h)}" if br else "")
            + f"H{num(x + bl)}"
            + (f"A{num(bl)} {num(bl)} 0 0 1 {num(x)} {num(y + h - bl)}" if bl else "")
            + f"V{num(y + tl)}"
            + (f"A{num(tl)} {num(tl)} 0 0 1 {num(x + tl)} {num(y)}" if tl else "")
            + "Z")


def _svg_paint(layer: ShapeLayer, fill, paint: _Paint, defs: list[str]) -> str:
    if fill is None:
        return "none"
    if isinstance(fill, SolidFill):
        return paint.color(fill.color)
    gid = f"{esc(layer.id)}-fill"
    stops = "".join(f'<stop offset="{num(s.offset)}" stop-color="{paint.color(s.color)}"></stop>' for s in fill.stops)
    w, h = layer.width, layer.height
    if isinstance(fill, LinearFill):
        a = math.radians(fill.angle)
        dx, dy = math.sin(a), -math.cos(a)
        half = (abs(w * dx) + abs(h * dy)) / 2
        cx, cy = w / 2, h / 2
        defs.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{num(cx - dx * half)}" '
                    f'y1="{num(cy - dy * half)}" x2="{num(cx + dx * half)}" y2="{num(cy + dy * half)}">{stops}</linearGradient>')
    else:
        cx, cy = fill.cx * w, fill.cy * h
        r = max(math.hypot(cx - px, cy - py) for px in (0, w) for py in (0, h))
        defs.append(f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" cx="{num(cx)}" cy="{num(cy)}" '
                    f'r="{num(r)}">{stops}</radialGradient>')
    return f"url(#{gid})"


def _shape(layer: ShapeLayer, paint: _Paint) -> str:
    dashed = layer.stroke is not None and layer.stroke.dash is not None
    if layer.shape != "path" and not dashed:
        style = ";box-sizing:border-box"
        if layer.fill is not None:
            style += f";background:{paint.fill(layer.fill)}"
        if layer.stroke is not None:
            style += f";border:{num(layer.stroke.width)}px solid {paint.color(layer.stroke.color)}"
        if layer.shape == "ellipse":
            style += ";border-radius:50%"
        else:
            r = _radius(layer.radius)
            if r:
                style += f";border-radius:{r}"
        shadows = paint.shadows(layer)
        if shadows:
            style += f";box-shadow:{shadows}"
        return _open(layer, _outer(layer, style)) + "</div>"
    w, h = layer.width, layer.height
    defs: list[str] = []
    fill = _svg_paint(layer, layer.fill, paint, defs)
    attrs = f' fill="{fill}"'
    sw = 0.0
    if layer.stroke is not None:
        sw = layer.stroke.width
        attrs += f' stroke="{paint.color(layer.stroke.color)}" stroke-width="{num(sw)}"'
        if layer.stroke.dash:
            attrs += f' stroke-dasharray="{num(layer.stroke.dash[0])} {num(layer.stroke.dash[1])}"'
    if layer.shape == "path":
        body = f'<path d="{esc(layer.path or "")}"{attrs}></path>'
    elif layer.shape == "ellipse":
        body = (f'<ellipse cx="{num(w / 2)}" cy="{num(h / 2)}" rx="{num(max(0.0, w / 2 - sw / 2))}" '
                f'ry="{num(max(0.0, h / 2 - sw / 2))}"{attrs}></ellipse>')
    else:
        inner = [max(0.0, r - sw / 2) for r in layer.radius]
        body = f'<path d="{_rounded_rect_path(sw / 2, sw / 2, w - sw, h - sw, inner)}"{attrs}></path>'
    extra = ""
    drops = paint.drop_shadows(layer)
    if drops:
        extra = f";filter:{drops}"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {num(w)} {num(h)}" width="100%" height="100%" '
           f'preserveAspectRatio="none" style="display:block;overflow:visible">'
           + (f"<defs>{''.join(defs)}</defs>" if defs else "") + body + "</svg>")
    return _open(layer, _outer(layer, extra)) + svg + "</div>"


# ─── SVG art ────────────────────────────────────────────────────────────────

_HEX6 = re.compile(r"#([0-9A-Fa-f]{6})(?![0-9A-Fa-f])")
_ID = re.compile(r'\sid="([^"]+)"')
_URL_REF = re.compile(r"url\(#([^)\s]+)\)")
_HREF = re.compile(r'\shref="#([^"]+)"')
_FIT = {"contain": "xMidYMid meet", "cover": "xMidYMid slice", "stretch": "none"}


def svg_markup(layer: SvgLayer, paint: _Paint) -> str:
    markup = sanitize(layer.markup)
    if layer.recolor:
        table = {k.upper(): paint.color(v) for k, v in layer.recolor.items()}
        markup = _HEX6.sub(lambda m: table.get("#" + m.group(1).upper(), m.group(0)), markup)
    prefix = layer.id
    markup = _ID.sub(lambda m: f' id="{prefix}-{m.group(1)}"', markup)
    markup = _URL_REF.sub(lambda m: f"url(#{prefix}-{m.group(1)})", markup)
    markup = _HREF.sub(lambda m: f' href="#{prefix}-{m.group(1)}"', markup)
    return markup.replace(
        "<svg", f'<svg preserveAspectRatio="{_FIT[layer.fit]}" width="100%" height="100%" style="display:block"', 1)


def _svg(layer: SvgLayer, paint: _Paint) -> str:
    drops = paint.drop_shadows(layer)
    return _open(layer, _outer(layer, f";filter:{drops}" if drops else "")) + svg_markup(layer, paint) + "</div>"


# ─── Images and the QR ──────────────────────────────────────────────────────

_POS_X = {"left": "0%", "center": "50%", "right": "100%"}
_POS_Y = {"top": "0%", "middle": "50%", "bottom": "100%"}


def _image(layer: ImageLayer, values, ctx: Context, paint: _Paint) -> Optional[str]:
    src = ctx.image(layer.source, layer.image_key)
    if src and not _DATA_IMAGE.match(src):
        src = ""  # never a URL: the renderer fetches nothing (doc-designs D1)
    extra = ""
    shadows = paint.shadows(layer)
    if shadows:
        extra = f";box-shadow:{shadows}"
        r = _radius(layer.radius)
        if r:
            extra += f";border-radius:{r}"
    look = ""
    r = _radius(layer.radius)
    if r:
        look += f";border-radius:{r}"
    if layer.border is not None:
        look += f";border:{num(layer.border.width)}px solid {paint.color(layer.border.color)}"
    if src:
        img = (f'<img src="{src}" alt="" style="display:block;width:100%;height:100%;box-sizing:border-box;'
               f'object-fit:{layer.fit};object-position:{_POS_X[layer.align]} {_POS_Y[layer.valign]}{look}">')
        return _open(layer, _outer(layer, extra)) + img + "</div>"
    if layer.placeholder != "initials":
        return None
    st = layer.placeholder_style
    color = paint.color(st.color) if st else "#3C4257"
    background = paint.fill(st.background) if st and st.background is not None else "#E9ECF2"
    family = st.family if st else "Inter"
    weight = st.weight if st else 700
    tile = (f'<div class="moa-initials" style="width:100%;height:100%;box-sizing:border-box;display:flex;'
            f"align-items:center;justify-content:center;container-type:size;color:{color};background:{background};"
            f"font-family:'{family}';font-weight:{weight}{look}\">"
            f'<span style="font-size:42cqmin;line-height:1;letter-spacing:-0.02em">{esc(_initials(values))}</span></div>')
    return _open(layer, _outer(layer, extra)) + tile + "</div>"


def qr_svg(payload: str, quiet_zone: int) -> str:
    svg = doc_qr.svg(payload or "MOA", "M", quiet_zone)
    return svg.replace("<svg", '<svg width="100%" height="100%" style="display:block"', 1)


def _qr(layer: QrLayer, ctx: Context, paint: _Paint) -> str:
    look = ""
    if layer.background is not None:
        look += f";background:{paint.fill(layer.background)}"
    r = _radius(layer.radius)
    if r:
        look += f";border-radius:{r}"
    shadows = paint.shadows(layer)
    extra = f";box-shadow:{shadows}" if shadows else ""
    return (_open(layer, _outer(layer, extra))
            + f'<div class="moa-qr" style="width:100%;height:100%;box-sizing:border-box{look}">'
            + qr_svg(ctx.qr, layer.quiet_zone) + "</div></div>")


# ─── The body and the page ──────────────────────────────────────────────────


def _on_stock(layer) -> bool:
    if not layer.preprinted:
        return False
    if isinstance(layer, TextLayer) and carries_per_guest_token(layer.text):
        return False
    return True


def _layers(layout: LayoutV2, values, ctx: Context, kind: str, paint: _Paint, skip_preprinted: bool):
    for layer in layout.layers:
        if layer.hidden or not tk.show(layer.show_if, values, kind):
            continue
        if skip_preprinted and _on_stock(layer):
            continue
        if isinstance(layer, TextLayer):
            out = _text(layer, values, kind, paint)
        elif isinstance(layer, ShapeLayer):
            out = _shape(layer, paint)
        elif isinstance(layer, SvgLayer):
            out = _svg(layer, paint)
        elif isinstance(layer, ImageLayer):
            out = _image(layer, values, ctx, paint)
        else:
            out = _qr(layer, ctx, paint)
        if out:
            yield out


def build_body(layout: Union[LayoutV2, dict], values: Mapping[str, Any], *, context=None, kind: str = "ticket",
               skip_preprinted: bool = False) -> str:
    """The `<div class="moa-doc">` with every layer drawn: what the editor's
    stage shows and the fixtures compare."""
    layout = parse(layout)
    ctx = Context.of(context)
    values = {k: ("" if v is None else str(v)) for k, v in (values or {}).items()}
    for token, source in tk.IMAGE_TOKENS.items():
        values.setdefault(token, "1" if ctx.image(source) else "")
    paint = _Paint(layout, ctx)
    c = layout.canvas
    root = f"position:relative;width:{num(c.width)}px;height:{num(c.height)}px;overflow:hidden"
    if c.background is not None:
        root += f";background:{paint.fill(c.background)}"
    inner = "\n".join(_layers(layout, values, ctx, kind, paint, skip_preprinted))
    return f'<div class="moa-doc" style="{root}">\n{inner}\n</div>' if inner else f'<div class="moa-doc" style="{root}">\n</div>'


def font_uses(layout: LayoutV2):
    """(family, weight, style) of every face the layout's text asks for."""
    for layer in layout.layers:
        if isinstance(layer, TextLayer):
            f = layer.font
            yield f.family, f.weight, f.style
            for s in layer.spans or []:
                yield s.family or f.family, s.weight or f.weight, s.style or f.style
        elif isinstance(layer, ImageLayer) and layer.placeholder == "initials":
            st = layer.placeholder_style
            yield (st.family, st.weight, "normal") if st else ("Inter", 700, "normal")


@lru_cache(maxsize=1)
def _runtime() -> tuple[str, str]:
    return (DOC_DESIGNS / "fit.js").read_text(), (DOC_DESIGNS / "base.css").read_text()


def _page_css(layout: LayoutV2) -> str:
    c, p = layout.canvas, layout.page
    key = f"{num(c.width)}x{num(c.height)}"
    css = ("html,body{margin:0;padding:0}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}"
           f'.moa-page[data-canvas="{key}"]{{width:{num(c.width)}px;height:{num(c.height)}px}}')
    kx = p.width_mm * PX_PER_MM / c.width
    ky = p.height_mm * PX_PER_MM / c.height
    css += (f'@media print{{.moa-page[data-canvas="{key}"]{{width:{p.width_mm:g}mm;height:{p.height_mm:g}mm}}')
    if abs(kx - 1) > 1e-4 or abs(ky - 1) > 1e-4:
        css += (f'.moa-page[data-canvas="{key}"] .moa-doc{{transform:scale({kx:.6f},{ky:.6f});'
                "transform-origin:0 0}")
    return css + "}"


def page_key(layout: LayoutV2) -> str:
    return f"{num(layout.canvas.width)}x{num(layout.canvas.height)}"


def build_html(layout: Union[LayoutV2, dict], values: Mapping[str, Any], *, context=None, kind: str = "ticket",
               for_browser: bool = False, base_url: Optional[str] = None, skip_preprinted: bool = False) -> str:
    """The complete page: for the render service (fonts inline as data:
    URIs), or `for_browser` (an editor's srcdoc iframe: the fonts from
    `base_url`/static/doc-fonts/, default APP_PUBLIC_API_URL)."""
    layout = parse(layout)
    ctx = Context.of(context)
    body = build_body(layout, values, context=ctx, kind=kind, skip_preprinted=skip_preprinted)
    faces = v2_fonts.faces_used(font_uses(layout))
    if for_browser:
        if base_url is None:
            from app.config import settings

            base_url = settings.PUBLIC_API_URL
        font_css = doc_fonts.for_browser(faces, base_url)
    else:
        font_css = doc_fonts.for_render(faces, _html.unescape(re.sub(r"<[^>]+>", "", body)))
    script, base = _runtime()
    p = layout.page
    title = esc(ctx.title or "Design")
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="moa-design" content="layout-v2">\n'
        f'<meta name="moa-page" content="{p.width_mm:g}mm {p.height_mm:g}mm">\n'
        f"<title>{title}</title>\n"
        "<!--moa:head-->\n"
        f'<style data-moa="fonts">\n{font_css}\n</style>\n'
        f'<style data-moa="base">{base}{_page_css(layout)}</style>\n'
        "<!--/moa:head-->\n"
        f'<script data-moa="fit">{script}</script>\n'
        "</head>\n<body>\n"
        f'<!--moa:page--><div class="moa-page" data-canvas="{page_key(layout)}">\n{body}\n</div><!--/moa:page-->\n'
        "</body>\n</html>\n"
    )


# ─── The cache key ──────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def runtime_digest() -> str:
    """The fit script, the base styles and the code that builds a page."""
    h = hashlib.sha256()
    for path in (DOC_DESIGNS / "fit.js", DOC_DESIGNS / "base.css", HERE / "build.py", HERE / "sanitize.py",
                 HERE / "vocabulary.py", HERE / "fonts.py", DOC_DESIGNS / "qr.py"):
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def fingerprint(layout: Union[LayoutV2, dict], values: Mapping[str, Any], context=None, kind: str = "ticket") -> str:
    """A short hash of everything a render reads: the layout, the values,
    the context (QR payload, colours, pictures), the fonts' lock and the
    builder. Pass it to doc_cache.fingerprint as one part."""
    from app.core.layout_v2.schema import dump

    layout = parse(layout)
    ctx = Context.of(context)
    images = {k: hashlib.sha256(str(v).encode()).hexdigest()[:16] for k, v in sorted(ctx.images.items())}
    blob = json.dumps(
        {"v": 2, "layout": dump(layout), "values": {k: str(v) for k, v in (values or {}).items()},
         "qr": ctx.qr, "colors": ctx.colors, "images": images, "kind": kind,
         "fonts": doc_fonts.digest(), "runtime": runtime_digest()},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:24]
