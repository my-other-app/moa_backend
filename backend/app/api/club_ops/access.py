"""Who is acting, and whether they may move the club's money.

A team member's role is held by the role rules on every request
(club_access.policy: an event's payments, refunds and cancelling are money,
which only the owner and admins have). What those rules cannot see is the
MyOtherApp admin behind a request: an admin signed in as themselves, or an
admin acting as the club (impersonation). Giving a guest's money back needs
the admin role that manages money (MANAGE_MONEY), whichever way the admin
came in -- the same capability the admin portal's own refund asks for.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Request, status
from sqlalchemy import select

from app.api.events.access import is_admin_user
from app.response import CustomHTTPException


@dataclass
class Actor:
    user: object
    member: Optional[object] = None
    impersonated_by: Optional[int] = None

    @property
    def user_id(self) -> Optional[int]:
        return getattr(self.user, "id", None)

    @property
    def member_id(self) -> Optional[int]:
        return getattr(self.member, "id", None)

    @property
    def name(self) -> str:
        if self.member is not None:
            return (self.member.full_name or self.member.email or "A team member")[:120]
        if self.impersonated_by:
            return "MyOtherApp support"
        if is_admin_user(self.user):
            return "MyOtherApp"
        return (getattr(self.user, "full_name", None) or "The club")[:120]

    def details(self) -> dict:
        return {"member_id": self.member_id, "impersonated_by": self.impersonated_by}


def actor_of(request: Optional[Request], user) -> Actor:
    from app.api.club_access import service as club_access
    from app.api.subscriptions import gates

    member = club_access.current_member(request) if request is not None else None
    return Actor(user=user, member=member, impersonated_by=gates.impersonated_by())


def _refuse(message: str) -> CustomHTTPException:
    return CustomHTTPException(status.HTTP_403_FORBIDDEN, message=message, error_code="INSUFFICIENT_ADMIN_ROLE")


async def require_money(session, actor: Actor) -> None:
    """An admin, or an admin acting as the club, must be one who manages money."""
    from app.api.users.models import Users
    from app.core.auth.permissions import MANAGE_MONEY, can

    if is_admin_user(actor.user) and not can(actor.user, MANAGE_MONEY):
        raise _refuse("Your admin role cannot give money back")
    if actor.impersonated_by:
        admin = await session.scalar(select(Users).where(Users.id == actor.impersonated_by))
        if admin is None or not can(admin, MANAGE_MONEY):
            raise _refuse("Your admin role cannot give money back")
