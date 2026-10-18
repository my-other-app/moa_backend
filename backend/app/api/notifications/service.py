from datetime import datetime
from typing import Optional
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.api.notifications.models import Notifications, NotificationStatus
from app.response import CustomHTTPException
from app.api.users.models import Users, UserProfiles


async def create_notification(
    session: AsyncSession,
    user_id: int,
    title: str,
    description: str,
    type: str,
    data: Optional[dict] = None,
    from_club_id: Optional[int] = None,
    from_user_id: Optional[int] = None,
    event_id: Optional[int] = None,
) -> Notifications:
    """Create a new notification for a user.
    
    Args:
        session: Database session
        user_id: Target user ID
        title: Notification title
        description: Notification body/description
        type: Notification type (from NotificationType enum)
        data: Optional JSON data (e.g., certificate_id)
        from_club_id: Optional club ID that triggered the notification
        from_user_id: Optional user ID that triggered the notification
        event_id: Optional event ID related to the notification
        
    Returns:
        Created notification object
    """
    notification = Notifications(
        user_id=user_id,
        title=title,
        description=description,
        type=type,
        data=data,
        from_club_id=from_club_id,
        from_user_id=from_user_id,
        event_id=event_id,
        status=NotificationStatus.unread,
    )
    session.add(notification)
    await session.commit()
    await session.refresh(notification)
    return notification


async def create_notifications_batch(
    session: AsyncSession,
    user_ids: list[int],
    title: str,
    description: str,
    type: str,
    data: Optional[dict] = None,
    from_club_id: Optional[int] = None,
    event_id: Optional[int] = None,
) -> int:
    """Create notifications for multiple users at once.
    
    Args:
        session: Database session
        user_ids: List of target user IDs
        title: Notification title
        description: Notification body/description
        type: Notification type
        data: Optional JSON data
        from_club_id: Optional club ID
        event_id: Optional event ID
        
    Returns:
        Number of notifications created
    """
    if not user_ids:
        return 0
    # One statement for the whole list (one array bind), however long it is:
    # it was one INSERT per row.
    from app.core.notifications.fanout import insert_for_audience, users_in

    reached = await insert_for_audience(
        session,
        users_in(user_ids),
        title=title,
        description=description,
        type=type,
        data=data,
        from_club_id=from_club_id,
        event_id=event_id,
    )
    await session.commit()
    return len(reached)


async def list_notifications(
    session: AsyncSession, 
    user_id: int, 
    limit: int = 20, 
    offset: int = 0
) -> list[Notifications]:
    """List notifications for a user with eager loading of relationships.
    
    Args:
        session: Database session
        user_id: User ID to fetch notifications for
        limit: Maximum number of notifications to return
        offset: Number of notifications to skip
        
    Returns:
        List of notifications ordered by created_at descending
    """
    query = (
        select(Notifications)
        .where(
            Notifications.user_id == user_id,
            Notifications.is_deleted == False,
        )
        .options(
            selectinload(Notifications.from_club),
            selectinload(Notifications.from_user).options(
                selectinload(Users.profile).options(
                    selectinload(UserProfiles.avatar),
                    selectinload(UserProfiles.org)
                )
            ),
            selectinload(Notifications.event),
        )
        .order_by(Notifications.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return result.scalars().all()


async def get_unread_count(session: AsyncSession, user_id: int) -> int:
    """Get count of unread notifications for a user.
    
    Args:
        session: Database session
        user_id: User ID
        
    Returns:
        Number of unread notifications
    """
    query = select(func.count(Notifications.id)).where(
        Notifications.user_id == user_id,
        Notifications.status == NotificationStatus.unread,
        Notifications.is_deleted == False,
    )
    result = await session.execute(query)
    return result.scalar() or 0


async def mark_notification_as_read(
    session: AsyncSession, notification_id: str, user_id: int
) -> Notifications:
    """Mark a single notification as read.
    
    Args:
        session: Database session
        notification_id: Notification UUID
        user_id: User ID (for authorization)
        
    Returns:
        Updated notification object
        
    Raises:
        CustomHTTPException: If notification not found or unauthorized
    """
    query = select(Notifications).where(Notifications.id == notification_id)
    result = await session.execute(query)
    notification = result.scalar_one_or_none()
    
    if not notification:
        raise CustomHTTPException(404, "Notification not found")
    if notification.user_id != user_id:
        raise CustomHTTPException(403, "Not authorized to update this notification")

    notification.status = NotificationStatus.read
    await session.commit()
    await session.refresh(notification)
    return notification


async def mark_all_as_read(session: AsyncSession, user_id: int) -> int:
    """Mark all notifications as read for a user.
    
    Args:
        session: Database session
        user_id: User ID
        
    Returns:
        Number of notifications updated
    """
    stmt = (
        update(Notifications)
        .where(
            Notifications.user_id == user_id,
            Notifications.status == NotificationStatus.unread,
            Notifications.is_deleted == False,
        )
        .values(status=NotificationStatus.read)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount


async def get_event_notification_stats(
    session: AsyncSession, event_id: int
) -> dict:
    """Get notification statistics for an event.
    
    Args:
        session: Database session
        event_id: Event ID to get stats for
        
    Returns:
        Dictionary with total_sent, total_read, and read_percentage
    """
    # Get total notifications sent for this event
    total_query = select(func.count(Notifications.id)).where(
        Notifications.event_id == event_id,
        Notifications.is_deleted == False,
    )
    total_result = await session.execute(total_query)
    total_sent = total_result.scalar() or 0
    
    # Get total notifications read for this event
    read_query = select(func.count(Notifications.id)).where(
        Notifications.event_id == event_id,
        Notifications.status == NotificationStatus.read,
        Notifications.is_deleted == False,
    )
    read_result = await session.execute(read_query)
    total_read = read_result.scalar() or 0
    
    # Calculate read percentage
    read_percentage = round((total_read / total_sent * 100), 1) if total_sent > 0 else 0
    
    return {
        "total_sent": total_sent,
        "total_read": total_read,
        "read_percentage": read_percentage,
    }


