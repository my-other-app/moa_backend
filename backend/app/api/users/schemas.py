from datetime import datetime
from fastapi import Body, File, Form, UploadFile
from pydantic import BaseModel, Field, EmailStr, HttpUrl

from app.api.users.models import UserAvatarTypes
from app.api.orgs.schema import OrganizationPublicMin
from app.api.auth.schemas import Token
from app.core.response.base_model import CustomBaseModel


class AvatarPublic(CustomBaseModel):
    id: int
    name: str
    image: dict | None = None


class UserProfileBase(CustomBaseModel):
    whatsapp: str | None = Field(None)


# class UserProfileCreate(CustomBaseModel):
#     whatsapp: str | None = Form(None)
#     org_id: int | None = Form(None)
#     avatar_id: int | None = Form(None)
#     profile_pic: UploadFile | None = File(None)


class UserProfilePublic(CustomBaseModel):
    org: OrganizationPublicMin | None = None
    avatar: AvatarPublic | None = None
    profile_pic: dict | None = None


class UserProfilePrivate(CustomBaseModel):
    whatsapp: str | None
    org: OrganizationPublicMin | None
    avatar: AvatarPublic | None
    profile_pic: dict | None = Field(None)


class UserBase(CustomBaseModel):
    full_name: str = Field(...)
    email: EmailStr = Field(...)
    phone: str | None = Field(None)
    username: str = Field(...)


class UserCreate(CustomBaseModel):
    full_name: str = Field(...)
    email: EmailStr = Field(...)
    phone: str = Field(None)
    password: str = Field(...)


class UserPublic(UserBase):
    id: int = Field(...)
    user_type: str = Field(...)
    profile: UserProfilePublic | None = Field(None)


class UserPrivate(UserBase):
    id: int = Field(...)
    user_type: str = Field(...)
    profile: UserProfilePrivate | None = Field(None)


class UserInDB(UserCreate):
    id: int = Field(...)
    password: str = Field(..., min_length=8)


class UserAvatarSelect(CustomBaseModel):
    avatar_id: int | None = Body(None)


class UserInterestSelect(CustomBaseModel):
    interest_ids: list[int] = Body(...)


# RESPONSE MODELS


class UserOrganizationDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    type: str = Field(...)
    is_verified: bool = Field(...)
    logo: dict | None = Field(None)


class UserAvatarDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    image: dict | None = Field(None)


class UserInterestDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class UserRegisterResponse(Token):
    username: str


class UserCreateResponse(CustomBaseModel):
    full_name: str = Field(...)
    whatsapp: str | None = Field(None)
    org_id: int | None = Field(None)
    avatar_id: int | None = Field(None)
    profile_pic: dict | None = Field(None)


class UserProfileDetailResponse(CustomBaseModel):
    full_name: str = Field(...)
    whatsapp: str | None = Field(None)
    org: UserOrganizationDetail | None = Field(None)
    avatar: UserAvatarDetail | None = Field(None)
    profile_pic: dict | None = Field(None)


class UserDetailResponse(CustomBaseModel):
    id: int = Field(...)
    full_name: str = Field(...)
    email: str = Field(...)
    phone: str | None = Field(None)
    username: str = Field(...)
    user_type: str = Field(...)
    profile: UserProfileDetailResponse | None = Field(None)
    interests: list[UserInterestDetail] | None = Field(None)


# Response Model


class UserEventCategoryDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class UserEventClubDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    logo: dict | None = Field(None)


class UserEventList(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    poster: dict | None = Field(None)
    event_datetime: datetime = Field(...)
    duration: float = Field(...)
    location_name: str | None = Field(None)
    has_fee: bool = Field(...)
    has_prize: bool = Field(True)
    prize_amount: float | None = Field(None)
    is_online: bool = Field(False)
    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    club: UserEventClubDetail = Field(...)
    category: UserEventCategoryDetail = Field(...)


class UserRegisteredEvents(CustomBaseModel):
    full_name: str = Field(...)
    email: str = Field(...)
    phone: str | None = Field(None)
    ticket_id: str = Field(...)
    is_paid: bool = Field(...)
    actual_amount: float | None = Field(None)
    paid_amount: float | None = Field(None)
    event: UserEventList = Field(...)


class FCMTokenRequest(CustomBaseModel):
    """Request schema for registering FCM token"""
    fcm_token: str = Field(..., description="Firebase Cloud Messaging token")
    platform: str = Field(..., description="Device platform (ios/android)")


class FCMTokenResponse(CustomBaseModel):
    """Response schema for FCM token registration"""
    success: bool = Field(...)
    message: str = Field(...)

