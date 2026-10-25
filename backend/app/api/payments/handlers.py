from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import uuid

from fastapi import BackgroundTasks
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.events.models import Events, EventRegistrationsLink
from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus
from app.api.store.models import EventStoreOrders as StoreOrders
from app.core import money
from app.core.validations.exceptions import CodedRequestError, RequestValidationError

logger = logging.getLogger(__name__)


async def _check_still_payable(session: AsyncSession, registration: EventRegistrationsLink, *, ticket_unpaid: bool) -> None:
    """An existing registration may be paid for until its event is over --
    the registration deadline stops new sign-ups, not someone who signed up
    in time finishing their payment -- and, while its ticket is unpaid, only
    while the event has a paid place left: the lower of max_participants and
    the plan's per-event cap, against the paid places (capacity.paid_cap).
    An early answer, without a lock: the capture takes the place exactly."""
    from app.api.payments import capacity

    event = await session.get(Events, registration.event_id)
    if event is None or event.is_deleted:
        raise CodedRequestError(404, "Event not found", "EVENT_NOT_FOUND", event_id="event_id is invalid")
    now = datetime.now(timezone.utc)
    lifecycle = getattr(event.lifecycle, "value", event.lifecycle)
    if lifecycle == "cancelled":
        raise CodedRequestError(409, "This event was cancelled; it can no longer be paid for.", "EVENT_CANCELLED")
    if event.event_datetime + timedelta(hours=event.duration or 0) <= now or lifecycle in ("closed", "archived"):
        raise CodedRequestError(409, "This event is over; it can no longer be paid for.", "EVENT_ENDED")
    if ticket_unpaid and event.has_fee and await capacity.full(session, event):
        raise CodedRequestError(400, "Event is full", "EVENT_FULL", max_participants=event.max_participants)
    if ticket_unpaid and event.has_fee:
        # Its ticket type, and the waiting list (app/api/ticketing/places.py).
        from app.api.ticketing.places import check_payable

        await check_payable(session, event, registration)


async def validate_event_registration_payload(session: AsyncSession, payload: dict):
    """
    validate payment payload of an event registration. Returns (amount in
    rupees as a Decimal, receipt).
    """
    errors = {}
    if "event_id" not in payload:
        errors["event_id"] = "event_id is required"
    if "event_registration_id" not in payload:
        errors["event_registration_id"] = "event_registration_id is required"

    if errors:
        raise RequestValidationError(**errors)

    event_exists = await session.scalar(
        select(exists().where(Events.id == payload["event_id"]))
    )
    if not event_exists:
        raise CodedRequestError(404, "Event not found", "EVENT_NOT_FOUND", event_id="event_id is invalid")

    event_registration = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.id == payload["event_registration_id"]
        )
    )

    if not event_registration:
        raise CodedRequestError(
            404, "Registration not found", "REGISTRATION_NOT_FOUND",
            event_registration_id="event_registration_id is invalid",
        )

    from app.api.store import service as store

    ticket_due = money.rupees(event_registration.actual_amount) - money.rupees(event_registration.paid_amount)
    if ticket_due < 0:
        ticket_due = Decimal("0.00")
    # What they picked from the shelf rides on the same payment as the ticket.
    pending = await store.pending_for(session, event_registration.id)
    store_due = money.rupees(pending.total) if pending is not None else Decimal("0.00")
    if event_registration.is_paid and store_due == 0:
        raise CodedRequestError(
            409, "This registration is already paid for.", "ALREADY_PAID",
            event_registration_id="already paid",
        )
    amount_to_pay = ticket_due + store_due
    if amount_to_pay <= 0:
        raise CodedRequestError(
            422, "There is nothing to pay for this registration.", "NOTHING_TO_PAY",
            event_registration_id="nothing to pay",
        )
    await _check_still_payable(session, event_registration, ticket_unpaid=not event_registration.is_paid and ticket_due > 0)
    if pending is not None:
        # The picks are held for the checkout about to open: a hold that ran
        # out is taken again (SOLD_OUT if the pieces went meanwhile, before
        # any money moves), a live one is given the checkout's time.
        await store.hold_for_checkout(session, pending)
    receipt = f"er_{event_registration.ticket_id}"
    event_registration.payment_receipt = receipt
    await session.commit()
    return (amount_to_pay, receipt)


async def registration_for_order(
    session: AsyncSession, order: PaymentOrders, *, lock: bool = False, include_deleted: bool = False,
) -> EventRegistrationsLink | None:
    """The registration an order pays for: by the id the order was made
    with (its primary key), checked against the receipt; by the receipt when
    the order carries no usable id. `lock` takes its row; `include_deleted`
    finds one deleted since (the ledger: its money was still taken)."""
    def query():
        q = select(EventRegistrationsLink)
        if include_deleted:
            q = q.execution_options(include_deleted=True)
        return q.with_for_update().execution_options(populate_existing=True) if lock else q

    raw = (order.payload or {}).get("event_registration_id")
    try:
        reg_id = uuid.UUID(str(raw)) if raw else None
    except ValueError:
        reg_id = None
    if reg_id is not None:
        reg = await session.scalar(query().where(EventRegistrationsLink.id == reg_id))
        if reg is not None and reg.payment_receipt in (None, order.receipt):
            return reg
    return await session.scalar(
        query().where(EventRegistrationsLink.payment_receipt == order.receipt).order_by(EventRegistrationsLink.created_at).limit(1)
    )


@dataclass
class Applied:
    """What applying a captured payment did to the registration."""

    registration_id: uuid.UUID | None = None
    event_id: int | None = None
    #: The ticket became paid now: its mail goes once this has committed.
    newly_paid: bool = False
    #: The event's last paid place went while this was being paid: nothing
    #: was applied, and the whole payment goes back (service.refund_due).
    full: bool = False


async def handle_event_registration_payment(
    session: AsyncSession,
    order: PaymentOrders,
    background_tasks: BackgroundTasks | None = None,
    *,
    commit: bool = True,
) -> Applied:
    """
    The registration (and the store's picks riding on it) brought in line
    with what this order has captured. Idempotent: run again, it changes
    nothing. `commit=False` leaves the transaction to the caller (verify,
    which marks the order paid in the same one), and the ticket mail to it;
    with `commit=True` it commits and sends the mail itself.

    A ticket about to become paid on a paid event first takes its place
    (capacity.take_paid_place). With none left, nothing is applied, the
    picks' hold is given back, and `full` says so.

    What was applied goes into the ledger in the same transaction (an APPLY
    posting: the ticket and the orders, against the money captured).
    """
    from app.api.ledger import book as ledger_book
    from app.api.ledger import service as ledger
    from app.api.payments import capacity
    from app.api.store import service as store

    event_registration = await registration_for_order(session, order, lock=True)
    if not event_registration:
        raise RequestValidationError(receipt="receipt is invalid")
    # Paid before the ledger existed: posted as it stands, before this changes it.
    await ledger.ensure_history(session, event_registration)
    outcome = Applied(registration_id=event_registration.id, event_id=event_registration.event_id)

    # PaymentLogs.amount is rupees (Numeric), like every other money column.
    total_paid = money.rupees(
        await session.scalar(
            select(func.sum(PaymentLogs.amount)).where(
                PaymentLogs.order_id == order.id,
                PaymentLogs.status == PaymentStatus.captured,
                # A payment being given back automatically paid for nothing.
                PaymentLogs.refund_requested_at.is_(None),
            )
        )
    )

    # The store's share comes off the top; the rest is the ticket's.
    pending = await store.pending_for(session, event_registration.id)
    actual = money.rupees(event_registration.actual_amount)
    ticket_due = max(Decimal(0), actual - money.rupees(event_registration.paid_amount))
    covers_store = pending is not None and total_paid >= money.rupees(pending.total) + ticket_due
    store_share = money.rupees(
        await session.scalar(
            select(func.coalesce(func.sum(StoreOrders.total), 0)).where(
                StoreOrders.registration_id == event_registration.id,
                # Money taken for pieces, sold or oversold, is the store's.
                StoreOrders.status.in_(store.TAKEN),
            )
        )
    ) + (money.rupees(pending.total) if covers_store else Decimal(0))
    ticket_paid = max(Decimal(0), total_paid - store_share)
    becomes_paid = ticket_paid >= actual

    was_paid = bool(event_registration.is_paid)
    if becomes_paid and not was_paid:
        event = await session.get(Events, event_registration.event_id)
        from app.api.ticketing.places import capture_room

        # The event's paid place, then its ticket type's (the waiting list
        # counted, and a place offered from it claimed).
        if event is not None and event.has_fee and not (
            await capacity.take_paid_place(session, event) and await capture_room(session, event, event_registration)
        ):
            if pending is not None:
                await store.release(session, pending, status="cancelled")
            outcome.full = True
            if commit:
                await session.commit()
            return outcome

    if covers_store:
        await store.mark_paid(session, pending)
    event_registration.paid_amount = min(actual, ticket_paid)
    event_registration.is_paid = becomes_paid
    outcome.newly_paid = becomes_paid and not was_paid
    await ledger.sync_registration(session, event_registration, kind=ledger_book.APPLY, channel=ledger.ONLINE)
    if commit:
        await session.commit()
        await send_ticket_if_paid(session, outcome, background_tasks)
    return outcome


async def send_ticket_if_paid(session: AsyncSession, outcome: Applied, background_tasks: BackgroundTasks | None = None) -> None:
    """The paid-for ticket, as the event's invite email through the same
    queue as Send tickets. Never raises: the payment stands either way."""
    if not outcome.newly_paid or outcome.registration_id is None:
        return
    try:
        registration = await session.get(EventRegistrationsLink, outcome.registration_id)
        db_event = await session.scalar(
            select(Events).filter(Events.id == outcome.event_id).options(joinedload(Events.club))
        )
        from app.api.deliveries import service as deliveries

        await deliveries.send_ticket(session, db_event, registration, background_tasks)
    except Exception:
        logger.exception("Error sending the ticket email after payment")


"""

HANDLERS ENTRY POINT

"""


async def validate_payload(session: AsyncSession, source: str, payload: dict):
    """
    validates payment payload based on the source
    """
    if source == "event_registration":
        return await validate_event_registration_payload(session, payload)
    raise RequestValidationError(source="source is invalid")


async def event_for_payload(session: AsyncSession, source: str, payload: dict) -> Events | None:
    """
    The event whose club is selling what this order pays for: the
    registration's own event (not the payload's event_id, which the caller
    wrote). Its club's Razorpay account takes the money.
    """
    if source == "event_registration":
        registration_event = (
            select(EventRegistrationsLink.event_id)
            .where(EventRegistrationsLink.id == payload["event_registration_id"])
            .scalar_subquery()
        )
        return await session.scalar(select(Events).where(Events.id == registration_event))
    return None


async def handle_post_payment(
    session: AsyncSession,
    order: PaymentOrders,
    background_tasks: BackgroundTasks | None = None,
    *,
    commit: bool = True,
) -> Applied:
    """
    handles post payment operations
    """
    if order.source == "event_registration":
        return await handle_event_registration_payment(
            session, order, background_tasks=background_tasks, commit=commit
        )
    raise RequestValidationError(source="source is invalid")
