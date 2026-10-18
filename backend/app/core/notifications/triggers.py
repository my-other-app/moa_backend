"""
Notification trigger functions for different scenarios.

This module provides trigger functions that are called when specific
events occur in the application (new event, check-in, etc.).

Each trigger:
1. Sends push notification via FCM
2. Saves notification to database for in-app notification history
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import ClubUsersLink
from app.api.events.models import Events, EventRegistrationsLink
from app.api.notifications import service as db_notification_service
from app.core.notifications import fanout
from app.core.notifications.service import (
    send_notification_to_user,
    send_notification_to_users,
)

logger = logging.getLogger(__name__)


async def notify_followers_of_new_event(
    session: AsyncSession,
    club_id: int,
    club_name: str,
    event_id: int,
    event_name: str,
) -> Optional[int]:
    """
    Notify all followers of a club about a new event.

    Trigger 1: When a club posts a new event, notify all users following the club.

    The inbox rows are one INSERT ... SELECT over the followers and the pushes
    go in batches, after the response (app.core.notifications.fanout). Returns
    the devices reached when it ran inline (the tests), None when scheduled.
    """
    title = f"🎉 New from {club_name}!"
    body = f"Check out: {event_name}"
    audience = (
        select(ClubUsersLink.user_id)
        .where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,  # noqa: E712
            ClubUsersLink.is_deleted == False,  # noqa: E712
        )
        .distinct()
    )

    async def job(s: AsyncSession) -> int:
        return await _tell(
            s, audience, title=title, body=body, type="new_event", from_club_id=club_id, event_id=event_id,
            data={"club_name": club_name, "event_name": event_name},
            push_data={"type": "new_event", "event_id": str(event_id), "club_id": str(club_id)},
        )

    return await fanout.dispatch(session, job, name=f"new_event:{event_id}")


async def notify_users_by_interest(
    session: AsyncSession,
    category_id: int,
    category_name: str,
    club_id: int,
    event_id: int,
    event_name: str,
) -> Optional[int]:
    """
    Notify users whose interests match the event category.

    Trigger 2: Notify users whose interests match the event category.
    Excludes users who are already followers (they got Trigger 1).

    Capped, so one event cannot push to every user on the platform: at most
    INTEREST_PUSH_MAX_PER_EVENT people (the newest accounts first: index
    order, and the people most likely still finding their way in), and
    nobody who has already had INTEREST_PUSH_MAX_PER_USER_PER_DAY of these in
    the last day. Followers are never capped: they asked. Runs after the
    response, as notify_followers_of_new_event does.
    """
    from app.api.interests.models import Interests
    from app.api.notifications.models import Notifications
    from app.api.users.models import UserInterests
    from app.config import settings

    per_event = max(0, int(settings.INTEREST_PUSH_MAX_PER_EVENT))
    per_day = max(0, int(settings.INTEREST_PUSH_MAX_PER_USER_PER_DAY))
    if per_event == 0 or per_day == 0:
        return 0

    title = f"📌 Event in {category_name}"
    body = f"New event matching your interests: {event_name}"
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    in_category = select(Interests.id).where(Interests.category_id == category_id, Interests.is_deleted == False)  # noqa: E712
    follows = (
        select(ClubUsersLink.id)
        .where(
            ClubUsersLink.user_id == UserInterests.user_id,
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,  # noqa: E712
            ClubUsersLink.is_deleted == False,  # noqa: E712
        )
        .exists()
    )
    told_today = (
        select(func.count())
        .select_from(Notifications)
        .where(
            Notifications.user_id == UserInterests.user_id,
            Notifications.type == "nearby_event",
            Notifications.is_deleted == False,  # noqa: E712
            Notifications.created_at >= day_ago,
        )
        .scalar_subquery()
    )
    audience = (
        select(UserInterests.user_id)
        .where(
            UserInterests.interest_id.in_(in_category),
            UserInterests.is_deleted == False,  # noqa: E712
            ~follows,
            told_today < per_day,
        )
        .group_by(UserInterests.user_id)
        .order_by(UserInterests.user_id.desc())
        .limit(per_event)
    )

    async def job(s: AsyncSession) -> int:
        return await _tell(
            s, audience, title=title, body=body, type="nearby_event", from_club_id=club_id, event_id=event_id,
            data={"category_name": category_name, "event_name": event_name},
            push_data={"type": "interest_match", "event_id": str(event_id), "category_id": str(category_id)},
        )

    return await fanout.dispatch(session, job, name=f"interest:{event_id}")


async def _tell(
    session: AsyncSession,
    audience,
    *,
    title: str,
    body: str,
    type: str,
    data: dict,
    push_data: dict,
    from_club_id: Optional[int] = None,
    event_id: Optional[int] = None,
) -> int:
    """The inbox rows in one statement, kept; then the pushes in batches."""
    reached = await fanout.insert_for_audience(
        session, audience, title=title, description=body, type=type, data=data,
        from_club_id=from_club_id, event_id=event_id,
    )
    await session.commit()
    if not reached:
        logger.debug("Nobody to tell about %s for event %s", type, event_id)
        return 0
    logger.info("Told %d people about %s for event %s; pushing", len(reached), type, event_id)
    return await send_notification_to_users(session=session, user_ids=reached, title=title, body=body, data=push_data)


async def notify_user_check_in(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    event_name: str,
    club_id: Optional[int] = None,
) -> bool:
    """
    Notify a user when they successfully check in to an event.
    
    Trigger 3: Confirmation notification after event check-in.
    """
    logger.info(f"Sending check-in confirmation to user {user_id} for event {event_id}")
    
    title = "✅ Checked in successfully!"
    body = f"Welcome to {event_name}. Enjoy the event!"
    
    # Save to database
    await db_notification_service.create_notification(
        session=session,
        user_id=user_id,
        title=title,
        description=body,
        type="event_checkin",
        event_id=event_id,
        from_club_id=club_id,
        data={"event_name": event_name},
    )
    
    # Send push notification
    return await send_notification_to_user(
        session=session,
        user_id=user_id,
        title=title,
        body=body,
        data={
            "type": "check_in",
            "event_id": str(event_id),
        },
    )


async def notify_event_participants(
    session: AsyncSession,
    event_id: int,
    title: str,
    body: str,
    user_ids: Optional[list[int]] = None,
    audience: str = "all",
    image_url: Optional[str] = None,
    club_id: Optional[int] = None,
) -> int:
    """
    Send notification to event participants from club dashboard.
    
    Trigger 4: Allow clubs to send custom notifications to event participants.
    """
    # Get club_id from event if not provided
    if club_id is None:
        event_query = select(Events.club_id).where(Events.id == event_id)
        event_result = await session.execute(event_query)
        club_id = event_result.scalar_one_or_none()
    
    if user_ids is None:
        # Build query based on audience targeting
        query = select(EventRegistrationsLink.user_id).where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
        
        # Apply audience filter
        if audience == "attendees":
            query = query.where(EventRegistrationsLink.is_attended == True)
        elif audience == "non_attendees":
            query = query.where(EventRegistrationsLink.is_attended == False)
        
        result = await session.execute(query)
        user_ids = [row[0] for row in result.fetchall()]
    
    if not user_ids:
        logger.debug(f"No participants to notify for event {event_id} (audience: {audience})")
        return 0, []
    
    logger.info(f"Sending club notification to {len(user_ids)} {audience} of event {event_id}")
    
    # Save to database
    await db_notification_service.create_notifications_batch(
        session=session,
        user_ids=user_ids,
        title=title,
        description=body,
        type="club_announcement",
        from_club_id=club_id,
        event_id=event_id,
        data={"audience": audience, "has_image": image_url is not None},
    )
    
    # Send push notification
    sent_count = await send_notification_to_users(
        session=session,
        user_ids=user_ids,
        title=title,
        body=body,
        data={
            "type": "club_announcement",
            "event_id": str(event_id),
            "image_url": image_url or "",
        },
        image_url=image_url,
    )
    
    return sent_count, user_ids


async def notify_user_added_as_volunteer(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    event_name: str,
    club_name: str,
    club_id: Optional[int] = None,
) -> bool:
    """
    Notify a user when they are added as a volunteer to an event.
    
    Trigger 5: When a user is added as a volunteer, notify them.
    """
    logger.info(f"Sending volunteer notification to user {user_id} for event {event_id}")
    
    title = f"🎖️ You're now a volunteer!"
    body = f"{club_name} added you as a volunteer for {event_name}"
    
    # Save to database
    await db_notification_service.create_notification(
        session=session,
        user_id=user_id,
        title=title,
        description=body,
        type="volunteer_added",
        event_id=event_id,
        from_club_id=club_id,
        data={"club_name": club_name, "event_name": event_name},
    )
    
    # Send push notification
    return await send_notification_to_user(
        session=session,
        user_id=user_id,
        title=title,
        body=body,
        data={
            "type": "volunteer_assigned",
            "event_id": str(event_id),
        },
    )


async def notify_certificate_generated(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    event_name: str,
    certificate_id: Optional[str] = None,
    club_id: Optional[int] = None,
) -> bool:
    """
    Notify a user when their certificate is generated.
    
    Trigger 6: When a certificate is generated for a user after event completion.
    """
    logger.info(f"Sending certificate notification to user {user_id} for event {event_id}")
    
    title = "🏆 Certificate Ready!"
    body = f"Your certificate for {event_name} is now available!"
    
    # Save to database
    await db_notification_service.create_notification(
        session=session,
        user_id=user_id,
        title=title,
        description=body,
        type="certificate_generated",
        event_id=event_id,
        from_club_id=club_id,
        data={"event_name": event_name, "certificate_id": certificate_id},
    )
    
    # Send push notification
    return await send_notification_to_user(
        session=session,
        user_id=user_id,
        title=title,
        body=body,
        data={
            "type": "certificate_generated",
            "event_id": str(event_id),
            "certificate_id": certificate_id or "",
        },
    )
