"""
Club notifications router for sending push notifications to event participants.
"""

import logging
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from typing import Optional, Literal, List
from enum import Enum
from datetime import datetime

from sqlalchemy.orm import joinedload
from app.db.core import SessionDep
from app.core.auth.dependencies import ClubAuth
from app.core.notifications.triggers import notify_event_participants
from app.api.events.models import Events
from app.api.notifications.models import Notifications, NotificationStatus
from app.api.clubs.models import Clubs
from sqlalchemy import select, func, distinct, String


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["Club Notifications"])


class AudienceType(str, Enum):
    """Audience types for notifications."""
    ALL = "all"
    ATTENDEES = "attendees"
    NON_ATTENDEES = "non_attendees"


class SendNotificationRequest(BaseModel):
    """Request schema for sending notification to event participants."""
    title: str = Field(..., max_length=255, description="Notification title")
    body: str = Field(..., max_length=1000, description="Notification body")
    image_url: Optional[str] = Field(
        None,
        max_length=500,
        description="Optional image URL to include in notification"
    )
    audience: AudienceType = Field(
        AudienceType.ALL,
        description="Target audience: 'all' (registrants), 'attendees', or 'non_attendees'"
    )


class SendNotificationResponse(BaseModel):
    """Response schema for notification sent."""
    success: bool
    message: str
    notifications_sent: int


@router.post(
    "/events/{event_id}/send",
    summary="Send announcement to event participants",
    response_model=SendNotificationResponse,
)
async def send_notification_to_participants(
    event_id: int,
    request_body: SendNotificationRequest,
    session: SessionDep,
    user: ClubAuth,
) -> SendNotificationResponse:
    """
    Send a push notification to participants of an event.
    
    This endpoint allows clubs to send announcements to:
    - **all**: All registered participants
    - **attendees**: Only users who have checked in
    - **non_attendees**: Only users who registered but haven't checked in
    
    Optionally include an image URL to display in the notification.
    """
    logger.info(f"Sending announcement for event {event_id} by user {user.id}")
    logger.info(f"Request: title='{request_body.title}', audience={request_body.audience.value}")
    
    # Verify the event belongs to this club - use joinedload for club relationship
    query = select(Events).where(
        Events.id == event_id,
        Events.is_deleted == False,
    ).options(joinedload(Events.club))
    
    result = await session.execute(query)
    event = result.scalar_one_or_none()
    
    if not event:
        logger.warning(f"Event {event_id} not found")
        return SendNotificationResponse(
            success=False,
            message="Event not found",
            notifications_sent=0,
        )
    
    logger.info(f"Event found: {event.name}, club_id={event.club_id}, club_user_id={event.club.user_id}")
    
    # Check if the club user owns this event through their club
    if event.club.user_id != user.id:
        logger.warning(f"User {user.id} doesn't own event {event_id} (club_user_id={event.club.user_id})")
        return SendNotificationResponse(
            success=False,
            message="You don't have permission to send notifications for this event",
            notifications_sent=0,
        )
    
    # Send notifications with audience targeting
    logger.info(f"Calling notify_event_participants for event {event_id}")
    sent_count, participant_ids = await notify_event_participants(
        session=session,
        event_id=event_id,
        title=request_body.title,
        body=request_body.body,
        image_url=request_body.image_url,
        audience=request_body.audience.value,
    )
    
    participant_count = len(participant_ids)
    
    audience_label = {
        "all": "registrants",
        "attendees": "attendees",
        "non_attendees": "non-attendees",
    }.get(request_body.audience.value, "participants")
    
    logger.info(f"Announcement sent to {sent_count} of {participant_count} {audience_label}")
    
    if participant_count == 0:
        return SendNotificationResponse(
            success=False,
            message=f"No {audience_label} found for this event.",
            notifications_sent=0,
        )
    
    if sent_count == 0:
        return SendNotificationResponse(
            success=False, # Treat as warning/failure to alert user
            message=f"Found {participant_count} {audience_label} (IDs: {participant_ids}), but none have active push notifications (mobile app login required).",
            notifications_sent=0,
        )
        
    return SendNotificationResponse(
        success=True,
        message=f"Notification sent to {sent_count} of {participant_count} {audience_label}",
        notifications_sent=sent_count,
    )


class NotificationStatsResponse(BaseModel):
    """Response schema for notification statistics."""
    total_sent: int
    total_read: int
    read_percentage: float


@router.get(
    "/events/{event_id}/stats",
    summary="Get notification statistics for an event",
    response_model=NotificationStatsResponse,
)
async def get_notification_stats(
    event_id: int,
    session: SessionDep,
    user: ClubAuth,
) -> NotificationStatsResponse:
    """
    Get notification statistics for an event.
    
    Returns:
    - **total_sent**: Total notifications sent for this event
    - **total_read**: Total notifications read by users
    - **read_percentage**: Percentage of notifications read
    """
    from app.api.notifications.service import get_event_notification_stats
    
    # Verify the event belongs to this club
    query = select(Events).where(
        Events.id == event_id,
        Events.is_deleted == False,
    ).options(joinedload(Events.club))
    
    result = await session.execute(query)
    event = result.scalar_one_or_none()
    
    if not event:
        return NotificationStatsResponse(total_sent=0, total_read=0, read_percentage=0)
    
    # Check ownership
    if event.club.user_id != user.id:
        return NotificationStatsResponse(total_sent=0, total_read=0, read_percentage=0)
    
    stats = await get_event_notification_stats(session, event_id)
    
    return NotificationStatsResponse(
        total_sent=stats["total_sent"],
        total_read=stats["total_read"],
        read_percentage=stats["read_percentage"],
    )


class NotificationHistoryItem(BaseModel):
    """Single notification history entry."""
    id: str
    title: str
    description: str
    event_id: int
    event_name: str
    event_poster: Optional[dict]
    sent_count: int
    read_count: int
    read_percentage: float
    created_at: datetime


class NotificationHistoryResponse(BaseModel):
    """Response for notification history list."""
    items: List[NotificationHistoryItem]
    total: int


@router.get(
    "/history",
    summary="Get notification history for club events",
    response_model=NotificationHistoryResponse,
)
async def get_notification_history(
    session: SessionDep,
    user: ClubAuth,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> NotificationHistoryResponse:
    """
    Get history of all notifications sent for events owned by this club.
    
    Returns grouped notifications with:
    - Title and description
    - Event name and poster
    - Sent count and read count
    - Read percentage
    """
    # Get the club for this user
    club_query = select(Clubs).where(
        Clubs.user_id == user.id,
        Clubs.is_deleted == False,
    )
    club_result = await session.execute(club_query)
    club = club_result.scalar_one_or_none()
    
    if not club:
        return NotificationHistoryResponse(items=[], total=0)
    
    # Get all events for this club with their notifications
    events_query = select(Events).where(
        Events.club_id == club.id,
        Events.is_deleted == False,
    )
    events_result = await session.execute(events_query)
    events = {e.id: e for e in events_result.scalars().all()}
    
    if not events:
        return NotificationHistoryResponse(items=[], total=0)
    
    event_ids = list(events.keys())
    
    # Get all club_announcement notifications for these events
    # Simple query - get all notifications and process in Python
    notifications_query = (
        select(Notifications)
        .where(
            Notifications.event_id.in_(event_ids),
            Notifications.type == "club_announcement",
            Notifications.is_deleted == False,
        )
        .order_by(Notifications.created_at.desc())
    )
    
    result = await session.execute(notifications_query)
    all_notifications = result.scalars().all()
    
    # Group notifications by (title, event_id, hour)
    groups = {}
    for n in all_notifications:
        # Group key: title + event_id + hour (rounded)
        hour_key = n.created_at.replace(minute=0, second=0, microsecond=0)
        key = (n.title, n.event_id, hour_key)
        
        if key not in groups:
            groups[key] = {
                "title": n.title,
                "description": n.description,
                "event_id": n.event_id,
                "created_at": n.created_at,
                "sent_count": 0,
                "read_count": 0,
            }
        
        groups[key]["sent_count"] += 1
        if n.status == NotificationStatus.read:
            groups[key]["read_count"] += 1
    
    # Convert to list and sort
    grouped_list = sorted(groups.values(), key=lambda x: x["created_at"], reverse=True)
    total = len(grouped_list)
    
    # Apply pagination
    paginated = grouped_list[offset:offset + limit]
    
    # Build response
    items = []
    for idx, g in enumerate(paginated):
        event = events.get(g["event_id"])
        sent = g["sent_count"]
        read = g["read_count"]
        read_pct = round((read / sent * 100), 1) if sent > 0 else 0
        
        unique_id = f"{g['event_id']}-{idx}"
        
        items.append(NotificationHistoryItem(
            id=unique_id,
            title=g["title"],
            description=g["description"],
            event_id=g["event_id"],
            event_name=event.name if event else "Unknown Event",
            event_poster=event.poster if event else None,
            sent_count=sent,
            read_count=read,
            read_percentage=read_pct,
            created_at=g["created_at"],
        ))
    
    return NotificationHistoryResponse(items=items, total=total)

