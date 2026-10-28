from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from app.config import settings
from app.core.auth.dependencies import UserAuth, AdminAuth
from app.core.response.pagination import PaginationParams, paginated_response
from app.db.core import SessionDep
from app.api.notifications.schemas import NotificationSchema
from app.core.notifications import inbox, service as push_service, topics as topic_lists
from app.core.notifications.delivery import ALL_TOPIC
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
    cursor: Optional[str] = Query(None, max_length=200, description="From `next`: the page after the last line seen (keyset)."),
):
    """List notifications for the authenticated user.

    Returns paginated list of notifications with related club/user/event data.
    With INBOX_SHARED the club and topic broadcasts are shared rows read
    through the person's follows and interests (app.core.notifications.inbox),
    in the same shape; `next` then carries a keyset `cursor` as well as the
    offset, and a page from a cursor reads only lines older than it.
    """
    at = inbox.decode_cursor(cursor) if settings.INBOX_SHARED else None
    notifications = await service.list_notifications(
        session=session,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
        cursor=at,
    )
    page = paginated_response(notifications, request, schema=NotificationSchema)
    if settings.INBOX_SHARED and page.next and page.items:
        last = page.items[-1]
        params = dict(request.query_params)
        params.update(offset=str(page.offset + page.limit), cursor=inbox.encode_cursor(last.created_at, last.id))
        page.next = f"{request.url.path}?{urlencode(params)}"
    return page


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


@router.delete("/{notification_id}", status_code=204, summary="Dismiss a notification")
async def dismiss(notification_id: str, session: SessionDep, user: UserAuth) -> Response:
    """The line leaves the person's inbox: their own row is soft-deleted, a
    shared broadcast gets their dismissed state (written only now)."""
    await service.dismiss_notification(session, notification_id, user.id)
    return Response(status_code=204)


# ==================== FCM TOPICS ====================


class TopicsResponse(BaseModel):
    """The FCM topics this person's devices are to be subscribed to, and the
    list's version: the same list, the same version."""
    topics: List[str]
    version: str


class SwitchesBody(BaseModel):
    clubs: Optional[bool] = None
    topics: Optional[bool] = None
    platform: Optional[bool] = None


class SwitchesResponse(BaseModel):
    clubs: bool
    topics: bool
    platform: bool


class MuteResponse(BaseModel):
    club_id: int
    muted: bool


@router.get("/topics", response_model=TopicsResponse, summary="The FCM topics to subscribe to")
async def my_topics(session: SessionDep, user: UserAuth) -> TopicsResponse:
    """`club_{id}` for each club followed and not muted, `topic_{slug}` for
    the topic of each interest, `all` -- each kind unless its switch is off.
    The app subscribes to what is new on the list and unsubscribes from what
    left it, on start, on sign-in and after it changes one of those; it keeps
    the version it applied and does nothing while the version is the same.
    Asked for whatever PUSH_TOPICS is: the subscriptions must be in place
    before the server sends by topic."""
    topics = await topic_lists.topics_for(session, user.id)
    return TopicsResponse(**topic_lists.topic_list_payload(topics))


@router.get("/settings", response_model=SwitchesResponse, summary="Which kinds of broadcast push are on")
async def my_switches(session: SessionDep, user: UserAuth) -> SwitchesResponse:
    return SwitchesResponse(**await topic_lists.switches(session, user.id))


@router.patch("/settings", response_model=SwitchesResponse, summary="Turn a kind of broadcast push on or off")
async def set_my_switches(body: SwitchesBody, session: SessionDep, user: UserAuth) -> SwitchesResponse:
    """clubs: the followed clubs' posts and events; topics: new events under
    the topics of your interests; platform: the platform's announcements.
    Off drops the kind's topics from GET /notifications/topics (and takes
    your devices out of them) and its lines from your inbox."""
    out = await topic_lists.set_switches(session, user.id, body.model_dump(exclude_none=True))
    await session.commit()
    return SwitchesResponse(**out)


@router.get("/mutes/clubs/{club_id}", response_model=MuteResponse, summary="Whether you muted a club")
async def club_muted(club_id: int, session: SessionDep, user: UserAuth) -> MuteResponse:
    return MuteResponse(club_id=club_id, muted=await topic_lists.is_muted(session, user.id, club_id))


@router.put("/mutes/clubs/{club_id}", response_model=MuteResponse, summary="Mute a club you follow")
async def mute_club(club_id: int, session: SessionDep, user: UserAuth) -> MuteResponse:
    """Still following, but no pushes or inbox lines from the club: its
    club_{id} leaves GET /notifications/topics, and your devices leave it."""
    await topic_lists.set_mute(session, user.id, club_id, True)
    await session.commit()
    return MuteResponse(club_id=club_id, muted=True)


@router.delete("/mutes/clubs/{club_id}", response_model=MuteResponse, summary="Unmute a club")
async def unmute_club(club_id: int, session: SessionDep, user: UserAuth) -> MuteResponse:
    await topic_lists.set_mute(session, user.id, club_id, False)
    await session.commit()
    return MuteResponse(club_id=club_id, muted=False)


class AdoptionResponse(BaseModel):
    devices: int
    topic_devices: int
    share: float
    by_platform: dict
    push_topics: bool
    push_topics_legacy: bool


@router.get("/topics/adoption", response_model=AdoptionResponse, summary="Devices that subscribe to topics (admins)")
async def topic_adoption(session: SessionDep, user: AdminAuth) -> AdoptionResponse:
    """How many live devices have said they subscribe to their topics (an app
    with topic support registers its token with `topics: 1`): watch `share`
    before turning PUSH_TOPICS on, and before turning PUSH_TOPICS_LEGACY off."""
    return AdoptionResponse(
        **await topic_lists.adoption(session), push_topics=bool(settings.PUSH_TOPICS), push_topics_legacy=bool(settings.PUSH_TOPICS_LEGACY),
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
    if settings.PUSH_TOPICS:
        # One send to the `all` topic, and device by device the devices that
        # do not subscribe to topics yet: committed with the audit line.
        await fanout.announce(session, fanout.Broadcast(
            kind="platform", ref=uuid.uuid4().hex, title=body.title, body=body.body, image_url=body.image_url,
            from_user_id=user.id, everyone=True,
        ), topic=ALL_TOPIC)
        return SendPushResponse(success=True, tokens_count=devices, sent_count=0, message=f"Sending to {devices} devices")
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
        
        # Create message. No badge: a token alone says nobody's unread
        # count, and a made-up number stays on the icon.
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            token=token,
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default"),
                ),
            ),
        )
        
        # Send, on a worker thread: the SDK blocks until FCM answers.
        import asyncio

        response = await asyncio.to_thread(messaging.send, message)
        return {"success": True, "message_id": response}
        
    except Exception as e:
        return {"success": False, "error": str(e), "error_type": type(e).__name__}
