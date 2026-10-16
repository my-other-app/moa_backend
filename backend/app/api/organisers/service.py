"""Who from the club's team runs an event, in the order its page lists them."""
from typing import Optional

from sqlalchemy import delete, select

from app.api.clubs.models import ClubTeamMembers
from app.api.events.models import Events
from app.api.organisers.models import EventTeamMembers
from app.response import CustomHTTPException


def _photo(m: ClubTeamMembers) -> Optional[str]:
    p = m.photo
    if not isinstance(p, dict):
        return None
    return p.get("thumbnail") or p.get("medium") or None


async def rows_for(session, event: Events) -> list[tuple[EventTeamMembers, ClubTeamMembers]]:
    """The event's organisers with their team entries; someone since taken off the team drops out."""
    if event.club_id is None:
        return []
    return list((await session.execute(
        select(EventTeamMembers, ClubTeamMembers)
        .join(ClubTeamMembers, ClubTeamMembers.id == EventTeamMembers.member_id)
        .where(
            EventTeamMembers.event_id == event.id,
            ClubTeamMembers.club_id == event.club_id,
            ClubTeamMembers.is_deleted == False,  # noqa: E712
        )
        .order_by(EventTeamMembers.sort_order, EventTeamMembers.id)
    )).all())


async def list_organisers(session, event: Events) -> list[dict]:
    return [
        dict(member_id=m.id, name=m.name, club_role=m.role, role=o.role, photo=_photo(m))
        for o, m in await rows_for(session, event)
    ]


async def replace(session, event: Events, members: list) -> list[dict]:
    """The whole set at once: the list's order is the page's order."""
    ids = [x.member_id for x in members]
    if len(set(ids)) != len(ids):
        raise CustomHTTPException(422, message="Each person can be listed once.")
    if ids:
        on_team = set((await session.scalars(
            select(ClubTeamMembers.id).where(
                ClubTeamMembers.id.in_(ids), ClubTeamMembers.club_id == event.club_id, ClubTeamMembers.is_deleted == False,  # noqa: E712
            )
        )).all())
        missing = [i for i in ids if i not in on_team]
        if missing:
            raise CustomHTTPException(
                422,
                message=f"Not on this club's team: {', '.join(str(i) for i in missing)}. Organisers are picked from the club's team.",
            )
    await session.execute(delete(EventTeamMembers).where(EventTeamMembers.event_id == event.id))
    for i, x in enumerate(members):
        session.add(EventTeamMembers(event_id=event.id, member_id=x.member_id, role=x.role, sort_order=i))
    await session.commit()
    return await list_organisers(session, event)
