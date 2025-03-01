from datetime import datetime
from typing import List
from fastapi import File, Form, UploadFile
from pydantic import BaseModel, EmailStr, Field, HttpUrl
from app.api.users.schemas import UserPublic
from app.core.storage.fields import S3Image
from app.api.interests.schemas import InterestPublic


class ClubBaseMin(BaseModel):
    name: str = Field(..., min_length=3, max_length=20)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)


class ClubBase(ClubBaseMin):
    about: str | None = Field(None)


class ClubSocials(BaseModel):
    instagram: HttpUrl | None = None
    linkedin: HttpUrl | None = None
    youtube: HttpUrl | None = None
    website: HttpUrl | None = None


class ClubSocialsCreate(BaseModel):
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


class CreateClubAdmin(BaseModel):
    email: EmailStr = Field(...)
    name: str = Field(...)


class ClubPublic(ClubBase):
    id: int = Field(...)
    rating: int = Field(...)
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
    rating: int = Field(...)
    total_ratings: int = Field(...)

    class Config:
        from_attributes = True


class NotesBase(BaseModel):
    title: str = Field(...)
    note: str = Field(...)


class NoteCreate(NotesBase):
    pass


class NotesPrivate(NotesBase):
    id: int = Field(...)


class NotesPublic(NotesBase):
    id: int = Field(...)
    club: ClubPublicMin


class ClubFollow(BaseModel):
    club_id: int
    user_id: int
    is_following: bool


class ClubFollowPublic(BaseModel):
    user: UserPublic
    is_following: bool
    created_at: datetime


# RESPONSE MODELS


class ClubCreateUpdateResponse(BaseModel):
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


class ClubInterestDetailMin(BaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class ClubOrgDetail(BaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    type: str = Field(..., min_length=2)
    address: str | None = Field(None)
    phone: str | None = Field(None)
    email: str | None = Field(None)


class ClubPublicDetailResponse(BaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=3, max_length=20)
    slug: str = Field(...)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)
    location_link: str | None = Field(None)
    about: str | None = Field(None)
    rating: int = Field(...)
    total_ratings: int = Field(...)
    socials: ClubSocials | None = Field(None)
    interests: List[ClubInterestDetailMin] | None = Field(None)
    followers_count: int = Field(...)
    contact_phone: str | None = Field(None)
    contact_email: str | None = Field(None)
    user_data: dict | None = Field(None)
    org: ClubOrgDetail | None = Field(None)


class ClubAdminDetailResponse(BaseModel):
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


class NoteCreateUpdateResponse(BaseModel):
    id: int = Field(...)
    title: str = Field(...)
    note: str = Field(...)


class NoteListResponse(BaseModel):
    id: int = Field(...)
    title: str = Field(...)
    note: str = Field(...)
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)


class UserClubLinkDetailResponse(BaseModel):
    is_following: bool
    is_pinned: bool
    is_hidden: bool
    created_at: datetime


class ClubUserAvatarDetail(BaseModel):
    id: int = Field(...)
    name: str = Field(...)
    image: dict | None = Field(None)


class ClubUserProfileDetail(BaseModel):
    id: int = Field(...)
    avatar: ClubUserAvatarDetail | None = Field(None)
    profile_pic: dict | None = Field(None)


class ClubFollowerDetailResponse(BaseModel):
    id: int = Field(...)
    full_name: str = Field(...)
    username: str = Field(...)
    profile: ClubUserProfileDetail | None = Field(None)


class ClubFollowersListResponse(BaseModel):
    user: ClubFollowerDetailResponse
    is_following: bool
    created_at: datetime


class ClubListResponse(BaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)
    user_data: dict | None = Field(None)
    followers_count: int = Field(...)
    interests: List[ClubInterestDetailMin] | None = Field(None)
    # org_id: int | None = Field(None)


class ClubAdminListResponse(BaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)
    user_data: dict | None = Field(None)
    followers_count: int = Field(...)
    initialPassword: str | None = Field(None)
    interests: List[ClubInterestDetailMin] | None = Field(None)
