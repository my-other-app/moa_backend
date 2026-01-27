from datetime import datetime
from typing import List
from fastapi import File, Form, UploadFile
from pydantic import BaseModel, EmailStr, Field, HttpUrl
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
        self.name = name
        self.logo = logo
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


class ClubPublicDetail(ClubPublic):
    socials: ClubSocials = Field(...)
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
    user_data: dict | None = Field(None)
    org: ClubOrgDetail | None = Field(None)


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
