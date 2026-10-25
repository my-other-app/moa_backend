"""A club's own WhatsApp number, further: routing by kind, spend, broadcasts,
and "Connect with Meta" (Embedded Signup); for the admins, the clubs whose
deliveries fell back most.

Who: routing and connecting are the connection's (the owner and admins,
access.require_manage; /clubs/whatsapp/account/... is admins-only for
writes in club_access.policy); the spend is anyone's who may see the number
(require_any); broadcasts are message content, the inbox's (require_inbox):
the owner and members with the WhatsApp permission, never a platform admin
acting as the club (403 WHATSAPP_PRIVATE). Enterprise only, as the rest
(403 PLAN_FEATURE_LOCKED); reading what exists is never gated.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.club_access import service as club_access
from app.api.club_whatsapp import access, broadcasts, consent, delivery_routes, embedded_signup, spend
from app.api.club_whatsapp import service
from app.api.club_whatsapp.reach_schemas import (
    AdminFallbacksOut,
    BroadcastIn,
    BroadcastList,
    BroadcastOut,
    EmbeddedSignupEventIn,
    EmbeddedSignupIn,
    KindsIn,
    OkOut,
    PreviewOut,
    ReachOut,
    SpendOut,
)
from app.api.club_whatsapp.schemas import AccountOut
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep

club_router = APIRouter(prefix="/clubs/whatsapp", tags=["club-whatsapp"])
admin_router = APIRouter(prefix="/admin/club-whatsapp", tags=["club-whatsapp"])


async def _reach(session, club) -> dict:
    account = await service.live_account(session, club.id) or await service.latest_account(session, club.id)
    return {
        "routing": account.routing if account is not None else True,
        "kinds": delivery_routes.kinds(account),
        "embedded_signup": embedded_signup.public_config(),
        "mode": account.mode if account is not None else None,
        "token_expires_at": account.token_expires_at if account is not None else None,
        "registered_at": account.registered_at if account is not None else None,
        "on_biz_app": bool(account.on_biz_app) if account is not None else False,
        "mm_status": account.mm_status if account is not None else None,
        "messaging_limit": {
            "tier": account.messaging_limit_tier if account is not None else None,
            "per_day": broadcasts.per_day(account.messaging_limit_tier) if account is not None else broadcasts.DEFAULT_PER_DAY,
        },
        "consent_text_version": consent.TEXT_VERSION,
        "consent_text": consent.text_for(club.name),
    }


# ─── Routing by kind, and Meta's flow ────────────────────────────────────


@club_router.get("/account/reach", summary="Routing by kind, Connect with Meta, the messaging limit")
async def get_reach(request: Request, session: SessionDep, user: ClubAuth) -> ReachOut:
    """`embedded_signup` is set only while "Connect with Meta" is on: the
    dashboard shows the button by it, and pasting keys works either way."""
    access.require_any(request)
    club = await service.club_of(session, user)
    return await _reach(session, club)


@club_router.patch("/account/routing", summary="Which deliveries go from the club's number: tickets, certificates, reminders")
async def patch_routing(payload: KindsIn, request: Request, session: SessionDep, user: ClubAuth) -> ReachOut:
    """Under the master switch (`routing` on PATCH /account): a kind switched
    off goes from MyOtherApp's number."""
    access.require_manage(request)
    club = await service.club_of(session, user)
    await delivery_routes.set_kinds(session, club, user, payload.model_dump(), request=request)
    return await _reach(session, club)


@club_router.post("/account/embedded-signup", summary="Connect the club's number with Meta (Embedded Signup)")
@limiter.limit("20/hour")
async def post_embedded_signup(payload: EmbeddedSignupIn, request: Request, session: SessionDep, user: ClubAuth) -> AccountOut:
    """Posted the moment Meta's flow finishes: its code lasts 30 seconds.
    404 EMBEDDED_SIGNUP_OFF while it is off; 422
    EMBEDDED_SIGNUP_CODE_EXPIRED (connect again), EMBEDDED_SIGNUP_REFUSED,
    EMBEDDED_SIGNUP_NO_ACCOUNT, EMBEDDED_SIGNUP_NO_NUMBER, and what a pasted
    connection's check says. A number Meta won't register yet (10 in 72
    hours, 133016) is kept, `status` failed with why; "Check again" retries."""
    access.require_manage(request)
    club = await service.club_of(session, user)
    finish = embedded_signup.Finish(
        code=payload.code, phone_number_id=payload.phone_number_id,
        waba_id=(payload.messaging_account_ids[0] if payload.messaging_account_ids else None) or payload.waba_id,
        business_id=payload.business_id, is_on_biz_app=payload.is_on_biz_app, event=payload.event,
    )
    await embedded_signup.connect(session, club, user, finish, member=club_access.current_member(request), request=request)
    return await service.account_out(session, club.id, manage=access.can_manage(request), inbox=access.can_inbox(request),
                                     actor=user, impersonated=access.impersonated())


@club_router.post("/account/embedded-signup/event", summary="Meta's flow ended without finishing: cancelled or an error")
async def post_embedded_signup_event(payload: EmbeddedSignupEventIn, request: Request, session: SessionDep, user: ClubAuth) -> OkOut:
    """CANCEL with its `current_step`, or ERROR with Meta's message, code
    and session id: kept in the audit log for Meta's support."""
    access.require_manage(request)
    club = await service.club_of(session, user)
    await embedded_signup.abandoned(
        session, club, user, event=payload.event, current_step=payload.current_step, error_message=payload.error_message,
        error_code=payload.error_code, session_id=payload.session_id, request=request,
    )
    return {"ok": True}


# ─── Spend ───────────────────────────────────────────────────────────────


@club_router.get("/spend", summary="This month: Meta's volume and approximate cost, sends and fallbacks")
async def get_spend(request: Request, session: SessionDep, user: ClubAuth) -> SpendOut:
    """As last read from Meta by the sweep (at most daily): a page view asks
    Meta nothing. Meta's cost is approximate, in the account's currency."""
    access.require_any(request)
    club = await service.club_of(session, user)
    return await spend.this_month(session, club.id)


# ─── Broadcasts ──────────────────────────────────────────────────────────


@club_router.get("/broadcasts", summary="The club's broadcasts, newest first")
async def list_broadcasts(request: Request, session: SessionDep, user: ClubAuth,
                          offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)) -> BroadcastList:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    rows, total = await broadcasts.listed(session, club.id, offset=offset, limit=limit)
    return {"items": [await broadcasts.out(session, b) for b in rows], "total": total}


@club_router.post("/broadcasts/preview", summary="Who a broadcast would reach, and what it would cost")
@limiter.limit("120/hour")
async def preview_broadcast(payload: BroadcastIn, request: Request, session: SessionDep, user: ClubAuth) -> PreviewOut:
    """Recipients, who is left out and why, Meta's list-rate estimate (INR,
    GST apart) and the messaging limit's days, and the words the first
    person gets. Nothing is sent."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    return await broadcasts.preview(session, club.id, payload.model_dump(), actor=user)


@club_router.post("/broadcasts", summary="Send a broadcast from the club's number, now or later")
@limiter.limit("30/hour")
async def create_broadcast(payload: BroadcastIn, request: Request, session: SessionDep, user: ClubAuth) -> BroadcastOut:
    """An event update (a UTILITY template, to one event's registrants who
    asked for WhatsApp) or a promotion (a MARKETING template, to the people
    who said yes to the club's updates), never to anyone who asked to stop.
    409 NO_WHATSAPP, WHATSAPP_FAILED, TEMPLATE_NOT_APPROVED; 422
    INVALID_TEMPLATE, INVALID_BROADCAST (`errors.field`), NO_RECIPIENTS;
    403 PLAN_FEATURE_LOCKED, WHATSAPP_PRIVATE (an admin acting as the club)."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    # The time as a datetime (the model's dump writes it as text).
    b = await broadcasts.create(session, club, user, {**payload.model_dump(), "scheduled_at": payload.scheduled_at},
                                member=club_access.current_member(request),
                                actor_name=access.actor_name(request, user), request=request)
    return await broadcasts.out(session, await broadcasts.get(session, club.id, b.id))


@club_router.get("/broadcasts/{broadcast_id}", summary="A broadcast and how it went")
async def get_broadcast(broadcast_id: int, request: Request, session: SessionDep, user: ClubAuth) -> BroadcastOut:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    return await broadcasts.out(session, await broadcasts.get(session, club.id, broadcast_id))


@club_router.post("/broadcasts/{broadcast_id}/cancel", summary="Cancel a scheduled broadcast, or the rest of one being sent")
async def cancel_broadcast(broadcast_id: int, request: Request, session: SessionDep, user: ClubAuth) -> BroadcastOut:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    b = await broadcasts.cancel(session, club, user, broadcast_id, request=request)
    return await broadcasts.out(session, b)


# ─── Admin ───────────────────────────────────────────────────────────────


@admin_router.get("/fallbacks", summary="The clubs whose WhatsApp deliveries fell back most this month (admin)")
async def admin_fallbacks(session: SessionDep, admin: AdminAuth, limit: int = Query(10, ge=1, le=50)) -> AdminFallbacksOut:
    """Deliveries the club wanted from its own number that went from
    MyOtherApp's, by club, with each one's commonest reason. No message."""
    return {"since": delivery_routes.month_start(), "items": await delivery_routes.top_fallbacks(session, limit=limit)}
