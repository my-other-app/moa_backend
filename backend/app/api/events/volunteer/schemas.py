from typing import List

from pydantic import BaseModel, EmailStr


class VolunteerCreateRemove(BaseModel):
    email_id: EmailStr
    event_id: int


class ListVolunteersResponse(BaseModel):
    email: str
    is_approved: bool
    user_id: int | None = None
    profile_pic: str | None = None
    full_name: str | None = None
