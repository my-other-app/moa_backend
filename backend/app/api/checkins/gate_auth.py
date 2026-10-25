"""Who is at the door.

Three ways in: the club that runs the event (everything), an approved
volunteer signed into their account (their switches), or a phone opened on
a door's gate link (that door's switches, with the door as the name on
every admission).
"""
import logging
from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from app.api.checkins import doors as doors_mod
from app.api.checkins.models import Doors
from app.api.events.access import is_admin_user
from app.api.events.models import Events
from app.api.events.volunteer.models import Volunteer
from app.api.users.models import Users, UserTypes
from app.core.auth.dependencies import check_user_type
from app.db.core import SessionDep
from app.response import CustomHTTPException

GATE_KEY_HEADER = "X-Gate-Key"

logger = logging.getLogger(__name__)

AnyUser = Annotated[Optional[Users], Depends(check_user_type([t.value for t in UserTypes], optional=True))]


@dataclass
class GateActor:
    kind: str  # "user" | "door"
    name: str
    user: Optional[Users] = None
    volunteer: Optional[Volunteer] = None
    door: Optional[Doors] = None
    can_check_in: bool = False
    can_walkup: bool = False
    can_merch: bool = False
    # Access from above volunteer: the club or an admin. Overrides the switches.
    full: bool = False

    @property
    def user_id(self) -> Optional[int]:
        return self.user.id if self.user is not None else None

    @property
    def permissions(self) -> dict:
        return {"check_in": self.can_check_in or self.full, "walkup": self.can_walkup or self.full, "merch": self.can_merch or self.full, "full": self.full}


async def load_event(session, event_id: int) -> Events:
    event = await session.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == event_id, Events.is_deleted == False))
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    return event


async def actor_for(session, event: Events, user: Optional[Users], gate_key: Optional[str], method: str = "GET") -> GateActor:
    if gate_key:
        door = await doors_mod.door_by_key(session, gate_key)
        if door is None or door.event_id != event.id:
            raise CustomHTTPException(403, message="This gate link is no longer valid. Ask the organiser for a new one.", error_code="GATE_LINK_INVALID")
        return GateActor(kind="door", name=door.name, door=door, can_check_in=True, can_walkup=door.link_can_walkup)
    if user is None:
        raise CustomHTTPException(401, message="Sign in, or open the gate link the organiser sent you.")
    if is_admin_user(user):
        # Any event's door, so the admin's role must allow what the request
        # does: looking is VIEW, admitting someone changes the club's records.
        from app.core.auth.permissions import capability_for_method, ensure_can

        ensure_can(user, capability_for_method(method))
        return GateActor(kind="user", name=user.full_name or user.email, user=user, full=True)
    if event.club is not None and event.club.user_id == user.id:
        return GateActor(kind="user", name=user.full_name or user.email, user=user, full=True)
    volunteer = await session.scalar(
        select(Volunteer).where(
            Volunteer.user_id == user.id,
            Volunteer.is_deleted == False,
            Volunteer.is_approved == True,
            or_(Volunteer.event_id == event.id, Volunteer.club_id == event.club_id),
        ).order_by(Volunteer.event_id.desc().nulls_last())
    )
    if volunteer is None:
        raise CustomHTTPException(403, message="You are not on this event's team.", error_code="NOT_A_VOLUNTEER")
    return GateActor(
        kind="user", name=user.full_name or user.email, user=user, volunteer=volunteer,
        can_check_in=volunteer.can_check_in, can_walkup=volunteer.can_walkup, can_merch=volunteer.can_merch,
    )


async def gate_actor(request: Request, session: SessionDep, event_id: int, user: AnyUser) -> GateActor:
    """The key from the X-Gate-Key header only: a key in the query string
    lands in every access log between the phone and here."""
    event = await load_event(session, event_id)
    actor = await actor_for(session, event, user, request.headers.get(GATE_KEY_HEADER), request.method)
    request.state.gate_event = event
    return actor


async def gate_actor_for_frame(request: Request, session: SessionDep, event_id: int, user: AnyUser) -> GateActor:
    """For a PDF the console loads into a frame, which cannot send a header:
    the header, else `?key=` -- deprecated, kept only while the deployed
    console prints cards that way (moa_web idcards cardPdfUrl). Fetching the
    PDF with the header and framing the blob ends it."""
    key = request.headers.get(GATE_KEY_HEADER)
    if not key and request.query_params.get("key"):
        key = request.query_params.get("key")
        logger.info("gate key read from the query string (deprecated) for %s", request.url.path)
    event = await load_event(session, event_id)
    actor = await actor_for(session, event, user, key, request.method)
    request.state.gate_event = event
    return actor


GateAuth = Annotated[GateActor, Depends(gate_actor)]
GateFrameAuth = Annotated[GateActor, Depends(gate_actor_for_frame)]
