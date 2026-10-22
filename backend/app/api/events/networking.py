"""
Event Networking Discovery — "People Like You"

Lets checked-in attendees discover others at the same event who share
their interests. Access is time-limited to event_start → event_end + 1hr.

Who is listed: the other guests checked in, except anyone who turned "Show
me in People Like You" off (users.networking_visible), anyone the caller
blocked or who blocked the caller (user_blocks, from Threads' report and
block), and accounts deleted or suspended since.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.events.models import Events, EventRegistrationsLink
from app.api.users.models import Users, UserProfiles, UserInterests
from app.api.interests.models import Interests
from app.api.orgs.models import Organizations
from app.api.reports.models import UserBlocks
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: Whether someone hidden from the list may still look at it. True: turning
#: yourself off only stops others seeing you. False makes it two-way: a
#: hidden person is refused the list too (403 NETWORKING_HIDDEN).
HIDDEN_CAN_BROWSE = True

#: Why the list is refused, for the app to say so in its own words.
NOT_CHECKED_IN = "NETWORKING_NOT_CHECKED_IN"
CLOSED = "NETWORKING_CLOSED"
HIDDEN = "NETWORKING_HIDDEN"


def is_within_networking_window(event: Events) -> tuple[bool, datetime | None]:
    """
    Check if the current time is within the networking window.
    Window: event_datetime → event_datetime + duration (hours) + 1hr buffer.
    Returns (is_valid, window_closes_at).
    """
    if not event.event_datetime or not event.duration:
        return False, None

    event_end = event.event_datetime + timedelta(hours=float(event.duration))
    window_closes_at = event_end + timedelta(hours=1)
    now = datetime.now(timezone.utc)

    return now <= window_closes_at, window_closes_at


def _picture(profile: UserProfiles | None) -> str | None:
    """A link to the person's picture: the photo they uploaded, else the
    avatar they picked (as reports.avatar). Only the photo was read, so
    everyone with an avatar showed no picture. `profile.avatar` must be
    loaded with the profile (selectinload): a lazy load fails here."""
    if profile is None:
        return None
    pic = profile.profile_pic or (profile.avatar.image if profile.avatar is not None else None)
    if isinstance(pic, dict):
        return pic.get("medium") or pic.get("thumbnail") or pic.get("original")
    if isinstance(pic, str):
        return pic
    return None


async def get_networking_attendees(
    session: AsyncSession,
    event_id: int,
    user_id: int,
) -> dict:
    """
    Returns interest-matched, checked-in attendees for an event.

    Rules:
    - Only accessible by checked-in users
    - Only checked-in users appear in results, and only those who show
    - Excludes the requesting user, and blocks either way
    - Sorted by shared interest count (descending)
    - Time-limited to event window + 1hr

    `visible` is the caller's own switch, for the page's toggle; `matches`
    counts the people sharing at least one interest.
    """

    # 1. Load the event
    event = await session.get(Events, event_id)
    if not event:
        raise CustomHTTPException(404, "Event not found.")

    # 2. Check time window
    within_window, window_closes_at = is_within_networking_window(event)
    if not within_window:
        raise CustomHTTPException(
            403,
            "Networking window has closed. It is only available during the event and 1 hour after.",
            error_code=CLOSED,
        )

    # 3. Verify requesting user is checked in
    my_reg = await session.scalar(
        select(EventRegistrationsLink.id).where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_attended == True,  # noqa: E712
            EventRegistrationsLink.is_deleted == False,  # noqa: E712
        ).limit(1)
    )
    if not my_reg:
        raise CustomHTTPException(
            403,
            "You must be checked in to this event to use networking.",
            error_code=NOT_CHECKED_IN,
        )

    visible = bool(await session.scalar(select(Users.networking_visible).where(Users.id == user_id)))
    if not visible and not HIDDEN_CAN_BROWSE:
        raise CustomHTTPException(
            403,
            "Turn on “Show me in People Like You” to see who else is here.",
            error_code=HIDDEN,
        )

    # 4. Get requesting user's interests
    my_interests_result = await session.execute(
        select(UserInterests.interest_id).where(
            UserInterests.user_id == user_id,
            UserInterests.is_deleted == False,  # noqa: E712
        )
    )
    my_interest_ids = set(row[0] for row in my_interests_result.all())

    # 5. Everyone else checked in who shows, blocks left out both ways. A
    # person with two registrations (a repeat guest) is listed once.
    blocked = select(UserBlocks.blocked_id).where(UserBlocks.blocker_id == user_id)
    blocked_me = select(UserBlocks.blocker_id).where(UserBlocks.blocked_id == user_id)
    others = (
        await session.scalars(
            select(EventRegistrationsLink.user_id)
            .join(Users, Users.id == EventRegistrationsLink.user_id)
            .where(
                EventRegistrationsLink.event_id == event_id,
                EventRegistrationsLink.is_attended == True,  # noqa: E712
                EventRegistrationsLink.is_deleted == False,  # noqa: E712
                EventRegistrationsLink.user_id != user_id,
                EventRegistrationsLink.user_id.not_in(blocked),
                EventRegistrationsLink.user_id.not_in(blocked_me),
                Users.is_deleted == False,  # noqa: E712
                Users.is_active == True,  # noqa: E712
                Users.networking_visible == True,  # noqa: E712
            )
        )
    ).all()
    attendee_user_ids = list(dict.fromkeys(others))

    result = {
        "event_id": event_id,
        "event_name": event.name,
        "window_closes_at": window_closes_at,
        "visible": visible,
        "attendees": [],
        "total": 0,
        "matches": 0,
    }
    if not attendee_user_ids:
        return result

    # 6. Build attendee list with interest matching

    # Fetch user profiles, with the college in the same query. It used to be
    # read afterwards as `profile.org`, a lazy load, which an async session
    # cannot do: the request died the moment any other checked-in guest had a
    # college on their profile, which is nearly every guest.
    profiles_result = await session.execute(
        select(Users, UserProfiles, Organizations.name)
        .outerjoin(UserProfiles, (Users.id == UserProfiles.user_id) & (UserProfiles.is_deleted == False))  # noqa: E712
        .outerjoin(Organizations, Organizations.id == UserProfiles.org_id)
        .where(Users.id.in_(attendee_user_ids))
        # The avatars too, in one more query for them all (_picture).
        .options(selectinload(UserProfiles.avatar))
    )
    user_profiles = {u.id: (u, p, org) for u, p, org in profiles_result.all()}

    # Fetch all interests for attendee users
    attendee_interests_result = await session.execute(
        select(UserInterests.user_id, Interests)
        .join(Interests, UserInterests.interest_id == Interests.id)
        .where(
            UserInterests.user_id.in_(attendee_user_ids),
            UserInterests.is_deleted == False,  # noqa: E712
        )
    )
    # Group interests by user
    user_interests_map: dict[int, list] = {}
    for uid, interest in attendee_interests_result.all():
        user_interests_map.setdefault(uid, []).append(interest)

    # 7. Build response with match counts
    attendees = []
    for uid in attendee_user_ids:
        user_data, profile_data, org_name = user_profiles.get(uid, (None, None, None))
        if not user_data:
            continue

        their_interests = user_interests_map.get(uid, [])
        their_interest_ids = {i.id for i in their_interests}
        shared_ids = my_interest_ids & their_interest_ids
        shared_interests = [i for i in their_interests if i.id in shared_ids]

        attendees.append({
            "user_id": uid,
            "name": user_data.full_name,
            "profile_pic_url": _picture(profile_data),
            "organization": org_name,
            "shared_interests": [
                {
                    "id": i.id,
                    "name": i.name,
                    "icon": i.icon,
                    "icon_type": i.icon_type.value if i.icon_type else None,
                }
                for i in shared_interests
            ],
            "match_count": len(shared_ids),
        })

    # Sort by match count descending, then name ascending
    attendees.sort(key=lambda a: (-a["match_count"], a["name"]))

    result["attendees"] = attendees
    result["total"] = len(attendees)
    result["matches"] = sum(1 for a in attendees if a["match_count"] > 0)
    return result
