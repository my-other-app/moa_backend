"""The shapes of feature-register/profiles-networking-contract.md, by name."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.profiles import rules
from app.api.profiles.models import (
    LEVELS, LINK_TYPES, MAX_LINKS, MAX_NETWORK_TAGS, MAX_PROJECT_TAGS, MAX_TAG_CHARS, METHODS, SECTIONS, SHARE_LEVELS,
)
from app.core.response.base_model import CustomBaseModel

Level = Literal["everyone", "met", "mutual", "me"]
ShareLevel = Literal["nobody", "mutual", "met"]
LinkType = Literal["linkedin", "github", "behance", "dribbble", "medium", "youtube", "x", "instagram", "website", "portfolio", "other"]
Method = Literal["qr", "discovery", "link", "search"]

assert set(LEVELS) == set(Level.__args__) and set(SHARE_LEVELS) == set(ShareLevel.__args__)  # noqa: S101
assert set(LINK_TYPES) == set(LinkType.__args__) and set(METHODS) == set(Method.__args__)  # noqa: S101


# ─── Out ────────────────────────────────────────────────────────────────


class PersonBrief(CustomBaseModel):
    user_id: int
    handle: str
    full_name: str
    #: A link to their picture (the photo they uploaded, else the avatar they picked), or null.
    picture: Optional[str] = None
    headline: Optional[str] = None
    org_name: Optional[str] = None


class OrgOut(CustomBaseModel):
    id: int
    name: str


class InterestOut(CustomBaseModel):
    id: int
    name: str
    emoji: Optional[str] = None


class LinkOut(CustomBaseModel):
    type: LinkType
    url: str
    label: Optional[str] = None


class ProjectOut(CustomBaseModel):
    id: int
    title: str
    summary: Optional[str] = None
    url: Optional[str] = None
    #: A link to the project's picture (its medium size), or null.
    image: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    role: Optional[str] = None
    #: The first of the month, YYYY-MM-DD, or null.
    month: Optional[date] = None


class CertificateOut(CustomBaseModel):
    event_name: str
    url: str


class TalkOut(CustomBaseModel):
    event_name: str
    title: Optional[str] = None
    event_slug: str


class ClubOut(CustomBaseModel):
    name: str
    slug: str


class Achievements(CustomBaseModel):
    events_attended: Optional[int] = None
    certificates: Optional[list[CertificateOut]] = None
    talks: Optional[list[TalkOut]] = None
    clubs: Optional[list[ClubOut]] = None


class Contact(CustomBaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None


class RelationOut(CustomBaseModel):
    saved_by_me: bool
    saved_me: bool
    mutual: bool
    network_id: Optional[int] = None


class PublicProfile(CustomBaseModel):
    handle: str
    full_name: str
    picture: Optional[str] = None
    headline: Optional[str] = None
    bio: Optional[str] = None
    city: Optional[str] = None
    org: Optional[OrgOut] = None
    interests: list[InterestOut] = Field(default_factory=list)
    links: list[LinkOut] = Field(default_factory=list)
    projects: list[ProjectOut] = Field(default_factory=list)
    achievements: Achievements = Field(default_factory=Achievements)
    contact: Contact = Field(default_factory=Contact)
    #: The sections this viewer can't see (all of them on a private profile seen by a stranger).
    hidden_sections: list[str] = Field(default_factory=list)
    private: bool = False
    indexable: bool = False
    is_me: bool = False
    #: Null when signed out, or for one's own profile.
    relation: Optional[RelationOut] = None


class Visibility(BaseModel):
    links: Level = "everyone"
    projects: Level = "everyone"
    achievements: Level = "everyone"
    interests: Level = "everyone"
    org: Level = "everyone"


class ProfileSettings(CustomBaseModel):
    visibility: Visibility
    share_email: ShareLevel
    share_phone: ShareLevel
    is_private: bool
    indexable: bool
    notify_saves: bool
    recap_emails: bool
    scanning_enabled: bool
    show_events: bool
    show_certificates: bool
    show_talks: bool
    show_clubs: bool


class Completeness(CustomBaseModel):
    score: int
    missing: list[Literal["headline", "links", "projects", "photo"]]


class MyProfile(PublicProfile):
    settings: ProfileSettings
    completeness: Completeness
    email: Optional[str] = None
    phone: Optional[str] = None


class QrOut(CustomBaseModel):
    code: str
    url: str
    created_at: datetime


class ByCodeOut(CustomBaseModel):
    handle: str
    profile: PublicProfile


class EventBrief(CustomBaseModel):
    id: int
    slug: str
    name: str
    starts_at: Optional[datetime] = None


class EncounterOut(CustomBaseModel):
    event: Optional[EventBrief] = None
    method: Method
    met_at: datetime


class NetworkPerson(CustomBaseModel):
    id: int
    person: PersonBrief
    mutual: bool
    saved_me: bool
    note: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    first_met_at: Optional[datetime] = None
    last_met_at: Optional[datetime] = None
    #: In a list: the newest three. In GET /network/{id}: all of them.
    encounters: list[EncounterOut] = Field(default_factory=list)


class SaveOut(CustomBaseModel):
    person: NetworkPerson
    created: bool
    new_encounter: bool
    profile: PublicProfile


class EventCount(CustomBaseModel):
    event: EventBrief
    count: int


class TagCount(CustomBaseModel):
    tag: str
    count: int


class NetworkPage(CustomBaseModel):
    items: list[NetworkPerson]
    total: int
    events: list[EventCount]
    tags: list[TagCount]


class SavedMeItem(CustomBaseModel):
    person: PersonBrief
    saved_at: datetime
    event: Optional[EventBrief] = None


class SavedMePage(CustomBaseModel):
    items: list[SavedMeItem]
    total: int


class HourSaves(CustomBaseModel):
    #: The hour's start in India time, e.g. 2026-11-28T10:00:00+05:30.
    hour: str
    saves: int


class NetworkingStats(CustomBaseModel):
    checked_in: int
    people_networked: int
    saves: int
    mutual_pairs: int
    by_hour: list[HourSaves]


class NetworkingSummary(CustomBaseModel):
    profiles_completed: int
    profiles_total: int
    codes_active: int
    saves_7d: int
    saves_30d: int
    #: Of all saves, the share whose pair is mutual, 0 to 1.
    mutual_rate: float
    reports_open: int


# ─── In ─────────────────────────────────────────────────────────────────


class ProfilePatch(BaseModel):
    """Only what is sent changes. `headline`, `bio`, `city` and `org_id` may
    be null (cleared); the switches may not."""

    handle: Optional[str] = None
    headline: Optional[str] = Field(None, max_length=120)
    bio: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    org_id: Optional[int] = None
    #: Only the sections sent change.
    visibility: Optional[dict[Literal["links", "projects", "achievements", "interests", "org"], Level]] = None
    share_email: Optional[ShareLevel] = None
    share_phone: Optional[ShareLevel] = None
    is_private: Optional[bool] = None
    indexable: Optional[bool] = None
    notify_saves: Optional[bool] = None
    recap_emails: Optional[bool] = None
    scanning_enabled: Optional[bool] = None
    show_events: Optional[bool] = None
    show_certificates: Optional[bool] = None
    show_talks: Optional[bool] = None
    show_clubs: Optional[bool] = None

    @model_validator(mode="after")
    def _no_null_switches(self):
        nullable = {"headline", "bio", "city", "org_id"}
        for name in self.model_fields_set - nullable:
            if getattr(self, name) is None:
                raise ValueError(f"{name} can't be null")
        return self


class LinkIn(BaseModel):
    type: LinkType
    #: Checked by the service (422 LINK_INVALID), not here, so the code is the contract's.
    url: str = Field(max_length=2000)
    label: Optional[str] = Field(None, max_length=60)


class LinksIn(BaseModel):
    links: list[LinkIn] = Field(max_length=MAX_LINKS)


def _project_tags(v):
    return rules.tags(v, most=MAX_PROJECT_TAGS, chars=MAX_TAG_CHARS)


def _month(v):
    """YYYY-MM or a date: the first of that month."""
    if v is None or isinstance(v, date):
        return v.replace(day=1) if isinstance(v, date) else None
    text = str(v).strip()
    if len(text) == 7:
        text += "-01"
    try:
        return date.fromisoformat(text[:10]).replace(day=1)
    except ValueError:
        raise ValueError("a month is YYYY-MM")


class ProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    summary: Optional[str] = Field(None, max_length=160)
    url: Optional[str] = Field(None, max_length=2000)
    tags: list[str] = Field(default_factory=list)
    role: Optional[str] = Field(None, max_length=60)
    month: Optional[date] = None

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        return _project_tags(v)

    @field_validator("month", mode="before")
    @classmethod
    def _first_of_month(cls, v):
        return _month(v)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        v = rules.one_line(v, 80)
        if not v:
            raise ValueError("a project needs a title")
        return v


class ProjectPatch(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=80)
    summary: Optional[str] = Field(None, max_length=160)
    url: Optional[str] = Field(None, max_length=2000)
    tags: Optional[list[str]] = None
    role: Optional[str] = Field(None, max_length=60)
    month: Optional[date] = None

    @field_validator("month", mode="before")
    @classmethod
    def _first_of_month(cls, v):
        return _month(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        return None if v is None else _project_tags(v)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        if v is None:
            raise ValueError("a project needs a title")
        v = rules.one_line(v, 80)
        if not v:
            raise ValueError("a project needs a title")
        return v


class ProjectOrder(BaseModel):
    ids: list[int] = Field(max_length=100)


class SaveIn(BaseModel):
    code: Optional[str] = Field(None, max_length=64)
    handle: Optional[str] = Field(None, max_length=100)
    method: Method
    event_id: Optional[int] = None
    #: When an offline scan happened (ISO 8601); up to 72 hours ago is kept, otherwise now.
    met_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _one_target(self):
        if bool(self.code) == bool(self.handle):
            raise ValueError("send a code or a handle")
        return self


class NetworkPatch(BaseModel):
    note: Optional[str] = Field(None, max_length=500)
    tags: Optional[list[str]] = None

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        return None if v is None else rules.tags(v, most=MAX_NETWORK_TAGS, chars=MAX_TAG_CHARS)


SECTIONS_ALL = list(SECTIONS)
