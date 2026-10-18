"""The personalised event feed behind GET /events/for-you.

Until now "For You" was `GET /events/list` with no arguments, which is every
event ever created ordered by date ascending -- so the first card was the
oldest event in the database, and a user scrolled through years of finished
events before reaching anything they could attend.

The ranking is a weighted sum evaluated in one query. Weights are module
constants so they can be tuned without touching the SQL, and every component
is bounded, so no single signal can run away with the ordering.

The user's side of the score (their interests, their clubs, where they have
been) is read once per request as small grouped subqueries joined to the
candidates by key, not once per candidate event: with ten thousand upcoming
events, a correlated count per event and per signal was tens of thousands
of index probes, and the history counts had no index at all. Popularity reads
events.going_count, which a trigger keeps equal to the count it replaces.

Only a bounded set of candidates is scored (docs/SCALING.md, fix 3): scoring
every upcoming event in India read the whole events table at a lakh of
clubs (4 s). The candidates are a UNION of small LIMITed selects, each on an
index, that between them hold everything the score can put near the top:

- the soonest FOLLOWED_LIMIT upcoming events of the clubs the user follows
  or pinned (the club weights, 2.5 and 3.5, are the score's largest);
- the soonest INTEREST_LIMIT within INTEREST_DAYS sharing an interest;
- the POPULAR_LIMIT most popular in the user's region (everywhere, when
  where they are is not known), the same for everyone there, so worked out
  once a minute per region;
- the ONLINE_LIMIT most popular online events, the same for everyone.

They are scored with the same formula, and the interests they share with the
user are counted for them alone (not for every event ever held). With fewer
upcoming events than the limits, the candidates are every one of them, and
the feed is what scoring them all gave.

Each user's ranking (the first KEEP ids and scores) is kept KEEP_SECONDS in
cache.personal, and dropped when they follow, unfollow, pin or hide a club or
change their interests (`forget`). A kept ranking is checked against the rows
before it is served: an event since registered for, hidden, closed or over is
left out.

Variety: of the first SPREAD_OVER cards, at most PER_CLUB are one club's; a
club's further events wait until after them, in their order (`_spread`). A
club with forty events this month that a user follows would otherwise fill
the whole first screen.

The popular candidates are read off events.popularity (attention decayed by
age, discover.popularity) through its index, a page of the order rather than
a sort of everything on in the region.
"""

from array import array
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import ARRAY, Float, Integer, and_, any_, bindparam, case, cast, exists, func, literal, literal_column, or_, select, text, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.discover import cache, geo, upcoming
from app.api.events.models import (
    EventInterestsLink,
    EventRegistrationsLink,
    Events,
    ends_at_sql,
)
from app.api.interests.models import Interests
from app.api.users.models import UserInterests, UserProfiles

# --- Weights -----------------------------------------------------------------
# "Balanced": the most a user can earn from interests (3 x 2.5 = 7.5, and 1 for
# a sibling) is close to the most they can earn from club ties (3.5 + 2.5 =
# 6.0), so neither a
# topic match nor a club subscription can bury the other. Organisation,
# history, popularity and urgency are tiebreakers an order of magnitude
# smaller, there to order events that are otherwise equally relevant.
W_INTEREST = 2.5
INTEREST_CAP = 3  # beyond three shared interests, more tells us little
# An interest the user did not pick under a topic one of theirs is in (Web
# Development for someone who picked Cybersecurity): a weaker sign, counted
# once, so it lifts an event above an unrelated one and never above one
# that shares an interest outright.
W_SIBLING_INTEREST = 1.0
W_PINNED = 3.5
W_FOLLOWING = 2.5
W_SAME_ORG = 1.5
W_CLUB_HISTORY = 1.5
W_CATEGORY_HISTORY = 1.0
W_POPULARITY = 1.0
# Urgency is piecewise rather than continuous: the thing that matters is
# "this week" vs "this month", not the difference between 11 and 12 days.
URGENCY_BANDS = ((3, 1.5), (7, 1.0), (14, 0.5))
W_CLOSING_SOON = 0.75

# Registrations and page views both measure attention, but page views are
# cheap and plentiful while a registration is a real commitment, so one
# registration counts for more. Both go through log1p and a cap, so a single
# runaway event cannot dominate the feed.
POPULARITY_REGISTRATION_FACTOR = 5

# --- Candidates ----------------------------------------------------------------
FOLLOWED_LIMIT = 200
INTEREST_LIMIT = 200
INTEREST_DAYS = 30
POPULAR_LIMIT = 200
ONLINE_LIMIT = 100
# A region: a one-degree cell (about 110 km), and the events within
# REGION_KM of its centre, which takes in all of the cell and around it.
REGION_DEG = 1.0
REGION_KM = 150.0
# How much of each user's ranking is kept, and for how long.
KEEP = 200
KEEP_SECONDS = 300.0
# At most PER_CLUB of the first SPREAD_OVER cards come from one club.
PER_CLUB = 3
SPREAD_OVER = 20


def region_of(where: Optional[dict]) -> Optional[tuple[int, int]]:
    """The one-degree cell the user is in, from a point; None without one."""
    if not where or where.get("lat") is None or where.get("lng") is None:
        return None
    return (int(where["lat"] // REGION_DEG), int(where["lng"] // REGION_DEG))


def _actionable() -> list:
    """What the feed can show at all: what the public sees (published or
    live, not deleted, a listed club's -- verified, not deleted, not under a
    blocked organisation -- not over: the discover pages' rule; drafts and
    unverified clubs' events used to reach the feed), with registration
    still open. The statuses are written into the SQL so the partial indexes
    on them still apply."""
    now = func.now()
    return [
        Events.is_deleted == False,  # noqa: E712
        Events.lifecycle.in_(bindparam(None, [EventStatus.published, EventStatus.live], expanding=True, literal_execute=True)),
        Clubs.listed == True,  # noqa: E712
        Clubs.is_verified == True,  # noqa: E712
        Clubs.is_deleted == False,  # noqa: E712
        # Not finished. `duration` is hours after event_datetime.
        ends_at_sql(Events) >= now,
        # Registration still open. A null end date means no deadline was
        # set, which is open rather than closed.
        (Events.reg_enddate.is_(None)) | (Events.reg_enddate >= now),
    ]


def _shown(floor_hours: float) -> list:
    """_actionable, with the start bounded from below (discover.upcoming): no
    event still on started earlier, and date walks then start near now."""
    return _actionable() + [Events.event_datetime >= func.now() - timedelta(hours=floor_hours)]


def _attention():
    """Page views and registrations, a registration worth several views."""
    return cast(Events.page_views, Float) + POPULARITY_REGISTRATION_FACTOR * cast(Events.going_count, Float)


async def _most_attended(session, floor_hours: float, *extra, limit: int) -> list[int]:
    """The most popular events (events.popularity, on ix_events_shown_popular)."""
    rows = await session.scalars(
        select(Events.id).join(Clubs, Clubs.id == Events.club_id)
        .where(*_shown(floor_hours), *extra)
        .order_by(Events.popularity.desc(), Events.event_datetime, Events.id)
        .limit(limit)
    )
    return list(rows.all())


async def _popular_ids(session, region: Optional[tuple[int, int]], floor_hours: float, now: datetime) -> list[int]:
    """The POPULAR_LIMIT most popular events in the region (everywhere when
    None): the same for everyone, so kept for the minute (and for one more
    while it is worked out again in the background)."""
    extra = []
    if region is not None:
        centre = ((region[0] + 0.5) * REGION_DEG, (region[1] + 0.5) * REGION_DEG)
        extra = [Events.lat.isnot(None), Events.lng.isnot(None), geo.in_box_sql(geo.bbox(centre[0], centre[1], REGION_KM), Events.lat, Events.lng)]

    async def build(s, n):
        return await _most_attended(s, floor_hours if n is now else await upcoming.hours(s, n), *extra, limit=POPULAR_LIMIT)

    return await cache.kept(cache.shared, ("for-you-popular", region), build, session, now)


async def _online_ids(session, floor_hours: float, now: datetime) -> list[int]:
    """The ONLINE_LIMIT most popular online events, kept as the popular ones are."""
    async def build(s, n):
        return await _most_attended(s, floor_hours if n is now else await upcoming.hours(s, n), Events.is_online == True, limit=ONLINE_LIMIT)  # noqa: E712

    return await cache.kept(cache.shared, ("for-you-online",), build, session, now)


def _candidates(user_id: int, interests: list[int], floor_hours: float, listed: list[int]):
    """The ids to score, as a CTE: a UNION of the user's own small selects
    (followed and pinned clubs, shared interests) and the listed ids (the
    region's popular and the online ones). `interests` are the user's,
    written into the statement so the planner sees how common they are; with
    none, there is no interests arm (it would walk a month of events for
    nothing)."""
    now = func.now()
    followed = (
        select(Events.id)
        .join(Clubs, Clubs.id == Events.club_id)
        .join(ClubUsersLink, ClubUsersLink.club_id == Events.club_id)
        .where(
            ClubUsersLink.user_id == user_id, ClubUsersLink.is_deleted == False,  # noqa: E712
            or_(ClubUsersLink.is_following == True, ClubUsersLink.is_pinned == True),  # noqa: E712
            *_shown(floor_hours),
        )
        .order_by(Events.event_datetime, Events.id)
        .limit(FOLLOWED_LIMIT)
    )
    arms = [followed]
    if interests:
        shares = exists(
            select(EventInterestsLink.id).where(
                EventInterestsLink.event_id == Events.id,
                EventInterestsLink.is_deleted == False,  # noqa: E712
                EventInterestsLink.interest_id.in_(interests),
            )
        )
        arms.append(
            select(Events.id)
            .join(Clubs, Clubs.id == Events.club_id)
            .where(*_shown(floor_hours), Events.event_datetime < now + timedelta(days=INTEREST_DAYS), shares)
            .order_by(Events.event_datetime, Events.id)
            .limit(INTEREST_LIMIT)
        )
    if listed:
        arms.append(select(func.unnest(bindparam("for_you_listed", sorted(set(listed)), type_=ARRAY(Integer))).label("id")))
    return (union(*arms) if len(arms) > 1 else arms[0]).cte("for_you_candidates")


async def for_you_events(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
    where: Optional[dict] = None,
):
    """Rank upcoming events for one user. `where` ({lat, lng}, as the
    discover pages have it) picks the region whose popular events are
    candidates; without it, the most popular anywhere are.

    Returns `(events, scores)` so a caller can expose the score for debugging
    without a second pass.
    """
    region = region_of(where)
    if offset + limit > KEEP:
        # Past what is kept: worked out for this page alone.
        ids, scores, _ = await _ranked(session, user_id, region, offset + limit)
    else:
        made = []

        async def build():
            made.append(True)
            return await _ranked(session, user_id, region, KEEP, keep=True)

        key = ("for-you", user_id)
        got = await cache.personal.remember(key, build, ttl=KEEP_SECONDS)
        if got[0] != region:  # moved since: rank for where they are now
            got = await build()
            cache.personal.put(key, got, ttl=KEEP_SECONDS)
        ids, scores, clubs = got[1], got[2], got[3]
        if not made:
            ids, scores, clubs = await _still_for(session, user_id, ids, scores, clubs)
            # What dropped out may have moved a club's next event up into the first places.
            spread = _spread(list(zip(ids, scores, clubs)))
            ids, scores = [i for i, _, _ in spread], [s for _, s, _ in spread]
    ids, scores = list(ids[offset:offset + limit]), list(scores[offset:offset + limit])
    if not ids:
        return [], []
    loaded = await session.scalars(
        select(Events)
        .where(Events.id.in_(ids))
        .options(joinedload(Events.category), joinedload(Events.club))
    )
    by_id = {e.id: e for e in loaded.unique().all()}
    rows = [(by_id[event_id], score_) for event_id, score_ in zip(ids, scores) if event_id in by_id]
    return [e for e, _ in rows], [float(score_) for _, score_ in rows]


def forget(user_id: int) -> None:
    """Drop the user's kept ranking: what it rests on (their clubs, their interests) changed."""
    cache.personal.forget(("for-you", user_id))


async def _still_for(session, user_id: int, ids, scores, clubs):
    """A kept ranking without what can no longer be shown or acted on: the
    events of it that are over, closed, gone, registered for since, or from a
    club since hidden. Primary-key reads of at most KEEP events."""
    if not ids:
        return ids, scores, clubs
    registered = select(EventRegistrationsLink.id).where(
        EventRegistrationsLink.event_id == Events.id, EventRegistrationsLink.user_id == user_id, EventRegistrationsLink.is_deleted == False,  # noqa: E712
    ).exists()
    hidden = select(ClubUsersLink.id).where(
        ClubUsersLink.club_id == Events.club_id, ClubUsersLink.user_id == user_id,
        ClubUsersLink.is_hidden == True, ClubUsersLink.is_deleted == False,  # noqa: E712
    ).exists()
    alive = set((await session.scalars(
        select(Events.id).join(Clubs, Clubs.id == Events.club_id)
        .where(Events.id.in_(list(ids)), *_actionable(), ~registered, ~hidden)
    )).all())
    kept = [(i, s, c) for i, s, c in zip(ids, scores, clubs) if i in alive]
    return [i for i, _, _ in kept], [s for _, s, _ in kept], [c for _, _, c in kept]


async def _ranked(session, user_id: int, region, n: int, keep: bool = False):
    """The user's first `n` candidates by score, spread across clubs: (ids,
    scores, club ids), or (region, ids, scores, club ids) as arrays when
    `keep` (what cache.personal holds; a club id of 0 for none)."""
    py_now = datetime.now(timezone.utc)
    floor_hours = await upcoming.hours(session, py_now)
    listed = await _popular_ids(session, region, floor_hours, py_now) + await _online_ids(session, floor_hours, py_now)
    interests = list((await session.scalars(
        select(UserInterests.interest_id).where(UserInterests.user_id == user_id, UserInterests.is_deleted == False)  # noqa: E712
    )).all())
    ranked = _spread(await _score(session, user_id, floor_hours, listed, interests, n))
    ids, scores, clubs = [i for i, _, _ in ranked], [float(s) for _, s, _ in ranked], [c or 0 for _, _, c in ranked]
    if keep:
        return (region, array("q", ids), array("d", scores), array("q", clubs))
    return ids, scores, clubs


def _spread(ranked: list) -> list:
    """The ranking with no club holding more than PER_CLUB of the first
    SPREAD_OVER places: a club's events past its share there wait, in their
    order, and come right after them. Rows are (id, score, club id)."""
    head: list = []
    held: list = []
    per: dict = {}
    rest = list(ranked)
    while rest and len(head) < SPREAD_OVER:
        row = rest.pop(0)
        club = row[2]
        if club is not None and per.get(club, 0) >= PER_CLUB:
            held.append(row)
            continue
        per[club] = per.get(club, 0) + 1
        head.append(row)
    return head + held + rest


async def _score(session, user_id: int, floor_hours: float, listed: list[int], interests: list[int], n: int):
    """[(event id, score, club id)] of the user's candidates, best first, at most `n`."""
    now = func.now()

    # --- The user's own context, each read once ------------------------------
    # Joined to the candidates by key rather than correlated per event. A
    # plain join against several one-to-many link tables would multiply rows,
    # so each is first grouped down to one row per key.

    # The user's interests as one array, so the lookup below can use the
    # (interest_id, event_id) index rather than read every event's interests.
    user_interest_ids = func.coalesce(
        select(func.array_agg(UserInterests.interest_id))
        .where(
            UserInterests.user_id == user_id,
            UserInterests.is_deleted == False,
        )
        .scalar_subquery(),
        cast(literal_column("'{}'"), ARRAY(Integer)),
    )

    user_org_id = (
        select(UserProfiles.org_id)
        .where(UserProfiles.user_id == user_id)
        .limit(1)
        .scalar_subquery()
    )

    candidates = _candidates(user_id, interests, floor_hours, listed)

    # How many of each candidate's interests the user shares.
    shared = (
        select(EventInterestsLink.event_id, func.count().label("n"))
        .where(
            EventInterestsLink.is_deleted == False,
            EventInterestsLink.interest_id == any_(user_interest_ids),
            EventInterestsLink.event_id.in_(select(candidates.c.id)),
        )
        .group_by(EventInterestsLink.event_id)
        .subquery("me_shared_interests")
    )

    # And how many interests each has that the user did not pick, under a
    # topic of one they did (the topics: the categories of their few interests).
    mine = Interests.__table__.alias("me_interests")
    my_topics = select(mine.c.category_id).where(mine.c.id == any_(user_interest_ids), mine.c.is_deleted == False)  # noqa: E712
    sibling = (
        select(EventInterestsLink.event_id, func.count().label("n"))
        .join(Interests, Interests.id == EventInterestsLink.interest_id)
        .where(
            EventInterestsLink.is_deleted == False,  # noqa: E712
            EventInterestsLink.event_id.in_(select(candidates.c.id)),
            ~(EventInterestsLink.interest_id == any_(user_interest_ids)),
            Interests.is_deleted == False,  # noqa: E712
            Interests.category_id.in_(my_topics),
        )
        .group_by(EventInterestsLink.event_id)
        .subquery("me_sibling_interests")
    )

    # The user's relationship with each club they have one with: 1 or 0.
    def _flag(column):
        return func.max(case((column == True, 1), else_=0))

    clubs_of_mine = (
        select(
            ClubUsersLink.club_id,
            _flag(ClubUsersLink.is_pinned).label("pinned"),
            _flag(ClubUsersLink.is_following).label("following"),
        )
        .where(ClubUsersLink.user_id == user_id, ClubUsersLink.is_deleted == False)
        .group_by(ClubUsersLink.club_id)
        .subquery("me_clubs")
    )

    # Behavioural: has this user turned up for this club, or this kind of
    # event, before? A registration is a stronger statement of taste than a
    # checkbox ticked once at onboarding. (Any earlier event counts, deleted
    # or not, as it always has: hence the plain table alias.)
    def _history(column_name: str, label: str):
        prev = Events.__table__.alias(f"prev_{label}")
        key = prev.c[column_name]
        return (
            select(key.label("key"), func.count().label("n"))
            .select_from(EventRegistrationsLink)
            .join(prev, prev.c.id == EventRegistrationsLink.event_id)
            .where(
                EventRegistrationsLink.user_id == user_id,
                EventRegistrationsLink.is_deleted == False,
            )
            .group_by(key)
            .subquery(f"me_{label}_history")
        )

    club_history_rows = _history("club_id", "club")
    category_history_rows = _history("category_id", "category")

    shared_interests = func.coalesce(shared.c.n, 0)
    is_pinned = func.coalesce(clubs_of_mine.c.pinned, 0)
    is_following = func.coalesce(clubs_of_mine.c.following, 0)
    club_history = func.coalesce(club_history_rows.c.n, 0)
    category_history = func.coalesce(category_history_rows.c.n, 0)
    # Kept by a trigger on event_registrations_link: the count of rows that
    # are not deleted, which is what was counted here per event.
    registration_count = Events.going_count

    # --- Score components ----------------------------------------------------
    interest_score = W_INTEREST * func.least(shared_interests, INTEREST_CAP) + (
        W_SIBLING_INTEREST * func.least(func.coalesce(sibling.c.n, 0), 1)
    )

    club_score = (
        W_PINNED * func.least(is_pinned, 1) + W_FOLLOWING * func.least(is_following, 1)
    )

    org_score = W_SAME_ORG * case(
        (and_(Clubs.org_id.isnot(None), Clubs.org_id == user_org_id), 1),
        else_=0,
    )

    history_score = W_CLUB_HISTORY * func.least(club_history, 1) + (
        W_CATEGORY_HISTORY * func.least(category_history, 1)
    )

    # log1p keeps a viral event from flattening everything else, and the two
    # attention signals are summed before the log so they cannot each
    # contribute a full point.
    attention = cast(Events.page_views, Float) + (
        POPULARITY_REGISTRATION_FACTOR * cast(registration_count, Float)
    )
    popularity_score = W_POPULARITY * func.least(func.ln(1.0 + attention) / 6.0, 1.0)

    days_until = func.extract("epoch", Events.event_datetime - now) / 86400.0
    urgency_score = case(
        *[(days_until <= days, literal(points)) for days, points in URGENCY_BANDS],
        else_=literal(0.0),
    )

    closing_soon = W_CLOSING_SOON * case(
        (
            and_(
                Events.reg_enddate.isnot(None),
                Events.reg_enddate >= now,
                Events.reg_enddate <= now + text("INTERVAL '3 days'"),
            ),
            1,
        ),
        else_=0,
    )

    score = (
        interest_score
        + club_score
        + org_score
        + history_score
        + popularity_score
        + urgency_score
        + closing_soon
    ).label("score")

    # --- Hard filters --------------------------------------------------------
    # Everything here is something the user cannot act on, so it is removed
    # rather than demoted: a feed of things you cannot do is noise.
    already_registered = (
        select(EventRegistrationsLink.id)
        .where(
            EventRegistrationsLink.event_id == Events.id,
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_deleted == False,
        )
        .exists()
    )

    club_hidden = (
        select(ClubUsersLink.id)
        .where(
            ClubUsersLink.club_id == Events.club_id,
            ClubUsersLink.user_id == user_id,
            ClubUsersLink.is_hidden == True,
            ClubUsersLink.is_deleted == False,
        )
        .exists()
    )

    # Ranked on narrow rows (the id and the score); only the page's events
    # are then read in full.
    query = (
        select(Events.id, score, Events.club_id)
        .outerjoin(Clubs, Clubs.id == Events.club_id)
        .outerjoin(shared, shared.c.event_id == Events.id)
        .outerjoin(sibling, sibling.c.event_id == Events.id)
        .outerjoin(clubs_of_mine, clubs_of_mine.c.club_id == Events.club_id)
        .outerjoin(club_history_rows, club_history_rows.c.key == Events.club_id)
        .outerjoin(category_history_rows, category_history_rows.c.key == Events.category_id)
        .where(
            Events.id.in_(select(candidates.c.id)),
            # Drafts and unverified clubs' events never reach the feed; nor
            # what is over or closed for registration (_shown).
            *_shown(floor_hours),
            ~already_registered,
            ~club_hidden,
        )
        # Score first; event date and id break ties so that paging through the
        # feed cannot show the same event twice or skip one. Without the id,
        # two events scoring equally at the same instant order arbitrarily and
        # the boundary between pages shifts between requests.
        .order_by(score.desc(), Events.event_datetime.asc(), Events.id.asc())
        .limit(n)
    )
    return (await session.execute(query)).all()
