from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.api.event_partners.models import LABEL_MAX, LINK_MAX, MAX_PER_EVENT, NAME_MAX

Kind = Literal["sponsor", "partner"]


def clean_name(v):
    if v is None:
        return None
    v = v.strip()
    if not v:
        raise ValueError("Name the sponsor or partner")
    if len(v) > NAME_MAX:
        raise ValueError(f"Keep the name under {NAME_MAX} characters")
    return v


def clean_label(v):
    """Trimmed; empty is none."""
    if v is None:
        return None
    v = v.strip()
    if len(v) > LABEL_MAX:
        raise ValueError(f"Keep the label under {LABEL_MAX} characters")
    return v or None


def https_link(v):
    """An absolute https:// address with a host, or none. Nothing else: the
    page opens it for anyone who taps the logo."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if len(v) > LINK_MAX:
        raise ValueError(f"Keep the link under {LINK_MAX} characters")
    if any(c.isspace() for c in v):
        raise ValueError("A link has no spaces")
    try:
        parts = urlsplit(v)
    except ValueError:
        raise ValueError("Use a link that starts with https://")
    if parts.scheme.lower() != "https" or not parts.hostname or "." not in parts.hostname:
        raise ValueError("Use a link that starts with https://")
    return v


class EventPartnerIn(BaseModel):
    kind: Kind
    name: str = Field(..., max_length=NAME_MAX + 50)
    label: Optional[str] = Field(None, max_length=LABEL_MAX + 50)
    link: Optional[str] = Field(None, max_length=LINK_MAX + 50)

    _name = field_validator("name")(clean_name)
    _label = field_validator("label")(clean_label)
    _link = field_validator("link")(https_link)


class EventPartnerPatch(BaseModel):
    kind: Optional[Kind] = None
    name: Optional[str] = Field(None, max_length=NAME_MAX + 50)
    label: Optional[str] = Field(None, max_length=LABEL_MAX + 50)
    link: Optional[str] = Field(None, max_length=LINK_MAX + 50)

    _name = field_validator("name")(clean_name)
    _label = field_validator("label")(clean_label)
    _link = field_validator("link")(https_link)


class EventPartnerOrder(BaseModel):
    kind: Kind
    # Every live row of the kind, in the page's order.
    ids: list[int] = Field(..., max_length=MAX_PER_EVENT)


class FromClubIn(BaseModel):
    kind: Kind
    club_partner_ids: list[int] = Field(..., min_length=1, max_length=MAX_PER_EVENT)


class EventPartnerOut(BaseModel):
    """A row as the club's dashboard (and /events/info) reads it."""

    id: int
    kind: Kind
    name: str
    label: Optional[str] = None
    link: Optional[str] = None
    position: int = 0
    # The picture's sizes by name: thumbnail, medium, large, original.
    logo: Optional[dict] = None
    club_partner_id: Optional[int] = None


class FromClubOut(BaseModel):
    added: list[EventPartnerOut]
    # Club partners already on the event, not added again.
    skipped: list[int]


class PagePartner(BaseModel):
    """A row as the public event page reads it (GET /discover/events/{ref})."""

    id: int
    kind: Kind
    name: str
    label: Optional[str] = None
    link: Optional[str] = None
    # One URL: the medium size, else the large, else the original.
    logo: Optional[str] = None
