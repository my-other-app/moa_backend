"""Threads: a club's posts, with the likes, comments, votes and views they earn.

A post is text, maybe a photo, maybe a poll. Followers are told when the
club asks, after the post is answered. The club's page lists its posts
pinned-first; a follower's feed lists every followed club's posts
newest-first. Counts live on the post so a page of feed is one query; the
rows behind them keep one-per-person honest, and the database moves the
counts with the rows (threads.models.THREAD_COUNTS_SQL): a like, a vote and a
comment are written with ON CONFLICT, so two taps at once are answered,
never a 500 or a count off by one. Views are counted in batches
(discover.visitors).

Only a listed club's posts are anyone's to read (clubs.listed: verified, not
deleted, not under a blocked organisation).
"""
from __future__ import annotations

import io
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, func, select, text, true, tuple_, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.discover import cursor as cursors, visitors
from app.api.reports import service as reports
from app.api.threads.models import ClubThreads, ThreadComments, ThreadLikes, ThreadPollOptions, ThreadVotes
from app.api.threads.schemas import MAX_BODY, MAX_OPTION, MAX_OPTIONS
from app.api.notifications.models import Notifications
from app.api.users.models import Users
from app.core import jobs
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: A list's total counts at most this many (past it: total_capped).
COUNT_CAP = 1000
#: Comments in one page under a post.
COMMENTS_PAGE = 200
#: Followers told about a post per batch of notifications and pushes.
TELL_BATCH = 1000


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


@asynccontextmanager
async def session_scope():
    """A session for work after the response (telling followers). Tests swap this for their own."""
    from app.api.deliveries import service as deliveries

    async with deliveries.session_scope() as session:
        yield session


# ─── Shaping ─────────────────────────────────────────────────────────────


async def _options_for(session, thread_ids: list[uuid.UUID], fresh: bool = False) -> dict[uuid.UUID, list[ThreadPollOptions]]:
    """Each post's poll options. `fresh`: re-read rows this session already
    holds (a trigger moved their counts)."""
    out: dict[uuid.UUID, list[ThreadPollOptions]] = {i: [] for i in thread_ids}
    if thread_ids:
        stmt = select(ThreadPollOptions).where(ThreadPollOptions.thread_id.in_(thread_ids)).order_by(ThreadPollOptions.sort_order, ThreadPollOptions.id)
        if fresh:
            stmt = stmt.execution_options(populate_existing=True)
        for o in (await session.scalars(stmt)).all():
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


async def _fresh_counts(session, threads: list[ClubThreads]) -> None:
    """Read the posts' counters again: the database moved them, not this session."""
    if threads:
        await session.scalars(select(ClubThreads).where(ClubThreads.id.in_([t.id for t in threads])).execution_options(populate_existing=True))


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
    """The post, and -- when the followers are to be told -- the job that
    tells them (`threads.tell_followers`), committed with it: a club with a
    lakh of followers posts as fast as one with ten."""
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
    if notify_followers:
        await jobs.enqueue(session, "threads.tell_followers", {"club_id": club.id, "thread_id": str(t.id)}, dedupe_key=f"threads:tell:{t.id}")
    await session.commit()
    await session.refresh(t)
    options = (await _options_for(session, [t.id]))[t.id]
    return _out(t, club, options, False, None)


@jobs.handler("threads.tell_followers", max_attempts=4, timeout=1800.0, concurrency=2)
async def _tell_job(p: dict) -> None:
    await tell_followers(int(p["club_id"]), uuid.UUID(str(p["thread_id"])))


async def tell_followers(club_id: int, thread_id) -> int:
    """A push to every follower, and a line in each inbox: after the post was
    answered, on a session of its own, TELL_BATCH followers at a time (read
    in id order through the club's follower index, never all at once).
    Followers who already have the post's line are skipped, so a run cut off
    by a deploy and run again goes on from where it stopped rather than
    telling anyone twice. Returns how many were told one by one. Never raises.

    With PUSH_TOPICS or INBOX_SHARED on, the post is a broadcast instead
    (app.core.notifications.fanout.announce): one inbox row and one send to
    club_{id}, written once however often this runs; 0 is returned."""
    from app.api.notifications.service import create_notifications_batch
    from app.core.notifications import delivery, fanout
    from app.core.notifications.service import send_notification_to_users
    from app.core.notifications.topics import settings_off
    from app.core.notifications.triggers import broadcasts_on

    told = 0
    try:
        async with session_scope() as session:
            club = await session.scalar(select(Clubs).where(Clubs.id == club_id))
            t = await session.scalar(select(ClubThreads).where(ClubThreads.id == thread_id, ClubThreads.is_deleted == False))  # noqa: E712
            if club is None or t is None:
                return 0
            preview = t.body if len(t.body) <= 140 else t.body[:137].rstrip() + "…"
            title = f"{club.name} asked a question" if t.kind == "poll" else f"{club.name} posted"
            data = {"type": "club_thread", "thread_id": str(t.id), "club_id": str(club.id)}
            if broadcasts_on():
                # One row and one topic send (fanout.announce); the devices
                # that still need it one by one are the push.devices jobs'.
                await fanout.announce(session, fanout.Broadcast(
                    kind="club_thread", ref=str(t.id), title=title, body=preview, data=data, push_data=data,
                    from_club_id=club.id, club_id=club.id,
                ), topic=delivery.club_topic(club.id))
                return 0
            last = 0
            while True:
                ids = list((await session.scalars(
                    select(ClubUsersLink.user_id).where(
                        ClubUsersLink.club_id == club.id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False,  # noqa: E712
                        ClubUsersLink.is_muted == False, ~settings_off("clubs", ClubUsersLink.user_id),  # noqa: E712
                        ClubUsersLink.user_id > last,
                    ).order_by(ClubUsersLink.user_id).limit(TELL_BATCH)
                )).all())
                if not ids:
                    break
                last = ids[-1]
                already = set((await session.scalars(
                    select(Notifications.user_id).where(
                        Notifications.user_id.in_(ids), Notifications.type == "club_thread", Notifications.from_club_id == club.id,
                        Notifications.data["thread_id"].as_string() == str(t.id),
                    )
                )).all())
                ids = [i for i in ids if i not in already]
                if not ids:
                    continue
                await create_notifications_batch(session, user_ids=ids, title=title, description=preview, type="club_thread", data=data, from_club_id=club.id)
                await session.commit()
                await send_notification_to_users(session, ids, title, preview, data=data)
                told += len(ids)
                if len(ids) < TELL_BATCH:
                    break
    except Exception:  # noqa: BLE001 — a post is never undone by a failed push
        logger.warning("followers of club %s could not all be told about thread %s (%d told)", club_id, thread_id, told, exc_info=True)
    return told


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


def _visitor(user, request) -> str | None:
    """Who is reading: their account, or today's hash of where they are and
    what they read with (discover.visitors). None with neither."""
    if user is not None:
        return f"u:{user.id}"
    anon = visitors.anonymous(request)
    return f"a:{anon}" if anon else None


async def _write_views(session, items: list[tuple]) -> None:
    """One statement for a batch of (post id, visitor): the rows a post does
    not have yet go in, and each post's views_count moves by how many did."""
    if not items:
        return
    await session.execute(
        text(
            """
            WITH v AS (SELECT * FROM unnest(CAST(:ids AS uuid[]), CAST(:who AS varchar[])) AS v(thread_id, visitor)),
            fresh AS (
              INSERT INTO thread_views (thread_id, visitor)
              SELECT v.thread_id, v.visitor FROM v JOIN club_threads t ON t.id = v.thread_id
              ON CONFLICT DO NOTHING
              RETURNING thread_id
            ),
            counted AS (SELECT thread_id, count(*) AS n FROM fresh GROUP BY thread_id)
            UPDATE club_threads t SET views_count = t.views_count + c.n FROM counted c WHERE t.id = c.thread_id
            """
        ),
        {"ids": [t for t, _ in items], "who": [v for _, v in items]},
    )


VIEWS = visitors.ViewBuffer("thread", _write_views)


async def _count_views(session, threads: list[ClubThreads], visitor: str | None) -> None:
    """One view per person per post (an anonymous reader: per day), however
    many times the page is opened: recorded, and written in a batch."""
    if not threads or visitor is None:
        return
    await VIEWS.record(session, [((t.id, visitor), (t.id, visitor)) for t in threads])
    await session.commit()
    if not VIEWS.enabled:
        await _fresh_counts(session, threads)


async def public_club(session, club_id: int) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.id == club_id, Clubs.listed == True))  # noqa: E712
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


FEED_ORDER = "feed"


async def feed(session, user, offset: int, limit: int, after: str | None = None) -> dict:
    """The newest posts of the clubs this person follows (and has not
    hidden), newest first.

    Each followed club gives at most the page's worth of its own newest posts,
    read through ix_club_threads_club_newest (a LATERAL top-k per club), and
    those are merged: the work is the followed clubs times the page, never
    every post they ever made. `after` (the `next_cursor` of the page before)
    starts after a post; `total` counts at most COUNT_CAP."""
    start = cursors.decode(after, FEED_ORDER, ["dt", "uuid"])
    followed = (
        select(ClubUsersLink.club_id.label("club_id"))
        .join(Clubs, Clubs.id == ClubUsersLink.club_id)
        .where(
            ClubUsersLink.user_id == user.id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False,  # noqa: E712
            ClubUsersLink.is_hidden == False, Clubs.listed == True,  # noqa: E712
        )
        .subquery("followed")
    )
    conds = [ClubThreads.club_id == followed.c.club_id, ClubThreads.is_deleted == False]  # noqa: E712
    if start is not None:
        offset = 0
        conds.append(cursors.after([(ClubThreads.created_at, True), (ClubThreads.id, False)], start))
    k = offset + limit + 1
    top = (
        select(ClubThreads.id.label("id"), ClubThreads.created_at.label("created_at"))
        .where(*conds).order_by(ClubThreads.created_at.desc(), ClubThreads.id).limit(k)
        .lateral("top")
    )
    page_rows = (await session.execute(
        select(top.c.id, top.c.created_at).select_from(followed).join(top, true())
        .order_by(top.c.created_at.desc(), top.c.id).offset(offset).limit(limit + 1)
    )).all()
    more = len(page_rows) > limit
    page_rows = page_rows[:limit]
    ids = [i for i, _ in page_rows]
    by_id = {t.id: t for t in (await session.scalars(select(ClubThreads).where(ClubThreads.id.in_(ids)))).all()} if ids else {}
    rows = [by_id[i] for i in ids if i in by_id]
    every = select(ClubThreads.id).where(
        ClubThreads.club_id.in_(select(followed.c.club_id)), ClubThreads.is_deleted == False,  # noqa: E712
    ).limit(COUNT_CAP + 1)
    total = int(await session.scalar(select(func.count()).select_from(every.subquery())) or 0)
    clubs = await _clubs_by_id(session, {t.club_id for t in rows})
    return dict(
        items=await _shape(session, rows, clubs, user.id),
        total=min(total, COUNT_CAP), total_capped=total > COUNT_CAP, limit=limit, offset=offset,
        next_cursor=cursors.encode(FEED_ORDER, [rows[-1].created_at, rows[-1].id]) if more and rows else None,
    )


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
    # Asked of the database: a block on the club's organisation changes `listed` behind any copy held here.
    club = await session.scalar(select(Clubs).where(Clubs.id == t.club_id, Clubs.listed == True))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="Post not found")
    return t, club


async def toggle_like(session, t: ClubThreads, user) -> dict:
    """Like, or take it back: the like's row is claimed or released in one
    statement each, and the trigger moves the count with it."""
    added = await session.scalar(
        pg_insert(ThreadLikes).values(thread_id=t.id, user_id=user.id)
        .on_conflict_do_nothing(index_elements=["thread_id", "user_id"]).returning(ThreadLikes.id)
    )
    if added is None:
        await session.execute(sa_delete(ThreadLikes).where(ThreadLikes.thread_id == t.id, ThreadLikes.user_id == user.id))
    likes = await session.scalar(select(ClubThreads.likes_count).where(ClubThreads.id == t.id))
    await session.commit()
    return dict(liked=added is not None, likes=int(likes or 0))


async def vote(session, t: ClubThreads, user, option_id) -> dict:
    """One vote per person; voting again moves it. The first vote is an
    insert ON CONFLICT; a change locks the person's vote row first, so two
    taps at once never count twice. The trigger moves the options' counts."""
    option = await session.scalar(select(ThreadPollOptions).where(ThreadPollOptions.id == _uuid(option_id), ThreadPollOptions.thread_id == t.id))
    if option is None:
        raise CustomHTTPException(404, message="That option is not on this poll")
    added = await session.scalar(
        pg_insert(ThreadVotes).values(thread_id=t.id, option_id=option.id, user_id=user.id)
        .on_conflict_do_nothing(index_elements=["thread_id", "user_id"]).returning(ThreadVotes.id)
    )
    if added is None:
        # Changing one's mind moves the vote; it never adds a second one.
        await session.execute(
            sa_update(ThreadVotes).where(ThreadVotes.thread_id == t.id, ThreadVotes.user_id == user.id, ThreadVotes.option_id != option.id)
            .values(option_id=option.id, updated_at=_now())
        )
    await session.commit()
    options = (await _options_for(session, [t.id], fresh=True))[t.id]
    return _poll(options, option.id)


# ─── Comments ────────────────────────────────────────────────────────────


async def _comment_out(c: ThreadComments, club: Clubs, users: dict[int, Users]) -> dict:
    if c.user_id == club.user_id:
        author = dict(id=c.user_id, name=club.name, is_club=True, avatar=club.logo)
    else:
        u = users.get(c.user_id)
        author = dict(id=c.user_id, name=(u.full_name if u and u.full_name else "A follower"), is_club=False, avatar=reports.avatar(u))
    return dict(id=str(c.id), body=c.body, author=author, created_at=_iso(c.created_at))


COMMENTS_ORDER = "comments"


async def comment_page(session, t: ClubThreads, club: Clubs, viewer_id: int | None = None, *, limit: int = COMMENTS_PAGE, after: str | None = None) -> tuple[list[dict], str | None]:
    """A page of the comments under a post, oldest first, as this viewer may
    see them: without anyone they have blocked, and without a comment whose
    reports weigh enough (app/api/reports) unless they wrote it. A page is a
    range of ix_thread_comments_thread_created after the cursor; returns the
    page and the next page's cursor (None at the end)."""
    start = cursors.decode(after, COMMENTS_ORDER, ["dt", "uuid"])
    stmt = select(ThreadComments).where(ThreadComments.thread_id == t.id, ThreadComments.is_deleted == False)  # noqa: E712
    if start is not None:
        stmt = stmt.where(tuple_(ThreadComments.created_at, ThreadComments.id) > tuple_(start[0], start[1]))
    rows = list((await session.scalars(stmt.order_by(ThreadComments.created_at, ThreadComments.id).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = cursors.encode(COMMENTS_ORDER, [rows[-1].created_at, rows[-1].id]) if more and rows else None
    blocked = await reports.blocked_by(session, viewer_id)
    hidden = await reports.hidden_comments(session, [c.id for c in rows])
    rows = [c for c in rows if c.user_id not in blocked and (c.id not in hidden or (viewer_id is not None and c.user_id == viewer_id))]
    users = await reports.people(session, {c.user_id for c in rows})
    return [await _comment_out(c, club, users) for c in rows], nxt


async def comments(session, t: ClubThreads, club: Clubs, viewer_id: int | None = None) -> list[dict]:
    """The first page of comments (comment_page)."""
    return (await comment_page(session, t, club, viewer_id))[0]


async def add_comment(session, t: ClubThreads, club: Clubs, user, body: str) -> dict:
    c = ThreadComments(thread_id=t.id, user_id=user.id, body=body.strip())
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return await _comment_out(c, club, await reports.people(session, [user.id]))


async def remove_comment(session, t: ClubThreads, club: Clubs, comment_id, *, by_user, as_club: bool) -> None:
    c = await session.scalar(select(ThreadComments).where(ThreadComments.id == _uuid(comment_id), ThreadComments.thread_id == t.id, ThreadComments.is_deleted == False))  # noqa: E712
    if c is None:
        raise CustomHTTPException(404, message="Comment not found")
    if not as_club and c.user_id != by_user.id:
        raise CustomHTTPException(403, message="Only the person who wrote it, or the club, can remove a comment")
    # The post's count follows by trigger, once, however many removals race.
    c.is_deleted = True
    c.deleted_at = _now()
    await session.commit()
