from fastapi import Body, File, Form, UploadFile
from pydantic import BaseModel, Field, EmailStr, HttpUrl
from app.api.users.models import UserAvatarTypes
from app.api.orgs.schema import OrganizationPublicMin


class AvatarPublic(BaseModel):
    id: int
    icon_type: UserAvatarTypes
    content: str


class UserProfileBase(BaseModel):
    whatsapp: str | None = Field(None)


# class UserProfileCreate(BaseModel):
#     whatsapp: str | None = Form(None)
#     org_id: int | None = Form(None)
#     avatar_id: int | None = Form(None)
#     profile_pic: UploadFile | None = File(None)


class UserProfilePublic(BaseModel):
    org: OrganizationPublicMin | None
    avatar: AvatarPublic | None
    profile_pic: dict | None


class UserProfilePrivate(BaseModel):
    whatsapp: str | None
    org: OrganizationPublicMin | None
    avatar: AvatarPublic | None
    profile_pic: dict | None = Field(None)


class UserBase(BaseModel):
    full_name: str = Field(...)
    email: EmailStr = Field(...)
    phone: str | None = Field(None)
    username: str = Field(...)


class UserCreate(BaseModel):
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


class UserAvatarSelect(BaseModel):
    avatar_id: int | None = Body(None)


class UserInterestSelect(BaseModel):
    interest_ids: list[int] = Body(...)
