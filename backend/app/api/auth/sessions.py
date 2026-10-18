"""Active sessions, and revoking one.

`token_version` could already invalidate *every* session for a user at once,
which is the right tool for a password change or a suspension. It is the
wrong tool for "sign out my old laptop": there was no way to see which
sessions existed, let alone end one of them.

Each token carries the session's id (`sid`) matching a row here. Revoking
sets a flag on that row, which every request and refresh checks -- so the
session dies at once rather than surviving until the token expires.

Refresh tokens rotate. Each carries the session's generation (`gen`); a
refresh hands back a token for the next one, and the one it replaced is
spent. A spent token presented again means two parties hold the session --
one of them stole it -- so the session ends (`rotate` says "reused"). The
token just replaced is still answered for REFRESH_REUSE_GRACE_SECONDS, with
the same generation the first answer gave: a reply lost on a bad network, or
two tabs refreshing together, is not a theft.

An admin's session as someone else (`impersonated_by`) has a row too, with
a hard `expires_at` and no refresh token at all.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Index, Integer, String, select, text, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID, insert as pg_insert
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
    # Which refresh token is the live one: each refresh moves it on by one.
    refresh_generation = Column(BigInteger, nullable=False, default=0, server_default="0")
    rotated_at = Column(TimestampsMixin.created_at.type, nullable=True)
    # A hard end, whatever the tokens say: an admin's session as someone else.
    expires_at = Column(TimestampsMixin.created_at.type, nullable=True)
    # The admin acting as this account, when one is.
    impersonated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        # The housekeeping sweep (app.api.auth.retention): idle sessions, and
        # those past a hard end, as index ranges.
        Index("ix_user_sessions_last_seen", "last_seen_at"),
        Index("ix_user_sessions_expires", "expires_at", postgresql_where=text("expires_at IS NOT NULL")),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _device(request) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    from app.core.net import client_address

    return (request.headers.get("user-agent") or "")[:255] or None, client_address(request)[:45] or None


async def start(
    session: AsyncSession, user_id: int, request=None, member_id: int | None = None
) -> uuid.UUID:
    """Record a new session and return the id to embed in the token."""
    user_agent, ip_address = _device(request)
    row = UserSessions(
        id=uuid.uuid4(),
        user_id=user_id,
        user_agent=user_agent,
        ip_address=ip_address,
        last_seen_at=_now(),
        member_id=member_id,
        refresh_generation=0,
    )
    session.add(row)
    await session.flush()
    return row.id


async def start_once(
    session: AsyncSession, session_id: uuid.UUID, user_id: int, *, request=None,
    impersonated_by: int | None = None, expires_at: datetime | None = None,
) -> bool:
    """Record a session under an id chosen beforehand -- an impersonation
    hand-off's `jti` -- unless one already exists under it. True when this
    call made it: the primary key makes a hand-off good for one exchange,
    however many requests race with it."""
    user_agent, ip_address = _device(request)
    now = _now()
    made = await session.scalar(
        pg_insert(UserSessions)
        .values(
            id=session_id, user_id=user_id, user_agent=user_agent, ip_address=ip_address, last_seen_at=now,
            revoked=False, refresh_generation=0, impersonated_by=impersonated_by, expires_at=expires_at,
            created_at=now, updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(UserSessions.id)
    )
    return made is not None


def _ended(row) -> bool:
    expires = _aware(row.expires_at)
    return bool(row.revoked) or (expires is not None and expires <= _now())


async def rotate(session: AsyncSession, session_id, presented: int, *, grace_seconds: int) -> tuple[str, int | None]:
    """Spend the refresh token of generation `presented` for this session.

    Returns (outcome, generation for the new token):
      ("rotated", n)  -- it was the live one; the session is now at n.
      ("grace", n)    -- it was replaced under grace_seconds ago: answered
                         with the generation that replaced it.
      ("reused", None) -- an older one: the session is revoked now.
      ("ended", None) -- the session is signed out, expired or gone.
    The caller commits."""
    try:
        key = uuid.UUID(str(session_id))
    except (ValueError, AttributeError):
        return "ended", None
    now = _now()
    moved = await session.scalar(
        update(UserSessions)
        .where(
            UserSessions.id == key,
            UserSessions.revoked == False,  # noqa: E712
            UserSessions.refresh_generation == presented,
            (UserSessions.expires_at.is_(None)) | (UserSessions.expires_at > now),
        )
        .values(refresh_generation=UserSessions.refresh_generation + 1, rotated_at=now, last_seen_at=now)
        .returning(UserSessions.refresh_generation)
        .execution_options(synchronize_session=False)
    )
    if moved is not None:
        return "rotated", int(moved)
    row = await session.get(UserSessions, key, populate_existing=True)
    if row is None or _ended(row):
        return "ended", None
    rotated = _aware(row.rotated_at)
    if (
        presented == row.refresh_generation - 1
        and rotated is not None
        and (now - rotated) <= timedelta(seconds=max(0, grace_seconds))
    ):
        return "grace", int(row.refresh_generation)
    row.revoked = True
    await session.flush()
    return "reused", None


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
    return not _ended(row)


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
    now = _now()
    rows = await session.scalars(
        select(UserSessions)
        .where(
            UserSessions.user_id == user_id,
            UserSessions.revoked == False,  # noqa: E712
            (UserSessions.expires_at.is_(None)) | (UserSessions.expires_at > now),
        )
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
    """The session row, or None when it is gone, revoked or past its end."""
    try:
        row = await session.get(UserSessions, uuid.UUID(str(session_id)))
    except (ValueError, AttributeError):
        return None
    if row is None or _ended(row):
        return None
    return row


#: How stale last_seen_at may get before a request writes it again. Every
#: signed-in request reads the session; at a minute this was a write per
#: active user per minute, for a "last active" line on the Security page.
TOUCH_EVERY_SECONDS = 300


async def touch_throttled(session: AsyncSession, row, every_seconds: int = TOUCH_EVERY_SECONDS) -> None:
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
