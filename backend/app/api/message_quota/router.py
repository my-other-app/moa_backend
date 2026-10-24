"""Message quotas: a club's own usage, and the admin's view and override.

- GET /clubs/messages/usage: this month so far, each channel against its
  limit, the extra and its estimated charge, and a plan with room.
- GET /clubs/messages/history: the closed months' statements.
- GET /admin/messages/clubs/{club_id}: one club's usage, history and own limits.
- PUT /admin/messages/clubs/{club_id}/limits: an Enterprise club's own
  limits and prices (they apply while it is on Enterprise).
- DELETE /admin/messages/clubs/{club_id}/limits: back to the plan's.
- POST /admin/messages/statements/{id}/retry: try a failed charge again now.
"""
from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.audit import service as audit
from app.api.clubs.models import Clubs
from app.api.message_quota import billing, service
from app.api.message_quota.models import ClubMessageQuotas, MessageStatements
from app.api.message_quota.schemas import (
    AdminClubMessagesOut,
    MessageHistoryOut,
    MessageStatementOut,
    MessageUsageOut,
    OwnLimitsIn,
)
from app.config import settings
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.auth.permissions import MoneyAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

club_router = APIRouter(prefix="/clubs/messages", tags=["message-quotas"])
admin_router = APIRouter(prefix="/admin/messages", tags=["message-quotas"])


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


@club_router.get("/history", summary="The club's closed months: what it sent and what the extra cost")
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


@admin_router.put("/clubs/{club_id}/limits", summary="Set an Enterprise club's own message limits and prices (admin)")
async def set_own_limits(
    club_id: int, payload: OwnLimitsIn, session: SessionDep, user: MoneyAuth, request: Request = None,
) -> AdminClubMessagesOut:
    """Null limits are unlimited; null prices are the plan's. They apply
    while the club is on Enterprise (granted, a deal or a trial), and can be
    set before it is."""
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


@admin_router.post("/statements/{statement_id}/retry", summary="Try a failed extra-messages charge again now (admin)")
async def retry_statement(statement_id: int, session: SessionDep, user: MoneyAuth, request: Request = None) -> MessageStatementOut:
    """Only a statement Razorpay refused (failed, or given up on): one that
    may have been made is found out by the sweep before anything is asked
    again, and one charged is never charged twice."""
    row = await session.get(MessageStatements, statement_id)
    if row is None:
        raise CustomHTTPException(status_code=404, message="Statement not found")
    if row.status not in ("failed", "gave_up"):
        raise CustomHTTPException(
            status_code=409, message="Only a charge Razorpay refused can be tried again.", error_code="NOT_RETRYABLE"
        )
    await billing.retry(session, row)
    await audit.record(
        session, actor=user, action="messages.overage_retry", entity_type="club", entity_id=row.club_id,
        summary=f"Asked for the {service.label(row.period)} extra-messages charge to be tried again",
        details={"statement_id": row.id}, request=request,
    )
    await session.commit()
    if settings.MESSAGE_OVERAGE_BILLING:
        await billing.charge(session, row.id)
    fresh = await session.scalar(
        select(MessageStatements).where(MessageStatements.id == statement_id).execution_options(populate_existing=True)
    )
    return service.statement_out(fresh)


def _jsonable(value):
    if isinstance(value, dict):
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in value.items()}
    return value
