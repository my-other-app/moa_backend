from pydantic import BaseModel, Field, EmailStr, HttpUrl
from app.api.users.models import UserAvatarTypes


class AvatarPublic(BaseModel):
    id: int
    icon_type: UserAvatarTypes
    content: str


class UserPublic(BaseModel):
    username: str | None = Field(None, max_length=100, min_length=3)
    full_name: str = Field(..., max_length=100, min_length=3)
    profile_pic: HttpUrl | None = Field(None)
    avatar: AvatarPublic | None = Field(None)


class User(UserPublic):
    email: EmailStr | None = Field(default=None)
    phone: str | None = Field(default=None)
    whatsapp: str | None = Field(default=None)
    org_id: int | None = Field(default=None)


class UserCreate(BaseModel):
    full_name: str = Field(..., max_length=100, min_length=3)
    email: EmailStr | None = Field(default=None)
    phone: str | None = Field(default=None)
    whatsapp: str | None = Field(default=None)
    org_id: int | None = Field(default=None)
    password: str = Field(..., min_length=8)


class UserInDB(UserCreate):
    id: int = Field(...)
    password: str = Field(..., min_length=8)
