"""Whether a v2 layout is right for its kind (layout-v2 contract).

`problems(layout, kind)` -> [{code, field, message, level, layer?}]:
"error"s stop a save; "warning"s are shown and may be saved. `field` is a
dotted path into the wire form ("layers.3.font.family"); `layer` the
layer's id when there is one. The messages are the editor's, word for word
in the TypeScript validation.
"""
from __future__ import annotations

from typing import Any, Union

from pydantic import ValidationError

from app.core.layout_v2 import fonts as v2_fonts
from app.core.layout_v2 import vocabulary as tk
from app.core.layout_v2.sanitize import UnsafeSvg, problems_of, sanitize
from app.core.layout_v2.schema import (
    ASPECT_TOLERANCE,
    ImageLayer,
    LayoutV2,
    QrLayer,
    ShapeLayer,
    SvgLayer,
    TextLayer,
    parse,
)

#: Kinds whose documents are scanned at a gate: exactly one QR.
QR_KINDS = ("ticket", "badge")
#: A QR smaller than this on paper is refused; smaller than WARN_MM, warned.
MIN_QR_MM = 10.0
WARN_QR_MM = 15.0


def _p(code: str, field: str, message: str, level: str = "error", layer: str | None = None) -> dict:
    out = {"code": code, "field": field, "message": message, "level": level}
    if layer:
        out["layer"] = layer
    return out


def _schema_problems(exc: ValidationError) -> list[dict]:
    out = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", ()) if not (isinstance(x, str) and x in (
            "text", "shape", "svg", "image", "qr", "solid", "linear", "radial")))
        msg = e.get("msg", "invalid")
        out.append(_p("schema", loc or "layout", msg.removeprefix("Value error, ")))
    return out


def _colors(layer) -> list[str]:
    out = []

    def fill(f):
        if f is None:
            return
        if getattr(f, "type", None) == "solid":
            out.append(f.color)
        else:
            out.extend(s.color for s in f.stops)

    for s in layer.shadows():
        out.append(s.color)
    if isinstance(layer, TextLayer):
        out.append(layer.color)
        fill(layer.background)
        out.extend(s.color for s in layer.spans or [] if s.color)
    elif isinstance(layer, ShapeLayer):
        fill(layer.fill)
        if layer.stroke:
            out.append(layer.stroke.color)
    elif isinstance(layer, SvgLayer):
        out.extend((layer.recolor or {}).values())
    elif isinstance(layer, ImageLayer):
        if layer.border:
            out.append(layer.border.color)
        st = layer.placeholder_style
        if st:
            out.append(st.color)
            fill(st.background)
    elif isinstance(layer, QrLayer):
        fill(layer.background)
    return out


def _font_problems(family: str, weight: int, style: str, field: str, name: str, lid: str) -> list[dict]:
    if not v2_fonts.has_family(family):
        near = ", ".join(v2_fonts.nearest_names(family))
        return [_p("unknown_font", f"{field}.family",
                   f"{name}: there is no font called {family!r}. The nearest are {near}.", layer=lid)]
    if not v2_fonts.has_face(family, weight, style):
        faces = next(f for f in v2_fonts.fonts_available() if f["family"] == family)["faces"]
        have = ", ".join(f"{f['weight']}{' italic' if f['style'] == 'italic' else ''}" for f in faces)
        return [_p("font_face", f"{field}.weight",
                   f"{name}: {family} has {have}, not {weight}{' italic' if style == 'italic' else ''}.", layer=lid)]
    return []


def problems(layout: Union[LayoutV2, dict, Any], kind: str = "ticket") -> list[dict]:
    try:
        lay = parse(layout)
    except ValidationError as exc:
        return _schema_problems(exc)
    out: list[dict] = []
    c, page = lay.canvas, lay.page
    want, have = page.width_mm / page.height_mm, c.width / c.height
    if abs(want - have) / want > ASPECT_TOLERANCE:
        out.append(_p("page_shape", "canvas",
                      f"The canvas is {c.width:g} × {c.height:g}, which is not the shape of a "
                      f"{page.width_mm:g} × {page.height_mm:g} mm page. Resize it, or change the page."))
    px_per_mm = c.width / page.width_mm
    qrs = [i for i, l in enumerate(lay.layers) if isinstance(l, QrLayer)]
    if kind in QR_KINDS and not qrs:
        out.append(_p("qr_missing", "layers", "This design needs its QR code: it is what the gate scans."))
    if len(qrs) > 1:
        out.append(_p("qr_extra", f"layers.{qrs[1]}", "A design has one QR code.", layer=lay.layers[qrs[1]].id))
    ids: set[str] = set()
    uses_accent = False
    for i, layer in enumerate(lay.layers):
        at = f"layers.{i}"
        name, lid = layer.name, layer.id
        if layer.id in ids:
            out.append(_p("duplicate_id", f"{at}.id", f'Two layers share the id "{layer.id}".', layer=lid))
        ids.add(layer.id)
        if "var:accent" in _colors(layer):
            uses_accent = True
        for t in tk.show_if_names(layer.show_if):
            if tk.canonical(t, kind) is None:
                out.append(_p("unknown_token", f"{at}.showIf", f"{name}: {t} is not a token.", layer=lid))
        if (layer.x >= c.width or layer.y >= c.height or layer.x + layer.width <= 0 or layer.y + layer.height <= 0) \
                and not layer.rotation:
            out.append(_p("off_canvas", f"{at}.x", f"{name}: this layer is outside the canvas.", "warning", lid))
        if isinstance(layer, TextLayer):
            out += _font_problems(layer.font.family, layer.font.weight, layer.font.style, f"{at}.font", name, lid)
            sources = [(f"{at}.text", layer.text)] if not layer.spans else \
                [(f"{at}.spans.{j}.text", s.text) for j, s in enumerate(layer.spans)]
            for field, src in sources:
                for e in tk.parse(src, kind).errors:
                    out.append(_p("template", field, f"{name}: {e}", layer=lid))
            for j, s in enumerate(layer.spans or []):
                if s.family or s.weight or s.style:
                    out += _font_problems(s.family or layer.font.family, s.weight or layer.font.weight,
                                          s.style or layer.font.style, f"{at}.spans.{j}", name, lid)
            if not layer.text.strip():
                out.append(_p("empty_text", f"{at}.text", f"{name}: this text says nothing.", "warning", lid))
        elif isinstance(layer, ShapeLayer):
            if layer.fill is None and layer.stroke is None and not layer.shadows():
                out.append(_p("invisible", f"{at}.fill", f"{name}: a shape needs a fill or an outline.", "warning", lid))
        elif isinstance(layer, SvgLayer):
            try:
                sanitize(layer.markup)
                removed = problems_of(layer.markup)
                if removed:
                    out.append(_p("svg_cleaned", f"{at}.markup",
                                  f"{name}: removed what a design may not hold: {', '.join(removed[:5])}.", "warning", lid))
            except UnsafeSvg as exc:
                out.append(_p("svg_unsafe", f"{at}.markup", f"{name}: {exc}.", layer=lid))
        elif isinstance(layer, ImageLayer):
            if layer.source == "upload" and not layer.image_key and not layer.hidden:
                out.append(_p("image_missing", f"{at}.imageKey", f"{name}: choose an image to show.", layer=lid))
            if layer.source == "guest_photo" and layer.preprinted:
                out.append(_p("photo_preprinted", f"{at}.preprinted",
                              f"{name}: a guest photo cannot be on the card stock.", layer=lid))
            st = layer.placeholder_style
            if st:
                out += _font_problems(st.family, st.weight, "normal", f"{at}.placeholderStyle", name, lid)
        elif isinstance(layer, QrLayer):
            if layer.hidden or layer.show_if:
                out.append(_p("qr_hidden", f"{at}.hidden", f"{name}: the QR code is always printed.", layer=lid))
            side_mm = min(layer.width, layer.height) / px_per_mm
            if side_mm < MIN_QR_MM:
                out.append(_p("qr_small", f"{at}.width",
                              f"{name}: {side_mm:.0f} mm on paper; at least {MIN_QR_MM:g} mm, or phones struggle to read it.",
                              layer=lid))
            elif side_mm < WARN_QR_MM:
                out.append(_p("qr_small", f"{at}.width",
                              f"{name}: {side_mm:.0f} mm on paper; {WARN_QR_MM:g} mm or more scans best.", "warning", lid))
            if abs(layer.width - layer.height) > 0.5:
                out.append(_p("qr_square", f"{at}.height", f"{name}: a QR code is square.", "warning", lid))
            if layer.x < 0 or layer.y < 0 or layer.x + layer.width > c.width + 0.5 or layer.y + layer.height > c.height + 0.5:
                out.append(_p("qr_outside", f"{at}.x", f"{name}: the whole QR code must be on the page.", layer=lid))
    if uses_accent and not lay.vars.accent:
        out.append(_p("accent_missing", "vars.accent", "Colours say var:accent, but the design has no accent colour."))
    return out


def errors(layout, kind: str = "ticket") -> list[dict]:
    return [p for p in problems(layout, kind) if p["level"] == "error"]
