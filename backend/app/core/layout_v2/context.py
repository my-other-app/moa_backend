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

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

DEFAULT_COLORS = {"primary": "#1F3BD8", "secondary": "#FF8A3D", "ticketType": "#1F3BD8"}


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
        colors = {**DEFAULT_COLORS, **(value.get("colors") or {})}
        return cls(qr=str(value.get("qr") or ""), colors=colors, images=dict(value.get("images") or {}),
                   title=str(value.get("title") or ""))

    def image(self, source: str, image_key: Optional[str] = None) -> str:
        if source == "upload":
            return str(self.images.get(f"upload:{image_key}") or "") if image_key else ""
        return str(self.images.get(source) or "")

    def to_dict(self) -> dict:
        return {"qr": self.qr, "colors": dict(self.colors), "images": dict(self.images), "title": self.title}
