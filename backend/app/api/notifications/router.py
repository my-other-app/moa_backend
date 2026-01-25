from typing import List, Optional
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from app.core.auth.dependencies import UserAuth, AdminAuth
from app.core.response.pagination import PaginationParams, paginated_response
from app.db.core import SessionDep
from app.api.notifications.schemas import NotificationSchema
from app.core.notifications import service as push_service
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


# ==================== TEST/ADMIN ENDPOINTS ====================

class SendPushRequest(BaseModel):
    """Request to send a push notification."""
    title: str
    body: str
    image_url: Optional[str] = None


class SendPushResponse(BaseModel):
    """Response from push notification send."""
    success: bool
    tokens_count: int
    sent_count: int
    message: str


class TokenInfo(BaseModel):
    """FCM token info."""
    user_id: int
    fcm_token: str
    platform: str


class ListTokensResponse(BaseModel):
    """Response with list of registered tokens."""
    count: int
    tokens: List[TokenInfo]


@router.post("/test/send-all", response_model=SendPushResponse, summary="Send push to all devices")
async def send_push_to_all_devices(
    request: SendPushRequest,
    session: SessionDep,
    user: AdminAuth,  # Admin only
):
    """
    Send a test push notification to ALL registered devices.
    
    This is an admin-only endpoint for testing push notifications.
    """
    from sqlalchemy import select
    from app.api.users.models import UserDeviceTokens
    
    # Get all tokens
    query = select(UserDeviceTokens).where(UserDeviceTokens.is_deleted == False)
    result = await session.execute(query)
    tokens = result.scalars().all()
    
    if not tokens:
        return SendPushResponse(
            success=False,
            tokens_count=0,
            sent_count=0,
            message="No registered devices found"
        )
    
    # Get all user IDs
    user_ids = list(set([t.user_id for t in tokens]))
    
    # Send notifications
    sent_count = await push_service.send_notification_to_users(
        session=session,
        user_ids=user_ids,
        title=request.title,
        body=request.body,
        image_url=request.image_url,
    )
    
    return SendPushResponse(
        success=sent_count > 0,
        tokens_count=len(tokens),
        sent_count=sent_count,
        message=f"Sent to {sent_count} of {len(tokens)} devices"
    )


@router.get("/test/tokens", response_model=ListTokensResponse, summary="List all registered FCM tokens")
async def list_all_tokens(
    session: SessionDep,
    user: AdminAuth,  # Admin only
):
    """
    List all registered FCM tokens.
    
    This is an admin-only endpoint for debugging push notifications.
    """
    from sqlalchemy import select
    from app.api.users.models import UserDeviceTokens
    
    query = select(UserDeviceTokens).where(UserDeviceTokens.is_deleted == False)
    result = await session.execute(query)
    tokens = result.scalars().all()
    
    return ListTokensResponse(
        count=len(tokens),
        tokens=[
            TokenInfo(
                user_id=t.user_id,
                fcm_token=t.fcm_token[:50] + "..." if len(t.fcm_token) > 50 else t.fcm_token,
                platform=t.platform
            )
            for t in tokens
        ]
    )
