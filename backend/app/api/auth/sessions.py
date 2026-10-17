"""Active sessions, and revoking one.

`token_version` could already invalidate *every* session for a user at once,
which is the right tool for a password change or a suspension. It is the
wrong tool for "sign out my old laptop": there was no way to see which
sessions existed, let alone end one of them.

Each refresh token now carries a `jti` matching a row here. Revoking sets a
flag on that row, which the refresh path checks -- so the session dies at its
next refresh rather than surviving until the token expires.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class UserSessions(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "user_sessions"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # What the person would recognise it by. A user agent is a poor name for a
    # device but it is the only thing the browser actually tells us.
    user_agent = Column(String(255), nullable=True)
    ip_address = Column(String(45), nullable=True)

    last_seen_at = Column(
        TimestampsMixin.created_at.type, nullable=True
    )
    revoked = Column(Boolean, nullable=False, default=False)
    # A club team member's session on the club's account: who it really is.
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True, index=True)


async def start(
    session: AsyncSession, user_id: int, request=None, member_id: int | None = None
) -> uuid.UUID:
    """Record a new session and return the id to embed in the token."""
    user_agent = None
    ip_address = None
    if request is not None:
        from app.core.net import client_address

        user_agent = (request.headers.get("user-agent") or "")[:255] or None
        ip_address = client_address(request)[:45] or None

    row = UserSessions(
        id=uuid.uuid4(),
        user_id=user_id,
        user_agent=user_agent,
        ip_address=ip_address,
        last_seen_at=datetime.now(timezone.utc),
        member_id=member_id,
    )
    session.add(row)
    await session.flush()
    return row.id


async def is_live(session: AsyncSession, session_id) -> bool:
    """
    Whether a session may still be refreshed.

    Returns True for a token with no session id: tokens issued before this
    existed have none, and failing them closed would sign out every user on
    deploy.
    """
    if not session_id:
        return True
    try:
        key = uuid.UUID(str(session_id))
    except (ValueError, AttributeError):
        return False

    row = await session.get(UserSessions, key)
    if row is None:
        # Issued by this build but no longer on record: treat as revoked.
        return False
    return not row.revoked


async def touch(session: AsyncSession, session_id) -> None:
    """Record activity. Best effort -- never fail a refresh over it."""
    if not session_id:
        return
    try:
        row = await session.get(UserSessions, uuid.UUID(str(session_id)))
        if row:
            row.last_seen_at = datetime.now(timezone.utc)
    except Exception:
        pass


async def list_for_user(session: AsyncSession, user_id: int):
    rows = await session.scalars(
        select(UserSessions)
        .where(UserSessions.user_id == user_id, UserSessions.revoked == False)
        .order_by(UserSessions.last_seen_at.desc().nulls_last())
    )
    return list(rows)


async def revoke(session: AsyncSession, session_id, user_id: int) -> bool:
    """Revoking is scoped to the owner so one user cannot end another's."""
    try:
        row = await session.get(UserSessions, uuid.UUID(str(session_id)))
    except (ValueError, AttributeError):
        return False
    if row is None or row.user_id != user_id:
        return False
    row.revoked = True
    await session.commit()
    return True


async def revoke_all_for_user(session: AsyncSession, user_id: int) -> int:
    rows = await list_for_user(session, user_id)
    for row in rows:
        row.revoked = True
    await session.commit()
    return len(rows)


async def get_live(session: AsyncSession, session_id):
    """The session row, or None when it is gone or revoked."""
    try:
        row = await session.get(UserSessions, uuid.UUID(str(session_id)))
    except (ValueError, AttributeError):
        return None
    if row is None or row.revoked:
        return None
    return row


async def touch_throttled(session: AsyncSession, row, every_seconds: int = 60) -> None:
    """Move last_seen_at along, but not on every single request."""
    now = datetime.now(timezone.utc)
    last = row.last_seen_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or (now - last).total_seconds() >= every_seconds:
        row.last_seen_at = now
        try:
            await session.commit()
        except Exception:
            await session.rollback()
