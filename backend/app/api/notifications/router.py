from typing import List
from fastapi import APIRouter, Depends, Request
from app.core.auth.dependencies import UserAuth
from app.core.response.pagination import PaginationParams, paginated_response
from app.db.core import SessionDep
from app.api.notifications.schemas import NotificationSchema
from . import service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/list")
async def list_notifications(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: UserAuth,
):
    """List notifications for the authenticated user."""
    notifications = await service.list_notifications(
        session=session,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return paginated_response(notifications, request, schema=NotificationSchema)


@router.post("/read/{notification_id}")
async def mark_as_read(
    notification_id: int,
    session: SessionDep,
    user: UserAuth,
):
    """Mark a notification as read."""
    return await service.mark_notification_as_read(
        session=session,
        notification_id=notification_id,
        user_id=user.id,
    )
