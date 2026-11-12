"""Older designs as v2 layouts (layout-v2 contract, V4).

`from_v1(v1_layout)` -- a saved v1 layout (config.layout, drawn by
reportlab), mechanically: the ticket area becomes the canvas, every layer
keeps its place, the five v1 fonts become their CSS families, a text's
first baseline stays where reportlab put it, a shape's outline (drawn in a
1000 x 1000 box) is scaled to the layer's pixels, and the template image
(if any) is an image layer at the bottom. A v1 token spelled with an alias
that is now a design's token of another meaning ({{role}}: v1's ticket
type, a badge's ATTENDEE/SPEAKER/STAFF) is written as the v1 key it meant.
Agenda layouts stay v1 (ValueError).

`from_preset(preset)` -- a saved config.preset `{id, options, bindings,
accent}`: the design's v2 starter (for a certificate, the one converted
with the preset's wording), its accent as vars.accent, a club's own words
for a line in that line's text layer ("slot-<slot>"), the lanyard pill
hidden when the option is off, and each binding written into the text:
"text" puts the fixed words in place of the token, "field" reads the
registration answer ({{field.<slug>}}), "hidden" takes the token out and
hides what is shown only with it.
"""
from __future__ import annotations

import copy
import re
from functools import lru_cache
from typing import Any, Mapping, Optional

from app.core.layout.tokens import BY_NAME, parse_ticket_template
from app.core.layout_v2 import fonts as v2_fonts
from app.core.layout_v2.build import num
from app.core.layout_v2 import vocabulary as voc
from app.core.layout_v2.schema import CANVAS_MAX, CANVAS_MIN, dump, parse

# ─── v1 ─────────────────────────────────────────────────────────────────────

_V1_TOKEN = re.compile(r"(\{\{\s*)([A-Za-z0-9_.]+)((?:\s*\|[^}]*)?\s*\}\})")
_V1_IF = re.compile(r"(\{%\s*if\s+(?:not\s+)?)(?:\{\{\s*)?([A-Za-z0-9_.]+)(?:\s*\}\})?(\s*%\})", re.I)


def _v1_text(src: str) -> str:
    """v1 aliases that a design now uses with another meaning, as their v1 keys."""
    clash = voc.all_design_tokens()

    def fix(m: re.Match) -> str:
        name = m.group(2).lower()
        key = BY_NAME.get(name)
        if key and key != name and name in clash:
            return m.group(1) + key + m.group(3)
        return m.group(0)

    return _V1_IF.sub(fix, _V1_TOKEN.sub(fix, src))


@lru_cache(maxsize=32)
def _metrics(family: str, weight: int) -> tuple[float, float]:
    """A face's ascent and descent per em, as Chromium lays its lines out."""
    from fontTools.ttLib import TTFont

    from app.core.doc_designs import fonts as doc_fonts

    face = v2_fonts.nearest_face(family, weight, "normal")
    info = doc_fonts.lock()["families"].get(family)
    if not face or not info:
        return 0.9, 0.25
    entry = next(f for f in info["faces"] if f["style"] == face[0] and f["weight"] == face[1])
    with TTFont(doc_fonts.ASSETS / entry["ttf"]["file"], lazy=True) as f:
        upm = f["head"].unitsPerEm
        os2 = f["OS/2"] if "OS/2" in f else None
        if os2 is not None and os2.fsSelection & (1 << 7):
            asc, desc = os2.sTypoAscender, -os2.sTypoDescender
        else:
            asc, desc = f["hhea"].ascent, -f["hhea"].descent
    return asc / upm, desc / upm


def _v1_metrics(font_key: str, weight: int) -> tuple[float, float]:
    from app.core.layout.fonts import FONT_METRICS, font_file_of

    m = FONT_METRICS.get(font_file_of(font_key, weight)) or {"unitsPerEm": 1000, "ascent": 950, "descent": -250}
    return m["ascent"] / m["unitsPerEm"], -m["descent"] / m["unitsPerEm"]


def _solid(c: Optional[str]) -> Optional[dict]:
    return {"type": "solid", "color": c} if c else None


def from_v1(v1_layout: Mapping[str, Any]) -> dict:
    from app.core.layout.fonts import font_weight_of
    from app.core.layout.model import TicketLayoutFields, scale_shape_path, ticket_area

    v1 = TicketLayoutFields.model_validate(dict(v1_layout))
    if any(l.kind == "agenda" for l in v1.layers):
        raise ValueError("a programme (agenda) layout stays a v1 layout")
    area = ticket_area(v1.canvas)
    big, small = max(area.width, area.height), min(area.width, area.height)
    s = 1.0
    if big > CANVAS_MAX:
        s = CANVAS_MAX / big
    elif small < CANVAS_MIN:
        s = min(CANVAS_MIN / small, CANVAS_MAX / big)
    ox, oy = -area.x, -area.y

    def X(v: float) -> float:
        return round((v + ox) * s, 3)

    def Y(v: float) -> float:
        return round((v + oy) * s, 3)

    def S(v: float) -> float:
        return round(v * s, 3)

    width_mm = v1.page.width_mm
    height_mm = v1.page.height_mm or round(width_mm * area.height / area.width, 3)
    layers: list[dict] = []
    if v1.canvas.template_key:
        layers.append({"id": "template", "name": "Template", "kind": "image", "source": "upload",
                       "imageKey": v1.canvas.template_key, "x": X(0), "y": Y(0), "width": S(v1.canvas.width),
                       "height": S(v1.canvas.height), "fit": "cover", "preprinted": v1.canvas.template_preprinted})
    for l in v1.layers:
        base = {"id": l.id, "name": l.name, "kind": l.kind, "hidden": l.hidden, "locked": l.locked,
                "preprinted": l.preprinted}
        if l.kind == "text":
            family = v2_fonts.V1_FAMILIES[l.font]
            weight = font_weight_of(l.font, l.weight)
            lh = 1.2 + l.line_spacing / l.size
            a1, _d1 = _v1_metrics(l.font, weight)
            a2, d2 = _metrics(family, weight)
            # The first baseline where reportlab drew it (ascent below the top).
            top = l.y + a1 * l.size - ((lh - a2 - d2) / 2 + a2) * l.size
            layers.append({**base, "x": X(l.x), "y": Y(top), "width": S(l.width), "height": S(l.max_lines * lh * l.size),
                           "text": _v1_text(l.text), "font": {"family": family, "weight": weight},
                           "size": S(l.size), "color": l.color, "letterSpacing": round(l.letter_spacing / l.size, 4),
                           "lineHeight": round(lh, 4), "align": l.align, "maxLines": l.max_lines,
                           "fit": "shrink" if l.fit == "shrink" else "ellipsis",
                           "minSize": S(l.size * 0.5), "opacity": l.opacity})
        elif l.kind == "qr":
            layers.append({**base, "x": X(l.x), "y": Y(l.y), "width": S(l.size), "height": S(l.size),
                           "quietZone": l.quiet_zone})
        elif l.kind == "image":
            img = {**base, "x": X(l.x), "y": Y(l.y), "width": S(l.width), "height": S(l.height), "source": l.source,
                   "imageKey": l.image_key, "fit": l.fit, "align": l.align, "radius": S(l.radius), "opacity": l.opacity}
            if l.source == "guest_photo":
                img.update(placeholder="initials", placeholderStyle={
                    "color": "brand.primary", "background": _solid("#E9ECF2"), "family": "Inter", "weight": 700})
            layers.append(img)
        elif l.kind == "shape":
            shape = {**base, "x": X(l.x), "y": Y(l.y), "width": S(l.width), "height": S(l.height), "opacity": l.opacity,
                     "fill": _solid(l.fill), "radius": S(l.radius)}
            if l.stroke and l.stroke_width > 0:
                shape["stroke"] = {"color": l.stroke, "width": S(l.stroke_width)}
            if l.path:
                scaled = scale_shape_path(l.path, S(l.width) / 1000, S(l.height) / 1000)
                shape.update(shape="path", path=re.sub(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", lambda m: num(float(m.group(0))), scaled))
            else:
                shape["shape"] = "rect"
            layers.append(shape)
    return dump(parse({
        "version": 2,
        "page": {"widthMm": width_mm, "heightMm": height_mm},
        "canvas": {"width": S(area.width), "height": S(area.height), "background": _solid(v1.canvas.background)},
        "layers": layers,
    }))


# ─── Presets ────────────────────────────────────────────────────────────────


def _serialise(nodes: list) -> str:
    out = []
    for n in nodes:
        if n["t"] == "text":
            out.append(n["v"])
        elif n["t"] == "var":
            out.append("{{" + " | ".join([n["key"], *n["filters"]]) + "}}")
        else:
            out.append("{% if " + ("not " if n["negate"] else "") + n["key"] + " %}" + _serialise(n["then"])
                       + ("{% else %}" + _serialise(n["else"]) if n["else"] else "") + "{% endif %}")
    return "".join(out)


def _filters(v: str, filters: list) -> str:
    for f in filters:
        if f == "upper":
            v = v.upper()
        elif f == "lower":
            v = v.lower()
        else:
            v = re.sub(r"(^|[\s\-/'])(\w)", lambda m: m.group(1) + m.group(2).upper(), v.lower())
    return v


def _bind(nodes: list, token: str, source: str, value: str) -> list:
    out = []
    for n in nodes:
        if n["t"] == "var" and n["key"] == token:
            if source == "text":
                out.append({"t": "text", "v": _filters(value, n["filters"])})
            elif source == "field":
                out.append({**n, "key": f"field.{value}"})
            continue  # hidden: nothing
        if n["t"] == "if":
            then, other = _bind(n["then"], token, source, value), _bind(n["else"], token, source, value)
            if n["key"] == token and source in ("text", "hidden"):
                truthy = source == "text" and bool(value.strip())
                out.extend(then if truthy != n["negate"] else other)
                continue
            key = f"field.{value}" if n["key"] == token and source == "field" else n["key"]
            out.append({**n, "key": key, "then": then, "else": other})
            continue
        out.append(n)
    return out


def _bind_text(src: str, token: str, source: str, value: str, kind: str) -> str:
    if token not in src:
        return src
    parsed = parse_ticket_template(src, set(voc.design_tokens(kind)))
    return _serialise(_bind(parsed.nodes, token, source, value))


def _bind_show_if(layer: dict, token: str, source: str, value: str) -> None:
    show = layer.get("showIf")
    if not show or token not in voc.show_if_names(show):
        return
    anyof = "|" in show
    names = voc.show_if_names(show)
    if source == "field":
        names = [f"field.{value}" if t == token else t for t in names]
    elif source == "text":
        if anyof:
            names = [] if value.strip() else [t for t in names if t != token]
        else:
            names = [t for t in names if t != token]
    elif source == "hidden":
        if anyof:
            names = [t for t in names if t != token]
            if not names:
                layer["hidden"] = True
        else:
            layer["hidden"] = True
    layer["showIf"] = ("|" if anyof else " ").join(names) or None
    if layer["showIf"] is None:
        layer.pop("showIf")


def _clean_text(words: str) -> str:
    return " ".join(str(words).split())[:200]


def from_preset(preset: Mapping[str, Any]) -> dict:
    from app.core import doc_designs
    from app.core.layout_v2 import gallery

    design_id = str(preset.get("id") or "")
    d = doc_designs.get(design_id)
    options = dict(preset.get("options") or {})
    wording = options.get("wording")
    default = (d.raw.get("default_wording") if d.kind == "certificate" else None)
    layout = gallery.starter(design_id, wording if wording and wording != default else None)
    layout = copy.deepcopy(layout)
    accent = preset.get("accent")
    if accent and d.accent:
        layout.setdefault("vars", {})["accent"] = str(accent).upper()
    layers = layout["layers"]
    for slot, words in (options.get("text") or {}).items():
        for layer in layers:
            if layer["kind"] == "text" and layer["id"] == f"slot-{slot}":
                layer.pop("spans", None)
                layer["text"] = _clean_text(words)
    if options.get("lanyard") is False:
        for layer in layers:
            if layer["id"].startswith("lanyard"):
                layer["hidden"] = True
    for token, b in (preset.get("bindings") or {}).items():
        if not isinstance(b, dict):
            continue
        source = b.get("source", "auto")
        if source not in ("text", "field", "hidden"):
            continue
        value = _clean_text(b.get("value") or "").replace("{", "(").replace("}", ")") if source == "text" else \
            str(b.get("value") or "")
        for layer in layers:
            if layer["kind"] == "text":
                if layer.get("spans"):
                    for span in layer["spans"]:
                        span["text"] = _bind_text(span["text"], token, source, value, d.kind)
                else:
                    layer["text"] = _bind_text(layer.get("text", ""), token, source, value, d.kind)
            _bind_show_if(layer, token, source, value)
    layout["starter"] = design_id
    return dump(parse(layout))
