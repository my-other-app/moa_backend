"""A person's FCM topics: the server's list, which the app subscribes to.

The app asks GET /notifications/topics on start, on sign-in and after it
changes a follow, a mute, an interest or a switch; it subscribes to what is
new on the list, unsubscribes from what left it, and keeps the version it
applied, so an unchanged list costs it no FCM call. The list is:

- `all`, unless the person turned the platform's announcements off;
- `topic_{slug}` for the topic of each interest they picked, unless they
  turned new events by topic off;
- `club_{id}` for each club they follow and have not muted, unless they
  turned their clubs' posts and events off -- the most recently followed
  first, so that with more clubs than FCM allows topics (2,000 per app
  install) the oldest follows are the ones left out.

The version is a hash of the list: the app and the server agree it changed
exactly when the list did.

When a topic leaves someone's list (a mute, an unfollow, an interest dropped,
a switch turned off), the server also takes their devices out of it
(`forget`, a job): the device they did it on reconciles at once, but their
other devices would only on their next start, and until then still get the
club they muted.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import ClubUsersLink
from app.api.interests.models import InterestCategory, Interests
from app.api.notifications.models import NotificationSettings
from app.api.users.models import UserDeviceTokens, UserInterests
from app.core import jobs
from app.core.notifications.delivery import ALL_TOPIC, club_topic, topic_topic

logger = logging.getLogger(__name__)

#: FCM allows an app install 2,000 topics; the list stays under that.
MAX_TOPICS = 1900

SWITCHES = ("clubs", "topics", "platform")


async def switches(session: AsyncSession, user_id: int) -> dict[str, bool]:
    """The person's switches: every one on unless they turned it off. Read
    as columns, not the row: a row this session already holds would answer
    with what it held before an upsert in the same request."""
    row = (await session.execute(
        select(*(getattr(NotificationSettings, k) for k in SWITCHES)).where(NotificationSettings.user_id == user_id)
    )).first()
    return {k: (bool(getattr(row, k)) if row is not None else True) for k in SWITCHES}


async def set_switches(session: AsyncSession, user_id: int, changes: dict[str, bool]) -> dict[str, bool]:
    """Turn switches on or off; the caller commits. Topics that leave the
    list go from the person's devices too (`forget`)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    changes = {k: bool(v) for k, v in changes.items() if k in SWITCHES and v is not None}
    if not changes:
        return await switches(session, user_id)
    before = await topics_for(session, user_id)
    stmt = pg_insert(NotificationSettings).values(user_id=user_id, **changes)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[NotificationSettings.user_id], set_={**changes, "updated_at": func.statement_timestamp()},
    ))
    after = await topics_for(session, user_id)
    await forget(session, user_id, set(before) - set(after))
    return await switches(session, user_id)


async def is_muted(session: AsyncSession, user_id: int, club_id: int) -> bool:
    return bool(await session.scalar(select(ClubUsersLink.is_muted).where(
        ClubUsersLink.club_id == club_id, ClubUsersLink.user_id == user_id, ClubUsersLink.is_deleted == False,  # noqa: E712
    )))


async def set_mute(session: AsyncSession, user_id: int, club_id: int, muted: bool) -> None:
    """Mute or unmute a club the person follows (404 when they do not); the
    caller commits. Muted, its topic goes from their devices too."""
    from sqlalchemy import update

    from app.response import CustomHTTPException

    row = await session.scalar(
        update(ClubUsersLink)
        .where(
            ClubUsersLink.club_id == club_id, ClubUsersLink.user_id == user_id,
            ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False,  # noqa: E712
        )
        .values(is_muted=muted)
        .returning(ClubUsersLink.id)
    )
    if row is None:
        raise CustomHTTPException(404, "You do not follow this club")
    if muted:
        await forget(session, user_id, [club_topic(club_id)])


async def interest_topics(session: AsyncSession, user_id: int) -> list[str]:
    """`topic_{slug}` for the live topic of each live interest the person picked."""
    slugs = (await session.scalars(
        select(InterestCategory.slug)
        .join(Interests, Interests.category_id == InterestCategory.id)
        .join(UserInterests, UserInterests.interest_id == Interests.id)
        .where(
            UserInterests.user_id == user_id, UserInterests.is_deleted == False,  # noqa: E712
            Interests.is_deleted == False, InterestCategory.is_deleted == False,  # noqa: E712
        )
        .distinct()
    )).all()
    return sorted(topic_topic(s) for s in slugs if s)


async def topics_for(session: AsyncSession, user_id: int) -> list[str]:
    """The topics this person's devices are to be subscribed to, sorted."""
    on = await switches(session, user_id)
    out: list[str] = []
    if on["platform"]:
        out.append(ALL_TOPIC)
    if on["topics"]:
        out += await interest_topics(session, user_id)
    if on["clubs"]:
        clubs = (await session.scalars(
            select(ClubUsersLink.club_id)
            .where(
                ClubUsersLink.user_id == user_id, ClubUsersLink.is_following == True,  # noqa: E712
                ClubUsersLink.is_deleted == False, ClubUsersLink.is_muted == False,  # noqa: E712
            )
            .order_by(func.coalesce(ClubUsersLink.followed_at, ClubUsersLink.created_at).desc(), ClubUsersLink.club_id.desc())
            .limit(max(0, MAX_TOPICS - len(out)))
        )).all()
        out += [club_topic(c) for c in clubs]
    return sorted(set(out))


def version_of(topics: Iterable[str]) -> str:
    """The list's version: the same list, the same version, on any server."""
    return hashlib.sha256("\n".join(sorted(topics)).encode()).hexdigest()[:16]


async def has_topic_devices(session: AsyncSession, user_id: int) -> bool:
    return bool(await session.scalar(select(exists().where(
        UserDeviceTokens.user_id == user_id, UserDeviceTokens.supports_topics == True,  # noqa: E712
        UserDeviceTokens.is_deleted == False,  # noqa: E712
    ))))


async def forget(session: AsyncSession, user_id: int, topics: Iterable[str]) -> None:
    """Take the person's devices out of these topics, after the caller's
    commit (a job). Nothing when they have no device that subscribes."""
    topics = sorted(set(t for t in topics if t))
    if not topics or not await has_topic_devices(session, user_id):
        return
    await jobs.enqueue(session, "push.unsubscribe", {"user_id": int(user_id), "topics": topics})


@jobs.handler("push.unsubscribe", max_attempts=5, timeout=120.0)
async def _unsubscribe_job(p: dict) -> None:
    """The person's topic devices leave each topic -- unless it is back on
    their list by now (they followed again): the list is the truth."""
    from app.core.notifications import service as push

    user_id = int(p["user_id"])
    async with jobs.scope() as s:
        tokens = list((await s.scalars(
            select(UserDeviceTokens.fcm_token).where(
                UserDeviceTokens.user_id == user_id, UserDeviceTokens.supports_topics == True,  # noqa: E712
                UserDeviceTokens.is_deleted == False,  # noqa: E712
            )
        )).all())
        still = set(await topics_for(s, user_id))
        await s.commit()
    for topic in p.get("topics") or ():
        if topic not in still:
            await push.unsubscribe_from_topic(tokens, topic)


async def adoption(session: AsyncSession) -> dict:
    """How many live devices have said they subscribe to topics: the number
    to watch before PUSH_TOPICS goes on (and PUSH_TOPICS_LEGACY off)."""
    total, ready = (await session.execute(
        select(func.count(), func.count().filter(UserDeviceTokens.supports_topics == True))  # noqa: E712
        .select_from(UserDeviceTokens).where(UserDeviceTokens.is_deleted == False)  # noqa: E712
    )).one()
    by_platform = {
        platform: dict(devices=int(n), topics=int(r))
        for platform, n, r in (await session.execute(
            select(UserDeviceTokens.platform, func.count(), func.count().filter(UserDeviceTokens.supports_topics == True))  # noqa: E712
            .where(UserDeviceTokens.is_deleted == False)  # noqa: E712
            .group_by(UserDeviceTokens.platform)
        )).all()
    }
    return dict(devices=int(total), topic_devices=int(ready), share=round(ready / total, 4) if total else 0.0, by_platform=by_platform)


def settings_off(switch: str, user_id_column) -> sa.ColumnElement:
    """`user_id_column`'s person turned `switch` off (an EXISTS on their
    settings row; no row is every switch on)."""
    col = getattr(NotificationSettings, switch)
    return exists().where(NotificationSettings.user_id == user_id_column, col == False)  # noqa: E712


def topic_list_payload(topics: list[str], version: Optional[str] = None) -> dict:
    return dict(topics=topics, version=version or version_of(topics))
