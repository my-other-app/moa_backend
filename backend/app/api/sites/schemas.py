from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _trim(v):
    if v is None:
        return None
    v = v.strip()
    return v or None


class LayoutPatch(BaseModel):
    """Only the fields sent change (layout.apply): `order` and `hidden`
    replace, `titles` and `slots` merge by key (null or "" clears one),
    `accent` and `join_text` set or (null, "") clear."""

    order: Optional[list[str]] = Field(None, max_length=40)
    hidden: Optional[list[str]] = Field(None, max_length=40)
    titles: Optional[dict[str, Optional[str]]] = None
    slots: Optional[dict[str, Optional[int]]] = None
    accent: Optional[str] = Field(None, max_length=7)
    #: The join section's line, plain text (240 characters at most: SITE_JOIN_TEXT_TOO_LONG past it).
    join_text: Optional[str] = Field(None, max_length=2000)

    @field_validator("titles", "slots")
    @classmethod
    def _small(cls, v):
        if v is not None and len(v) > 40:
            raise ValueError("at most 40 keys")
        return v


class SitePatch(BaseModel):
    template: Optional[str] = Field(None, max_length=20)
    layout: Optional[LayoutPatch] = None
    headline: Optional[str] = Field(None, max_length=120)
    subline: Optional[str] = Field(None, max_length=240)

    _t = field_validator("headline", "subline")(_trim)

    def changes(self) -> dict:
        """What was sent, the layout's own fields only as sent."""
        out = self.model_dump(exclude_unset=True, exclude={"layout"})
        if self.layout is not None:
            out["layout"] = self.layout.model_dump(exclude_unset=True)
        return out


class SiteImageOut(BaseModel):
    id: int
    url: Optional[str]
    srcset: Optional[str]
    alt: Optional[str]
    width: int
    height: int
    position: int
    #: The current template's slots the photo shows in: chosen, or filling the gallery.
    used_in: list[str]


class SiteImagePatch(BaseModel):
    alt: Optional[str] = Field(None, max_length=200)


class SiteImageOrder(BaseModel):
    ids: list[int] = Field(..., max_length=40)


class SiteOut(BaseModel):
    template: str
    headline: Optional[str]
    subline: Optional[str]
    #: What the hero says when the headline is left empty: the tagline, else the name.
    default_headline: str
    default_subline: Optional[str]
    #: The layout for the current template: {order, hidden, titles, slots, accent, join_text}.
    layout: dict
    #: Every movable section's title as the page shows it (the club's, else the template's).
    titles: dict[str, str]
    #: The movable sections in order, each shown (true) or hidden.
    sections: dict[str, bool]
    #: Which sections have anything to show, so the console can say "no partners yet".
    available: dict[str, bool]
    #: The club's own colour (its brand colour), for the accent switch.
    club_colour: Optional[str]
    images: list[SiteImageOut]
    limits: dict[str, int]
    published_at: Optional[datetime]
    #: Whether the saved choices or the photos changed since the page was last made.
    stale: bool
    url: str
    slug: str
    templates: list[dict]
    #: The site's own tracking link (id, code, short_url) once it is published; None before.
    tracking: Optional[dict] = None
    #: After a PATCH: what the layout check changed in what was sent
    #: (order_dropped, order_appended, hidden_dropped, titles_dropped); None otherwise.
    adjusted: Optional[dict[str, list[str]]] = None


class PreviewOut(BaseModel):
    html: str
