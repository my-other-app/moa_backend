from datetime import datetime
from pydantic import BaseModel, EmailStr, Field
from app.api.users.schemas import UserPublic


class ClubBaseMin(BaseModel):
    name: str = Field(..., min_length=3, max_length=20)
    logo: str | None = Field(None)
    location_name: str | None = Field(None)


class ClubBase(ClubBaseMin):
    about: str | None = Field(None)


class CreateClub(BaseModel):
    email: EmailStr = Field(..., max_length=100)
    phone: str | None = Field(None, max_length=15)
    password: str = Field(..., min_length=6, max_length=100)
    name: str = Field(..., min_length=3, max_length=20)
    logo: str | None = Field(None)
    about: str | None = Field(None)
    org_id: int | None = Field(None)
    location_name: str | None = Field(None)

    # @field_validator("org_id", mode="before")
    # @classmethod
    # async def validate_org_id(cls, value: int | None, info: ValidationInfo):
    #     if not value:
    #         return value
    #     session: AsyncSession = info.context.get("session")
    #     result = await session.execute(
    #         select(Organizations.id).where(Organizations.id == value)
    #     )
    #     if not result.scalar():
    #         raise ValueError(f"Organization with id {value} does not exist")
    # return value


class EditClub(CreateClub):
    name: str | None = Field(..., min_length=3, max_length=20)
    id: int = Field(...)


class ClubPublic(ClubBase):
    id: int = Field(...)
    rating: int = Field(...)
    total_ratings: int = Field(...)

    class Config:
        from_attributes = True


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


class NotePublic(NotesBase):
    id: int = Field(...)


class ClubFollow(BaseModel):
    club_id: int
    user_id: int
    is_following: bool


class ClubFollowPublic(ClubFollow):
    created_at: datetime
