"""Beside a case: the person's account, their registrations and tickets,
their payments, refunds and disputes, and the clubs they run -- each with the
admin portal's path to open it, where the existing actions (refund, resend
the ticket, suspend) keep their own permissions.

Whose: the user the case links to, else the account that asked, else an
app account with the address a guest wrote from (said so: `matched_by`).
Read only; nothing here changes anything.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import func, or_, select

from app.api.support.models import SupportCases

LIMIT = 10


def _uuid(value) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _account(session, case: SupportCases):
    from app.api.users.models import UserTypes, Users

    links = case.links or {}
    if links.get("user_id"):
        user = await session.get(Users, int(links["user_id"]))
        if user is not None:
            return user, "link"
    if case.requester_user_id:
        user = await session.get(Users, case.requester_user_id)
        if user is not None:
            return user, "requester"
    if case.requester_email and case.requester_kind == "guest":
        user = await session.scalar(
            select(Users).where(
                func.lower(func.btrim(Users.email)) == case.requester_email.strip().lower(), Users.user_type == UserTypes.app_user,
            ).limit(1)
        )
        if user is not None:
            return user, "email"
    return None, None


async def panel(session, case: SupportCases) -> dict:
    from app.api.admin_ops.models import PaymentDisputes, PaymentRefunds
    from app.api.club_access.models import ClubMembers
    from app.api.clubs.models import Clubs
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.payments.models import PaymentLogs, PaymentOrders

    user, matched_by = await _account(session, case)
    out: dict = {"account": None, "matched_by": matched_by, "registrations": [], "payments": [], "refunds": [], "disputes": [],
                 "clubs": [], "club": None}
    club_id = (case.links or {}).get("club_id") or case.requester_club_id
    if club_id:
        club = await session.get(Clubs, int(club_id))
        if club is not None:
            out["club"] = {"id": club.id, "name": club.name, "slug": club.slug, "verified": bool(club.is_verified),
                           "path": f"/clubs/{club.id}"}
    if user is None:
        return out
    provider = getattr(user.provider, "value", user.provider)
    out["account"] = {
        "id": user.id, "name": user.full_name, "email": user.email, "phone": user.phone,
        "user_type": getattr(user.user_type, "value", user.user_type), "sign_in": provider or ("email" if user.password else None),
        "has_password": bool(user.password), "email_verified": bool(user.email_verified),
        "suspended": getattr(user, "is_active", True) is False, "suspended_reason": user.suspended_reason,
        "created_at": user.created_at, "path": f"/users/{user.id}",
    }
    R = EventRegistrationsLink
    regs = (await session.execute(
        select(R, Events.name).join(Events, Events.id == R.event_id).where(R.user_id == user.id)
        .order_by(R.created_at.desc()).limit(LIMIT)
    )).all()
    out["registrations"] = [
        {"id": str(r.id), "event_id": r.event_id, "event_name": name, "ticket_id": r.ticket_id, "created_at": r.created_at,
         "is_paid": bool(r.is_paid), "paid_amount": float(r.paid_amount or 0), "attended": bool(r.is_attended),
         "ticket_revoked": r.ticket_revoked_at is not None, "path": f"/events/{r.event_id}"}
        for r, name in regs
    ]
    orders = (await session.execute(
        select(PaymentOrders, PaymentLogs).outerjoin(PaymentLogs, PaymentLogs.order_id == PaymentOrders.id)
        .where(PaymentOrders.user_id == user.id).order_by(PaymentOrders.created_at.desc()).limit(LIMIT)
    )).all()
    log_ids = []
    for order, log in orders:
        if log is not None:
            log_ids.append(log.id)
        out["payments"].append({
            "order_id": str(order.id), "receipt": order.receipt, "amount": float(order.amount or 0), "currency": order.currency,
            "status": getattr(order.status, "value", order.status), "source": order.source, "created_at": order.created_at,
            "payment_id": str(log.id) if log is not None else None,
            "razorpay_payment_id": log.razorpay_payment_id if log is not None else None,
            "payment_status": getattr(log.status, "value", log.status) if log is not None else None,
            "path": f"/payments?q={log.razorpay_payment_id if log is not None else order.receipt}",
        })
    reg_ids = [r.id for r, _ in regs]
    refund_where = [PaymentRefunds.payment_log_id.in_(log_ids)] if log_ids else []
    if reg_ids:
        refund_where.append(PaymentRefunds.registration_id.in_(reg_ids))
    if refund_where:
        refunds = (await session.scalars(
            select(PaymentRefunds).where(or_(*refund_where)).order_by(PaymentRefunds.created_at.desc()).limit(LIMIT)
        )).all()
        out["refunds"] = [
            {"id": f.id, "amount": float(f.amount or 0), "reason": f.reason, "kind": f.kind, "created_at": f.created_at,
             "razorpay_payment_id": f.razorpay_payment_id, "path": f"/payments?q={f.razorpay_payment_id}"}
            for f in refunds
        ]
    if log_ids:
        disputes = (await session.scalars(
            select(PaymentDisputes).where(PaymentDisputes.payment_log_id.in_(log_ids)).limit(LIMIT)
        )).all()
        out["disputes"] = [
            {"id": d.id, "status": d.status, "phase": d.phase, "amount_paise": int(d.amount_paise or 0),
             "razorpay_payment_id": d.razorpay_payment_id, "path": f"/payments?q={d.razorpay_payment_id or ''}"}
            for d in disputes
        ]
    runs = (await session.execute(
        select(Clubs.id, Clubs.name, Clubs.slug).where(Clubs.user_id == user.id)
    )).all()
    member_of = (await session.execute(
        select(Clubs.id, Clubs.name, Clubs.slug, ClubMembers.role).join(ClubMembers, ClubMembers.club_id == Clubs.id)
        .where(func.lower(ClubMembers.email) == (user.email or "").strip().lower(), ClubMembers.status == "active")
    )).all()
    seen = set()
    for cid, name, slug, *role in [*runs, *member_of]:
        if cid in seen:
            continue
        seen.add(cid)
        out["clubs"].append({"id": cid, "name": name, "slug": slug, "role": role[0] if role else "owner", "path": f"/clubs/{cid}"})
    return out


async def check_links(session, links: dict) -> dict:
    """The links an admin set, each checked to exist; 422 naming the first that doesn't."""
    from app.api.admin_ops.models import PaymentDisputes, PaymentRefunds
    from app.api.clubs.models import Clubs
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.payments.models import PaymentLogs
    from app.api.users.models import Users
    from app.response import CustomHTTPException

    models = {
        "user_id": (Users, int), "club_id": (Clubs, int), "event_id": (Events, int), "registration_id": (EventRegistrationsLink, _uuid),
        "payment_id": (PaymentLogs, _uuid), "refund_id": (PaymentRefunds, int), "dispute_id": (PaymentDisputes, int),
    }
    out = {}
    for key, value in (links or {}).items():
        if key not in models:
            raise CustomHTTPException(422, message=f"{key} isn't something a case links to.", error_code="INVALID_LINK", errors={"field": key})
        if value in (None, ""):
            continue
        model, cast = models[key]
        try:
            ident = cast(value)
        except (TypeError, ValueError):
            ident = None
        if ident is None or await session.get(model, ident) is None:
            raise CustomHTTPException(422, message=f"There's no such {key.replace('_id', '')}.", error_code="INVALID_LINK",
                                      errors={"field": key})
        out[key] = str(ident) if isinstance(ident, uuid.UUID) else ident
    return out
