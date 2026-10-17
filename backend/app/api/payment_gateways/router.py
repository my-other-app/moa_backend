"""A club's own Razorpay account: the owner connects it, the admins see and
can switch off every club's.

Changing it is the owner's alone (the club's own account): a team member gets
403 OWNER_ONLY, from the auth dependency (club_access.policy) and again here.
A member who sees the club's money (admin role) may read the page, without
the webhook secret; editors and door staff get ROLE_FORBIDDEN, as for finance.
"""
from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.club_access import service as club_access
from app.api.clubs.models import Clubs
from app.api.payment_gateways import service
from app.api.payment_gateways.schemas import AdminGatewayItem, AdminGatewayList, GatewayKeysIn, GatewayOut
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.auth.permissions import MoneyAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

club_router = APIRouter(prefix="/clubs/payments", tags=["payment-gateways"])
admin_router = APIRouter(prefix="/admin/payment-gateways", tags=["payment-gateways"])


async def _own_club(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id))
    if club is None:
        raise CustomHTTPException(status_code=404, message="No club for this account")
    return club


async def _owner_club(session, request: Request, user) -> Clubs:
    if club_access.current_member(request) is not None:
        raise CustomHTTPException(
            status_code=403,
            message="Only the club's owner can change where its payments go.",
            error_code="OWNER_ONLY",
        )
    return await _own_club(session, user)


@club_router.get("/gateway", summary="The club's own Razorpay account, if any")
async def get_gateway(request: Request, session: SessionDep, user: ClubAuth) -> GatewayOut:
    """
    Where the club's ticket and merch payments go. `webhook_url` and
    `webhook_secret` are for the owner to paste into Razorpay → Webhooks,
    ticking `webhook_events`; a team member gets `webhook_secret: null` and
    `can_manage: false`. The key secret is never returned. While an admin has
    the club's gateway disabled, `status` is "disabled", `connected` false
    and `can_manage` false.
    """
    club = await _own_club(session, user)
    owner = club_access.current_member(request) is None
    return await service.gateway_out(session, club.id, owner=owner)


@club_router.put("/gateway", summary="Connect the club's own Razorpay account")
async def put_gateway(payload: GatewayKeysIn, request: Request, session: SessionDep, user: ClubAuth) -> GatewayOut:
    """
    Check the keys with Razorpay and make them the club's: new orders for its
    events are made on its account. Keys already connected are kept (as
    `replaced`) for the orders made on them.

    422 INVALID_KEYS (with Razorpay's reason), 422 TEST_KEYS_NOT_ALLOWED,
    502 RAZORPAY_UNREACHABLE, 503 GATEWAY_UNAVAILABLE, and 409
    GATEWAY_DISABLED while an admin has the club's gateway turned off (so
    for verify and DELETE).
    """
    club = await _owner_club(session, request, user)
    await service.connect(session, club, user, payload.key_id, payload.key_secret, request=request)
    return await service.gateway_out(session, club.id)


@club_router.post("/gateway/verify", summary="Check the saved keys with Razorpay again")
async def verify_gateway(request: Request, session: SessionDep, user: ClubAuth) -> GatewayOut:
    club = await _owner_club(session, request, user)
    await service.verify(session, club, user, request=request)
    return await service.gateway_out(session, club.id)


@club_router.delete("/gateway", summary="Disconnect the club's own Razorpay account")
async def delete_gateway(request: Request, session: SessionDep, user: ClubAuth) -> GatewayOut:
    """New orders go to the platform's account; orders already made keep theirs."""
    club = await _owner_club(session, request, user)
    await service.disconnect(session, club, user, request=request)
    return await service.gateway_out(session, club.id)


@admin_router.get("", summary="Clubs' own Razorpay accounts (admin)")
async def list_gateways(
    session: SessionDep,
    admin: AdminAuth,
    status: str | None = Query(None, description="active, failed, replaced, disconnected or disabled; default all but replaced"),
    q: str | None = Query(None, description="Club name or slug, or key id"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> AdminGatewayList:
    rows, total = await service.admin_list(session, status=status, q=q, offset=offset, limit=limit)
    return {"items": [service.admin_item(row) for row in rows], "total": total}


@admin_router.post("/{club_id}/disable", summary="Switch off a club's own Razorpay account (admin)")
async def disable_gateway(club_id: int, session: SessionDep, admin: MoneyAuth, request: Request) -> AdminGatewayItem:
    """The club's new orders go to the platform's account; its existing orders keep theirs."""
    gateway = await service.disable(session, club_id, admin, request=request)
    await session.refresh(gateway, attribute_names=["club"])
    return service.admin_item(gateway)


@admin_router.post("/{club_id}/enable", summary="Lift an admin's disable (admin)")
async def enable_gateway(club_id: int, session: SessionDep, admin: MoneyAuth, request: Request) -> AdminGatewayItem:
    """
    The stored keys are checked again: still good, the gateway is active;
    otherwise it is `failed` with `last_error`, and the club can check it
    again or paste new keys. 404 NO_GATEWAY, 409 NOT_DISABLED.
    """
    gateway = await service.enable(session, club_id, admin, request=request)
    await session.refresh(gateway, attribute_names=["club"])
    return service.admin_item(gateway)
