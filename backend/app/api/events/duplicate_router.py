from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deliveries import service as deliveries
from app.api.events.duplicate import duplicate_event
from app.api.subscriptions import gates
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}")


class DuplicateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    event_datetime: datetime
    duration: Optional[float] = Field(default=None, gt=0, le=24 * 30)
    # The parts of the setup that are a judgement call, rather than always wanted.
    copy_doors: bool = False
    copy_team: bool = False
    copy_agenda: bool = False
    copy_merch: bool = True


class Duplicated(BaseModel):
    id: int
    slug: str
    name: str
    copied: dict


@router.post("/duplicate", summary="A new event from this one's setup", status_code=201)
async def duplicate(session: SessionDep, user: ClubAuth, event_id: int, body: DuplicateBody) -> Duplicated:
    """Settings, fields, designs, the email journey, the merchandise and the
    organisers come across; doors, team and agenda when asked; the venue, the
    guest list and every record of the day stay behind. The copy starts as a draft."""
    source = await deliveries.event_for_club(session, user, event_id)
    # A new event: this month's count, and paid tickets when the copy is paid.
    await gates.check_event_create(session, source.club_id, has_fee=bool(source.has_fee), actor=user)
    return Duplicated(**await duplicate_event(
        session, source, user, name=body.name, event_datetime=body.event_datetime, duration=body.duration,
        copy_doors=body.copy_doors, copy_team=body.copy_team, copy_agenda=body.copy_agenda, copy_merch=body.copy_merch,
    ))
