from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.notifications.models import Notifications, NotificationStatus
from app.response import CustomHTTPException


async def create_notification(
    session: AsyncSession,
    user_id: int,
    title: str,
    description: str,
    type: str,
    data: dict | None = None,
) -> Notifications:
    """Create a new notification for a user."""
    notification = Notifications(
        user_id=user_id,
        title=title,
        description=description,
        type=type,
        data=data,
        status=NotificationStatus.unread,
    )
    session.add(notification)
    await session.commit()
    await session.refresh(notification)
    return notification


async def list_notifications(
    session: AsyncSession, user_id: int, limit: int = 10, offset: int = 0
):
    """List notifications for a user."""
    query = (
        select(Notifications)
        .where(Notifications.user_id == user_id)
        .order_by(Notifications.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return result.scalars().all()


async def mark_notification_as_read(
    session: AsyncSession, notification_id: int, user_id: int
):
    """Mark a notification as read."""
    notification = await session.get(Notifications, notification_id)
    if not notification:
        raise CustomHTTPException(404, "Notification not found")
    if notification.user_id != user_id:
        raise CustomHTTPException(403, "Not authorized to update this notification")

    notification.status = NotificationStatus.read
    await session.commit()
    await session.refresh(notification)
    return notification
