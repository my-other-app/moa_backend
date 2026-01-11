"""
Notification trigger functions for different scenarios.

This module provides trigger functions that are called when specific
events occur in the application (new event, check-in, etc.).
"""

import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import ClubUsersLink
from app.api.users.models import Users
from app.api.events.models import Events, EventRegistrationsLink
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
    
    Args:
        session: Database session
        club_id: Club ID
        club_name: Club name for notification title
        event_id: Event ID
        event_name: Event name for notification body
        
    Returns:
        Number of notifications sent
    """
    # Get all followers of this club
    query = select(ClubUsersLink.user_id).where(
        ClubUsersLink.club_id == club_id,
        ClubUsersLink.is_deleted == False,
    )
    result = await session.execute(query)
    follower_ids = [row[0] for row in result.fetchall()]
    
    if not follower_ids:
        logger.debug(f"No followers for club {club_id}")
        return 0
    
    logger.info(f"Notifying {len(follower_ids)} followers of new event from club {club_id}")
    
    return await send_notification_to_users(
        session=session,
        user_ids=follower_ids,
        title=f"🎉 New from {club_name}!",
        body=f"Check out: {event_name}",
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
    
    Trigger 2: When an event is posted, notify users whose interests
    match the event category. Excludes users who are already followers
    of the club (they already received notification via Trigger 1).
    
    Args:
        session: Database session
        category_id: Event category ID
        category_name: Category name for notification
        club_id: Club ID (to exclude followers)
        event_id: Event ID
        event_name: Event name
        
    Returns:
        Number of notifications sent
    """
    # Get users with matching interests
    # Interest categories map to event categories
    from app.api.interests.models import Interests
    
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
    from app.api.users.models import UserInterests
    
    users_query = select(UserInterests.user_id).where(
        UserInterests.interest_id.in_(interest_ids),
    ).distinct()
    users_result = await session.execute(users_query)
    interested_user_ids = set(row[0] for row in users_result.fetchall())
    
    if not interested_user_ids:
        logger.debug(f"No users found with interests in category {category_id}")
        return 0
    
    # Get followers of this club to exclude them (they already got notification)
    followers_query = select(ClubUsersLink.user_id).where(
        ClubUsersLink.club_id == club_id,
        ClubUsersLink.is_deleted == False,
    )
    followers_result = await session.execute(followers_query)
    follower_ids = set(row[0] for row in followers_result.fetchall())
    
    # Calculate users to notify (interested but not following)
    user_ids_to_notify = list(interested_user_ids - follower_ids)
    
    if not user_ids_to_notify:
        logger.debug(f"All interested users are already followers of club {club_id}")
        return 0
    
    logger.info(
        f"Notifying {len(user_ids_to_notify)} users with matching interests "
        f"(excluded {len(follower_ids)} followers)"
    )
    
    return await send_notification_to_users(
        session=session,
        user_ids=user_ids_to_notify,
        title=f"📌 Event in {category_name}",
        body=f"New event matching your interests: {event_name}",
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
) -> bool:
    """
    Notify a user when they successfully check in to an event.
    
    Trigger 3: Confirmation notification after event check-in.
    
    Args:
        session: Database session
        user_id: User ID who checked in
        event_id: Event ID
        event_name: Event name
        
    Returns:
        True if notification sent successfully
    """
    logger.info(f"Sending check-in confirmation to user {user_id} for event {event_id}")
    
    return await send_notification_to_user(
        session=session,
        user_id=user_id,
        title="✅ Checked in successfully!",
        body=f"Welcome to {event_name}. Enjoy the event!",
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
) -> int:
    """
    Send notification to event participants from club dashboard.
    
    Trigger 4: Allow clubs to send custom notifications to event participants.
    
    Args:
        session: Database session
        event_id: Event ID
        title: Custom notification title
        body: Custom notification body
        user_ids: Optional list of specific user IDs. If None, notifies all participants.
        
    Returns:
        Number of notifications sent
    """
    if user_ids is None:
        # Get all registered participants for this event
        query = select(EventRegistrationsLink.user_id).where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
        result = await session.execute(query)
        user_ids = [row[0] for row in result.fetchall()]
    
    if not user_ids:
        logger.debug(f"No participants to notify for event {event_id}")
        return 0
    
    logger.info(f"Sending club notification to {len(user_ids)} participants of event {event_id}")
    
    return await send_notification_to_users(
        session=session,
        user_ids=user_ids,
        title=title,
        body=body,
        data={
            "type": "club_message",
            "event_id": str(event_id),
        },
    )


async def notify_user_added_as_volunteer(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    event_name: str,
    club_name: str,
) -> bool:
    """
    Notify a user when they are added as a volunteer to an event.
    
    Trigger 5: When a user is added as a volunteer, notify them.
    
    Args:
        session: Database session
        user_id: User ID who was added as volunteer
        event_id: Event ID
        event_name: Event name
        club_name: Club name that added them
        
    Returns:
        True if notification sent successfully
    """
    logger.info(f"Sending volunteer assignment notification to user {user_id} for event {event_id}")
    
    return await send_notification_to_user(
        session=session,
        user_id=user_id,
        title=f"🎖️ You're now a volunteer!",
        body=f"{club_name} added you as a volunteer for {event_name}",
        data={
            "type": "volunteer_assigned",
            "event_id": str(event_id),
        },
    )
