"""What a v2 page needs besides the token values: the QR's payload, the
brand colours, and the pictures (layout-v2 contract, builder rules).

Every picture is a `data:image/...;base64,` URI (doc-designs D1: a page
never makes the renderer fetch anything). `images` is keyed by the image
layer's source ("org_logo", "event_icon", "guest_photo", "signature_1",
"signature_2", "sponsor") and, for uploads, by "upload:<imageKey>".

The wire form (for the TypeScript builder and the fixtures) is the same as
a dict: {"qr", "colors": {"primary", "secondary", "ticketType"}, "images",
"title"}.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

DEFAULT_COLORS = {"primary": "#1F3BD8", "secondary": "#FF8A3D", "ticketType": "#1F3BD8"}
#: A colour as a page may carry it: #RGB, #RGBA, #RRGGBB or #RRGGBBAA, nothing else.
SAFE_HEX = re.compile(r"^#(?:[0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


def safe_color(value: Any, default: str) -> str:
    """`value` when it is a hex colour, else `default`: what reaches a page's
    style and SVG attributes is checked again here, whatever was stored."""
    return value if isinstance(value, str) and SAFE_HEX.match(value) else default


@dataclass
class Context:
    #: What the QR encodes: a ticket's code, a certificate's verify address.
    qr: str = ""
    #: brand.primary, brand.secondary and ticket_type, as #RRGGBB.
    colors: dict = field(default_factory=lambda: dict(DEFAULT_COLORS))
    #: source (or "upload:<imageKey>") -> data: URI.
    images: dict = field(default_factory=dict)
    #: The PDF's title.
    title: str = ""

    @classmethod
    def of(cls, value: Union["Context", Mapping[str, Any], None]) -> "Context":
        if isinstance(value, Context):
            return value
        value = dict(value or {})
        given = value.get("colors") or {}
        colors = {key: safe_color(given.get(key), default) for key, default in DEFAULT_COLORS.items()}
        return cls(qr=str(value.get("qr") or ""), colors=colors, images=dict(value.get("images") or {}),
                   title=str(value.get("title") or ""))

    def image(self, source: str, image_key: Optional[str] = None) -> str:
        if source == "upload":
            return str(self.images.get(f"upload:{image_key}") or "") if image_key else ""
        return str(self.images.get(source) or "")

    def to_dict(self) -> dict:
        return {"qr": self.qr, "colors": dict(self.colors), "images": dict(self.images), "title": self.title}
