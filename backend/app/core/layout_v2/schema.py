"""Layout v2: the wire format of an editable design (layout-v2 contract, V1).

A design is a fixed canvas and a list of absolutely positioned layers,
painted in order (the first is at the bottom). Field names are camelCase on
the wire, as the TypeScript types spell them (moa_web packages/layout/src/v2),
so what the editor saves is what the builder reads, with no translation.

    {"version": 2,
     "page": {"widthMm": 101.6, "heightMm": 152.4},
     "canvas": {"width": 384, "height": 576, "background": Fill | null},
     "vars": {"accent": "#FF8A3D"},
     "layers": [Layer, ...]}

Colours are "#RRGGBB", "#RRGGBBAA", "var:accent" (the document's accent,
`vars.accent`), or the v1 references "brand.primary", "brand.secondary" and
"ticket_type". A layer is one of five kinds: text, shape, svg, image, qr.

`parse(data)` checks the shape (a pydantic ValidationError when it is
wrong); whether the design is *right* -- known fonts and tokens, one QR, the
page's shape -- is `problems(layout, kind)` (validate.py).

Beyond the contract's first draft (recorded under the contract's Changes):
text `spans` (a run of differently styled words in one paragraph: the
certificates' bold course titles), `textWrap`, `showIf` naming several
tokens ("a b": all of them, "a|b": any of them), a list of shadows with
`inset`, image `align`/`valign`/`placeholderStyle`, and `starter`.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

VERSION = 2
MAX_LAYERS = 150
MAX_TEXT = 2000
MAX_SPANS = 40
MAX_MARKUP = 60_000
MAX_PATH = 20_000
CANVAS_MIN, CANVAS_MAX = 300, 3000
#: The canvas's shape must be the page's, within this share.
ASPECT_TOLERANCE = 0.015

HEX = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")
HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
COLOR_REFS = ("var:accent", "brand.primary", "brand.secondary", "ticket_type")
ID = r"^[A-Za-z0-9_-]{1,40}$"
#: A token name as showIf names it: a catalogue key or field.<slug>.
SHOW_IF = re.compile(r"^\s*[A-Za-z0-9_.]+(?:\s*(?:\||\s)\s*[A-Za-z0-9_.]+)*\s*$")
#: Path commands a shape may use, in the layer's own pixels.
PATH = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9.,\s+eE-]*$")


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


def check_color(value: str, field: str = "color") -> str:
    if value in COLOR_REFS:
        return value
    if not isinstance(value, str) or not HEX.match(value):
        raise ValueError(f"{field}: use #RRGGBB, #RRGGBBAA, var:accent, brand.primary, brand.secondary or ticket_type")
    return value.upper()


Color = Annotated[str, Field(min_length=4, max_length=16)]


# ─── Fills, strokes and shadows ─────────────────────────────────────────────


class Stop(Wire):
    offset: float = Field(ge=0, le=1)
    color: Color

    @field_validator("color")
    @classmethod
    def _c(cls, v: str) -> str:
        return check_color(v)


class SolidFill(Wire):
    type: Literal["solid"] = "solid"
    color: Color

    @field_validator("color")
    @classmethod
    def _c(cls, v: str) -> str:
        return check_color(v)


class LinearFill(Wire):
    """CSS linear-gradient: 0deg runs bottom to top, 90deg left to right."""

    type: Literal["linear"] = "linear"
    angle: float = Field(ge=-3600, le=3600)
    stops: list[Stop] = Field(min_length=2, max_length=6)


class RadialFill(Wire):
    """CSS radial-gradient(circle at cx cy, ...), to the farthest corner."""

    type: Literal["radial"] = "radial"
    cx: float = Field(default=0.5, ge=0, le=1)
    cy: float = Field(default=0.5, ge=0, le=1)
    stops: list[Stop] = Field(min_length=2, max_length=6)


Fill = Annotated[Union[SolidFill, LinearFill, RadialFill], Field(discriminator="type")]


class Shadow(Wire):
    """A box shadow (a text layer: a text shadow, where spread does nothing)."""

    x: float = Field(default=0, ge=-1000, le=1000)
    y: float = Field(default=0, ge=-1000, le=1000)
    blur: float = Field(default=0, ge=0, le=1000)
    spread: float = Field(default=0, ge=-1000, le=1000)
    color: Color = "#00000040"
    inset: bool = False

    @field_validator("color")
    @classmethod
    def _c(cls, v: str) -> str:
        return check_color(v)


class Stroke(Wire):
    color: Color
    width: float = Field(gt=0, le=500)
    dash: Optional[list[float]] = Field(default=None, min_length=2, max_length=2)

    @field_validator("color")
    @classmethod
    def _c(cls, v: str) -> str:
        return check_color(v)

    @field_validator("dash")
    @classmethod
    def _dash(cls, v):
        if v is not None and (any(x < 0 for x in v) or sum(v) <= 0):
            raise ValueError("dash: two lengths, a dash and a gap, not both zero")
        return v


class Border(Wire):
    color: Color
    width: float = Field(gt=0, le=500)

    @field_validator("color")
    @classmethod
    def _c(cls, v: str) -> str:
        return check_color(v)


def _four(v: Any, field: str, lo: float = 0) -> list[float]:
    """[t, r, b, l] (padding) or [tl, tr, br, bl] (radius); a number is all four."""
    if v is None:
        return [0, 0, 0, 0]
    if isinstance(v, (int, float)):
        v = [v] * 4
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise ValueError(f"{field}: four numbers, or one")
    out = [float(x) for x in v]
    if any(x < lo or x > 5000 for x in out):
        raise ValueError(f"{field}: from {lo:g} to 5000")
    return out


Four = Annotated[list[float], Field(min_length=4, max_length=4)]


# ─── Layers ─────────────────────────────────────────────────────────────────


class LayerBase(Wire):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=60)
    x: float = Field(ge=-10_000, le=10_000)
    y: float = Field(ge=-10_000, le=10_000)
    width: float = Field(gt=0, le=10_000)
    height: float = Field(gt=0, le=10_000)
    rotation: float = Field(default=0, ge=-360, le=360)
    opacity: float = Field(default=1, ge=0, le=1)
    hidden: bool = False
    locked: bool = False
    #: Left out when this token's value is empty (after trimming). Several
    #: tokens: "a b" needs every one, "a|b" any one.
    show_if: Optional[str] = Field(default=None, max_length=200)
    #: Already on the card stock: drawn on screen, skipped when printed on it.
    preprinted: bool = False
    shadow: Optional[Union[Shadow, list[Shadow]]] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Give the layer a name.")
        return v

    @field_validator("show_if")
    @classmethod
    def _show_if(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not SHOW_IF.match(v):
            raise ValueError("showIf: a token, or tokens separated by spaces (all) or | (any)")
        return v

    @field_validator("shadow")
    @classmethod
    def _shadows(cls, v):
        if isinstance(v, list):
            if not v:
                return None
            if len(v) > 6:
                raise ValueError("shadow: at most 6")
        return v

    def shadows(self) -> list[Shadow]:
        if self.shadow is None:
            return []
        return list(self.shadow) if isinstance(self.shadow, list) else [self.shadow]


class Font(Wire):
    family: str = Field(min_length=1, max_length=80)
    weight: int = Field(default=400, ge=100, le=900)
    style: Literal["normal", "italic"] = "normal"


Align = Literal["left", "center", "right"]
VAlign = Literal["top", "middle", "bottom"]
Transform = Literal["none", "upper", "lower"]


class Span(Wire):
    """Words in one paragraph that look different from the rest of it (a
    bold course title in a sentence). Unset fields take the layer's. `size`
    is in px; it scales with the layer when the layer's text shrinks."""

    text: str = Field(max_length=MAX_TEXT)
    family: Optional[str] = Field(default=None, min_length=1, max_length=80)
    weight: Optional[int] = Field(default=None, ge=100, le=900)
    style: Optional[Literal["normal", "italic"]] = None
    size: Optional[float] = Field(default=None, gt=0, le=800)
    color: Optional[Color] = None
    letter_spacing: Optional[float] = Field(default=None, ge=-1, le=4)
    transform: Optional[Transform] = None

    @field_validator("color")
    @classmethod
    def _c(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else check_color(v)


class TextLayer(LayerBase):
    kind: Literal["text"] = "text"
    #: The token language: {{token}}, {{token | upper}}, {% if token %}…{% else %}…{% endif %}.
    text: str = Field(default="", max_length=MAX_TEXT)
    #: Styled runs, in order; when set, they are the text (and `text` is their join).
    spans: Optional[list[Span]] = Field(default=None, max_length=MAX_SPANS)
    font: Font = Field(default_factory=lambda: Font(family="Inter"))
    size: float = Field(default=16, gt=0, le=800)
    color: Color = "#141614"
    letter_spacing: float = Field(default=0, ge=-1, le=4)  # em
    line_height: float = Field(default=1.2, ge=0.5, le=4)  # unitless
    align: Align = "left"
    valign: VAlign = "top"
    transform: Transform = "none"
    text_wrap: Literal["normal", "balance", "pretty"] = "normal"
    max_lines: int = Field(default=1, ge=1, le=20)
    fit: Literal["shrink", "ellipsis", "none"] = "shrink"
    min_size: Optional[float] = Field(default=None, gt=0, le=800)
    background: Optional[Fill] = None
    padding: Four = Field(default_factory=lambda: [0, 0, 0, 0])
    radius: Four = Field(default_factory=lambda: [0, 0, 0, 0])

    @field_validator("color")
    @classmethod
    def _c(cls, v: str) -> str:
        return check_color(v)

    @field_validator("padding", "radius", mode="before")
    @classmethod
    def _four(cls, v, info):
        return _four(v, info.field_name)

    @model_validator(mode="after")
    def _spans_are_the_text(self):
        if self.spans:
            self.text = "".join(s.text for s in self.spans)
        elif self.spans is not None:
            self.spans = None
        if self.min_size is not None and self.min_size > self.size:
            self.min_size = self.size
        return self

    def floor(self) -> float:
        """The smallest size `shrink` goes to (default: half)."""
        return self.min_size if self.min_size is not None else round(self.size * 0.5, 3)


class ShapeLayer(LayerBase):
    kind: Literal["shape"] = "shape"
    shape: Literal["rect", "ellipse", "path"] = "rect"
    #: For "path": M/L/H/V/C/S/Q/T/A/Z in the layer's own pixels (0..width, 0..height).
    path: Optional[str] = Field(default=None, max_length=MAX_PATH)
    fill: Optional[Fill] = None
    stroke: Optional[Stroke] = None
    radius: Four = Field(default_factory=lambda: [0, 0, 0, 0])

    @field_validator("radius", mode="before")
    @classmethod
    def _four(cls, v, info):
        return _four(v, info.field_name)

    @field_validator("path")
    @classmethod
    def _path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = " ".join(v.split())
        if not v:
            return None
        if not PATH.match(v) or v[0] not in "Mm":
            raise ValueError("path: SVG path commands (M, L, H, V, C, S, Q, T, A, Z) and numbers, starting with M")
        return v

    @model_validator(mode="after")
    def _needs_path(self):
        if self.shape == "path" and not self.path:
            raise ValueError("a path shape needs its path")
        return self


class SvgLayer(LayerBase):
    kind: Literal["svg"] = "svg"
    #: One <svg> element; sanitised before it is drawn (sanitize.py).
    markup: str = Field(min_length=10, max_length=MAX_MARKUP)
    fit: Literal["contain", "cover", "stretch"] = "contain"
    #: Colour replacements: {"#RRGGBB": Color}, matched in fill/stroke/stop-color/style.
    recolor: Optional[dict[str, Color]] = None

    @field_validator("recolor")
    @classmethod
    def _recolor(cls, v):
        if not v:
            return None
        if len(v) > 32:
            raise ValueError("recolor: at most 32 colours")
        out = {}
        for k, c in v.items():
            if not HEX.match(k):
                raise ValueError(f"recolor: {k!r} is not a #RRGGBB colour")
            out[k.upper()] = check_color(c, "recolor")
        return out


ImageSource = Literal["upload", "org_logo", "event_icon", "guest_photo", "signature_1", "signature_2", "sponsor"]


class PlaceholderStyle(Wire):
    """How the initials tile looks when a guest has no photo."""

    color: Color = "#3C4257"
    background: Optional[Fill] = None
    family: str = Field(default="Inter", min_length=1, max_length=80)
    weight: int = Field(default=700, ge=100, le=900)

    @field_validator("color")
    @classmethod
    def _c(cls, v: str) -> str:
        return check_color(v)


class ImageLayer(LayerBase):
    kind: Literal["image"] = "image"
    source: ImageSource = "upload"
    image_key: Optional[str] = Field(default=None, max_length=300)
    fit: Literal["contain", "cover"] = "contain"
    align: Align = "center"
    valign: VAlign = "middle"
    radius: Four = Field(default_factory=lambda: [0, 0, 0, 0])
    border: Optional[Border] = None
    placeholder: Literal["initials", "none"] = "none"
    placeholder_style: Optional[PlaceholderStyle] = None

    @field_validator("radius", mode="before")
    @classmethod
    def _four(cls, v, info):
        return _four(v, info.field_name)


class QrLayer(LayerBase):
    """What it encodes is the kind's: a ticket's or badge's ticket code, a
    certificate's verify address. Never editable; always black on white."""

    kind: Literal["qr"] = "qr"
    dark: Literal["#000000"] = "#000000"
    light: Literal["#FFFFFF"] = "#FFFFFF"
    quiet_zone: int = Field(default=0, ge=0, le=4)
    background: Optional[Fill] = None
    radius: Four = Field(default_factory=lambda: [0, 0, 0, 0])

    @field_validator("dark", "light", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.upper() if isinstance(v, str) else v

    @field_validator("radius", mode="before")
    @classmethod
    def _four(cls, v, info):
        return _four(v, info.field_name)


Layer = Annotated[Union[TextLayer, ShapeLayer, SvgLayer, ImageLayer, QrLayer], Field(discriminator="kind")]
KINDS = ("text", "shape", "svg", "image", "qr")


class Page(Wire):
    width_mm: float = Field(ge=20, le=1200)
    height_mm: float = Field(ge=20, le=1200)


class Canvas(Wire):
    width: float = Field(ge=CANVAS_MIN, le=CANVAS_MAX)
    height: float = Field(ge=CANVAS_MIN, le=CANVAS_MAX)
    background: Optional[Fill] = None


class Vars(Wire):
    accent: Optional[str] = None

    @field_validator("accent")
    @classmethod
    def _accent(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not HEX6.match(v):
            raise ValueError("accent: a colour like #1F3BD8")
        return v.upper()


class LayoutV2(Wire):
    version: Literal[2] = 2
    page: Page
    canvas: Canvas
    vars: Vars = Field(default_factory=Vars)
    layers: list[Layer] = Field(default_factory=list, max_length=MAX_LAYERS)
    #: The starter it was copied from ("badge/06-thermal-mono"), if any.
    starter: Optional[str] = Field(default=None, max_length=80)


def parse(data: Any) -> LayoutV2:
    """A layout from its wire form (a dict, or a LayoutV2 as it is). Raises
    pydantic's ValidationError on a wrong shape."""
    if isinstance(data, LayoutV2):
        return data
    return LayoutV2.model_validate(data)


def dump(layout: LayoutV2) -> dict:
    """The wire form, camelCase, without unset optional noise."""
    return layout.model_dump(by_alias=True, exclude_none=True, mode="json")


#: The model of each layer kind (for its own defaults).
KIND_MODELS = {"text": TextLayer, "shape": ShapeLayer, "svg": SvgLayer, "image": ImageLayer, "qr": QrLayer}
#: Kept in a compact layout even when they hold a default: what a layer is and where.
ALWAYS = frozenset({"id", "name", "kind", "x", "y", "width", "height"})


def _wire(v: Any) -> Any:
    if isinstance(v, BaseModel):
        return v.model_dump(by_alias=True, exclude_none=True, mode="json")
    if isinstance(v, list):
        return [_wire(x) for x in v]
    return v


def _ints(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, list):
        return [_ints(x) for x in v]
    if isinstance(v, dict):
        return {k: _ints(x) for k, x in v.items()}
    return v


def kind_defaults(kind: str) -> dict[str, Any]:
    """A layer kind's default for each optional field, in wire form (camelCase)."""
    out = {}
    for name, f in KIND_MODELS[kind].model_fields.items():
        if f.is_required():
            continue
        out[f.alias or name] = _ints(_wire(f.get_default(call_default_factory=True)))
    return out


def compact(layout: Any) -> dict:
    """The wire form without what parsing restores: each layer's fields that
    hold *its own kind's* default, and a text's `text` when it has spans
    (it is their join). Lossless: `dump(parse(compact(x))) == dump(parse(x))`
    (checked here, so a starter can never lose a field)."""
    full = dump(parse(layout))
    out = _ints(full)
    for layer in out["layers"]:
        defaults = kind_defaults(layer["kind"])
        for key in list(layer):
            if key not in ALWAYS and key in defaults and layer[key] == defaults[key]:
                del layer[key]
        if layer["kind"] == "text" and layer.get("spans"):
            layer.pop("text", None)
    if dump(parse(out)) != full:
        raise AssertionError("compacting this layout would lose a field")
    return out


def is_v2(config_layout: Any) -> bool:
    """Whether a stored `config.layout` is a v2 layout."""
    return isinstance(config_layout, dict) and config_layout.get("version") == VERSION and "layers" in config_layout
