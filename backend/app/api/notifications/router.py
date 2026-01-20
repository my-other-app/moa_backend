from typing import List
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from app.core.auth.dependencies import UserAuth
from app.core.response.pagination import PaginationParams, paginated_response
from app.db.core import SessionDep
from app.api.notifications.schemas import NotificationSchema
from . import service

router = APIRouter(prefix="/notifications", tags=["notifications"])


class UnreadCountResponse(BaseModel):
    """Response for unread count endpoint."""
    count: int


class MarkAllReadResponse(BaseModel):
    """Response for mark all read endpoint."""
    updated_count: int
    message: str


@router.get("/list")
async def list_notifications(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: UserAuth,
):
    """List notifications for the authenticated user.
    
    Returns paginated list of notifications with related club/user/event data.
    """
    notifications = await service.list_notifications(
        session=session,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return paginated_response(notifications, request, schema=NotificationSchema)


@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(
    session: SessionDep,
    user: UserAuth,
):
    """Get the count of unread notifications for the authenticated user."""
    count = await service.get_unread_count(session=session, user_id=user.id)
    return UnreadCountResponse(count=count)


@router.post("/read/{notification_id}")
async def mark_as_read(
    notification_id: str,
    session: SessionDep,
    user: UserAuth,
):
    """Mark a specific notification as read."""
    return await service.mark_notification_as_read(
        session=session,
        notification_id=notification_id,
        user_id=user.id,
    )


@router.post("/read-all", response_model=MarkAllReadResponse)
async def mark_all_as_read(
    session: SessionDep,
    user: UserAuth,
):
    """Mark all notifications as read for the authenticated user."""
    updated_count = await service.mark_all_as_read(session=session, user_id=user.id)
    return MarkAllReadResponse(
        updated_count=updated_count,
        message=f"Marked {updated_count} notification(s) as read"
    )

