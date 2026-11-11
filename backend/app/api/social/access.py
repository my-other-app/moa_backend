"""Who is asking, for which club, and what they may decide.

The routes' roles are held by club_access.policy before a route runs (door
staff: none of /clubs/social; connecting, disconnecting, the approval
setting and approving: owners and admins). A co-host's team acts on a
shared event from its own club: its own accounts, its own posts, never the
host's (cohosts.access.own_club_id).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Request
from sqlalchemy import select

from app.api.club_ops.access import Actor, actor_of
from app.api.clubs.models import Clubs
from app.response import CustomHTTPException


@dataclass
class Who:
    user: object
    club: Clubs
    actor: Actor
    #: owner (the club's own account, or MyOtherApp acting), admin, editor.
    role: str

    @property
    def member_id(self) -> Optional[int]:
        return self.actor.member_id

    @property
    def name(self) -> str:
        return self.actor.name

    @property
    def decides(self) -> bool:
        """An owner or admin: may approve, and is never held for approval."""
        return self.role in ("owner", "admin")

    def ref(self) -> dict:
        return {"user_id": getattr(self.user, "id", None), "member_id": self.member_id, "name": self.name}


def _role(request: Optional[Request]) -> str:
    from app.api.club_access import service as club_access

    return club_access.role_of(request) if request is not None else "owner"


async def club_of(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


async def for_club(session, request: Request, user) -> Who:
    club = await club_of(session, user)
    return Who(user=user, club=club, actor=actor_of(request, user), role=_role(request))


async def for_event(session, request: Request, user, event_id: int):
    """The event (host, co-host or admin may manage it) and the club acting on it."""
    from app.api.cohosts import access as cohosts
    from app.api.deliveries import service as deliveries

    event = await deliveries.event_for_club(session, user, event_id)
    club = await session.get(Clubs, cohosts.own_club_id(event, user))
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return Who(user=user, club=club, actor=actor_of(request, user), role=_role(request)), event


async def still_hosts(session, event, club_id: int) -> bool:
    """The club hosts the event, or co-hosts it still."""
    from app.api.cohosts import access as cohosts

    return event.club_id == club_id or await cohosts.is_cohost(session, event.id, club_id)
