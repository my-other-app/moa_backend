"""
Club notifications router for sending push notifications to event participants.
"""

import logging
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional, Literal
from enum import Enum

from sqlalchemy.orm import joinedload
from app.db.core import SessionDep
from app.core.auth.dependencies import ClubAuth
from app.core.notifications.triggers import notify_event_participants
from app.api.events.models import Events
from sqlalchemy import select

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
    # Send notifications with audience targeting
    logger.info(f"Calling notify_event_participants for event {event_id}")
    sent_count, participant_count = await notify_event_participants(
        session=session,
        event_id=event_id,
        title=request_body.title,
        body=request_body.body,
        image_url=request_body.image_url,
        audience=request_body.audience.value,
    )
    
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
            message=f"Found {participant_count} {audience_label}, but none have active push notifications (mobile app login required).",
            notifications_sent=0,
        )
        
    return SendNotificationResponse(
        success=True,
        message=f"Notification sent to {sent_count} of {participant_count} {audience_label}",
        notifications_sent=sent_count,
    )
