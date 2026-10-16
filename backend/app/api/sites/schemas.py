from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Template = Literal["launchpad", "chapter", "campus", "arena"]
Theme = Literal["light", "dark"]
Font = Literal["dm_sans", "inter", "fraunces", "space_grotesk", "manrope"]


def _trim(v):
    if v is None:
        return None
    v = v.strip()
    return v or None


class SitePatch(BaseModel):
    template: Optional[Template] = None
    theme: Optional[Theme] = None
    font: Optional[Font] = None
    headline: Optional[str] = Field(None, max_length=120)
    subline: Optional[str] = Field(None, max_length=240)
    sections: Optional[dict[str, bool]] = None

    _t = field_validator("headline", "subline")(_trim)


class SiteOut(BaseModel):
    template: Template
    theme: Theme
    font: Font
    headline: Optional[str]
    subline: Optional[str]
    #: What the hero says when the headline is left empty: the tagline, else the name.
    default_headline: str
    default_subline: Optional[str]
    sections: dict[str, bool]
    #: Which sections have anything to show, so the console can say "no partners yet".
    available: dict[str, bool]
    published_at: Optional[datetime]
    #: Whether the saved choices differ from what was last published.
    stale: bool
    url: str
    slug: str
    templates: list[dict]
    fonts: list[dict]
    #: The site's own tracking link (id, code, short_url) once it is published; None before.
    tracking: Optional[dict] = None


class PreviewOut(BaseModel):
    html: str
