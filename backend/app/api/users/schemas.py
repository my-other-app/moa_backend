from fastapi import Body, File, Form, UploadFile
from pydantic import BaseModel, Field, EmailStr, HttpUrl

from app.api.users.models import UserAvatarTypes
from app.api.orgs.schema import OrganizationPublicMin
from app.api.auth.schemas import Token
from app.core.response.base_model import CustomBaseModel


class AvatarPublic(CustomBaseModel):
    id: int
    icon_type: UserAvatarTypes
    content: str


class UserProfileBase(CustomBaseModel):
    whatsapp: str | None = Field(None)


# class UserProfileCreate(CustomBaseModel):
#     whatsapp: str | None = Form(None)
#     org_id: int | None = Form(None)
#     avatar_id: int | None = Form(None)
#     profile_pic: UploadFile | None = File(None)


class UserProfilePublic(CustomBaseModel):
    org: OrganizationPublicMin | None
    avatar: AvatarPublic | None
    profile_pic: dict | None


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
