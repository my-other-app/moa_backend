"""The personalised event feed behind GET /events/for-you.

Until now "For You" was `GET /events/list` with no arguments, which is every
event ever created ordered by date ascending -- so the first card was the
oldest event in the database, and a user scrolled through years of finished
events before reaching anything they could attend.

The ranking is a weighted sum evaluated in one query. Weights are module
constants so they can be tuned without touching the SQL, and every component
is bounded, so no single signal can run away with the ordering.
"""

from sqlalchemy import Float, Integer, and_, case, cast, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.events.models import (
    EventInterestsLink,
    EventRegistrationsLink,
    Events,
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


def _hours(column):
    """`duration` (a float number of hours) as a Postgres interval."""
    return column * text("INTERVAL '1 hour'")


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
    event_end = Events.event_datetime + _hours(Events.duration)

    # --- The user's own context, as scalar subqueries ------------------------
    # Subqueries rather than joins throughout: a join against several
    # one-to-many link tables multiplies rows and then needs a GROUP BY over
    # every selected column, which is both slower and a standing invitation
    # to a grouping bug.
    user_interest_ids = (
        select(UserInterests.interest_id)
        .where(
            UserInterests.user_id == user_id,
            UserInterests.is_deleted == False,
        )
        .scalar_subquery()
    )

    user_org_id = (
        select(UserProfiles.org_id)
        .where(UserProfiles.user_id == user_id)
        .limit(1)
        .scalar_subquery()
    )

    # How many of this event's interests the user shares.
    shared_interests = (
        select(func.count())
        .select_from(EventInterestsLink)
        .where(
            EventInterestsLink.event_id == Events.id,
            EventInterestsLink.is_deleted == False,
            EventInterestsLink.interest_id.in_(user_interest_ids),
        )
        .scalar_subquery()
    )

    # The user's relationship with this event's club, if any.
    def _club_flag(column):
        return (
            select(func.count())
            .select_from(ClubUsersLink)
            .where(
                ClubUsersLink.club_id == Events.club_id,
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_deleted == False,
                column == True,
            )
            .scalar_subquery()
        )

    is_pinned = _club_flag(ClubUsersLink.is_pinned)
    is_following = _club_flag(ClubUsersLink.is_following)

    # Behavioural: has this user turned up for this club, or this kind of
    # event, before? A registration is a stronger statement of taste than a
    # checkbox ticked once at onboarding.
    club_history = (
        select(func.count())
        .select_from(EventRegistrationsLink)
        .join(Events.__table__.alias("prev_event"), text("prev_event.id = event_registrations_link.event_id"))
        .where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_deleted == False,
            text("prev_event.club_id = events.club_id"),
        )
        .scalar_subquery()
    )

    category_history = (
        select(func.count())
        .select_from(EventRegistrationsLink)
        .join(Events.__table__.alias("prev_cat_event"), text("prev_cat_event.id = event_registrations_link.event_id"))
        .where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_deleted == False,
            text("prev_cat_event.category_id = events.category_id"),
        )
        .scalar_subquery()
    )

    registration_count = (
        select(func.count())
        .select_from(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == Events.id,
            EventRegistrationsLink.is_deleted == False,
        )
        .scalar_subquery()
    )

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

    query = (
        select(Events, score)
        .outerjoin(Clubs, Clubs.id == Events.club_id)
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
        .options(joinedload(Events.category), joinedload(Events.club))
        # Score first; event date and id break ties so that paging through the
        # feed cannot show the same event twice or skip one. Without the id,
        # two events scoring equally at the same instant order arbitrarily and
        # the boundary between pages shifts between requests.
        .order_by(score.desc(), Events.event_datetime.asc(), Events.id.asc())
        .limit(limit)
        .offset(offset)
    )

    result = await session.execute(query)
    rows = result.unique().all()
    return [row[0] for row in rows], [float(row[1]) for row in rows]
