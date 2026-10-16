"""Threads: a club's posts, with the likes, comments, votes and views they earn.

A post is text, maybe a photo, maybe a poll. Followers are told when the
club asks. The club's page lists its posts pinned-first; a follower's feed
lists every followed club's posts newest-first. Counts live on the post so
a page of feed is one query; the rows behind them keep one-per-person
honest.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.threads.models import ClubThreads, ThreadComments, ThreadLikes, ThreadPollOptions, ThreadViews, ThreadVotes
from app.api.threads.schemas import MAX_BODY, MAX_OPTION, MAX_OPTIONS
from app.api.users.models import Users
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _uuid(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise CustomHTTPException(404, message="Not found")


# ─── Shaping ─────────────────────────────────────────────────────────────


async def _options_for(session, thread_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[ThreadPollOptions]]:
    out: dict[uuid.UUID, list[ThreadPollOptions]] = {i: [] for i in thread_ids}
    if thread_ids:
        for o in (await session.scalars(select(ThreadPollOptions).where(ThreadPollOptions.thread_id.in_(thread_ids)).order_by(ThreadPollOptions.sort_order, ThreadPollOptions.id))).all():
            out[o.thread_id].append(o)
    return out


async def _mine(session, thread_ids: list[uuid.UUID], user_id: int | None) -> tuple[set, dict]:
    """What this person did: which posts they liked, and which option they chose."""
    if not thread_ids or user_id is None:
        return set(), {}
    liked = set((await session.scalars(select(ThreadLikes.thread_id).where(ThreadLikes.thread_id.in_(thread_ids), ThreadLikes.user_id == user_id))).all())
    voted = {t: o for t, o in (await session.execute(select(ThreadVotes.thread_id, ThreadVotes.option_id).where(ThreadVotes.thread_id.in_(thread_ids), ThreadVotes.user_id == user_id))).all()}
    return liked, voted


def _poll(options: list[ThreadPollOptions], voted_option) -> dict | None:
    if not options:
        return None
    total = sum(o.votes_count for o in options)
    return dict(
        options=[dict(id=str(o.id), label=o.label, votes=o.votes_count, share=round(o.votes_count * 100 / total) if total else 0) for o in options],
        votes=total,
        voted_option_id=str(voted_option) if voted_option else None,
    )


def _club_ref(club: Clubs) -> dict:
    return dict(id=club.id, name=club.name, slug=club.slug, logo=club.logo)


def _out(t: ClubThreads, club: Clubs, options: list[ThreadPollOptions], liked: bool, voted_option) -> dict:
    return dict(
        id=str(t.id), club=_club_ref(club), body=t.body, image=t.image, kind=t.kind, poll=_poll(options, voted_option),
        pinned=t.pinned, notify_followers=t.notify_followers, likes=t.likes_count, comments=t.comments_count, views=t.views_count,
        liked=liked, created_at=_iso(t.created_at),
    )


async def _shape(session, threads: list[ClubThreads], clubs: dict[int, Clubs], user_id: int | None) -> list[dict]:
    ids = [t.id for t in threads]
    options = await _options_for(session, ids)
    liked, voted = await _mine(session, ids, user_id)
    return [_out(t, clubs[t.club_id], options[t.id], t.id in liked, voted.get(t.id)) for t in threads]


async def _clubs_by_id(session, ids: set[int]) -> dict[int, Clubs]:
    if not ids:
        return {}
    return {c.id: c for c in (await session.scalars(select(Clubs).where(Clubs.id.in_(ids)))).all()}


# ─── The club's own posts ────────────────────────────────────────────────


async def list_for_club(session, club: Clubs, offset: int, limit: int, viewer_id: int | None = None) -> dict:
    base = select(ClubThreads).where(ClubThreads.club_id == club.id, ClubThreads.is_deleted == False)  # noqa: E712
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = list((await session.scalars(base.order_by(ClubThreads.pinned.desc(), ClubThreads.created_at.desc(), ClubThreads.id).offset(offset).limit(limit))).all())
    return dict(items=await _shape(session, rows, {club.id: club}, viewer_id), total=total or 0, limit=limit, offset=offset)


def parse_options(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        raise CustomHTTPException(422, message="poll_options must be a JSON list of strings")
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise CustomHTTPException(422, message="poll_options must be a JSON list of strings")
    labels = [x.strip() for x in data if x.strip()]
    if len(labels) < 2:
        raise CustomHTTPException(422, message="A poll needs at least two options")
    if len(labels) > MAX_OPTIONS:
        raise CustomHTTPException(422, message=f"A poll takes at most {MAX_OPTIONS} options")
    if any(len(x) > MAX_OPTION for x in labels):
        raise CustomHTTPException(422, message=f"Keep each option under {MAX_OPTION} characters")
    if len({x.lower() for x in labels}) != len(labels):
        raise CustomHTTPException(422, message="Two options say the same thing")
    return labels


async def create(session, club: Clubs, user, *, body: str, notify_followers: bool, poll_options: list[str], image) -> dict:
    body = (body or "").strip()
    if not body:
        raise CustomHTTPException(422, message="Write something first")
    if len(body) > MAX_BODY:
        raise CustomHTTPException(422, message=f"Keep it under {MAX_BODY} characters")
    t = ClubThreads(club_id=club.id, body=body, kind="poll" if poll_options else "post", notify_followers=notify_followers, created_by=user.id)
    if image is not None:
        if not (image.content_type or "").startswith("image/"):
            raise CustomHTTPException(422, message="The photo must be an image")
        t.image = {"bytes": io.BytesIO(await image.read()), "filename": image.filename}
    session.add(t)
    await session.flush()
    for i, label in enumerate(poll_options):
        session.add(ThreadPollOptions(thread_id=t.id, label=label, sort_order=i))
    await session.commit()
    await session.refresh(t)
    if notify_followers:
        await _tell_followers(session, club, t)
    options = (await _options_for(session, [t.id]))[t.id]
    return _out(t, club, options, False, None)


async def _tell_followers(session, club: Clubs, t: ClubThreads) -> None:
    """A push to every follower, and a line in each inbox. Never blocks the post."""
    try:
        from app.api.notifications.service import create_notifications_batch
        from app.core.notifications.service import send_notification_to_users

        ids = list((await session.scalars(select(ClubUsersLink.user_id).where(ClubUsersLink.club_id == club.id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False))).all())  # noqa: E712
        if not ids:
            return
        preview = t.body if len(t.body) <= 140 else t.body[:137].rstrip() + "…"
        title = f"{club.name} asked a question" if t.kind == "poll" else f"{club.name} posted"
        data = {"type": "club_thread", "thread_id": str(t.id), "club_id": str(club.id)}
        await create_notifications_batch(session, user_ids=ids, title=title, description=preview, type="club_thread", data=data, from_club_id=club.id)
        await send_notification_to_users(session, ids, title, preview, data=data)
    except Exception:
        logger.warning("followers of club %s could not be told about thread %s", club.id, t.id, exc_info=True)


async def thread_for_club(session, club: Clubs, thread_id) -> ClubThreads:
    t = await session.scalar(select(ClubThreads).where(ClubThreads.id == _uuid(thread_id), ClubThreads.club_id == club.id, ClubThreads.is_deleted == False))  # noqa: E712
    if t is None:
        raise CustomHTTPException(404, message="Post not found")
    return t


async def update(session, club: Clubs, t: ClubThreads, *, pinned: bool | None, body: str | None) -> dict:
    if pinned is not None:
        if pinned:
            # One pinned post at a time: it is the top of the page, not a list.
            for other in (await session.scalars(select(ClubThreads).where(ClubThreads.club_id == club.id, ClubThreads.pinned == True, ClubThreads.id != t.id))).all():  # noqa: E712
                other.pinned = False
        t.pinned = pinned
    if body is not None:
        t.body = body.strip()
    await session.commit()
    await session.refresh(t)
    options = (await _options_for(session, [t.id]))[t.id]
    return _out(t, club, options, False, None)


async def delete(session, t: ClubThreads) -> None:
    t.is_deleted = True
    t.deleted_at = _now()
    t.pinned = False
    await session.commit()


# ─── Readers: the public page and the followers' feed ───────────────────


def _visitor(user, request) -> str:
    if user is not None:
        return f"u:{user.id}"
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "")
    ua = request.headers.get("user-agent") or ""
    return "a:" + hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:40]


async def _count_views(session, threads: list[ClubThreads], visitor: str) -> None:
    """One view per person per post, however many times the page is opened."""
    if not threads:
        return
    stmt = pg_insert(ThreadViews).values([dict(thread_id=t.id, visitor=visitor) for t in threads]).on_conflict_do_nothing(index_elements=["thread_id", "visitor"]).returning(ThreadViews.thread_id)
    fresh = set((await session.scalars(stmt)).all())
    for t in threads:
        if t.id in fresh:
            t.views_count += 1
    await session.commit()


async def public_club(session, club_id: int) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.id == club_id, Clubs.is_deleted == False, Clubs.is_verified == True))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="Club not found")
    return club


async def list_public(session, club: Clubs, user, request, offset: int, limit: int) -> dict:
    page = await list_for_club(session, club, offset, limit, viewer_id=user.id if user else None)
    ids = [uuid.UUID(x["id"]) for x in page["items"]]
    if ids and getattr(user, "id", None) != club.user_id:
        rows = list((await session.scalars(select(ClubThreads).where(ClubThreads.id.in_(ids)))).all())
        await _count_views(session, rows, _visitor(user, request))
        counted = {t.id: t.views_count for t in rows}
        for x in page["items"]:
            x["views"] = counted.get(uuid.UUID(x["id"]), x["views"])
    return page


async def feed(session, user, offset: int, limit: int) -> dict:
    followed = select(ClubUsersLink.club_id).where(ClubUsersLink.user_id == user.id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False, ClubUsersLink.is_hidden == False)  # noqa: E712
    base = select(ClubThreads).join(Clubs, Clubs.id == ClubThreads.club_id).where(ClubThreads.club_id.in_(followed), ClubThreads.is_deleted == False, Clubs.is_deleted == False, Clubs.is_verified == True)  # noqa: E712
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = list((await session.scalars(base.order_by(ClubThreads.created_at.desc(), ClubThreads.id).offset(offset).limit(limit))).all())
    clubs = await _clubs_by_id(session, {t.club_id for t in rows})
    return dict(items=await _shape(session, rows, clubs, user.id), total=total or 0, limit=limit, offset=offset)


async def one_public(session, thread_id, user, request) -> dict:
    """One post as its reader sees it — what a tapped notification opens. Counts the view like the club page does."""
    t, club = await live_thread(session, thread_id)
    item = (await _shape(session, [t], {club.id: club}, getattr(user, "id", None)))[0]
    if getattr(user, "id", None) != club.user_id:
        await _count_views(session, [t], _visitor(user, request))
        item["views"] = t.views_count
    return item


async def live_thread(session, thread_id) -> tuple[ClubThreads, Clubs]:
    t = await session.scalar(select(ClubThreads).where(ClubThreads.id == _uuid(thread_id), ClubThreads.is_deleted == False))  # noqa: E712
    if t is None:
        raise CustomHTTPException(404, message="Post not found")
    club = await session.get(Clubs, t.club_id)
    if club is None or club.is_deleted or not club.is_verified:
        raise CustomHTTPException(404, message="Post not found")
    return t, club


async def toggle_like(session, t: ClubThreads, user) -> dict:
    row = await session.scalar(select(ThreadLikes).where(ThreadLikes.thread_id == t.id, ThreadLikes.user_id == user.id))
    if row is None:
        session.add(ThreadLikes(thread_id=t.id, user_id=user.id))
        t.likes_count += 1
        liked = True
    else:
        await session.delete(row)
        t.likes_count = max(0, t.likes_count - 1)
        liked = False
    await session.commit()
    return dict(liked=liked, likes=t.likes_count)


async def vote(session, t: ClubThreads, user, option_id) -> dict:
    option = await session.scalar(select(ThreadPollOptions).where(ThreadPollOptions.id == _uuid(option_id), ThreadPollOptions.thread_id == t.id))
    if option is None:
        raise CustomHTTPException(404, message="That option is not on this poll")
    existing = await session.scalar(select(ThreadVotes).where(ThreadVotes.thread_id == t.id, ThreadVotes.user_id == user.id))
    if existing is None:
        session.add(ThreadVotes(thread_id=t.id, option_id=option.id, user_id=user.id))
        option.votes_count += 1
    elif existing.option_id != option.id:
        # Changing one's mind moves the vote; it never adds a second one.
        old = await session.get(ThreadPollOptions, existing.option_id)
        if old is not None:
            old.votes_count = max(0, old.votes_count - 1)
        existing.option_id = option.id
        option.votes_count += 1
    await session.commit()
    options = (await _options_for(session, [t.id]))[t.id]
    return _poll(options, option.id)


# ─── Comments ────────────────────────────────────────────────────────────


async def _comment_out(c: ThreadComments, club: Clubs, users: dict[int, Users]) -> dict:
    if c.user_id == club.user_id:
        author = dict(id=c.user_id, name=club.name, is_club=True)
    else:
        u = users.get(c.user_id)
        author = dict(id=c.user_id, name=(u.full_name if u and u.full_name else "A follower"), is_club=False)
    return dict(id=str(c.id), body=c.body, author=author, created_at=_iso(c.created_at))


async def comments(session, t: ClubThreads, club: Clubs) -> list[dict]:
    rows = list((await session.scalars(select(ThreadComments).where(ThreadComments.thread_id == t.id, ThreadComments.is_deleted == False).order_by(ThreadComments.created_at, ThreadComments.id))).all())  # noqa: E712
    ids = {c.user_id for c in rows if c.user_id is not None}
    users = {u.id: u for u in (await session.scalars(select(Users).where(Users.id.in_(ids)))).all()} if ids else {}
    return [await _comment_out(c, club, users) for c in rows]


async def add_comment(session, t: ClubThreads, club: Clubs, user, body: str) -> dict:
    c = ThreadComments(thread_id=t.id, user_id=user.id, body=body.strip())
    session.add(c)
    t.comments_count += 1
    await session.commit()
    await session.refresh(c)
    users = {user.id: user}
    return await _comment_out(c, club, users)


async def remove_comment(session, t: ClubThreads, club: Clubs, comment_id, *, by_user, as_club: bool) -> None:
    c = await session.scalar(select(ThreadComments).where(ThreadComments.id == _uuid(comment_id), ThreadComments.thread_id == t.id, ThreadComments.is_deleted == False))  # noqa: E712
    if c is None:
        raise CustomHTTPException(404, message="Comment not found")
    if not as_club and c.user_id != by_user.id:
        raise CustomHTTPException(403, message="Only the person who wrote it, or the club, can remove a comment")
    c.is_deleted = True
    c.deleted_at = _now()
    t.comments_count = max(0, t.comments_count - 1)
    await session.commit()

