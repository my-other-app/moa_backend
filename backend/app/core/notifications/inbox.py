"""The inbox with shared broadcasts (INBOX_SHARED).

A broadcast -- a club's post or new event, a new event by topic -- was a row
in `notifications` for every recipient: a club with a lakh of followers
posting wrote a lakh rows, the platform's biggest write. With INBOX_SHARED it
is one row (notification_broadcasts) and a line per audience it went to
(notification_broadcast_targets: "club:12", "topic:5"), and a person's inbox
is read through their follows and interests:

- their targets: each club they follow and have not muted, from when they
  followed it (followed_at, or the follow row's created_at for follows from
  before it existed); each topic of their interests, from when they first
  picked an interest under it; a switch turned off drops a kind of target;
- for each target, its newest broadcasts after the page's cursor -- a short
  range of ix_notification_broadcast_targets_feed (target, created_at DESC,
  broadcast_id DESC), LIMIT the page: a LATERAL per target, so a person
  following 200 clubs reads 200 small index ranges, however many broadcasts
  there are in all;
- merged with their own rows (ix_notifications_user_created), newest first,
  cut to the page. A broadcast under two of their topics is listed once.

What they did with a broadcast is a row in notification_broadcast_states,
written only when they act: read, or dismissed (gone from the inbox). "Mark
all read" moves notification_settings.read_through to now: everything shared
up to it is read, without a row each.

A new event by topic is not listed to someone who followed its club before
it was announced: the club's own line (new_event) told them, as the
per-recipient fan-out left them out of the topic's.

Pages go by keyset: the response's `next` carries the last line's
(created_at, id) as `cursor`, and a page from a cursor reads only rows older
than it. An `offset` without a cursor (the app's pages) still works, reading
offset + limit rows per target.

The response has exactly the shape the per-recipient rows had
(NotificationSchema): a shared line's `user_id` is the reader's, its `status`
theirs.
"""
from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.notifications.models import (
    NotificationBroadcasts,
    NotificationBroadcastStates,
    NotificationSettings,
    Notifications,
    NotificationStatus,
)
from app.api.users.models import UserProfiles, Users

#: The unread count of shared lines stops here: past it the badge says as
#: much as an exact number would, and the count stays a bounded read.
UNREAD_CAP = 999

# The reader's targets: each followed, unmuted club from when they followed
# it; each topic of their interests from when they first picked one under it.
# A switch turned off (notification_settings) drops that kind of target.
_TARGETS = """
WITH prefs AS (
    SELECT coalesce(s.clubs, true) AS clubs, coalesce(s.topics, true) AS topics, s.read_through
    FROM (SELECT 1) AS one LEFT JOIN notification_settings s ON s.user_id = :uid
), mine AS (
    SELECT 'club' AS kind, 'club:' || l.club_id AS target, coalesce(l.followed_at, l.created_at) AS since
    FROM club_users_link l, prefs
    WHERE l.user_id = :uid AND l.is_following AND NOT l.is_deleted AND NOT l.is_muted AND prefs.clubs
    UNION ALL
    SELECT 'topic', 'topic:' || i.category_id, min(ui.created_at)
    FROM user_interests ui JOIN interests i ON i.id = ui.interest_id, prefs
    WHERE ui.user_id = :uid AND NOT ui.is_deleted AND NOT i.is_deleted AND prefs.topics
    GROUP BY i.category_id
)
"""

# One target's lines the reader may see: from when they joined it, not a
# topic line about a club they already followed, not dismissed. The
# dismissed (and, for the count, the read) are a NOT IN over the reader's own
# state rows: Postgres hashes those once per statement ("hashed SubPlan")
# and each line is a probe of the hash, where a NOT EXISTS per line was an
# index probe each -- most of the read's cost.
_VISIBLE = """
    bt.target = t.target AND bt.created_at >= t.since
    AND (bt.except_club_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM club_users_link f
        WHERE f.club_id = bt.except_club_id AND f.user_id = :uid AND f.is_following AND NOT f.is_deleted
          AND coalesce(f.followed_at, f.created_at) <= bt.created_at))
    AND bt.broadcast_id NOT IN (
        SELECT st.broadcast_id FROM notification_broadcast_states st
        WHERE st.user_id = :uid AND st.dismissed_at IS NOT NULL)
"""

_AFTER = "AND (bt.created_at, bt.broadcast_id) < (:c_at, :c_id)"
_N_AFTER = "AND (n.created_at, n.id) < (:c_at, :c_id)"


def _page_sql(cursor: bool) -> sa.TextClause:
    return text(_TARGETS + f"""
, shared AS (
    SELECT DISTINCT b.broadcast_id AS id, b.created_at
    FROM mine t CROSS JOIN LATERAL (
        SELECT bt.broadcast_id, bt.created_at
        FROM notification_broadcast_targets bt
        WHERE {_VISIBLE} {_AFTER if cursor else ""}
        ORDER BY bt.created_at DESC, bt.broadcast_id DESC
        LIMIT :k
    ) b
), own AS (
    SELECT n.id, n.created_at
    FROM notifications n
    WHERE n.user_id = :uid AND NOT n.is_deleted {_N_AFTER if cursor else ""}
    ORDER BY n.created_at DESC, n.id DESC
    LIMIT :k
)
SELECT 'b' AS src, id, created_at FROM shared
UNION ALL
SELECT 'n' AS src, id, created_at FROM own
ORDER BY created_at DESC, id DESC
LIMIT :k
""")


# The unread shared lines, at most :cap. A club's broadcast has one target,
# so its lines are counted as they stream from the index and the count stops
# at the cap (a reader with a lakh unread reads a thousand index entries, not
# a lakh). A broadcast by topic can be under two of the reader's topics: those
# are counted distinct -- few, as each topic has a handful a day.
_UNREAD_LINES = """
    SELECT bt.broadcast_id
    FROM notification_broadcast_targets bt
    WHERE {visible}
      AND bt.created_at >= greatest(t.since, prefs.read_through)
      AND (prefs.read_through IS NULL OR bt.created_at > prefs.read_through)
      AND bt.broadcast_id NOT IN (
          SELECT r.broadcast_id FROM notification_broadcast_states r
          WHERE r.user_id = :uid AND r.read_at IS NOT NULL)
    LIMIT :cap
""".format(visible=_VISIBLE)

_UNREAD = text(_TARGETS + f"""
SELECT least(:cap,
    (SELECT count(*) FROM (
        SELECT 1 FROM mine t CROSS JOIN prefs CROSS JOIN LATERAL ({_UNREAD_LINES}) bt
        WHERE t.kind = 'club' LIMIT :cap) clubs)
  + (SELECT count(DISTINCT bt.broadcast_id)
        FROM mine t CROSS JOIN prefs CROSS JOIN LATERAL ({_UNREAD_LINES}) bt
        WHERE t.kind = 'topic'))
""")

# The newest line on any of the reader's targets: "read all" reads up to it
# even when the clock that stamped it ran ahead of the database's.
_NEWEST = text(_TARGETS + """
SELECT max(x.created_at) FROM mine t CROSS JOIN LATERAL (
    SELECT bt.created_at FROM notification_broadcast_targets bt
    WHERE bt.target = t.target ORDER BY bt.created_at DESC LIMIT 1
) x
""")

_CAN_SEE = text(_TARGETS + f"""
SELECT EXISTS (
    SELECT 1 FROM mine t JOIN notification_broadcast_targets bt ON bt.target = t.target
    WHERE bt.broadcast_id = :bid AND {_VISIBLE}
)
""")


# ─── Cursors ────────────────────────────────────────────────────────────────


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = f"{created_at.astimezone(timezone.utc).isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value: Optional[str]) -> Optional[tuple[datetime, uuid.UUID]]:
    """A cursor from `next`, or None for one that is not ours (read as the first page)."""
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        at, _, rid = raw.partition("|")
        created_at = datetime.fromisoformat(at)
        if created_at.tzinfo is None:
            return None
        return created_at, uuid.UUID(rid)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


# ─── A shared line, as the inbox shows it ───────────────────────────────────


@dataclass
class SharedLine:
    """A broadcast as one reader's inbox line: NotificationSchema reads it as
    it reads a row of `notifications`."""

    id: uuid.UUID
    user_id: int
    title: str
    description: str
    type: str
    status: NotificationStatus
    from_club: Any
    from_user: Any
    event: Any
    data: Optional[dict]
    created_at: datetime
    updated_at: datetime
    from_club_id: Optional[int] = None
    from_user_id: Optional[int] = None
    event_id: Optional[int] = None

    def as_row(self) -> dict:
        """The columns of a `notifications` row, as POST /notifications/read/{id} answers."""
        return dict(
            id=str(self.id), title=self.title, type=self.type, event_id=self.event_id, created_at=self.created_at,
            updated_at=self.updated_at, deleted_at=None, user_id=self.user_id, description=self.description,
            from_club_id=self.from_club_id, from_user_id=self.from_user_id, status=self.status.value,
            data=self.data, is_deleted=False,
        )


async def _read_through(session: AsyncSession, user_id: int) -> Optional[datetime]:
    return await session.scalar(select(NotificationSettings.read_through).where(NotificationSettings.user_id == user_id))


async def _lines(session: AsyncSession, user_id: int, ids: list[uuid.UUID]) -> dict[uuid.UUID, SharedLine]:
    if not ids:
        return {}
    rows = (await session.scalars(
        select(NotificationBroadcasts).where(NotificationBroadcasts.id.in_(ids)).options(
            selectinload(NotificationBroadcasts.from_club),
            selectinload(NotificationBroadcasts.from_user).options(
                selectinload(Users.profile).options(selectinload(UserProfiles.avatar), selectinload(UserProfiles.org))
            ),
            selectinload(NotificationBroadcasts.event),
        )
    )).all()
    states = {
        s.broadcast_id: s for s in (await session.scalars(
            select(NotificationBroadcastStates).where(
                NotificationBroadcastStates.user_id == user_id, NotificationBroadcastStates.broadcast_id.in_(ids)
            )
        )).all()
    }
    through = await _read_through(session, user_id)
    out = {}
    for b in rows:
        state = states.get(b.id)
        read_at = state.read_at if state is not None else None
        read = read_at is not None or (through is not None and b.created_at <= through)
        out[b.id] = SharedLine(
            id=b.id, user_id=user_id, title=b.title, description=b.description, type=b.type,
            status=NotificationStatus.read if read else NotificationStatus.unread,
            from_club=b.from_club, from_user=b.from_user, event=b.event, data=b.data,
            created_at=b.created_at, updated_at=max(b.updated_at, read_at) if read_at else b.updated_at,
            from_club_id=b.from_club_id, from_user_id=b.from_user_id, event_id=b.event_id,
        )
    return out


# ─── Reads ──────────────────────────────────────────────────────────────────


async def page(
    session: AsyncSession, user_id: int, *, limit: int, offset: int = 0, cursor: Optional[tuple[datetime, uuid.UUID]] = None,
) -> list:
    """`limit` inbox lines, newest first: the reader's own rows and the
    shared broadcasts they can see, after `cursor` (keyset) or past `offset`
    (from the top, when there is no cursor). Rows of `notifications` and
    SharedLines, in order."""
    k = limit + (0 if cursor else offset)
    params: dict[str, Any] = dict(uid=int(user_id), k=int(k))
    if cursor:
        params.update(c_at=cursor[0], c_id=cursor[1])
    picked = (await session.execute(_page_sql(cursor is not None), params)).all()
    if not cursor:
        picked = picked[offset:]
    picked = picked[:limit]
    own_ids = [r.id for r in picked if r.src == "n"]
    shared_ids = [r.id for r in picked if r.src == "b"]
    own = {}
    if own_ids:
        rows = (await session.scalars(
            select(Notifications).where(Notifications.id.in_(own_ids)).options(
                selectinload(Notifications.from_club),
                selectinload(Notifications.from_user).options(
                    selectinload(Users.profile).options(selectinload(UserProfiles.avatar), selectinload(UserProfiles.org))
                ),
                selectinload(Notifications.event),
            )
        )).all()
        own = {n.id: n for n in rows}
    shared = await _lines(session, user_id, shared_ids)
    out = []
    for r in picked:
        line = own.get(r.id) if r.src == "n" else shared.get(r.id)
        if line is not None:
            out.append(line)
    return out


async def shared_unread(session: AsyncSession, user_id: int) -> int:
    """Shared lines the reader has not read, at most UNREAD_CAP."""
    return int(await session.scalar(_UNREAD, dict(uid=int(user_id), cap=UNREAD_CAP)) or 0)


async def can_see(session: AsyncSession, user_id: int, broadcast_id: uuid.UUID) -> bool:
    return bool(await session.scalar(_CAN_SEE, dict(uid=int(user_id), bid=broadcast_id)))


# ─── What the reader does ───────────────────────────────────────────────────


async def _state(session: AsyncSession, user_id: int, broadcast_id: uuid.UUID, **values) -> None:
    stmt = pg_insert(NotificationBroadcastStates).values(user_id=user_id, broadcast_id=broadcast_id, **values)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[NotificationBroadcastStates.user_id, NotificationBroadcastStates.broadcast_id],
        set_={k: sa.func.coalesce(getattr(NotificationBroadcastStates, k), stmt.excluded[k]) for k in values},
    ))


async def mark_read(session: AsyncSession, user_id: int, broadcast_id: uuid.UUID) -> Optional[SharedLine]:
    """The reader read this shared line: their state row says so (the first
    time it is marked stays). None when it is not one they can see."""
    if not await can_see(session, user_id, broadcast_id):
        return None
    await _state(session, user_id, broadcast_id, read_at=datetime.now(timezone.utc))
    await session.commit()
    return (await _lines(session, user_id, [broadcast_id])).get(broadcast_id)


async def dismiss(session: AsyncSession, user_id: int, broadcast_id: uuid.UUID) -> bool:
    if not await can_see(session, user_id, broadcast_id):
        return False
    await _state(session, user_id, broadcast_id, dismissed_at=datetime.now(timezone.utc))
    await session.commit()
    return True


async def mark_all_read(session: AsyncSession, user_id: int) -> int:
    """Everything shared up to now is read: one row moves (read_through).
    Returns how many shared lines that read (at most UNREAD_CAP). The caller
    commits."""
    unread = await shared_unread(session, user_id)
    newest = await session.scalar(_NEWEST, dict(uid=int(user_id)))
    through = sa.func.greatest(sa.func.statement_timestamp(), sa.literal(newest, NotificationSettings.read_through.type)) if newest else sa.func.statement_timestamp()
    stmt = pg_insert(NotificationSettings).values(user_id=user_id, read_through=through)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[NotificationSettings.user_id],
        set_={"read_through": stmt.excluded.read_through, "updated_at": sa.func.statement_timestamp()},
    ))
    return unread


async def personal_unread(session: AsyncSession, user_id: int) -> int:
    from sqlalchemy import func

    return int(await session.scalar(select(func.count(Notifications.id)).where(
        Notifications.user_id == user_id, Notifications.status == NotificationStatus.unread,
        Notifications.is_deleted == False,  # noqa: E712
    )) or 0)


async def mark_personal_read(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        update(Notifications)
        .where(Notifications.user_id == user_id, Notifications.status == NotificationStatus.unread, Notifications.is_deleted == False)  # noqa: E712
        .values(status=NotificationStatus.read)
    )
    return int(result.rowcount or 0)
