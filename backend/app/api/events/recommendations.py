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
"""

from sqlalchemy import ARRAY, Float, Integer, and_, any_, case, cast, func, literal, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.events.models import (
    EventInterestsLink,
    EventRegistrationsLink,
    Events,
    ends_at_sql,
)
from app.api.users.models import UserInterests, UserProfiles

# --- Weights -----------------------------------------------------------------
# "Balanced": the most a user can earn from interests (3 x 2.5 = 7.5) is close
# to the most they can earn from club ties (3.5 + 2.5 = 6.0), so neither a
# topic match nor a club subscription can bury the other. Organisation,
# history, popularity and urgency are tiebreakers an order of magnitude
# smaller, there to order events that are otherwise equally relevant.
W_INTEREST = 2.5
INTEREST_CAP = 3  # beyond three shared interests, more tells us little
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


async def for_you_events(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
):
    """Rank upcoming events for one user.

    Returns `(events, scores)` so a caller can expose the score for debugging
    without a second pass.
    """
    now = func.now()
    # The same instant as event_datetime + duration hours, in the form
    # ix_events_ends_at indexes.
    event_end = ends_at_sql(Events)

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

    # How many of each event's interests the user shares.
    shared = (
        select(EventInterestsLink.event_id, func.count().label("n"))
        .where(
            EventInterestsLink.is_deleted == False,
            EventInterestsLink.interest_id == any_(user_interest_ids),
        )
        .group_by(EventInterestsLink.event_id)
        .subquery("me_shared_interests")
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
    interest_score = W_INTEREST * func.least(shared_interests, INTEREST_CAP)

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
        select(Events.id, score)
        .outerjoin(Clubs, Clubs.id == Events.club_id)
        .outerjoin(shared, shared.c.event_id == Events.id)
        .outerjoin(clubs_of_mine, clubs_of_mine.c.club_id == Events.club_id)
        .outerjoin(club_history_rows, club_history_rows.c.key == Events.club_id)
        .outerjoin(category_history_rows, category_history_rows.c.key == Events.category_id)
        .where(
            Events.is_deleted == False,
            # Not finished. `duration` is hours after event_datetime.
            event_end >= now,
            # Registration still open. A null end date means no deadline was
            # set, which is open rather than closed.
            (Events.reg_enddate.is_(None)) | (Events.reg_enddate >= now),
            ~already_registered,
            ~club_hidden,
        )
        # Score first; event date and id break ties so that paging through the
        # feed cannot show the same event twice or skip one. Without the id,
        # two events scoring equally at the same instant order arbitrarily and
        # the boundary between pages shifts between requests.
        .order_by(score.desc(), Events.event_datetime.asc(), Events.id.asc())
        .limit(limit)
        .offset(offset)
    )

    ranked = (await session.execute(query)).all()
    if not ranked:
        return [], []
    loaded = await session.scalars(
        select(Events)
        .where(Events.id.in_([event_id for event_id, _ in ranked]))
        .options(joinedload(Events.category), joinedload(Events.club))
    )
    by_id = {e.id: e for e in loaded.unique().all()}
    rows = [(by_id[event_id], score_) for event_id, score_ in ranked if event_id in by_id]
    return [e for e, _ in rows], [float(score_) for _, score_ in rows]
