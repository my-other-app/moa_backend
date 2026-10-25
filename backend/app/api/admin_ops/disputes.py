"""Razorpay disputes and chargebacks on the platform's account.

Razorpay posts `payment.dispute.created`, `.under_review`,
`.action_required`, `.won`, `.lost` and `.closed` to the platform webhook
(payments.service._apply_platform_event hands them here, behind the same
once-per-event gate as every other event). Each is kept as one row per
dispute (`payment_disputes`), linked to the ticket payment or the
subscription charge it is about, and so to its club and event. A new
dispute, and one that is lost, alerts the admins (`payment_disputed`).

Nothing here moves money or answers Razorpay: the evidence is sent from
Razorpay's dashboard before `respond_by`. The page shows what is open and
when it is due, and keeps the admins' notes. A lost dispute comes off what
the club is owed (app.api.admin_ops.payouts).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.api.admin_ops.models import PaymentDisputes
from app.api.audit import service as audit
from app.api.clubs.models import Clubs
from app.api.events.models import Events
from app.api.payments.models import PaymentLogs
from app.core import money
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ModerateAuth
from app.core.validations.exceptions import RequestValidationError
from app.db.core import SessionDep
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/disputes", tags=["admin-disputes"])

EVENTS = ("created", "under_review", "action_required", "won", "lost", "closed")
OPEN = ("open", "under_review", "action_required")
CLOSED = ("won", "lost", "closed")
NOTES_MAX = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at(unix) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(unix), tz=timezone.utc) if unix else None
    except (TypeError, ValueError, OSError):
        return None


def _iso(at) -> Optional[str]:
    return at.isoformat() if at else None


def _text(value, limit: int) -> Optional[str]:
    text = " ".join(str(value).split()) if value not in (None, "") else ""
    return text[:limit] or None


async def _link(session, row: PaymentDisputes) -> None:
    """The ticket payment or subscription charge the dispute is about, and its club and event."""
    from app.api.payments.handlers import registration_for_order
    from app.api.subscriptions.models import SubscriptionCharges

    pid = row.razorpay_payment_id
    if not pid:
        return
    log = await session.scalar(
        select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == pid).execution_options(include_deleted=True)
    )
    if log is not None:
        row.payment_log_id = log.id
        from app.api.payments.models import PaymentOrders

        order = await session.get(PaymentOrders, log.order_id)
        reg = await registration_for_order(session, order, include_deleted=True) if order is not None else None
        if reg is not None:
            row.event_id = reg.event_id
            row.club_id = await session.scalar(
                select(Events.club_id).where(Events.id == reg.event_id).execution_options(include_deleted=True)
            )
        return
    charge = await session.scalar(select(SubscriptionCharges).where(SubscriptionCharges.razorpay_payment_id == pid))
    if charge is not None:
        row.subscription_charge_id = charge.id
        row.club_id = charge.club_id


async def apply_webhook(session, event_name: str, data: dict) -> None:
    """One `payment.dispute.*` event: the dispute's row brought up to date,
    and the admins told of a new one or a lost one."""
    from app.api.admin_alerts import notify

    payload = data.get("payload") or {}
    entity = (payload.get("dispute") or {}).get("entity") or {}
    dispute_id = entity.get("id")
    if not dispute_id:
        raise RequestValidationError(payload="Invalid dispute payload")
    suffix = event_name.rsplit(".", 1)[-1]
    status = _text(entity.get("status"), 30) or ("open" if suffix == "created" else suffix if suffix in EVENTS else "open")
    row = await session.scalar(
        select(PaymentDisputes).where(PaymentDisputes.razorpay_dispute_id == dispute_id).with_for_update()
    )
    new = row is None
    before = None if new else row.status
    if new:
        row = PaymentDisputes(razorpay_dispute_id=str(dispute_id)[:64], notes=[], created_at=_now())
        session.add(row)
    payment_id = entity.get("payment_id") or ((payload.get("payment") or {}).get("entity") or {}).get("id")
    if payment_id and not row.razorpay_payment_id:
        row.razorpay_payment_id = str(payment_id)[:64]
        await _link(session, row)
    if entity.get("amount") is not None:
        row.amount_paise = int(entity.get("amount") or 0)
    if entity.get("amount_deducted") is not None:
        row.amount_deducted_paise = int(entity.get("amount_deducted") or 0)
    row.currency = (entity.get("currency") or row.currency or "INR")[:3]
    row.reason_code = _text(entity.get("reason_code"), 80) or row.reason_code
    row.reason_description = _text(entity.get("reason_description"), 500) or row.reason_description
    row.phase = _text(entity.get("phase"), 40) or row.phase
    row.respond_by = _at(entity.get("respond_by")) or row.respond_by
    row.opened_at = row.opened_at or _at(entity.get("created_at")) or _now()
    row.status = status
    if status in CLOSED and row.closed_at is None:
        row.closed_at = _now()
    row.last_event = event_name[:60]
    row.updated_at = _now()
    await session.flush()

    amount = f"₹{money.from_paise(row.amount_paise)}"
    if new or (status == "lost" and before != "lost"):
        club_name = None
        if row.club_id:
            club_name = await session.scalar(select(Clubs.name).where(Clubs.id == row.club_id).execution_options(include_deleted=True))
        what = "ticket payment" if row.payment_log_id else "subscription charge" if row.subscription_charge_id else "payment"
        if status == "lost":
            title = f"A dispute of {amount} on a {what} was lost"
        else:
            title = f"A {what} of {amount} was disputed" + (f" ({club_name})" if club_name else "")
        due = f" Respond by {row.respond_by.strftime('%-d %b %Y')}." if row.respond_by and status != "lost" else ""
        await notify(
            session, "payment_disputed", title=title,
            body=f"Razorpay dispute {row.razorpay_dispute_id} on payment {row.razorpay_payment_id or 'unknown'}"
                 f" ({row.reason_code or 'no reason given'}).{due}",
            club=row.club_id,
            data={"dispute_id": row.razorpay_dispute_id, "payment_id": row.razorpay_payment_id, "status": status,
                  "amount_paise": row.amount_paise, "respond_by": _iso(row.respond_by), "id": row.id},
            dedupe_on={"dispute_id": row.razorpay_dispute_id, "status": status},
        )
    await session.commit()


# ─── The page ────────────────────────────────────────────────────────────


def _out(row: PaymentDisputes, clubs: dict[int, Clubs], events: dict[int, str]) -> dict:
    club = clubs.get(row.club_id) if row.club_id else None
    now = _now()
    return {
        "id": row.id,
        "razorpay_dispute_id": row.razorpay_dispute_id,
        "razorpay_payment_id": row.razorpay_payment_id,
        "payment_log_id": str(row.payment_log_id) if row.payment_log_id else None,
        "subscription_charge_id": row.subscription_charge_id,
        "kind": "ticket" if row.payment_log_id else "subscription" if row.subscription_charge_id else "unknown",
        "amount": float(money.from_paise(row.amount_paise)),
        "amount_deducted": float(money.from_paise(row.amount_deducted_paise)),
        "currency": row.currency,
        "reason_code": row.reason_code,
        "reason": row.reason_description,
        "phase": row.phase,
        "status": row.status,
        "is_open": row.status in OPEN,
        "respond_by": _iso(row.respond_by),
        "overdue": bool(row.respond_by and row.status in OPEN and row.respond_by < now),
        "opened_at": _iso(row.opened_at),
        "closed_at": _iso(row.closed_at),
        "updated_at": _iso(row.updated_at),
        "last_event": row.last_event,
        "club": {"id": club.id, "name": club.name, "slug": club.slug} if club is not None else None,
        "event": {"id": row.event_id, "name": events.get(row.event_id)} if row.event_id else None,
        "notes": list(row.notes or []),
    }


async def _context(session, rows: list[PaymentDisputes]) -> tuple[dict, dict]:
    club_ids = {r.club_id for r in rows if r.club_id}
    event_ids = {r.event_id for r in rows if r.event_id}
    clubs = {c.id: c for c in (await session.scalars(
        select(Clubs).where(Clubs.id.in_(club_ids)).execution_options(include_deleted=True)
    )).all()} if club_ids else {}
    events = dict((await session.execute(
        select(Events.id, Events.name).where(Events.id.in_(event_ids)).execution_options(include_deleted=True)
    )).all()) if event_ids else {}
    return clubs, events


@router.get("", summary="Razorpay disputes on the platform's account (admin)")
async def list_disputes(
    session: SessionDep,
    user: AdminAuth,
    status: str = Query("open", description="open, closed, or all"),
    q: Optional[str] = Query(None, max_length=80, description="A dispute or payment id"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    where = []
    if status == "open":
        where.append(PaymentDisputes.status.in_(OPEN))
    elif status == "closed":
        where.append(PaymentDisputes.status.in_(CLOSED))
    elif status != "all":
        raise CustomHTTPException(422, message="status is open, closed or all")
    if q and q.strip():
        term = f"%{q.strip()}%"
        where.append(or_(PaymentDisputes.razorpay_dispute_id.ilike(term), PaymentDisputes.razorpay_payment_id.ilike(term)))
    total = int(await session.scalar(select(func.count()).select_from(PaymentDisputes).where(*where)) or 0)
    rows = list((await session.scalars(
        select(PaymentDisputes).where(*where)
        .order_by(PaymentDisputes.respond_by.asc().nulls_last() if status == "open" else PaymentDisputes.updated_at.desc(),
                  PaymentDisputes.id.desc())
        .offset(offset).limit(limit)
    )).all())
    clubs, events = await _context(session, rows)
    open_count = int(await session.scalar(select(func.count()).select_from(PaymentDisputes).where(PaymentDisputes.status.in_(OPEN))) or 0)
    return {"items": [_out(r, clubs, events) for r in rows], "total": total, "open": open_count, "offset": offset, "limit": limit}


async def _row(session, dispute_id: int, *, lock: bool = False) -> PaymentDisputes:
    q = select(PaymentDisputes).where(PaymentDisputes.id == dispute_id)
    row = await session.scalar(q.with_for_update() if lock else q)
    if row is None:
        raise CustomHTTPException(404, message="No such dispute", error_code="DISPUTE_NOT_FOUND")
    return row


@router.get("/{dispute_id}", summary="One dispute (admin)")
async def get_dispute(dispute_id: int, session: SessionDep, user: AdminAuth) -> dict:
    row = await _row(session, dispute_id)
    clubs, events = await _context(session, [row])
    return _out(row, clubs, events)


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@router.post("/{dispute_id}/notes", summary="Add a note to a dispute (moderate)", status_code=201)
async def add_note(dispute_id: int, body: NoteIn, session: SessionDep, user: ModerateAuth, request: Request) -> dict:
    row = await _row(session, dispute_id, lock=True)
    notes = list(row.notes or [])
    if len(notes) >= NOTES_MAX:
        raise CustomHTTPException(409, message="This dispute has as many notes as it can hold", error_code="TOO_MANY_NOTES")
    text = body.text.strip()
    notes.append({"at": _now().isoformat(), "by_id": user.id, "by_name": user.full_name or user.email, "text": text})
    row.notes = notes
    row.updated_at = _now()
    await audit.record(session, actor=user, action="dispute.note", entity_type="dispute", entity_id=row.id,
                       summary=f"Noted on dispute {row.razorpay_dispute_id}: {text}"[:255], request=request)
    await session.commit()
    clubs, events = await _context(session, [row])
    return _out(row, clubs, events)
