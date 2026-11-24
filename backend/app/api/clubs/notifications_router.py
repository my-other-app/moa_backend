"""
Club notifications router for sending push notifications to event participants.
"""

import logging
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from typing import Optional, Literal, List
from uuid import UUID
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
from sqlalchemy.dialects.postgresql import aggregate_order_by, array_agg

from app.api.clubs import announcements_page
from app.response import CustomHTTPException


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
    registration_ids: Optional[List[UUID]] = Field(
        None, max_length=500,
        description="Only these guests of the event (a message to one person, or a few picked); the audience is then ignored",
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
    
    # The event's club, or one co-hosting it (app/api/cohosts), may announce.
    if not _may_manage(event, user):
        logger.warning(f"User {user.id} doesn't own event {event_id} (club_user_id={event.club.user_id})")
        return SendNotificationResponse(
            success=False,
            message="You don't have permission to send notifications for this event",
            notifications_sent=0,
        )

    # The club's plan counts announcements per month.
    from app.api.subscriptions import gates

    await gates.require_within(session, event.club_id, "announcements_per_month", actor=user)

    picked_users = None
    if request_body.registration_ids:
        from app.api.events.models import EventRegistrationsLink

        picked_users = list(dict.fromkeys((await session.scalars(
            select(EventRegistrationsLink.user_id).where(
                EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False,  # noqa: E712
                EventRegistrationsLink.id.in_(request_body.registration_ids), EventRegistrationsLink.user_id.isnot(None),
            )
        )).all()))
        if not picked_users:
            return SendNotificationResponse(success=False, message="None of these guests are on this event's list.", notifications_sent=0)

    # Send notifications with audience targeting
    logger.info(f"Calling notify_event_participants for event {event_id}")
    sent_count, participant_ids = await notify_event_participants(
        session=session,
        event_id=event_id,
        title=request_body.title,
        body=request_body.body,
        image_url=request_body.image_url,
        audience="picked" if picked_users else request_body.audience.value,
        user_ids=picked_users,
    )
    
    participant_count = len(participant_ids)
    
    audience_label = {
        "all": "registrants",
        "attendees": "attendees",
        "non_attendees": "non-attendees",
    }.get(request_body.audience.value, "participants")
    if picked_users:
        audience_label = "guest" if len(picked_users) == 1 else "picked guests"
    
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
            message=f"Found {participant_count} {audience_label}, but none is signed in to the app on a phone, so nothing was sent. Send an email instead.",
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
    
    # Check ownership (the event's club, or one co-hosting it)
    if not _may_manage(event, user):
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
    
    # One row per send, grouped in SQL by title, event and hour (IST, as the
    # rows read back), and paged there: every recipient's row was read into
    # Python and grouped there, for each page.
    N = Notifications
    hour = func.date_trunc("hour", func.timezone("Asia/Kolkata", N.created_at))
    sends = (
        select(
            N.title.label("title"),
            N.event_id.label("event_id"),
            # The latest row's words and time, as when the rows were walked newest first.
            array_agg(aggregate_order_by(N.description, N.created_at.desc()))[1].label("description"),
            func.max(N.created_at).label("created_at"),
            func.count().label("sent_count"),
            func.count().filter(N.status == NotificationStatus.read).label("read_count"),
        )
        .join(Events, Events.id == N.event_id)
        .where(Events.club_id == club.id, Events.is_deleted == False, N.type == "club_announcement", N.is_deleted == False)  # noqa: E712
        .group_by(N.title, N.event_id, hour)
        .subquery("sends")
    )
    rows = (await session.execute(
        select(sends, func.count().over().label("total"))
        .order_by(sends.c.created_at.desc(), sends.c.event_id.desc(), sends.c.title)
        .limit(limit)
        .offset(offset)
    )).all()
    if rows:
        total = int(rows[0].total)
    elif offset:
        total = int(await session.scalar(select(func.count()).select_from(sends)) or 0)
    else:
        total = 0
    ids = {g.event_id for g in rows}
    events = {e.id: e for e in (await session.scalars(select(Events).where(Events.id.in_(ids)))).all()} if ids else {}

    items = []
    for idx, g in enumerate(rows):
        event = events.get(g.event_id)
        sent = int(g.sent_count)
        read = int(g.read_count)
        read_pct = round((read / sent * 100), 1) if sent > 0 else 0

        # The place in the whole list, not the page: two sends of one event
        # on different pages had the same id.
        unique_id = f"{g.event_id}-{offset + idx}"

        items.append(NotificationHistoryItem(
            id=unique_id,
            title=g.title,
            description=g.description,
            event_id=g.event_id,
            event_name=event.name if event else "Unknown Event",
            event_poster=event.poster if event else None,
            sent_count=sent,
            read_count=read,
            read_percentage=read_pct,
            created_at=g.created_at,
        ))

    return NotificationHistoryResponse(items=items, total=total)



# ─── The Announcements page ──────────────────────────────────────────────


class PushSend(BaseModel):
    id: str
    kind: str
    title: str
    body: str
    event_id: Optional[int] = None
    event: Optional[dict] = None
    audience: Optional[str] = None
    sent: int
    read: int
    read_quickly: int
    read_pct: int
    slot: str
    created_at: datetime


class PushHistoryPage(BaseModel):
    items: List[PushSend]
    total: int
    limit: int
    offset: int
    counts: dict


class PushInsights(BaseModel):
    sends: int
    sends_delta: Optional[int] = None
    reached: int
    reached_delta: Optional[int] = None
    open_rate: Optional[int] = None
    open_rate_prev: Optional[int] = None
    quick_rate: Optional[int] = None
    last_sent_at: Optional[datetime] = None
    best: Optional[dict] = None
    slots: List[dict]
    best_slot: Optional[str] = None
    coming: List[dict]
    registered_ahead: int
    reachable_ahead: int
    all_time: dict
    generated_at: datetime


class PushAudience(BaseModel):
    event: dict
    registered: int
    checked_in: int
    reachable: int
    by_audience: dict


def _may_manage(event, user) -> bool:
    """The event's own club, a club co-hosting it, or an admin (events.access)."""
    from app.api.events.access import ensure_event_access, is_admin_user

    try:
        ensure_event_access(event, user.id, as_admin=is_admin_user(user))
    except CustomHTTPException:
        return False
    return True


async def _club_of(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


@router.get("/page", summary="Everything pushed to phones — announcements and live updates — scoped and searched")
async def push_history_page(session: SessionDep, user: ClubAuth, scope: str = Query("all"), search: Optional[str] = Query(None, max_length=80), limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> PushHistoryPage:
    if scope not in announcements_page.SCOPES:
        raise CustomHTTPException(422, message="Unknown scope.")
    club = await _club_of(session, user)
    return PushHistoryPage(**await announcements_page.history(session, club.id, scope=scope, search=search, limit=limit, offset=offset))


@router.get("/insights", summary="How much the club sends, how far it reaches, how much is opened, when it lands best, who can be reached")
async def push_insights(session: SessionDep, user: ClubAuth) -> PushInsights:
    club = await _club_of(session, user)
    return PushInsights(**await announcements_page.insights(session, club.id))


@router.get("/events/{event_id}/audience", summary="What a send to this event would reach, by audience")
async def push_audience(session: SessionDep, user: ClubAuth, event_id: int) -> PushAudience:
    from app.api.deliveries import service as deliveries

    event = await deliveries.event_for_club(session, user, event_id)
    return PushAudience(**await announcements_page.audience(session, event))
