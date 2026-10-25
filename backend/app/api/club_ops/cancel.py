"""Cancelling an event that people registered for.

The club gives a reason, sees what will happen (how many guests are told,
how many online payments go back and what they add up to, what was paid at
the door and is the club's to hand back), and confirms. Then, once:

- the event's status becomes `cancelled`: for good (closeout refuses any
  other status, its settings lock), off the status clock, out of every
  public list, while its page still opens and says "Cancelled";
- new registrations and payments are refused (REGISTRATIONS_CLOSED with
  closed_reason "cancelled"; EVENT_CANCELLED);
- every online payment is refunded in full, through the account that took
  it (refunds.py), each one a row of its own: one Razorpay refused is listed
  with its reason and tried again from the dashboard;
- every registered guest is told: by email, in the event's Messages (the
  messages pipeline: suppression, bounces, retries and the club's quota);
  in the app (push and the notification list); and on WhatsApp for those who
  asked for their ticket there (the event_update template, counted and held
  back like any update past the plan's limit).

Cancelling again answers with what happened the first time and does nothing
twice. Deleting the event is still refused while it has registrations.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from app.api.closeout.models import EventStatus
from app.api.club_ops import refunds
from app.api.club_ops.models import (
    CANCELLATION,
    FAILED,
    PENDING,
    PROCESSING,
    SUCCEEDED,
    WHATSAPP,
    ClubRefunds,
    EventCancellations,
    GuestNotices,
)
from app.api.events.models import EventRegistrationsLink as Reg
from app.api.events.models import Events
from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus
from app.core import jobs, money
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FOLLOW_UP_JOB = "club_ops.cancel_follow_up"
#: The messages pipeline's client_ref for the cancellation email: one per event.
_MESSAGE_NS = uuid.UUID("6f0c9f5e-2f0b-4a35-9b0e-0c1d2e3f4a5b")
#: Statuses an event may be cancelled from.
CANCELLABLE = (EventStatus.draft, EventStatus.published, EventStatus.live)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def lifecycle(event: Events) -> EventStatus:
    return event.lifecycle or EventStatus.published


def message_ref(event_id: int) -> uuid.UUID:
    return uuid.uuid5(_MESSAGE_NS, f"cancel:{event_id}")


async def cancellation_of(session, event_id: int) -> Optional[EventCancellations]:
    return await session.scalar(select(EventCancellations).where(EventCancellations.event_id == event_id))


async def public_cancellation(session, event_id) -> Optional[dict]:
    """What the event's public pages say about it being cancelled."""
    if event_id is None:
        return None
    c = await cancellation_of(session, int(event_id))
    if c is None:
        return None
    return {"reason": c.reason, "cancelled_at": c.cancelled_at.isoformat() if c.cancelled_at else None}


# ─── What cancelling does, before and after ──────────────────────────────


async def _to_refund(session, event: Events) -> list[tuple]:
    """(payment log, its registration, paise left to refund) for each online
    payment of the event with money left on it."""
    rows = (await session.execute(
        refunds.payments_query(event.id).where(PaymentLogs.status == PaymentStatus.captured)
    )).all()
    held = await refunds.refunds_of(session, [log.id for log, _, _ in rows])
    out = []
    for log, _order, reg in rows:
        left = refunds.refundable_paise(log, held.get(log.id, []))
        if left > 0:
            out.append((log, reg, left))
    return out


async def _live_registrations(session, event_id: int) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(Reg).where(Reg.event_id == event_id, Reg.is_deleted == False)  # noqa: E712
    ) or 0)


async def _paid_at_door(session, event_id: int) -> tuple[int, int]:
    """Paid, with no online receipt: the desk took the money (as finance counts it)."""
    n, total = (await session.execute(
        select(func.count(), func.coalesce(func.sum(Reg.paid_amount), 0)).where(
            Reg.event_id == event_id, Reg.is_deleted == False, Reg.is_paid == True, Reg.payment_receipt.is_(None),  # noqa: E712
        )
    )).one()
    return int(n or 0), money.to_paise(total or 0)


def _blocked(event: Events) -> Optional[str]:
    status = lifecycle(event)
    if status is EventStatus.cancelled:
        return "This event is already cancelled."
    if status not in CANCELLABLE:
        return "This event is over, so it can't be cancelled."
    return None


async def state(session, event: Events) -> dict:
    """Before cancelling: what it would do. After: what it did, and what is left."""
    c = await cancellation_of(session, event.id)
    registrations = await _live_registrations(session, event.id)
    door_n, door_paise = await _paid_at_door(session, event.id)
    rows = list((await session.scalars(
        select(ClubRefunds).where(ClubRefunds.event_id == event.id, ClubRefunds.source == CANCELLATION)
        .order_by(ClubRefunds.created_at, ClubRefunds.id)
    )).all())
    pending = await _to_refund(session, event)
    if c is None:
        paid_online = len(pending)
        refund_paise = sum(p for _, _, p in pending)
    else:
        paid_online = len(rows) + len(pending)
        refund_paise = sum(money.to_paise(r.amount) for r in rows) + sum(p for _, _, p in pending)
    failures = []
    if rows:
        regs = {r.id: r for r in (await session.scalars(
            select(Reg).where(Reg.id.in_([x.registration_id for x in rows if x.registration_id]))
            .execution_options(include_deleted=True)
        )).all()}
        gateway_of = dict((await session.execute(
            select(PaymentLogs.id, PaymentOrders.payment_gateway_id).join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
            .where(PaymentLogs.id.in_([x.payment_log_id for x in rows]))
        )).all())
        for r in rows:
            if r.status != FAILED:
                continue
            reg = regs.get(r.registration_id)
            failures.append({
                "id": r.id, "registration_id": r.registration_id, "full_name": reg.full_name if reg else None,
                "email": reg.email if reg else None, "amount": float(money.rupees(r.amount)), "error": r.error,
                "attempts": r.attempts or 0, "gateway": "club" if gateway_of.get(r.payment_log_id) else "platform",
            })
    notice = {"email_message_id": None, "push_sent": None, "whatsapp_sent": 0, "whatsapp_skipped": 0}
    if c is not None:
        wa = dict((await session.execute(
            select(GuestNotices.status, func.count()).where(GuestNotices.ref == f"cancel:{event.id}", GuestNotices.channel == WHATSAPP)
            .group_by(GuestNotices.status)
        )).all())
        notice = {
            "email_message_id": c.message_id, "push_sent": c.push_sent,
            "whatsapp_sent": int(wa.get("sent", 0)), "whatsapp_skipped": int(sum(v for k, v in wa.items() if k != "sent")),
        }
    blocked = _blocked(event)
    return {
        "status": lifecycle(event).value,
        "cancelled": c is not None or lifecycle(event) is EventStatus.cancelled,
        "reason": c.reason if c else None, "cancelled_at": c.cancelled_at if c else None,
        "cancelled_by": c.cancelled_by_name if c else None,
        "can_cancel": blocked is None, "blocked_reason": blocked,
        "registrations": registrations if c is None else (c.registrations or registrations),
        "paid_online": paid_online, "refund_total": float(money.from_paise(refund_paise)),
        "paid_at_door": door_n, "paid_at_door_total": float(money.from_paise(door_paise)),
        "refunds": {
            "total": len(rows),
            "succeeded": sum(1 for r in rows if r.status == SUCCEEDED),
            "pending": sum(1 for r in rows if r.status in (PENDING, PROCESSING)),
            "failed": sum(1 for r in rows if r.status == FAILED),
            "refunded_amount": float(money.from_paise(sum(money.to_paise(r.amount) for r in rows if r.status == SUCCEEDED))),
        },
        "failures": failures,
        "notice": notice,
    }


# ─── Cancelling ──────────────────────────────────────────────────────────


def _refuse(status_code: int, code: str, message: str, **errors) -> CustomHTTPException:
    return CustomHTTPException(status_code, message=message, error_code=code, errors=errors or None)


async def _add_refunds(session, event: Events, reason: str, actor) -> list:
    """A cancellation refund for every online payment with money left on it,
    each payment once (uq_club_refunds_cancellation_payment)."""
    added = []
    table = ClubRefunds.__table__
    for log, reg, left in await _to_refund(session, event):
        inserted = (await session.execute(
            pg_insert(table).values(
                id=uuid.uuid4(), club_id=event.club_id, event_id=event.id, payment_log_id=log.id,
                registration_id=reg.id if reg is not None else None,
                amount=money.from_paise(left), reason=f"Event cancelled: {reason}"[:500], source=CANCELLATION, status=PENDING,
                requested_by=actor.user_id, member_id=actor.member_id, impersonated_by=actor.impersonated_by,
                requested_by_name=actor.name, attempts=0, created_at=_now(), updated_at=_now(),
            )
            .on_conflict_do_nothing(index_elements=[table.c.payment_log_id], index_where=table.c.source == CANCELLATION)
            .returning(table.c.id)
        )).scalar_one_or_none()
        if inserted is not None:
            added.append(inserted)
    return added


async def _enqueue_refunds(session, ids: list) -> None:
    for rid in ids:
        await jobs.enqueue(session, refunds.REFUND_JOB, {"refund_id": str(rid)}, dedupe_key=f"{refunds.REFUND_JOB}:{rid}")


async def cancel(session, event_id: int, *, reason: str, actor, expected_registrations: Optional[int] = None,
                 expected_refund_total=None, request=None) -> dict:
    from app.api.audit import service as audit

    event = await session.scalar(
        select(Events).options(joinedload(Events.club)).where(Events.id == event_id).with_for_update(of=Events)
        .execution_options(populate_existing=True)
    )
    if event is None:
        raise _refuse(404, "EVENT_NOT_FOUND", "Event not found")
    if await cancellation_of(session, event.id) is not None or lifecycle(event) is EventStatus.cancelled:
        await session.commit()
        return await state(session, event)  # once: nothing done twice
    blocked = _blocked(event)
    if blocked:
        raise _refuse(409, "CANNOT_CANCEL", blocked)
    preview = await state(session, event)
    stale = (
        (expected_registrations is not None and int(expected_registrations) != preview["registrations"])
        or (expected_refund_total is not None and money.to_paise(expected_refund_total) != money.to_paise(preview["refund_total"]))
    )
    if stale:
        raise _refuse(
            409, "CANCEL_PREVIEW_STALE",
            "Registrations or payments changed while you were deciding. Check the numbers again.",
            registrations=preview["registrations"], refund_total=preview["refund_total"], paid_online=preview["paid_online"],
        )
    now = _now()
    previous = lifecycle(event).value
    event.lifecycle = EventStatus.cancelled
    event.status_clock = False
    c = EventCancellations(
        event_id=event.id, club_id=event.club_id, reason=reason.strip()[:500], previous_status=previous, cancelled_at=now,
        cancelled_by=actor.user_id, cancelled_by_name=actor.name, member_id=actor.member_id,
        impersonated_by=actor.impersonated_by, registrations=preview["registrations"],
    )
    session.add(c)
    await session.flush()
    ids = await _add_refunds(session, event, c.reason, actor)
    await audit.record(
        session, actor=actor.user, action="event.cancel", entity_type="event", entity_id=event.id,
        summary=f"Cancelled {event.name}: {preview['registrations']} told, {len(ids)} refunds of ₹{preview['refund_total']:g}"[:255],
        details={"reason": c.reason, "previous_status": previous, "registrations": preview["registrations"],
                 "refunds": len(ids), "refund_total": preview["refund_total"], **actor.details()},
        request=request,
    )
    await jobs.enqueue(session, FOLLOW_UP_JOB, {"event_id": event.id}, dedupe_key=f"{FOLLOW_UP_JOB}:{event.id}")
    await _enqueue_refunds(session, ids)
    await session.commit()
    return await state(session, event)


async def retry_failed(session, event: Events, *, actor, request=None) -> dict:
    """Every failed cancellation refund asked again; and a payment that came
    in after the cancel (a checkout already open) is refunded too."""
    from app.api.audit import service as audit

    c = await cancellation_of(session, event.id)
    if c is None:
        raise _refuse(409, "NOT_CANCELLED", "This event isn't cancelled.")
    failed = list((await session.scalars(
        select(ClubRefunds.id).where(ClubRefunds.event_id == event.id, ClubRefunds.source == CANCELLATION, ClubRefunds.status == FAILED)
    )).all())
    late = await _add_refunds(session, event, c.reason, actor)
    await audit.record(
        session, actor=actor.user, action="event.cancel.retry", entity_type="event", entity_id=event.id,
        summary=f"Tried {len(failed)} failed refunds again for {event.name}"[:255],
        details={"failed": len(failed), "late": len(late), **actor.details()}, request=request,
    )
    await session.commit()
    for rid in failed:
        await refunds.retry(session, event, rid, actor=actor)
    for rid in late:
        await refunds.execute(session, rid)
    return await state(session, event)


# ─── Telling the guests ──────────────────────────────────────────────────


def _email_body(event: Events, club_name: str, reason: str) -> str:
    when = event.event_datetime.strftime("%d %b %Y") if event.event_datetime else ""
    parts = [
        f"{club_name} has cancelled {event.name}{f' ({when})' if when else ''}.",
        f"Why: {reason}",
        "If you paid online, your payment is being refunded in full. It goes back the way you paid "
        "and usually reaches your account in 5–7 working days; we'll email you when it's done.",
        "Your ticket is no longer valid. Sorry for the change of plans.",
    ]
    return "\n\n".join(parts)


async def _email(session, event: Events, c: EventCancellations) -> None:
    from app.api.emails import service as emails
    from app.api.emails.models import MessageKind
    from app.core.mail.schema import EmailAttachments, EmailAudience

    club_name = event.club.name if event.club is not None else "The organisers"
    body = _email_body(event, club_name, c.reason)
    design = await emails.design_for_broadcast(session, event, body, None)
    msg = await emails.start_message(
        session, event, kind=MessageKind.BROADCAST, subject=f"Cancelled: {event.name}"[:200], design=design,
        attachments=EmailAttachments(), audience=EmailAudience(who="ALL"), body=body,
        client_ref=message_ref(event.id), created_by=c.cancelled_by,
    )
    c = await cancellation_of(session, event.id)
    if c.message_id is None:
        c.message_id = msg.id
        await session.commit()


async def _push(session, event: Events, c: EventCancellations) -> None:
    from app.core.notifications.triggers import notify_event_participants

    c = await session.scalar(
        select(EventCancellations).where(EventCancellations.id == c.id).with_for_update().execution_options(populate_existing=True)
    )
    if c.push_at is not None:
        await session.commit()
        return
    c.push_at = _now()  # claimed: once
    await session.commit()
    user_ids = sorted({u for u in (await session.scalars(
        select(Reg.user_id).where(Reg.event_id == event.id, Reg.is_deleted == False, Reg.user_id.isnot(None))  # noqa: E712
    )).all()})
    sent = 0
    if user_ids:
        try:
            result = await notify_event_participants(
                session, event.id, title=f"Cancelled: {event.name}"[:255],
                body=f"{c.reason[:300]} Paid online? Your payment is being refunded in full.",
                user_ids=user_ids, club_id=event.club_id,
            )
            sent = result[0] if isinstance(result, tuple) else int(result or 0)
        except Exception:  # noqa: BLE001
            logger.warning("event %s: the cancellation push could not be sent", event.id, exc_info=True)
    c = await cancellation_of(session, event.id)
    c.push_sent = sent
    await session.commit()


async def _whatsapp(session, event: Events, c: EventCancellations) -> None:
    from app.core import whatsapp

    if not whatsapp.configured():
        return
    regs = list((await session.scalars(
        select(Reg).where(
            Reg.event_id == event.id, Reg.is_deleted == False, Reg.whatsapp_opt_in == True,  # noqa: E712
            Reg.wa_bounced_at.is_(None), Reg.phone.isnot(None),
        )
    )).all())
    ref = f"cancel:{event.id}"
    club_name = event.club.name if event.club is not None else "The organisers"
    table = GuestNotices.__table__
    for reg in regs:
        claimed = (await session.execute(
            pg_insert(table).values(
                ref=ref, event_id=event.id, registration_id=reg.id, channel=WHATSAPP, status="sending",
                created_at=_now(), updated_at=_now(),
            ).on_conflict_do_nothing(constraint="uq_guest_notices_ref_registration_channel").returning(table.c.id)
        )).scalar_one_or_none()
        await session.commit()
        if claimed is None:
            continue  # told already (or being told)
        status, error, provider_id = "sent", None, None
        try:
            answer = await whatsapp.send(
                reg.phone,
                whatsapp.event_update(name=reg.full_name or "", club=club_name, event=event.name,
                                      update="it has been cancelled. If you paid online, your payment is being refunded in full"),
                meter={"club_id": event.club_id, "kind": "update", "ref": f"{ref}:{reg.id}"},
            )
            provider_id = (answer or {}).get("MessageId")
        except whatsapp.Undeliverable as exc:
            status, error = "skipped", str(exc)[:300]
        except Exception as exc:  # noqa: BLE001 - refused, unconfirmed, deferred: recorded, not sent again
            status, error = "failed", (str(exc) or exc.__class__.__name__)[:300]
        row = await session.get(GuestNotices, claimed)
        row.status, row.error, row.provider_id = status, error, provider_id
        await session.commit()


async def follow_up(session, event_id: int) -> None:
    """Tell everyone: each channel once, whatever else failed."""
    event = await session.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == event_id))
    c = await cancellation_of(session, event_id)
    if event is None or c is None:
        return
    for step in (_email, _push, _whatsapp):
        try:
            await step(session, event, c)
        except Exception:  # noqa: BLE001 - one channel failing must not stop the others
            logger.exception("event %s: cancellation %s failed", event_id, step.__name__)
            await session.rollback()


# A send to every guest (WhatsApp one by one): as other sends to many, one slot at most.
@jobs.handler(FOLLOW_UP_JOB, max_attempts=3, timeout=600.0, priority=jobs.NORMAL, concurrency=1)
async def _follow_up_job(p: dict) -> None:
    async with jobs.scope() as session:
        await follow_up(session, int(p["event_id"]))
