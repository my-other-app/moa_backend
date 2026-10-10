"""Layers for ticket and certificate artwork.

The template system styled a fixed layout: pick colours, a font and a border,
and the renderer arranged the content. That covers a club that wants the
default ticket in its own colours and nothing else -- anyone with artwork from
a designer had no way to use it.

A layer is a piece of content placed on that artwork at a coordinate. The
coordinate space is the artwork's own pixels, so what the editor shows and
what the renderer produces agree without either knowing the display size.

Both configs carry these, and both renderers switch to layer mode only when
`layers` is non-empty -- so every existing template keeps rendering exactly as
it did.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.api.templates_fit import fit_font_size


class TemplateLayer(BaseModel):
    """One positioned element on the artwork."""

    id: str = Field(..., description="Stable id, so the editor can track a layer across edits")
    type: Literal["text", "qr", "image", "shape"] = Field(default="text")
    name: Optional[str] = Field(default=None, description="What the editor calls it")

    # Content. For a text layer this may contain {{tokens}}; for a QR layer it
    # is ignored, since the QR always encodes the ticket.
    text: str = Field(default="")

    # Top-left, in artwork pixels.
    x: int = Field(default=0, ge=0)
    y: int = Field(default=0, ge=0)

    # QR layers are square, so width is the whole size.
    width: Optional[int] = Field(default=None, ge=1)

    font_size: int = Field(default=24, ge=1, le=400)
    color: str = Field(default="#ffffff")
    align: Literal["left", "center", "right"] = Field(default="left")
    bold: bool = Field(default=False)
    # Beyond this the text is clipped rather than pushing the layout around --
    # artwork has no reflow, so an overlong name must not run into the design.
    max_lines: int = Field(default=1, ge=1, le=20)
    line_spacing: float = Field(default=0, ge=-20, le=200)

    # --- Added for the layer editor -----------------------------------------
    # Every one defaults to what the renderer already did, so a template
    # saved before these existed renders exactly as it did.

    # Falls back to the template's own font when unset, so changing the
    # template font still moves every layer that never overrode it.
    font: Optional[str] = Field(default=None)
    # 400 or 700 in practice; `bold` above is kept so older configs still
    # render, and weight wins when both are set.
    weight: Optional[int] = Field(default=None, ge=100, le=900)
    letter_spacing: float = Field(default=0, ge=-20, le=100)
    opacity: int = Field(default=100, ge=0, le=100)
    height: Optional[int] = Field(default=None, ge=1)

    # What to do when the text does not fit its box. Artwork has no reflow,
    # so something has to give: shrink the type until it does, or cut it.
    overflow: Literal["shrink", "clip"] = Field(default="shrink")

    # Hidden layers stay in the file and are skipped when rendering, so a
    # club can park an element without losing its position. Locked ones
    # render normally and cannot be dragged.
    hidden: bool = Field(default=False)
    locked: bool = Field(default=False)

    # Image layers.
    image_url: Optional[str] = Field(default=None)
    # Shape layers: a filled rectangle, optionally rounded.
    fill: Optional[str] = Field(default=None)
    radius: int = Field(default=0, ge=0, le=400)

    @property
    def effective_weight(self) -> int:
        """The weight to render at, reconciling `weight` and the older `bold`."""
        if self.weight is not None:
            return self.weight
        return 700 if self.bold else 400


class LayerCanvas(BaseModel):
    """
    The artwork's pixel size.

    Stored rather than read from the image, because the renderer scales every
    coordinate against it: if the editor measured a 1600px-wide image and the
    renderer assumed something else, every layer would land in the wrong place.
    """

    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)


# The tokens a layer may use. Anything outside this list is left as written,
# so a typo shows up on the ticket as `{{even_name}}` rather than silently
# vanishing -- which is the difference between a club noticing and not.
TICKET_TOKENS = (
    "name",
    "ticket_code",
    "event_name",
    "event_title",
    "event_date",
    "event_time",
    "event_start_time",
    "event_end_time",
    "venue",
    "club_name",
    "team_name",
)

CERTIFICATE_TOKENS = (
    "name",
    "event_name",
    "event_title",
    "event_date",
    "club_name",
    "certificate_id",
    "placing",
)


def render_layers(layers, values: dict) -> list[dict]:
    """
    Substitute `{{token}}` in each layer's text.

    Done here rather than in the template because Jinja does not re-render the
    contents of a variable: `{{ layer.text }}` emits the literal braces. The
    result is attached as `rendered_text`, which the template prints.

    Substitution is a plain replace, not a Jinja render, on purpose -- layer
    text is club-supplied, and handing it to a template engine would let a
    club execute template syntax on the server.

    The fitted size is worked out here too, for the same reason: it depends
    on the substituted text, which only exists at this point. A layer set to
    shrink gets `rendered_font_size` reduced until the text fits its box;
    every other layer gets its own size back unchanged.
    """
    out = []
    for layer in layers:
        data = layer.model_dump() if hasattr(layer, "model_dump") else dict(layer)
        text = data.get("text") or ""
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", "" if value is None else str(value))
        data["rendered_text"] = text

        font_size = data.get("font_size") or 24
        # Missing keys mean a config saved before these existed. Defaulting
        # overflow to "shrink" here matches the schema default, so an old
        # template gets the fit as well -- it can only make text that used
        # to be cut off visible, never the reverse.
        if data.get("type", "text") == "text" and data.get("overflow", "shrink") == "shrink":
            weight = data.get("weight")
            data["rendered_font_size"] = fit_font_size(
                text,
                font_size=font_size,
                box_width=data.get("width"),
                max_lines=data.get("max_lines") or 1,
                bold=(weight >= 600) if weight else bool(data.get("bold")),
                letter_spacing=data.get("letter_spacing") or 0,
            )
        else:
            data["rendered_font_size"] = font_size

        out.append(data)
    return out
