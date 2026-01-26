"""
Notification trigger functions for different scenarios.

This module provides trigger functions that are called when specific
events occur in the application (new event, check-in, etc.).

Each trigger:
1. Sends push notification via FCM
2. Saves notification to database for in-app notification history
"""

import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import ClubUsersLink
from app.api.users.models import Users
from app.api.events.models import Events, EventRegistrationsLink
from app.api.notifications import service as db_notification_service
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
) -> int:
    """
    Notify all followers of a club about a new event.
    
    Trigger 1: When a club posts a new event, notify all users following the club.
    """
    # Get all followers of this club
    query = select(ClubUsersLink.user_id).where(
        ClubUsersLink.club_id == club_id,
        ClubUsersLink.is_following == True,
        ClubUsersLink.is_deleted == False,
    )
    result = await session.execute(query)
    follower_ids = [row[0] for row in result.fetchall()]
    
    if not follower_ids:
        logger.debug(f"No followers for club {club_id}")
        return 0
    
    logger.info(f"Notifying {len(follower_ids)} followers of new event from club {club_id}")
    
    title = f"🎉 New from {club_name}!"
    body = f"Check out: {event_name}"
    
    # Save to database for in-app notification history
    await db_notification_service.create_notifications_batch(
        session=session,
        user_ids=follower_ids,
        title=title,
        description=body,
        type="new_event",
        from_club_id=club_id,
        event_id=event_id,
        data={"club_name": club_name, "event_name": event_name},
    )
    
    # Send push notification
    return await send_notification_to_users(
        session=session,
        user_ids=follower_ids,
        title=title,
        body=body,
        data={
            "type": "new_event",
            "event_id": str(event_id),
            "club_id": str(club_id),
        },
    )


async def notify_users_by_interest(
    session: AsyncSession,
    category_id: int,
    category_name: str,
    club_id: int,
    event_id: int,
    event_name: str,
) -> int:
    """
    Notify users whose interests match the event category.
    
    Trigger 2: Notify users whose interests match the event category.
    Excludes users who are already followers (they got Trigger 1).
    """
    from app.api.interests.models import Interests
    from app.api.users.models import UserInterests
    
    # Get interest IDs that belong to this category
    interest_query = select(Interests.id).where(
        Interests.category_id == category_id,
        Interests.is_deleted == False,
    )
    interest_result = await session.execute(interest_query)
    interest_ids = [row[0] for row in interest_result.fetchall()]
    
    if not interest_ids:
        logger.debug(f"No interests found for category {category_id}")
        return 0
    
    # Get users with these interests
    users_query = select(UserInterests.user_id).where(
        UserInterests.interest_id.in_(interest_ids),
    ).distinct()
    users_result = await session.execute(users_query)
    interested_user_ids = set(row[0] for row in users_result.fetchall())
    
    if not interested_user_ids:
        logger.debug(f"No users found with interests in category {category_id}")
        return 0
    
    # Get followers of this club to exclude them
    followers_query = select(ClubUsersLink.user_id).where(
        ClubUsersLink.club_id == club_id,
        ClubUsersLink.is_following == True,
        ClubUsersLink.is_deleted == False,
    )
    followers_result = await session.execute(followers_query)
    follower_ids = set(row[0] for row in followers_result.fetchall())
    
    # Calculate users to notify (interested but not following)
    user_ids_to_notify = list(interested_user_ids - follower_ids)
    
    if not user_ids_to_notify:
        logger.debug(f"All interested users are already followers of club {club_id}")
        return 0
    
    logger.info(f"Notifying {len(user_ids_to_notify)} users with matching interests")
    
    title = f"📌 Event in {category_name}"
    body = f"New event matching your interests: {event_name}"
    
    # Save to database
    await db_notification_service.create_notifications_batch(
        session=session,
        user_ids=user_ids_to_notify,
        title=title,
        description=body,
        type="nearby_event",
        from_club_id=club_id,
        event_id=event_id,
        data={"category_name": category_name, "event_name": event_name},
    )
    
    # Send push notification
    return await send_notification_to_users(
        session=session,
        user_ids=user_ids_to_notify,
        title=title,
        body=body,
        data={
            "type": "interest_match",
            "event_id": str(event_id),
            "category_id": str(category_id),
        },
    )


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
        return 0
    
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
    return await send_notification_to_users(
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
