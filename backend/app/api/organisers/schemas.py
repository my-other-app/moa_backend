from typing import Optional

from pydantic import BaseModel, Field, field_validator

MAX_ORGANISERS = 24


class OrganiserIn(BaseModel):
    member_id: int
    role: Optional[str] = Field(default=None, max_length=80)

    @field_validator("role", mode="before")
    @classmethod
    def _blank(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class OrganisersIn(BaseModel):
    # In the order the page lists them.
    members: list[OrganiserIn] = Field(max_length=MAX_ORGANISERS)


class OrganiserOut(BaseModel):
    member_id: int
    name: str
    # Their role in the club, and their part in this event.
    club_role: Optional[str] = None
    role: Optional[str] = None
    photo: Optional[str] = None
