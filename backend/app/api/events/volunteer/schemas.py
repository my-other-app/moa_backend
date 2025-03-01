from typing import List

from pydantic import BaseModel


class VolunteerCreate(BaseModel):
    email_ids: List[str]
    event_id: int


class ListVolunteersResponse(BaseModel):
    email: str
    is_approved: bool
    user_id: int | None = None
    profile_pic: str | None = None
    full_name: str | None = None
