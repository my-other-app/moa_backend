"""Who a connected app acts as, worked out again on every request.

A grant names a club, the club's own account and, when a team member
connected it, the member. None of that is trusted from the time of consent:
each call (and each refresh) reads the club, the account and the membership
as they are now. A member removed from the club, a club taken down, an
account suspended or an owner's password changed ends the grant there and
then -- not when its token runs out.

The role read here is what the tools are checked against
(club_access.policy.allows_tool), so a member demoted to door staff stops
being able to draft events on their next call.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update

from app.api.club_access.models import ClubMembers
from app.api.club_access.service import role_label
from app.api.clubs.models import Clubs
from app.api.mcp.models import McpClients, McpGrants, McpTokens
from app.api.users.models import Users


class AccessEnded(Exception):
    """The grant no longer stands for anyone who may act for the club."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class Principal:
    grant: McpGrants
    client: McpClients
    club: Clubs
    user: Users
    member: ClubMembers | None
    scopes: frozenset

    @property
    def role(self) -> str:
        return self.member.role if self.member is not None else "owner"

    @property
    def role_label(self) -> str:
        return role_label(self.role)

    @property
    def client_name(self) -> str:
        return client_label(self.client)

    @property
    def who(self) -> str:
        """The person, as an audit line or a dashboard page names them."""
        if self.member is not None:
            return self.member.full_name or self.member.email
        return "the club's own account"


def client_label(client: McpClients | None) -> str:
    if client is None:
        return "An app"
    return (client.client_name or "").strip() or client.client_id[:60]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def principal_for(session, grant: McpGrants) -> Principal:
    """The grant's club, account and member as they are now, or AccessEnded."""
    if grant.revoked_at is not None:
        raise AccessEnded("revoked", "This connection was ended. Connect the app again.")
    club = await session.get(Clubs, grant.club_id)
    if club is None or club.is_deleted or not club.is_verified or club.user_id != grant.user_id:
        raise AccessEnded("club_unavailable", "This club's dashboard is closed right now.")
    user = await session.get(Users, grant.user_id)
    if user is None or user.is_deleted or getattr(user, "is_active", True) is False:
        raise AccessEnded("club_unavailable", "This club's dashboard is closed right now.")
    member = None
    if grant.member_id is not None:
        member = await session.get(ClubMembers, grant.member_id)
        if member is None or member.status != "active" or member.club_id != club.id:
            raise AccessEnded("access_removed", "Your access to this club's dashboard was removed.")
    elif grant.token_version is not None and grant.token_version != user.token_version:
        # The owner changed the password, or signed out everywhere.
        raise AccessEnded("signed_out", "The club's account was signed out everywhere. Connect the app again.")
    client = await session.get(McpClients, grant.client_pk)
    return Principal(grant=grant, client=client, club=club, user=user, member=member, scopes=frozenset(grant.scopes.split()))


async def end_grant(session, grant: McpGrants, *, reason: str, by_user_id: int | None = None, by_member_id: int | None = None) -> None:
    """Revoke the grant and every token it has. Not committed."""
    now = _now()
    if grant.revoked_at is None:
        grant.revoked_at = now
        grant.revoked_reason = reason[:40]
        grant.revoked_by_user_id = by_user_id
        grant.revoked_by_member_id = by_member_id
    await session.execute(
        update(McpTokens).where(McpTokens.grant_id == grant.id, McpTokens.revoked_at.is_(None)).values(revoked_at=now)
    )
