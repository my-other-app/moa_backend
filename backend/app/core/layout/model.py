"""The layer, canvas and page schemas -- the wire format a design is saved in.

Field names are camelCase on the wire, as the TypeScript contract spells
them, so a design the editor saves is the design the renderer reads with no
translation layer to drift. Bounds mirror the contract exactly.
"""

import math
import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.core.layout.fonts import TICKET_FONTS, font_weight_of

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
COLOR_REFS = ("brand.primary", "brand.secondary", "ticket_type")
COLOR_REF_LABEL = {
    "brand.primary": "Club primary",
    "brand.secondary": "Club secondary",
    "ticket_type": "Ticket type colour",
}

TicketFont = Literal["inter", "google_sans", "mono", "dm_serif", "bebas"]


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


def _hex(value: str, field: str) -> str:
    if not HEX.match(value):
        raise ValueError(f"{field}: use a 6-digit hex colour, like #1db9a0.")
    return value


def _color_ref(value: str, field: str) -> str:
    if value in COLOR_REFS:
        return value
    return _hex(value, field)


# ─── Colours ───────────────────────────────────────────────────────────────


def resolve_color(ref: str, ctx: dict) -> str:
    """A fixed colour, or one that follows the club's brand or the ticket type."""
    if ref == "brand.primary":
        return ctx["primary"]
    if ref == "brand.secondary":
        return ctx["secondary"]
    if ref == "ticket_type":
        return ctx["ticketType"]
    return ref


def luminance(hex_color: str) -> float:
    n = int(hex_color.lstrip("#"), 16)
    channels = []
    for c in ((n >> 16) & 255, (n >> 8) & 255, n & 255):
        s = c / 255
        channels.append(s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ─── Layers ────────────────────────────────────────────────────────────────


class LayerBase(Wire):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    name: str = Field(min_length=1, max_length=60)
    x: float = Field(ge=-10_000, le=10_000)
    y: float = Field(ge=-10_000, le=10_000)
    hidden: bool = False
    locked: bool = False
    # Already on the card stock; drawn on screen, skipped when printed.
    preprinted: bool = False

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Give the layer a name.")
        return v


class TextLayer(LayerBase):
    kind: Literal["text"] = "text"
    text: str = Field(max_length=600)
    width: float = Field(ge=10, le=10_000)
    font: TicketFont = "inter"
    weight: int = Field(default=400, ge=100, le=900)
    size: float = Field(ge=4, le=800)
    color: str = "#141614"
    align: Literal["left", "center", "right"] = "left"
    max_lines: int = Field(default=1, ge=1, le=12)
    line_spacing: float = Field(default=0, ge=-200, le=800)
    letter_spacing: float = Field(default=0, ge=-50, le=200)
    fit: Literal["shrink", "truncate"] = "shrink"
    opacity: float = Field(default=1, ge=0, le=1)

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        return _color_ref(v, "color")


class QrLayer(LayerBase):
    kind: Literal["qr"] = "qr"
    size: float = Field(ge=1, le=10_000)
    quiet_zone: int = Field(default=2, ge=0, le=4)
    dark: str = "#000000"
    light: str = "#FFFFFF"

    @field_validator("dark")
    @classmethod
    def _dark(cls, v: str) -> str:
        return _hex(v, "dark")

    @field_validator("light")
    @classmethod
    def _light(cls, v: str) -> str:
        return v if v == "transparent" else _hex(v, "light")


ImageSource = Literal["upload", "org_logo", "event_icon", "guest_photo"]
IMAGE_SOURCE_LABEL = {
    "upload": "An uploaded image",
    "org_logo": "Club logo",
    "event_icon": "Event poster",
    "guest_photo": "Guest photo (initials when there is none)",
}


class ImageLayer(LayerBase):
    kind: Literal["image"] = "image"
    width: float = Field(ge=1, le=10_000)
    height: float = Field(ge=1, le=10_000)
    source: ImageSource = "upload"
    image_key: Optional[str] = Field(default=None, max_length=300)
    fit: Literal["contain", "cover"] = "contain"
    align: Literal["left", "center", "right"] = "center"
    radius: float = Field(default=0, ge=0, le=5_000)
    opacity: float = Field(default=1, ge=0, le=1)


SHAPE_PATH_BOX = 1000
SHAPE_PATH = re.compile(r"^M[\sMLCQZ0-9.,-]*$")


class ShapeLayer(LayerBase):
    kind: Literal["shape"] = "shape"
    width: float = Field(ge=0.5, le=10_000)
    height: float = Field(ge=0.5, le=10_000)
    fill: Optional[str] = "#1B6B47"
    stroke: Optional[str] = None
    stroke_width: float = Field(default=0, ge=0, le=200)
    radius: float = Field(default=0, ge=0, le=5_000)
    opacity: float = Field(default=1, ge=0, le=1)
    # Null draws the rounded rectangle; an outline draws that curve instead.
    path: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("fill", "stroke")
    @classmethod
    def _refs(cls, v: Optional[str], info) -> Optional[str]:
        return None if v is None else _color_ref(v, info.field_name)

    @field_validator("path")
    @classmethod
    def _path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not SHAPE_PATH.match(v):
            raise ValueError("An outline uses only M, L, C, Q and Z with numbers.")
        return v


TicketLayer = Annotated[
    Union[TextLayer, QrLayer, ImageLayer, ShapeLayer], Field(discriminator="kind")
]


def scale_shape_path(path: str, sx: float, sy: float) -> str:
    """A shape's outline in real units: every x times sx, every y times sy."""
    i = 0

    def scale(m: re.Match) -> str:
        nonlocal i
        v = float(m.group(0)) * (sx if i % 2 == 0 else sy)
        i += 1
        return str(round(v * 100) / 100)

    return re.sub(r"-?\d*\.?\d+(?:e-?\d+)?", scale, path, flags=re.I)


# ─── Canvas and page ───────────────────────────────────────────────────────


class TicketArea(Wire):
    """The part of the canvas that is the ticket; the rest is artboard."""

    x: float = Field(ge=0, le=6000)
    y: float = Field(ge=0, le=6000)
    width: float = Field(ge=100, le=6000)
    height: float = Field(ge=50, le=6000)
    radius: float = Field(default=0, ge=0, le=3000)


class TicketCanvas(Wire):
    width: int = Field(ge=300, le=6000)
    height: int = Field(ge=150, le=6000)
    background: str = "#FFFFFF"
    template_key: Optional[str] = Field(default=None, max_length=300)
    template_preprinted: bool = False
    area: Optional[TicketArea] = None

    @field_validator("background")
    @classmethod
    def _bg(cls, v: str) -> str:
        return _hex(v, "background")


def ticket_area(canvas: TicketCanvas) -> TicketArea:
    """The printed rectangle: the ticket area, or the whole canvas."""
    return canvas.area or TicketArea(x=0, y=0, width=canvas.width, height=canvas.height, radius=0)


def area_is_whole_canvas(canvas: TicketCanvas, a: TicketArea) -> bool:
    return (
        a.x <= 0.5
        and a.y <= 0.5
        and abs(a.x + a.width - canvas.width) <= 0.5
        and abs(a.y + a.height - canvas.height) <= 0.5
        and a.radius == 0
    )


MM_PER_INCH = 25.4
PT_PER_MM = 72 / MM_PER_INCH


class PageSpec(Wire):
    """The physical page. Null height: the height follows the ticket area."""

    width_mm: float = Field(ge=20, le=1200)
    height_mm: Optional[float] = Field(default=None, ge=20, le=1200)


TICKET_PAGE = PageSpec(width_mm=203.2, height_mm=None)

PAGE_PRESETS: dict[str, dict] = {
    "ticket": {"label": "Ticket · 8 in wide", "page": PageSpec(width_mm=203.2, height_mm=None)},
    "cr80_portrait": {"label": "ID card · CR80 portrait · 54 × 85.6 mm", "page": PageSpec(width_mm=54, height_mm=85.6)},
    "cr80_landscape": {"label": "ID card · CR80 landscape · 85.6 × 54 mm", "page": PageSpec(width_mm=85.6, height_mm=54)},
    "a6_portrait": {"label": "Badge · A6 portrait · 105 × 148 mm", "page": PageSpec(width_mm=105, height_mm=148)},
    "a4_landscape": {"label": "A4 landscape · 297 × 210 mm", "page": PageSpec(width_mm=297, height_mm=210)},
    "a4_portrait": {"label": "A4 portrait · 210 × 297 mm", "page": PageSpec(width_mm=210, height_mm=297)},
    "letter_landscape": {"label": "Letter landscape · 11 × 8.5 in", "page": PageSpec(width_mm=279.4, height_mm=215.9)},
}


def page_preset_of(page: PageSpec) -> Optional[str]:
    for key, preset in PAGE_PRESETS.items():
        p = preset["page"]
        if abs(p.width_mm - page.width_mm) < 0.05 and (p.height_mm or -1) == (page.height_mm or -1):
            return key
    return None


def page_size_pt(page: PageSpec, area) -> tuple[float, float]:
    """The page in points, from the ticket area's shape when the height is free."""
    width = page.width_mm * PT_PER_MM
    height = page.height_mm * PT_PER_MM if page.height_mm else width * area.height / area.width
    return width, height


def canvas_for_page(page: PageSpec, dpi: int = 300) -> tuple[int, int]:
    """Canvas pixels for a fixed page at a print resolution."""
    return (
        round(page.width_mm / MM_PER_INCH * dpi),
        round((page.height_mm or page.width_mm) / MM_PER_INCH * dpi),
    )


def px_per_mm(page: PageSpec, area) -> float:
    return area.width / page.width_mm


def min_qr_size(canvas: TicketCanvas, page: PageSpec = TICKET_PAGE) -> int:
    """The smallest QR a phone reliably reads: a tenth of the short side, never under 96 px, never under 12 mm."""
    a = ticket_area(canvas)
    physical = round(12 * px_per_mm(page, a))
    return max(96, physical, round(min(a.width, a.height) * 0.1))


MAX_LAYERS = 60


class TicketLayoutFields(Wire):
    """What the editor saves: the wire shape. Whether it is right is `engine.layout_problems`."""

    canvas: TicketCanvas
    layers: list[TicketLayer] = Field(min_length=1, max_length=MAX_LAYERS)
    page: PageSpec = Field(default_factory=lambda: PageSpec(width_mm=203.2, height_mm=None))


def dump(model: BaseModel) -> dict:
    """The wire form, camelCase, as the editor sends and stores it."""
    return model.model_dump(by_alias=True)


__all__ = [
    "COLOR_REFS", "COLOR_REF_LABEL", "IMAGE_SOURCE_LABEL", "MAX_LAYERS", "PAGE_PRESETS",
    "SHAPE_PATH_BOX", "TICKET_FONTS", "TICKET_PAGE", "ImageLayer", "PageSpec", "QrLayer",
    "ShapeLayer", "TextLayer", "TicketArea", "TicketCanvas", "TicketLayer", "TicketLayoutFields",
    "area_is_whole_canvas", "canvas_for_page", "contrast_ratio", "dump", "font_weight_of",
    "luminance", "min_qr_size", "page_preset_of", "page_size_pt", "px_per_mm", "resolve_color",
    "scale_shape_path", "ticket_area",
]
