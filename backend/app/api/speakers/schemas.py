"""What admins and claimed speakers send to change a profile."""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.api.speakers.names import LINK_KINDS

LINK_MAX = 500
TOPICS_MAX = 10


def _blank(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def check_link(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("A link must be a web address.")
    v = v.strip()
    if not v:
        return None
    parts = urlsplit(v)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or any(c.isspace() for c in v):
        raise ValueError("A link must be a web address starting with http:// or https://.")
    if len(v) > LINK_MAX:
        raise ValueError(f"A link can be at most {LINK_MAX} characters.")
    return v


class ProfilePatch(BaseModel):
    """Fields left out stay as they are; an empty one clears it (the name cannot be cleared)."""

    name: Optional[str] = Field(None, max_length=100)
    headline: Optional[str] = Field(None, max_length=200)
    bio: Optional[str] = Field(None, max_length=600)
    #: {website, linkedin, x, github, other}: replaces the links; a kind left out or empty is removed.
    links: Optional[dict[str, Optional[str]]] = None
    city: Optional[str] = Field(None, max_length=100)
    #: Topic ids from the topic tree (interest_categories), at most ten.
    topics: Optional[list[int]] = Field(None, max_length=TOPICS_MAX)

    @field_validator("name", "headline", "bio", "city", mode="before")
    @classmethod
    def _strip(cls, v):
        return _blank(v)

    @field_validator("links")
    @classmethod
    def _links(cls, v):
        if v is None:
            return None
        out = {}
        for kind, url in v.items():
            if kind not in LINK_KINDS:
                raise ValueError(f"Links are {', '.join(LINK_KINDS)}.")
            url = check_link(url)
            if url:
                out[kind] = url
        return out


class InviteIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        import re

        v = v.strip().lower()
        if not re.match(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$", v):
            raise ValueError("Enter an email address like name@example.com.")
        return v


class MergeIn(BaseModel):
    keep_id: int = Field(..., ge=1)
    merge_id: int = Field(..., ge=1)


def mask_email(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    local, _, domain = value.partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"
