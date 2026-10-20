"""A club's connected apps, for the dashboard's Security page.

Owners and admins see and disconnect every app connected to the club; any
other member sees and disconnects the ones they connected themselves
(policy.can: connected_apps). Disconnecting ends the grant and every token
it has at once, and cancels what the app asked for that is still waiting.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update

from app.api.audit import service as audit
from app.api.club_access import policy
from app.api.club_access.models import ClubMembers
from app.api.mcp import scopes as catalogue
from app.api.mcp.access import client_label, end_grant
from app.api.mcp.models import McpClients, McpGrants, McpPendingActions
from app.response import CustomHTTPException


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def sees_all(role: str) -> bool:
    return policy.can(role)["connected_apps"]


async def apps(session, club_id: int, *, role: str, member: ClubMembers | None) -> dict:
    q = (
        select(McpGrants, McpClients, ClubMembers)
        .join(McpClients, McpClients.id == McpGrants.client_pk)
        .outerjoin(ClubMembers, ClubMembers.id == McpGrants.member_id)
        .where(McpGrants.club_id == club_id, McpGrants.revoked_at.is_(None))
        .order_by(McpGrants.created_at.desc())
    )
    if not sees_all(role):
        q = q.where(McpGrants.member_id == (member.id if member is not None else -1))
    rows = (await session.execute(q)).all()
    waiting = dict((await session.execute(
        select(McpPendingActions.grant_id, func.count())
        .where(McpPendingActions.club_id == club_id, McpPendingActions.status == "pending", McpPendingActions.expires_at > func.now())
        .group_by(McpPendingActions.grant_id)
    )).all())
    return {
        "can_manage_all": sees_all(role),
        "apps": [
            {
                "id": str(g.id),
                "app": {"name": client_label(c), "client_id": c.client_id, "uri": c.client_uri, "logo_uri": c.logo_uri, "registration": c.kind},
                "connected_by": (
                    {"type": "member", "member_id": m.id, "name": m.full_name, "email": m.email, "role": m.role}
                    if m is not None else {"type": "owner"}
                ),
                "scopes": [{"key": s, "label": catalogue.SCOPES[s][0]} for s in catalogue.parse(g.scopes)],
                "connected_at": _iso(g.created_at),
                "last_used_at": _iso(g.last_used_at),
                "pending_actions": waiting.get(g.id, 0),
            }
            for g, c, m in rows
        ],
    }


async def disconnect(session, club_id: int, grant_id: str, *, user, role: str, member: ClubMembers | None, request=None) -> None:
    try:
        key = UUID(str(grant_id))
    except (TypeError, ValueError):
        key = None
    grant = await session.get(McpGrants, key) if key else None
    mine = grant is not None and member is not None and grant.member_id == member.id
    if grant is None or grant.club_id != club_id or grant.revoked_at is not None or not (sees_all(role) or mine):
        raise CustomHTTPException(status_code=404, message="No such connected app.", error_code="APP_NOT_FOUND")
    client = await session.get(McpClients, grant.client_pk)
    await end_grant(session, grant, reason="disconnected", by_user_id=user.id, by_member_id=member.id if member else None)
    await session.execute(
        update(McpPendingActions)
        .where(McpPendingActions.grant_id == grant.id, McpPendingActions.status == "pending")
        .values(status="rejected", decided_at=datetime.now(timezone.utc), decided_by_user_id=user.id,
                decided_by_member_id=member.id if member else None, result={"reason": "app_disconnected"})
    )
    await audit.record(
        session, actor=user, action="mcp.app.disconnected", entity_type="club", entity_id=club_id,
        summary=f"{client_label(client)} disconnected"[:255],
        details={"via": "dashboard", "client": client_label(client), "grant_id": str(grant.id), "member_id": member.id if member else None},
        request=request,
    )
    await session.commit()
