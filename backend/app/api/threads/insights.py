"""The club's posts as the Threads page needs them: which ones wait for a
reply, the page's scopes and sorts, and the figures above the list — reach,
engagement, what kind of post works.

Weeks are India's: Monday 00:00 to Sunday 23:59 in Asia/Kolkata, where the
clubs are. They used to be half UTC and half IST -- the series' keys and the
reactions in UTC, the posts by the IST timestamps the database hands back
(TZAwareDateTime) -- so from Monday 00:00 to 05:30 IST a post made that
morning sat in a week the chart did not show yet, and the latest week read
no posts beside its reactions.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column, select

from app.api.clubs.models import Clubs
from app.api.threads.models import ClubThreads, ThreadComments, ThreadLikes, ThreadVotes
from app.api.threads.service import _options_for, _out

SCOPES = ("all", "pinned", "polls", "photos", "awaiting")
SORTS = ("newest", "seen", "engaged")
WINDOW = timedelta(days=30)
#: Where a week starts and ends.
IST = ZoneInfo("Asia/Kolkata")
# Inline, not a bind parameter: Postgres matches the GROUP BY to the SELECT
# by its text (app/core/utils/time_buckets.py).
_IST_NAME = literal_column("'Asia/Kolkata'")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _reply_state(session, club: Clubs, thread_ids: list) -> dict:
    """Per post: when the last comment landed, and whether a follower spoke after the club last did."""
    out = {t: dict(last_comment_at=None, awaiting_reply=False) for t in thread_ids}
    if not thread_ids:
        return out
    rows = (await session.execute(
        select(ThreadComments.thread_id, ThreadComments.user_id, func.max(ThreadComments.created_at))
        .where(ThreadComments.thread_id.in_(thread_ids), ThreadComments.is_deleted == False)  # noqa: E712
        .group_by(ThreadComments.thread_id, ThreadComments.user_id)
    )).all()
    last_follower: dict = {}
    last_club: dict = {}
    for tid, uid, at in rows:
        if uid == club.user_id:
            last_club[tid] = max(last_club.get(tid, at), at)
        else:
            last_follower[tid] = max(last_follower.get(tid, at), at)
    for tid in thread_ids:
        f, c = last_follower.get(tid), last_club.get(tid)
        latest = max([x for x in (f, c) if x is not None], default=None)
        out[tid] = dict(last_comment_at=latest.isoformat() if latest else None, awaiting_reply=bool(f and (c is None or f > c)))
    return out


def _engagement(t: ClubThreads, votes: int) -> int:
    return (t.likes_count or 0) + (t.comments_count or 0) + votes


async def _rows(session, club: Clubs) -> list[ClubThreads]:
    return list((await session.scalars(select(ClubThreads).where(ClubThreads.club_id == club.id, ClubThreads.is_deleted == False).order_by(ClubThreads.created_at.desc()))).all())  # noqa: E712


async def list_for_page(session, club: Clubs, *, scope: str = "all", sort: str = "newest", offset: int = 0, limit: int = 10, viewer_id: Optional[int] = None) -> dict:
    rows = await _rows(session, club)
    ids = [t.id for t in rows]
    options = await _options_for(session, ids)
    states = await _reply_state(session, club, ids)
    votes = {t.id: sum(o.votes_count for o in options[t.id]) for t in rows}
    counts = dict(all=len(rows), pinned=sum(1 for t in rows if t.pinned), polls=sum(1 for t in rows if t.kind == "poll"), photos=sum(1 for t in rows if t.image), awaiting=sum(1 for t in rows if states[t.id]["awaiting_reply"]))
    if scope == "pinned":
        rows = [t for t in rows if t.pinned]
    elif scope == "polls":
        rows = [t for t in rows if t.kind == "poll"]
    elif scope == "photos":
        rows = [t for t in rows if t.image]
    elif scope == "awaiting":
        rows = [t for t in rows if states[t.id]["awaiting_reply"]]
    if sort == "seen":
        rows.sort(key=lambda t: (-(t.views_count or 0), -t.created_at.timestamp()))
    elif sort == "engaged":
        rows.sort(key=lambda t: (-_engagement(t, votes[t.id]), -t.created_at.timestamp()))
    else:
        rows.sort(key=lambda t: (not t.pinned, -t.created_at.timestamp()))
    page = rows[offset : offset + limit]
    items = []
    for t in page:
        d = _out(t, club, options[t.id], False, None)
        d.update(states[t.id])
        d["engagement"] = _engagement(t, votes[t.id])
        items.append(d)
    return dict(items=items, total=len(rows), limit=limit, offset=offset, counts=counts)


def _week(at: datetime) -> tuple[int, int]:
    """(ISO year, ISO week) of an instant, on Indian time."""
    return tuple(at.astimezone(IST).isocalendar()[:2])


async def _by_week(session, model, club: Clubs, since: datetime, *extra) -> dict:
    """{(ISO year, ISO week): how many} of `model`'s rows on the club's posts
    since `since`, the weeks cut on Indian time as `_week` cuts them."""
    week = func.date_trunc("week", func.timezone(_IST_NAME, model.created_at))
    rows = (await session.execute(
        select(week, func.count())
        .join(ClubThreads, ClubThreads.id == model.thread_id)
        .where(ClubThreads.club_id == club.id, model.created_at > since, *extra)
        .group_by(week)
    )).all()
    out: dict = defaultdict(int)
    for monday, n in rows:
        out[monday.isocalendar()[:2]] += int(n)
    return out


async def insights(session, club: Clubs) -> dict:
    now = _now()
    rows = await _rows(session, club)
    ids = [t.id for t in rows]
    options = await _options_for(session, ids)
    votes = {t.id: sum(o.votes_count for o in options[t.id]) for t in rows}
    states = await _reply_state(session, club, ids)

    def window(start: datetime, end: datetime):
        return [t for t in rows if start < t.created_at <= end]

    cur = window(now - WINDOW, now)
    prev = window(now - 2 * WINDOW, now - WINDOW)
    reach = lambda ts: sum(t.views_count or 0 for t in ts)  # noqa: E731
    engaged = lambda ts: sum(_engagement(t, votes[t.id]) for t in ts)  # noqa: E731

    def rate(ts) -> Optional[float]:
        v = reach(ts)
        return round(100 * engaged(ts) / v, 1) if v else None

    # Likes, comments and votes as they landed, per week, for the last twelve
    # weeks: counted by the database, a row per week, not a row per reaction.
    since = now - timedelta(weeks=12)
    likes_by_week = await _by_week(session, ThreadLikes, club, since)
    comments_by_week = await _by_week(session, ThreadComments, club, since, ThreadComments.is_deleted == False)  # noqa: E712
    votes_by_week = await _by_week(session, ThreadVotes, club, since)
    weeks = []
    today = now.astimezone(IST)
    for k in range(11, -1, -1):
        day = today - timedelta(weeks=k)
        key = _week(day)
        posted = [t for t in rows if _week(t.created_at) == key]
        weeks.append(dict(week=day.strftime("%Y-%m-%d"), posts=len(posted), reach=reach(posted), reactions=likes_by_week[key] + comments_by_week[key] + votes_by_week[key]))

    # What works: average reach by kind of post, and whether telling followers moved it.
    def avg(ts) -> Optional[int]:
        return round(reach(ts) / len(ts)) if ts else None

    kinds = [
        dict(kind="poll", label="Polls", posts=sum(1 for t in rows if t.kind == "poll"), avg_reach=avg([t for t in rows if t.kind == "poll"]), avg_engagement=round(engaged([t for t in rows if t.kind == "poll"]) / max(1, sum(1 for t in rows if t.kind == "poll")), 1) if any(t.kind == "poll" for t in rows) else None),
        dict(kind="photo", label="With a photo", posts=sum(1 for t in rows if t.kind != "poll" and t.image), avg_reach=avg([t for t in rows if t.kind != "poll" and t.image]), avg_engagement=round(engaged([t for t in rows if t.kind != "poll" and t.image]) / max(1, sum(1 for t in rows if t.kind != "poll" and t.image)), 1) if any(t.kind != "poll" and t.image for t in rows) else None),
        dict(kind="text", label="Text only", posts=sum(1 for t in rows if t.kind != "poll" and not t.image), avg_reach=avg([t for t in rows if t.kind != "poll" and not t.image]), avg_engagement=round(engaged([t for t in rows if t.kind != "poll" and not t.image]) / max(1, sum(1 for t in rows if t.kind != "poll" and not t.image)), 1) if any(t.kind != "poll" and not t.image for t in rows) else None),
    ]
    told = [t for t in rows if t.notify_followers]
    quiet = [t for t in rows if not t.notify_followers]
    best = max(cur, key=lambda t: (_engagement(t, votes[t.id]), t.views_count or 0), default=None)
    # Kept by a trigger on club_users_link: the follows that are following and not deleted.
    followers = int(await session.scalar(select(Clubs.follower_count).where(Clubs.id == club.id)) or 0)
    return dict(
        posts=len(cur), posts_delta=(len(cur) - len(prev)) if (cur or prev) else None,
        reach=reach(cur), reach_delta=(reach(cur) - reach(prev)) if (cur or prev) else None,
        reach_per_follower=round(reach(cur) / max(1, len(cur)) / followers, 2) if (followers and cur) else None,
        engagement_rate=rate(cur), engagement_rate_prev=rate(prev),
        awaiting=sum(1 for t in rows if states[t.id]["awaiting_reply"]),
        best=dict(id=str(best.id), body=best.body[:120], views=best.views_count or 0, engagement=_engagement(best, votes[best.id]), kind=best.kind) if best else None,
        weeks=weeks, kinds=kinds,
        notify=dict(told=dict(posts=len(told), avg_reach=avg(told)), quiet=dict(posts=len(quiet), avg_reach=avg(quiet))),
        followers=followers, all_time=dict(posts=len(rows), reach=reach(rows), engagement=engaged(rows)),
        generated_at=now.isoformat(),
    )
