"""
Event Networking Discovery — "People Like You"

Lets checked-in attendees discover others at the same event who share
their interests. Access is time-limited to event_start → event_end + 1hr.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.events.models import Events, EventRegistrationsLink
from app.api.users.models import Users, UserProfiles, UserInterests
from app.api.interests.models import Interests
from app.api.orgs.models import Organizations
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


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


async def get_networking_attendees(
    session: AsyncSession,
    event_id: int,
    user_id: int,
) -> dict:
    """
    Returns interest-matched, checked-in attendees for an event.

    Rules:
    - Only accessible by checked-in users
    - Only checked-in users appear in results
    - Excludes the requesting user
    - Sorted by shared interest count (descending)
    - Time-limited to event window + 1hr
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
        )

    # 3. Verify requesting user is checked in
    my_reg = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_attended == True,
            EventRegistrationsLink.is_deleted == False,
        )
    )
    if not my_reg:
        raise CustomHTTPException(
            403,
            "You must be checked in to this event to use networking.",
        )

    # 4. Get requesting user's interests
    my_interests_result = await session.execute(
        select(UserInterests.interest_id).where(
            UserInterests.user_id == user_id,
            UserInterests.is_deleted == False,
        )
    )
    my_interest_ids = set(row[0] for row in my_interests_result.all())

    # 5. Get all other checked-in attendees
    other_regs_result = await session.execute(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_attended == True,
            EventRegistrationsLink.is_deleted == False,
            EventRegistrationsLink.user_id != user_id,
        )
    )
    other_regs = other_regs_result.scalars().all()

    if not other_regs:
        return {
            "event_id": event_id,
            "event_name": event.name,
            "window_closes_at": window_closes_at,
            "attendees": [],
            "total": 0,
        }

    # 6. Build attendee list with interest matching
    attendee_user_ids = [r.user_id for r in other_regs]

    # Fetch user profiles, with the college in the same query. It used to be
    # read afterwards as `profile.org`, a lazy load, which an async session
    # cannot do: the request died the moment any other checked-in guest had a
    # college on their profile, which is nearly every guest.
    profiles_result = await session.execute(
        select(Users, UserProfiles, Organizations.name)
        .outerjoin(UserProfiles, (Users.id == UserProfiles.user_id) & (UserProfiles.is_deleted == False))  # noqa: E712
        .outerjoin(Organizations, Organizations.id == UserProfiles.org_id)
        .where(Users.id.in_(attendee_user_ids), Users.is_deleted == False)  # noqa: E712
    )
    user_profiles = {u.id: (u, p, org) for u, p, org in profiles_result.all()}

    # Fetch all interests for attendee users
    attendee_interests_result = await session.execute(
        select(UserInterests.user_id, Interests)
        .join(Interests, UserInterests.interest_id == Interests.id)
        .where(
            UserInterests.user_id.in_(attendee_user_ids),
            UserInterests.is_deleted == False,
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

        # Profile pic URL
        profile_pic_url = None
        if profile_data and profile_data.profile_pic:
            try:
                pic = profile_data.profile_pic
                if isinstance(pic, dict):
                    profile_pic_url = pic.get("medium") or pic.get("thumbnail") or pic.get("original")
                elif isinstance(pic, str):
                    profile_pic_url = pic
            except Exception:
                pass

        attendees.append({
            "user_id": uid,
            "name": user_data.full_name,
            "profile_pic_url": profile_pic_url,
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

    return {
        "event_id": event_id,
        "event_name": event.name,
        "window_closes_at": window_closes_at,
        "attendees": attendees,
        "total": len(attendees),
    }
