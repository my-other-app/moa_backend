from typing import List, Optional
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from app.core.auth.dependencies import UserAuth, AdminAuth
from app.core.response.pagination import PaginationParams, paginated_response
from app.db.core import SessionDep
from app.api.notifications.schemas import NotificationSchema
from app.core.notifications import service as push_service
from . import service

from app.core.auth.permissions import BroadcastAuth
from app.core.rate_limit import account_or_address, limiter

router = APIRouter(prefix="/notifications", tags=["notifications"])


class UnreadCountResponse(BaseModel):
    """Response for unread count endpoint."""
    count: int


class MarkAllReadResponse(BaseModel):
    """Response for mark all read endpoint."""
    updated_count: int
    message: str


@router.get("/fetch-list")
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


@router.post("/test/send-all", response_model=SendPushResponse, summary="Send push to all devices (owners)")
@limiter.limit("2/hour", key_func=account_or_address)
async def send_push_to_all_devices(
    request: Request,
    body: SendPushRequest,
    session: SessionDep,
    user: BroadcastAuth,
):
    """
    Send a push to EVERY registered device. Owners only, twice an hour, and
    written to the audit log.

    It answers at once with how many devices there are; the sends go after the
    response as jobs, FCM_BATCH devices a job read in key order, each job
    writing the next (app.core.notifications.fanout): a crore of devices is
    never one list in memory, one request held open, nor one long run that a
    deploy cuts off and starts again.
    """
    import uuid

    from sqlalchemy import func, select

    from app.api.audit import service as audit
    from app.api.users.models import UserDeviceTokens
    from app.core.notifications import fanout

    devices = await session.scalar(
        select(func.count()).select_from(UserDeviceTokens).where(UserDeviceTokens.is_deleted == False)  # noqa: E712
    ) or 0
    if not devices:
        return SendPushResponse(success=False, tokens_count=0, sent_count=0, message="No registered devices found")

    await audit.record(
        session, actor=user, action="notifications.broadcast", entity_type="notification",
        summary=f"Pushed to every device: {body.title}"[:255], details={"title": body.title, "devices": devices}, request=request,
    )
    # The first batch's job, committed with the audit line.
    await fanout.enqueue_broadcast(session, uuid.uuid4().hex, body.title, body.body, body.image_url)
    await session.commit()
    return SendPushResponse(success=True, tokens_count=devices, sent_count=0, message=f"Sending to {devices} devices")


@router.get("/test/tokens", response_model=ListTokensResponse, summary="The newest registered FCM tokens")
async def list_all_tokens(
    session: SessionDep,
    user: AdminAuth,  # Admin only
):
    """
    The 100 newest registered FCM tokens, and how many there are in all.

    This is an admin-only endpoint for debugging push notifications. It used
    to return every row in the table.
    """
    from sqlalchemy import func, select
    from app.api.users.models import UserDeviceTokens

    live = UserDeviceTokens.is_deleted == False  # noqa: E712
    count = await session.scalar(select(func.count()).select_from(UserDeviceTokens).where(live)) or 0
    tokens = (await session.scalars(select(UserDeviceTokens).where(live).order_by(UserDeviceTokens.id.desc()).limit(100))).all()

    return ListTokensResponse(
        count=count,
        tokens=[
            TokenInfo(
                user_id=t.user_id,
                fcm_token=t.fcm_token[:50] + "..." if len(t.fcm_token) > 50 else t.fcm_token,
                platform=t.platform
            )
            for t in tokens
        ]
    )


@router.get("/test/firebase-status", summary="Check Firebase Admin SDK status")
async def check_firebase_status(
    user: AdminAuth,  # Admin only
):
    """
    Check if Firebase Admin SDK is properly initialized.
    
    This helps debug push notification issues.
    """
    from app.config import settings
    
    status = {
        "has_path_config": bool(settings.FIREBASE_SERVICE_ACCOUNT_PATH),
        "has_base64_config": bool(settings.FIREBASE_SERVICE_ACCOUNT_BASE64),
        "base64_length": len(settings.FIREBASE_SERVICE_ACCOUNT_BASE64) if settings.FIREBASE_SERVICE_ACCOUNT_BASE64 else 0,
        "firebase_initialized": False,
        "error": None,
    }
    
    try:
        # Try to initialize Firebase
        app = push_service.initialize_firebase()
        if app:
            status["firebase_initialized"] = True
            status["project_id"] = app.project_id if hasattr(app, 'project_id') else "unknown"
        else:
            status["error"] = "Firebase initialization returned None - check credentials"
    except Exception as e:
        status["error"] = str(e)
    
    return status


@router.post("/test/send-single", summary="Send push to a single token directly")
async def send_push_to_single_token(
    token: str,
    title: str,
    body: str,
    user: BroadcastAuth,
):
    """
    Send push notification directly to a single token for debugging.
    """
    from firebase_admin import messaging
    
    try:
        # Initialize Firebase
        app = push_service.initialize_firebase()
        if not app:
            return {"success": False, "error": "Firebase not initialized"}
        
        # Create message
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            token=token,
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        badge=1,
                    ),
                ),
            ),
        )
        
        # Send, on a worker thread: the SDK blocks until FCM answers.
        import asyncio

        response = await asyncio.to_thread(messaging.send, message)
        return {"success": True, "message_id": response}
        
    except Exception as e:
        return {"success": False, "error": str(e), "error_type": type(e).__name__}
