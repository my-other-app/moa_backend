from datetime import datetime
from typing import List

from pydantic import BaseModel, EmailStr


class VolunteerCreateRemove(BaseModel):
    email_id: EmailStr
    event_id: int | None = None
    club_id: int | None = None


class ListVolunteersResponse(BaseModel):
    email: str
    is_approved: bool
    user_id: int | None = None
    profile_pic: str | None = None
    full_name: str | None = None


class CheckinRequest(BaseModel):
    ticket_id: str


class MyEventsClubDetails(BaseModel):
    id: int
    name: str
    slug: str
    logo: dict | None = None


class MyEventEventDetails(BaseModel):
    id: int
    slug: str
    name: str
    poster: dict | None = None
    event_datetime: datetime
    has_fee: bool
    reg_fee: float | None = None
    max_participants: int | None = None
    club: MyEventsClubDetails


class MyEventsResponse(BaseModel):
    email: str
    is_approved: bool
    event: MyEventEventDetails | None = None
    club: MyEventsClubDetails | None = None
