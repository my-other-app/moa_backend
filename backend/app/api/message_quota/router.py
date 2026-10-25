"""Message quotas and prepaid credits: a club's own usage and credits, the
pricing page's packs, and the admin's view, grants, refunds and packs.

The club (any team role reads; buying is the owner's: club_access.policy):

- GET /clubs/messages/usage: this month so far, each channel against the
  plan's allowance, the credits used and left, and a plan with room.
- GET /clubs/messages/history: the closed months.
- GET /clubs/messages/credits: the balance, the lots, the packs, the purchases.
- POST /clubs/messages/credits/orders: a Razorpay order for one pack.
- POST /clubs/messages/credits/verify: checkout finished; the payment applied.
- GET /clubs/messages/credits/purchases/{id}/receipt: one purchase's receipt.

Anyone: GET /credit-packs, the packs on sale (the pricing page).

Admins (reads: any admin; changes: the money permission, each audited):

- GET /admin/messages/clubs/{club_id}: one club's usage, history and own limits.
- PUT / DELETE /admin/messages/clubs/{club_id}/limits: an Enterprise
  club's own monthly limits (they apply while it is on Enterprise).
- GET /admin/messages/clubs/{club_id}/credits: its balance, lots, purchases.
- POST /admin/messages/clubs/{club_id}/credits/grants: free credits, with a reason.
- POST /admin/messages/credits/purchases/{id}/refund: a purchase refunded,
  its unused credits taken back.
- GET / POST /admin/messages/packs, PATCH /admin/messages/packs/{id}.
"""
from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, select

from app.api.audit import service as audit
from app.api.clubs.models import Clubs
from app.api.message_quota import credits, service
from app.api.message_quota.models import ClubMessageQuotas, MessageCreditPacks, MessageCreditPurchases, PurchaseStatus
from app.api.message_quota.schemas import (
    AdminClubCreditsOut,
    AdminClubMessagesOut,
    AdminPackOut,
    AdminPacksOut,
    AdminPurchaseOut,
    ClubCreditsOut,
    GrantIn,
    MessageHistoryOut,
    MessageUsageOut,
    OrderIn,
    OrderOut,
    OwnLimitsIn,
    PackIn,
    PackPatch,
    PublicPacksOut,
    PurchaseOut,
    ReceiptOut,
    RefundIn,
    VerifyIn,
)
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.auth.permissions import MoneyAuth
from app.core.net import client_address
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

club_router = APIRouter(prefix="/clubs/messages", tags=["message-quotas"])
admin_router = APIRouter(prefix="/admin/messages", tags=["message-quotas"])
public_router = APIRouter(prefix="/credit-packs", tags=["message-quotas"])

#: As the plans' page (subscriptions.router).
PUBLIC_LIMIT = "120/minute"


async def _own_club(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(status_code=404, message="No club for this account")
    return club


async def _club(session, club_id: int) -> Clubs:
    club = await session.get(Clubs, club_id)
    if club is None or club.is_deleted:
        raise CustomHTTPException(status_code=404, message="Club not found")
    return club


@club_router.get("/usage", summary="This month's emails and WhatsApp messages against the plan's limits")
async def my_usage(session: SessionDep, user: ClubAuth) -> MessageUsageOut:
    club = await _own_club(session, user)
    return await service.usage(session, club.id)


@club_router.get("/history", summary="The club's closed months: what it sent and how it was paid for")
async def my_history(session: SessionDep, user: ClubAuth, months: int = Query(12, ge=1, le=36)) -> MessageHistoryOut:
    club = await _own_club(session, user)
    return {"items": await service.history(session, club.id, months=months)}


async def _admin_view(session, club: Clubs) -> dict:
    from app.api.subscriptions import service as subs

    usage = await service.usage(session, club.id)
    own = await session.get(ClubMessageQuotas, club.id)
    return {
        "club_id": club.id,
        "club_name": club.name,
        "usage": usage,
        "history": await service.history(session, club.id, months=24),
        "own_limits": service.own_limits_out(own),
        "own_limits_apply": own is not None and usage["plan_key"] == subs.ENTERPRISE_PLAN_KEY,
    }


@admin_router.get("/clubs/{club_id}", summary="One club's messages: this month, the closed months, its own limits (admin)")
async def club_messages(club_id: int, session: SessionDep, user: AdminAuth) -> AdminClubMessagesOut:
    return await _admin_view(session, await _club(session, club_id))


@admin_router.put("/clubs/{club_id}/limits", summary="Set an Enterprise club's own message limits (admin)")
async def set_own_limits(
    club_id: int, payload: OwnLimitsIn, session: SessionDep, user: MoneyAuth, request: Request = None,
) -> AdminClubMessagesOut:
    """Null limits are unlimited. They apply while the club is on
    Enterprise (granted, a deal or a trial), and can be set before it is.
    The prices are from the retired overage billing: kept, never used."""
    club = await _club(session, club_id)
    row = await session.get(ClubMessageQuotas, club.id)
    before = service.own_limits_out(row)
    if row is None:
        row = ClubMessageQuotas(club_id=club.id)
        session.add(row)
    row.emails_per_month = payload.emails_per_month
    row.whatsapp_per_month = payload.whatsapp_per_month
    row.email_overage_paise = payload.email_overage_paise
    row.whatsapp_overage_paise = payload.whatsapp_overage_paise
    row.note = (payload.note or "").strip() or None
    row.updated_by = getattr(user, "id", None)
    await session.flush()

    def _n(v):
        return "unlimited" if v is None else f"{v:,}"

    await audit.record(
        session, actor=user, action="messages.limits_set", entity_type="club", entity_id=club.id,
        summary=f"Set {club.name}'s message limits: {_n(payload.emails_per_month)} emails, "
                f"{_n(payload.whatsapp_per_month)} WhatsApp a month",
        details={"before": _jsonable(before), "after": payload.model_dump()}, request=request,
    )
    await session.commit()
    return await _admin_view(session, club)


@admin_router.delete("/clubs/{club_id}/limits", summary="Back to the plan's message limits (admin)")
async def clear_own_limits(club_id: int, session: SessionDep, user: MoneyAuth, request: Request = None) -> AdminClubMessagesOut:
    club = await _club(session, club_id)
    row = await session.get(ClubMessageQuotas, club.id)
    if row is not None:
        before = service.own_limits_out(row)
        await session.delete(row)
        await audit.record(
            session, actor=user, action="messages.limits_cleared", entity_type="club", entity_id=club.id,
            summary=f"{club.name} is back on its plan's message limits", details={"before": _jsonable(before)},
            request=request,
        )
        await session.commit()
    return await _admin_view(session, club)


def _jsonable(value):
    if isinstance(value, dict):
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in value.items()}
    return value


# ─── Prepaid credits: the club ───────────────────────────────────────────


@club_router.get("/credits", summary="The club's credits: balance, lots, packs on sale and purchases")
async def my_credits(session: SessionDep, user: ClubAuth) -> ClubCreditsOut:
    club = await _own_club(session, user)
    return await credits.club_view(session, club.id)


@club_router.post("/credits/orders", summary="Start buying a pack: a Razorpay order for checkout")
async def buy_pack(payload: OrderIn, session: SessionDep, user: ClubAuth) -> OrderOut:
    """Nothing is granted here: the credits come once Razorpay says the
    payment was captured (POST /credits/verify, or its webhook)."""
    club = await _own_club(session, user)
    return await credits.start_purchase(session, club, payload.pack_id, user)


@club_router.post("/credits/verify", summary="Checkout finished: apply the payment Razorpay took")
async def verify_purchase(payload: VerifyIn, session: SessionDep, user: ClubAuth) -> PurchaseOut:
    """`status` is "paid" once the payment is captured (the credits are in);
    "created" while Razorpay has only authorised it. Safe to call again."""
    club = await _own_club(session, user)
    purchase = await credits.verify(
        session, club.id, payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature,
    )
    lot = await credits._lot_of(session, purchase.id)
    return credits.purchase_out(purchase, lot)


@club_router.get("/credits/purchases/{purchase_id}/receipt", summary="One purchase's receipt")
async def purchase_receipt(purchase_id: int, session: SessionDep, user: ClubAuth) -> ReceiptOut:
    club = await _own_club(session, user)
    return await credits.receipt(session, club, purchase_id)


def _visitor(request: Request) -> str:
    return client_address(request) or "unknown"


@public_router.get("", summary="The top-up packs on sale (public)")
@limiter.limit(PUBLIC_LIMIT, key_func=_visitor)
async def credit_packs(request: Request, response: Response, session: SessionDep) -> PublicPacksOut:
    """Prices in paise, GST included, as the plans'. Cacheable for five minutes."""
    response.headers["Cache-Control"] = "public, max-age=300"
    return {
        "currency": "INR",
        "prices_include_gst": credits.PRICES_INCLUDE_GST,
        "validity_months": credits.VALIDITY_MONTHS,
        "packs": [credits.pack_out(p) for p in await credits.packs(session)],
    }


# ─── Prepaid credits: the admins ─────────────────────────────────────────


@admin_router.get("/clubs/{club_id}/credits", summary="One club's credits: balance, every lot and purchase (admin)")
async def club_credits(club_id: int, session: SessionDep, user: AdminAuth) -> AdminClubCreditsOut:
    return await credits.admin_view(session, await _club(session, club_id))


@admin_router.post("/clubs/{club_id}/credits/grants", summary="Give a club free credits, with a reason (admin)")
async def grant_credits(
    club_id: int, payload: GrantIn, session: SessionDep, user: MoneyAuth, request: Request = None,
) -> AdminClubCreditsOut:
    club = await _club(session, club_id)
    reason = payload.reason.strip()
    if len(reason) < 3:
        raise CustomHTTPException(status_code=422, message="Say why the credits are given.", error_code="REASON_REQUIRED")
    await credits.grant(session, club, payload.channel, payload.quantity, reason=reason,
                        valid_months=payload.valid_months, admin=user, request=request)
    await session.commit()
    return await credits.admin_view(session, club)


@admin_router.post("/credits/purchases/{purchase_id}/refund", summary="Refund a credits purchase; its unused credits go (admin)")
async def refund_purchase(
    purchase_id: int, payload: RefundIn, session: SessionDep, user: MoneyAuth, request: Request = None,
) -> AdminPurchaseOut:
    """The purchase's unused credits are taken back at once; Razorpay gives
    back `amount_paise`, by default the unused share of what was paid. One
    Razorpay refused or never answered (`refund_failed`) is asked again by
    posting again: Razorpay is checked first, so it is never refunded twice."""
    reason = payload.reason.strip()
    if len(reason) < 3:
        raise CustomHTTPException(status_code=422, message="Say why it is refunded.", error_code="REASON_REQUIRED")
    purchase = await credits.refund(session, purchase_id, reason=reason, amount_paise=payload.amount_paise,
                                    admin=user, request=request)
    return credits.purchase_out(purchase, await credits._lot_of(session, purchase.id), admin=True)


async def _sold(session) -> dict[int, int]:
    rows = await session.execute(
        select(MessageCreditPurchases.pack_id, func.count(MessageCreditPurchases.id))
        .where(MessageCreditPurchases.status.in_(PurchaseStatus.PAID_ONES), MessageCreditPurchases.pack_id.is_not(None))
        .group_by(MessageCreditPurchases.pack_id)
    )
    return {pack_id: int(n) for pack_id, n in rows.all()}


@admin_router.get("/packs", summary="Every top-up pack, on sale or not (admin)")
async def list_packs(session: SessionDep, user: AdminAuth) -> AdminPacksOut:
    sold = await _sold(session)
    return {
        "prices_include_gst": credits.PRICES_INCLUDE_GST,
        "validity_months": credits.VALIDITY_MONTHS,
        "packs": [credits.pack_out(p, admin=True, sold=sold.get(p.id, 0)) for p in await credits.packs(session, on_sale=False)],
    }


def _pack_words(pack: MessageCreditPacks) -> str:
    return f"{credits.pack_words(pack.channel, pack.quantity)} for {service.rupees(pack.price_paise)}"


@admin_router.post("/packs", summary="Add a top-up pack (admin)")
async def create_pack(payload: PackIn, session: SessionDep, user: MoneyAuth, request: Request = None) -> AdminPackOut:
    pack = MessageCreditPacks(channel=payload.channel, quantity=payload.quantity, price_paise=payload.price_paise,
                              active=payload.active, sort_order=payload.sort_order, updated_by=getattr(user, "id", None))
    session.add(pack)
    await session.flush()
    await audit.record(
        session, actor=user, action="messages.pack_created", entity_type="message_credit_pack", entity_id=pack.id,
        summary=f"Added a pack: {_pack_words(pack)}{'' if pack.active else ' (off sale)'}",
        details=payload.model_dump(), request=request,
    )
    await session.commit()
    return credits.pack_out(pack, admin=True)


@admin_router.patch("/packs/{pack_id}", summary="Change a top-up pack, or take it off sale (admin)")
async def update_pack(
    pack_id: int, payload: PackPatch, session: SessionDep, user: MoneyAuth, request: Request = None,
) -> AdminPackOut:
    """A price change applies to orders made from now on; a checkout opened
    before keeps the price it was opened with."""
    pack = await session.get(MessageCreditPacks, pack_id)
    if pack is None:
        raise CustomHTTPException(status_code=404, message="Pack not found", error_code="PACK_NOT_FOUND")
    before = {"quantity": pack.quantity, "price_paise": pack.price_paise, "active": pack.active, "sort_order": pack.sort_order}
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(pack, field, value)
    pack.updated_by = getattr(user, "id", None)
    await session.flush()
    if changes:
        await audit.record(
            session, actor=user, action="messages.pack_updated", entity_type="message_credit_pack", entity_id=pack.id,
            summary=f"Changed a pack: now {_pack_words(pack)}{'' if pack.active else ' (off sale)'}",
            details={"before": before, "after": changes}, request=request,
        )
    await session.commit()
    return credits.pack_out(pack, admin=True, sold=(await _sold(session)).get(pack.id, 0))
