"""
Club notifications router for sending push notifications to event participants.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from typing import Optional, List

from app.db.core import SessionDep
from app.core.auth.dependencies import ClubAuth
from app.core.notifications.triggers import notify_event_participants
from app.api.events.models import Events
from sqlalchemy import select

router = APIRouter(prefix="/notifications", tags=["Club Notifications"])


class SendNotificationRequest(BaseModel):
    """Request schema for sending notification to event participants."""
    title: str = Field(..., max_length=255, description="Notification title")
    body: str = Field(..., max_length=1000, description="Notification body")
    user_ids: Optional[List[int]] = Field(
        None, 
        description="Specific user IDs to notify. If empty, notifies all participants."
    )


class SendNotificationResponse(BaseModel):
    """Response schema for notification sent."""
    success: bool
    message: str
    notifications_sent: int


@router.post(
    "/events/{event_id}/send",
    summary="Send notification to event participants",
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
    
    This endpoint allows clubs to send custom notifications to:
    - All registered participants of an event (if user_ids is empty)
    - Specific participants (if user_ids is provided)
    
    Only the club that owns the event can send notifications.
    """
    # Verify the event belongs to this club
    query = select(Events).where(
        Events.id == event_id,
        Events.is_deleted == False,
    )
    result = await session.execute(query)
    event = result.scalar_one_or_none()
    
    if not event:
        return SendNotificationResponse(
            success=False,
            message="Event not found",
            notifications_sent=0,
        )
    
    # Check if the club user owns this event through their club
    if event.club.user_id != user.id:
        return SendNotificationResponse(
            success=False,
            message="You don't have permission to send notifications for this event",
            notifications_sent=0,
        )
    
    # Send notifications
    sent_count = await notify_event_participants(
        session=session,
        event_id=event_id,
        title=request_body.title,
        body=request_body.body,
        user_ids=request_body.user_ids,
    )
    
    return SendNotificationResponse(
        success=sent_count > 0,
        message=f"Notification sent to {sent_count} participants",
        notifications_sent=sent_count,
    )
