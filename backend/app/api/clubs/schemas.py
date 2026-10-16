from datetime import datetime
from typing import List, Optional
from fastapi import File, Form, UploadFile
from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator
from app.api.users.schemas import UserPublic
from app.core.storage.fields import S3Image
from app.api.interests.schemas import InterestPublic
from app.core.response.base_model import CustomBaseModel


class ClubBaseMin(CustomBaseModel):
    name: str = Field(..., min_length=3, max_length=20)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)


class ClubBase(ClubBaseMin):
    about: str | None = Field(None)


class ClubSocials(CustomBaseModel):
    instagram: HttpUrl | None = None
    linkedin: HttpUrl | None = None
    youtube: HttpUrl | None = None
    website: HttpUrl | None = None


class ClubSocialsCreate(CustomBaseModel):
    instagram: str | None = None
    linkedin: str | None = None
    youtube: str | None = None
    website: str | None = None


class CreateClub:
    def __init__(
        self,
        email: EmailStr = Form(..., max_length=100),
        phone: str | None = Form(None, max_length=15),
        password: str = Form(..., min_length=6, max_length=100),
        name: str = Form(..., min_length=3, max_length=20),
        logo: UploadFile | None = File(None),
        about: str | None = Form(None),
        org_id: int | None = Form(None),
        location_name: str | None = Form(None),
        location_link: str | None = Form(None),
        contact_phone: str | None = Form(None),
        contact_email: str | None = Form(None),
        interest_ids: str | None = Form(None),
        instagram: str | None = Form(None),
        linkedin: str | None = Form(None),
        youtube: str | None = Form(None),
        website: str | None = Form(None),
    ):
        self.email = email
        self.phone = phone
        self.password = password
        self.name = name
        self.logo = logo
        self.about = about
        self.org_id = org_id
        self.location_name = location_name
        self.location_link = location_link
        self.interest_ids = (
            [int(i) for i in interest_ids.split(",")] if interest_ids else []
        )
        self.instagram = instagram
        self.linkedin = linkedin
        self.youtube = youtube
        self.website = website
        self.contact_phone = contact_phone
        self.contact_email = contact_email


class UpdateClub:
    def __init__(
        self,
        # Ignored on the club's own form: the name, like the handle, changes only through support.
        name: str | None = Form(None, min_length=3, max_length=20),
        logo: UploadFile | None = File(None),
        about: str | None = Form(None),
        org_id: int | None = Form(None),
        location_name: str | None = Form(None),
        location_link: str | None = Form(None),
        contact_phone: str | None = Form(None),
        contact_email: str | None = Form(None),
        interest_ids: str | None = Form(None),
        instagram: str | None = Form(None),
        linkedin: str | None = Form(None),
        youtube: str | None = Form(None),
        website: str | None = Form(None),
        brand_primary: str | None = Form(None),
        brand_secondary: str | None = Form(None),
        tagline: str | None = Form(None, max_length=120),
    ):
        self.name = name
        self.logo = logo
        self.tagline = tagline
        self.brand_primary = brand_primary
        self.brand_secondary = brand_secondary
        self.about = about
        self.org_id = org_id
        self.location_name = location_name
        self.location_link = location_link
        self.interest_ids = (
            [int(i) for i in interest_ids.split(",")] if interest_ids else []
        )
        self.contact_phone = contact_phone
        self.contact_email = contact_email
        self.instagram = instagram
        self.linkedin = linkedin
        self.youtube = youtube
        self.website = website


class CreateClubAdmin(CustomBaseModel):
    email: EmailStr = Field(...)
    name: str = Field(...)


class ClubPublic(ClubBase):
    id: int = Field(...)
    rating: float = Field(...)
    total_ratings: int = Field(...)

    class Config:
        from_attributes = True


class ClubPartnerOut(CustomBaseModel):
    id: int
    name: str
    role: str | None = None
    since: int | None = None
    sort_order: int = 0
    logo: dict | None = None


def _named(v):
    if v is None:
        return None
    v = v.strip()
    if not v:
        raise ValueError("Name the partner")
    return v


class ClubPartnerCreate(CustomBaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    role: str | None = Field(None, max_length=80)
    since: int | None = Field(None, ge=1900, le=2100)

    _name = field_validator("name")(_named)


class ClubPartnerPatch(CustomBaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    role: str | None = Field(None, max_length=80)
    since: int | None = Field(None, ge=1900, le=2100)
    sort_order: int | None = Field(None, ge=0, le=10000)

    _name = field_validator("name")(_named)


SOCIALS = ("instagram", "linkedin", "x", "website")


def _link(v):
    """A handle or a link, as typed, trimmed; the pages make the URL."""
    if v is None:
        return None
    v = v.strip()
    return v or None


class ClubTeamMemberOut(CustomBaseModel):
    id: int
    name: str
    role: str | None = None
    instagram: str | None = None
    linkedin: str | None = None
    x: str | None = None
    website: str | None = None
    sort_order: int = 0
    photo: dict | None = None


class ClubTeamMemberCreate(CustomBaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    role: str | None = Field(None, max_length=80)
    instagram: str | None = Field(None, max_length=255)
    linkedin: str | None = Field(None, max_length=255)
    x: str | None = Field(None, max_length=255)
    website: str | None = Field(None, max_length=255)

    _name = field_validator("name")(_named)
    _links = field_validator(*SOCIALS)(_link)


class ClubTeamMemberPatch(CustomBaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    role: str | None = Field(None, max_length=80)
    instagram: str | None = Field(None, max_length=255)
    linkedin: str | None = Field(None, max_length=255)
    x: str | None = Field(None, max_length=255)
    website: str | None = Field(None, max_length=255)
    sort_order: int | None = Field(None, ge=0, le=10000)

    _name = field_validator("name")(_named)
    _links = field_validator(*SOCIALS)(_link)


class ClubPublicDetail(ClubPublic):
    socials: ClubSocials = Field(...)
    tagline: str | None = Field(None)
    cover: dict | None = Field(None)
    partners: list[ClubPartnerOut] = Field(default_factory=list)
    team: list[ClubTeamMemberOut] = Field(default_factory=list)
    location_link: str | None = Field(None)
    interests: list[str] = Field(...)
    followers_count: int = Field(...)
    user_data: dict | None = Field(None)


class ClubPublicMin(ClubBaseMin):
    id: int = Field(...)
    rating: float = Field(...)
    total_ratings: int = Field(...)

    class Config:
        from_attributes = True


class NotesBase(CustomBaseModel):
    title: str = Field(...)
    note: str = Field(...)


class NoteCreate(NotesBase):
    pass


class NotesPrivate(NotesBase):
    id: int = Field(...)


class NotesPublic(NotesBase):
    id: int = Field(...)
    club: ClubPublicMin


class ClubFollow(CustomBaseModel):
    club_id: int
    user_id: int
    is_following: bool


class ClubFollowPublic(CustomBaseModel):
    user: UserPublic
    is_following: bool
    created_at: datetime


# RESPONSE MODELS


class ClubCreateUpdateResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    logo: dict | None = Field(None)
    about: str | None = Field(None)
    org_id: int | None = Field(None)
    location_name: str | None = Field(None)
    location_link: str | None = Field(None)
    contact_phone: str | None = Field(None)
    contact_email: str | None = Field(None)
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)


class ClubInterestDetailMin(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class ClubOrgDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    type: str = Field(..., min_length=2)
    address: str | None = Field(None)
    phone: str | None = Field(None)
    email: str | None = Field(None)


class ClubPublicDetailResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=3, max_length=20)
    slug: str = Field(...)
    logo: dict | None = Field(None)
    cover: dict | None = Field(None)
    tagline: str | None = Field(None)
    partners: list[ClubPartnerOut] = Field(default_factory=list)
    team: list[ClubTeamMemberOut] = Field(default_factory=list)
    location_name: str | None = Field(None)
    location_link: str | None = Field(None)
    about: str | None = Field(None)
    rating: float = Field(...)
    total_ratings: int = Field(...)
    total_events: int = Field(0)
    live_events: int = Field(0)
    past_events: int = Field(0)
    socials: ClubSocials | None = Field(None)
    interests: List[ClubInterestDetailMin] | None = Field(None)
    followers_count: int = Field(...)
    contact_phone: str | None = Field(None)
    contact_email: str | None = Field(None)
    brand_primary: str | None = Field(None)
    brand_secondary: str | None = Field(None)
    user_data: dict | None = Field(None)
    org: ClubOrgDetail | None = Field(None)


class ClubOwnerDetail(CustomBaseModel):
    id: int = Field(...)
    full_name: str | None = Field(None)
    username: str | None = Field(None)
    email: str | None = Field(None)


class ClubAdminStats(CustomBaseModel):
    """Totals across every event the club has run."""

    total_registrations: int = Field(0)
    total_attended: int = Field(0)
    total_revenue: float = Field(0)


class ClubAdminFullResponse(ClubPublicDetailResponse):
    """
    The public detail plus what only an admin should see: whether the club is
    verified at all, who owns the login, and the money across its events.
    """

    is_verified: bool = Field(False)
    is_deleted: bool = Field(False)
    created_at: datetime | None = Field(None)
    owner: ClubOwnerDetail | None = Field(None)
    admin_stats: ClubAdminStats = Field(...)


class ClubOrgMin(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)


class ClubAdminDetailResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=3, max_length=20)
    slug: str = Field(...)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)
    location_link: str | None = Field(None)
    about: str | None = Field(None)
    contact_phone: str | None = Field(None)
    contact_email: str | None = Field(None)
    initial_password: str | None = Field(None)
    created_at: datetime = Field(...)
    is_verified: bool = Field(...)
    interests: List[ClubInterestDetailMin] | None = Field(None)

    # The four the admin list shows per row. All default, because the same
    # schema also serialises approve/reject, which return one club built
    # without them -- a required field there would turn an approval into a
    # 500 after the write had already committed.
    followers_count: int = Field(0)
    events_count: int = Field(0)
    org: ClubOrgMin | None = Field(None)
    #: The club's sign-in address, not the contact address on its page.
    owner_email: str | None = Field(None)


class NoteCreateUpdateResponse(CustomBaseModel):
    id: int = Field(...)
    title: str = Field(...)
    note: str = Field(...)


class NoteListResponse(CustomBaseModel):
    id: int = Field(...)
    title: str = Field(...)
    note: str = Field(...)
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)


class UserClubLinkDetailResponse(CustomBaseModel):
    is_following: bool
    is_pinned: bool
    is_hidden: bool
    created_at: datetime


class ClubUserAvatarDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    image: dict | None = Field(None)


class ClubUserProfileDetail(CustomBaseModel):
    id: int = Field(...)
    avatar: ClubUserAvatarDetail | None = Field(None)
    profile_pic: dict | None = Field(None)


class ClubFollowerDetailResponse(CustomBaseModel):
    id: int = Field(...)
    full_name: str = Field(...)
    username: str = Field(...)
    profile: ClubUserProfileDetail | None = Field(None)


class ClubFollowersListResponse(CustomBaseModel):
    user: ClubFollowerDetailResponse
    is_following: bool
    created_at: datetime


class ClubListResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)
    user_data: dict | None = Field(None)
    followers_count: int = Field(...)
    is_following: bool = Field(...)
    interests: List[ClubInterestDetailMin] | None = Field(None)
    # org_id: int | None = Field(None)


class ClubAdminListResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)
    user_data: dict | None = Field(None)
    followers_count: int = Field(...)
    initialPassword: str | None = Field(None)
    interests: List[ClubInterestDetailMin] | None = Field(None)


class FollowerEventRow(CustomBaseModel):
    event_id: int
    name: str
    starts_at: str | None = None
    status: str
    attended: bool
    paid: bool
    paid_amount: float
    registered_at: str | None = None


class FollowerProfileResponse(CustomBaseModel):
    user: dict
    followed_at: str | None = None
    contact: dict
    events: list[FollowerEventRow]
    totals: dict


class ClubEventsFigures(BaseModel):
    """The season above the events list."""

    upcoming: int
    next_event: Optional[dict] = None
    registered: int
    capacity: Optional[int] = None
    collected: float
    outstanding: float
    attention: dict
    past: int
    drafts: int
    generated_at: datetime


class FollowerUser(BaseModel):
    id: int
    full_name: str
    username: Optional[str] = None
    avatar: Optional[dict] = None


class FollowerRow(BaseModel):
    """A follower as the followers page lists them."""

    user: FollowerUser
    org: Optional[str] = None
    interests: list[str]
    followed_at: Optional[datetime] = None
    source: Optional[str] = None
    registrations: int
    attended: int
    last_registered_at: Optional[datetime] = None
    upcoming: int
    segments: list[str]


class FollowersPage(BaseModel):
    items: list[FollowerRow]
    total: int
    limit: int
    offset: int
    counts: dict


class FollowersInsights(BaseModel):
    """The audience above the list."""

    total: int
    new: int
    new_delta: Optional[int] = None
    came: int
    regulars: int
    registered_ahead: int
    quiet: int
    next_event: Optional[dict] = None
    growth: list[dict]
    by_org: list[dict]
    interests: list[dict]
    sources: list[dict]
    segments: dict
    generated_at: datetime
