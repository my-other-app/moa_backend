from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.api.cohosts.models import MAX_PER_EVENT, NOTE_MAX

Status = Literal["invited", "accepted", "declined", "left", "removed", "cancelled", "expired"]


class CohostRef(BaseModel):
    """A co-host as an event payload lists it: the accepted ones only."""

    club_id: int
    name: str
    slug: str
    #: As the club logo beside it in the same payload: the image's sizes
    #: under /events and /clubs, one URL under /discover.
    logo: Optional[dict] = None


class CohostRefUrl(BaseModel):
    club_id: int
    name: str
    slug: str
    logo: Optional[str] = None


class InviteIn(BaseModel):
    club_id: int = Field(..., gt=0)
    note: Optional[str] = Field(None, max_length=NOTE_MAX)

    @field_validator("note")
    @classmethod
    def _trim(cls, v):
        return (v or "").strip() or None


class InviteOut(BaseModel):
    id: int
    event_id: int
    club: Optional[CohostRef] = None
    status: Status
    note: Optional[str] = None
    invited_at: datetime
    expires_at: datetime
    responded_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    invited_by: Optional[str] = None


class Listing(BaseModel):
    event_id: int
    host: Optional[CohostRef] = None
    viewer_role: Literal["host", "cohost", "admin"]
    can_manage: bool
    limit: int = MAX_PER_EVENT
    slots_left: int
    items: list[InviteOut]


class ClubPick(CohostRef):
    state: Literal["invitable", "invited", "cohosting", "host"]


class ClubPicks(BaseModel):
    items: list[ClubPick]


class InvitationEvent(BaseModel):
    id: int
    name: str
    slug: str
    poster: Optional[dict] = None
    event_datetime: datetime
    ends_at: datetime
    location_name: Optional[str] = None
    is_online: bool = False
    status: str
    cohosts: list[CohostRef] = []


class Invitation(BaseModel):
    id: int
    status: Status
    note: Optional[str] = None
    invited_at: datetime
    expires_at: datetime
    responded_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    invited_by: Optional[str] = None
    host: Optional[CohostRef] = None
    event: InvitationEvent


class Invitations(BaseModel):
    #: Answerable invitations, for the home card and the nav badge.
    pending: int
    items: list[Invitation]
