from datetime import datetime
from fastapi import File, Form, UploadFile
from pydantic import BaseModel, EmailStr, Field
from app.api.users.schemas import UserPublic
from app.core.storage.fields import S3Image


class ClubBaseMin(BaseModel):
    name: str = Field(..., min_length=3, max_length=20)
    logo: dict | None = Field(None)
    location_name: str | None = Field(None)


class ClubBase(ClubBaseMin):
    about: str | None = Field(None)


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
        interest_ids: str | None = Form(None),
    ):
        self.email = email
        self.phone = phone
        self.password = password
        self.name = name
        self.logo = logo
        self.about = about
        self.org_id = org_id
        self.location_name = location_name
        self.interest_ids = (
            [int(i) for i in interest_ids.split(",")] if interest_ids else []
        )
        self.location_link = location_link

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
    id: int = Form(...)

    def __init__(
        self,
        id: int = Form(...),
        email=Form(..., max_length=100),
        phone=Form(None, max_length=15),
        password=Form(..., min_length=6, max_length=100),
        name=Form(..., min_length=3, max_length=20),
        logo=Form(None),
        about=Form(None),
        org_id=Form(None),
        location_name=Form(None),
    ):
        super().__init__(
            email, phone, password, name, logo, about, org_id, location_name
        )
        self.id = id


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
