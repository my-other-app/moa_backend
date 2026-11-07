"""What a design may put into a document's CSS.

The ticket and certificate HTML templates (templates/pdf/events) print a
design's colours, border style, fonts and layer geometry straight into
`<style>` and `style="..."`. Jinja's autoescape keeps a value from closing
the attribute or the tag, but not from being CSS: a colour of
`red; background: url(...)` is valid inside a declaration and needs no quote
at all. wkhtmltopdf would then fetch whatever the design named.

So every such value has a shape, checked twice:

* on write (`problems`, used by the template schemas and the event design
  save), so a design that is not one is refused with a 422 naming the field;
* at render (`safe`, applied to every template's `style` in
  app.core.utils.pdf.render_html), because designs saved before this exist:
  a value that is not one is dropped and the template's own default prints.

And wkhtmltopdf itself reaches nothing but Google Fonts
(app.core.utils.pdf.NETWORK_OPTIONS): pictures arrive as data: URIs.
"""
from __future__ import annotations

import re
from typing import Any, Optional

#: A colour: #rgb or #rrggbb. The dashboard's pickers send #RRGGBB.
HEX_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")
BORDER_STYLES = ("none", "solid", "dashed", "dotted", "double")
#: A Google Fonts family name: letters, digits and spaces ("Noto Sans JP").
FONT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{0,63}$")
ALIGNS = ("left", "center", "right")
#: A picture the renderer embedded (app.api.templates_images.embedded).
DATA_IMAGE = re.compile(r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$")

COLOUR_FIELDS = ("accent_color", "bg_color", "text_color", "secondary_text_color", "border_color")
FONT_FIELDS = ("font", "title_font")
IMAGE_URL_FIELDS = ("logo_url", "secondary_logo_url", "background_image_url", "signature_1_url", "signature_2_url")
LAYER_COLOUR_FIELDS = ("color", "fill")
LAYER_NUMBER_FIELDS = (
    "x", "y", "width", "height", "font_size", "rendered_font_size", "line_spacing",
    "letter_spacing", "opacity", "max_lines", "weight", "radius",
)
LAYER_TYPES = ("text", "qr", "image", "shape")


def colour_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(HEX_COLOUR.match(value))


def font_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(FONT_NAME.match(value))


def _number_ok(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) < 100_000


def _blank(value: Any) -> bool:
    return value is None or value == ""


# ─── On write ─────────────────────────────────────────────────────────────


def check_colour(value: Optional[str], field: str = "colour") -> Optional[str]:
    """A pydantic validator's body: the value, or ValueError."""
    if _blank(value):
        return value
    if not colour_ok(value):
        raise ValueError(f"{field} must be a hex colour like #1db9a0")
    return value


def check_font(value: Optional[str], field: str = "font") -> Optional[str]:
    if _blank(value):
        return value
    if not font_ok(value):
        raise ValueError(f"{field} must be a font's name: letters, digits and spaces")
    return value


def problems(config: Any) -> list[str]:
    """Every value in a design's config that the templates would print into
    CSS and that is not of its shape. Empty when the design is fine. Fields a
    config does not name, or names as empty, are the template's defaults."""
    if not isinstance(config, dict):
        return ["The design must be an object."]
    out: list[str] = []
    for f in COLOUR_FIELDS:
        if not _blank(config.get(f)) and not colour_ok(config.get(f)):
            out.append(f"{f} must be a hex colour like #1db9a0")
    for f in FONT_FIELDS:
        if not _blank(config.get(f)) and not font_ok(config.get(f)):
            out.append(f"{f} must be a font's name: letters, digits and spaces")
    border = config.get("border_style")
    if not _blank(border) and border not in BORDER_STYLES:
        out.append(f"border_style must be one of {', '.join(BORDER_STYLES)}")
    canvas = config.get("canvas")
    if canvas is not None:
        if not isinstance(canvas, dict):
            out.append("canvas must be an object")
        else:
            for f in ("width", "height"):
                if not _blank(canvas.get(f)) and not _number_ok(canvas.get(f)):
                    out.append(f"canvas.{f} must be a number")
    layers = config.get("layers")
    if layers is not None:
        if not isinstance(layers, list):
            out.append("layers must be a list")
        else:
            for i, layer in enumerate(layers):
                out.extend(f"layers[{i}].{p}" for p in _layer_problems(layer))
    return out


def _layer_problems(layer: Any) -> list[str]:
    if not isinstance(layer, dict):
        return ["must be an object"]
    out = []
    for f in LAYER_COLOUR_FIELDS:
        if not _blank(layer.get(f)) and not colour_ok(layer.get(f)):
            out.append(f"{f} must be a hex colour like #1db9a0")
    if not _blank(layer.get("font")) and not font_ok(layer.get("font")):
        out.append("font must be a font's name: letters, digits and spaces")
    if not _blank(layer.get("align")) and layer.get("align") not in ALIGNS:
        out.append(f"align must be one of {', '.join(ALIGNS)}")
    if not _blank(layer.get("type")) and layer.get("type") not in LAYER_TYPES:
        out.append(f"type must be one of {', '.join(LAYER_TYPES)}")
    for f in LAYER_NUMBER_FIELDS:
        if not _blank(layer.get(f)) and not _number_ok(layer.get(f)):
            out.append(f"{f} must be a number")
    return out


# ─── At render ────────────────────────────────────────────────────────────


def safe(style: Any) -> Any:
    """The style as the templates may print it: every value that is not of
    its shape left out, so the template's own default prints instead. A
    picture is kept only as an embedded data: URI. Anything that is not a
    dict is handed back as it is (a template without a design)."""
    if not isinstance(style, dict):
        return style
    out = dict(style)
    for f in COLOUR_FIELDS:
        if f in out and not colour_ok(out[f]):
            out.pop(f)
    for f in FONT_FIELDS:
        if f in out and not font_ok(out[f]):
            out.pop(f)
    if "border_style" in out and out["border_style"] not in BORDER_STYLES:
        out["border_style"] = "none"
    for f in IMAGE_URL_FIELDS:
        if f in out and not (isinstance(out[f], str) and DATA_IMAGE.match(out[f])):
            out[f] = None
    canvas = out.get("canvas")
    if isinstance(canvas, dict):
        out["canvas"] = {k: v for k, v in canvas.items() if k not in ("width", "height") or _number_ok(v)}
    elif "canvas" in out:
        out["canvas"] = {}
    layers = out.get("layers")
    if isinstance(layers, list):
        out["layers"] = [_safe_layer(layer) for layer in layers if isinstance(layer, dict)]
    elif "layers" in out:
        out["layers"] = []
    return out


#: What a layer falls back to when its value is dropped: what the template
#: would print for it, so the layer still draws.
_LAYER_DEFAULTS = {"x": 0, "y": 0, "font_size": 24, "line_spacing": 0, "max_lines": 1, "color": "#ffffff", "align": "left"}


def _safe_layer(layer: dict) -> dict:
    out = dict(layer)
    for f in LAYER_COLOUR_FIELDS:
        if f in out and not colour_ok(out[f]):
            out.pop(f)
    if "font" in out and not font_ok(out["font"]):
        out.pop("font")
    if out.get("align") not in ALIGNS:
        out.pop("align", None)
    if out.get("type") not in LAYER_TYPES:
        out["type"] = "text"
    for f in LAYER_NUMBER_FIELDS:
        if f in out and out[f] is not None and not _number_ok(out[f]):
            out.pop(f)
    if not (isinstance(out.get("image_url"), str) and DATA_IMAGE.match(out["image_url"])):
        out["image_url"] = None
    for f, default in _LAYER_DEFAULTS.items():
        out.setdefault(f, default)
    return out
