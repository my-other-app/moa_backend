"""Reading one person: who they are on this event, and every fact about them.

One query per source, never per row: the registration, the requests that led
to it, the account, then each table that holds a fact about them (check-in,
card prints, merch, payments, deliveries, asks, ...) read once with the
person's ids. What those rows say as a log is `timeline.py`'s.

A person is a registration, a registration request, or both: a request that
was confirmed (or whose person registered another way) belongs to the same
person as its registration, and the answer is the one continuous history.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app.api.approvals.models import RegistrationRequests as Req
from app.api.events.models import EventRatingsLink, Events, EventRegistrationsLink as Reg
from app.response import CustomHTTPException

#: A source that can grow without bound (a log, a message history) is read to here.
SOURCE_CAP = 200
#: The requests one person can have made on one event (a decline and a new try, ...).
REQUESTS_CAP = 25


def _uuid(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _registration(session, event_id: int, where) -> Optional[Reg]:
    """A registration of this event, a live one before a removed one."""
    return await session.scalar(
        select(Reg).where(Reg.event_id == event_id, where)
        .order_by(Reg.is_deleted, Reg.created_at.desc()).limit(1)
        .execution_options(include_deleted=True)
    )


async def resolve(session, event: Events, ref: str) -> tuple[Optional[Reg], Optional[Req], list[Req]]:
    """The person `ref` names, as (registration, the request ref named or the
    one that led to the registration, every request of theirs on this event).
    404 for a ref of nobody on this event (another event's person included)."""
    ref = (ref or "").strip()
    as_uuid = _uuid(ref)
    reg: Optional[Reg] = None
    req: Optional[Req] = None
    if as_uuid is not None:
        reg = await _registration(session, event.id, Reg.id == as_uuid)
        if reg is None:
            req = await session.scalar(select(Req).where(Req.id == as_uuid, Req.event_id == event.id))
    elif ref.upper().startswith("MOA"):
        reg = await _registration(session, event.id, Reg.ticket_id == ref)
    if reg is None and req is None:
        raise CustomHTTPException(404, message="Person not found")
    if reg is None:
        # A request whose person is on the list now: confirmed (its registration),
        # or registered another way since.
        match = [Reg.id == req.registration_id] if req.registration_id is not None else []
        if req.user_id is not None:
            match.append(Reg.user_id == req.user_id)
        match.append(func.lower(Reg.email) == (req.email or "").strip().lower())
        reg = await _registration(session, event.id, or_(*match) & (Reg.is_deleted == False))  # noqa: E712
        if reg is None and req.registration_id is not None:
            # Confirmed, then taken off the list: the registration still tells the story.
            reg = await _registration(session, event.id, Reg.id == req.registration_id)
    if reg is not None:
        match = [Req.registration_id == reg.id, Req.user_id == reg.user_id, func.lower(Req.email) == (reg.email or "").strip().lower()]
        requests = list((await session.scalars(
            select(Req).where(Req.event_id == event.id, or_(*match)).order_by(Req.submitted_at, Req.id).limit(REQUESTS_CAP)
        )).all())
        if req is None:
            # The one that led to the registration, else their latest.
            req = next((r for r in requests if r.registration_id == reg.id), None) or (requests[-1] if requests else None)
    else:
        requests = [req]
    return reg, req, requests


async def gather(session, event: Events, reg: Optional[Reg], req: Optional[Req], requests: list[Req]) -> SimpleNamespace:
    """Every fact about the person, one query per source."""
    from app.api.admin_ops.models import PaymentRefunds
    from app.api.audit.models import AdminAuditLogs
    from app.api.checkins.models import CheckIns, Doors
    from app.api.club_ops.models import GuestNotices
    from app.api.deliveries.models import DeliveryAttempts, Deliveries
    from app.api.emails.models import MessageDeliveries, Messages, ReviewResendRecipients, ReviewResends
    from app.api.idcards.models import PrintJobs
    from app.api.kiosk.models import CardPrintRequests, KioskDevices
    from app.api.links.models import TrackingLinks
    from app.api.live.models import FeedbackResponses, FeedbackSends
    from app.api.merch.models import MerchCollections, MerchItems, MerchVariants
    from app.api.missing_answers.models import AnswerAskPeople, AnswerAskSends, AnswerAsks
    from app.api.payments.models import PaymentLogs, PaymentOrders
    from app.api.ticket_cancel.models import TicketCancellations
    from app.api.ticketing import types as ticket_types
    from app.api.ticketing.models import WaitlistEntries
    from app.api.users.models import UserProfiles, Users

    d = SimpleNamespace(event=event, reg=reg, req=req, requests=requests)
    req_ids = [r.id for r in requests]
    reg_id = reg.id if reg is not None else None
    user_id = (reg.user_id if reg is not None else None) or next((r.user_id for r in requests if r.user_id is not None), None)

    # The account, with their organisation and photo.
    d.user = None
    if user_id is not None:
        d.user = await session.scalar(
            select(Users).where(Users.id == user_id)
            .options(joinedload(Users.profile).options(joinedload(UserProfiles.org), joinedload(UserProfiles.avatar)))
            .execution_options(include_deleted=True)
        )
    type_ids = [t for t in ((reg.ticket_type_id if reg else None), *(r.ticket_type_id for r in requests)) if t is not None]
    d.type_names = await ticket_types.names(session, type_ids)
    d.tracking_link = None
    if reg is not None and reg.tracking_link_id is not None:
        d.tracking_link = await session.scalar(select(TrackingLinks.name).where(TrackingLinks.id == reg.tracking_link_id))

    d.check_in = d.door_name = None
    d.deliveries = []
    d.attempts = []
    d.print_jobs = []
    d.print_requests = []
    d.merch = []
    d.orders = []
    d.logs = []
    d.refunds = []
    d.waitlist = []
    d.messages = []
    d.review_sends = []
    d.ask_people = []
    d.ask_sends = []
    d.cancellation = None
    d.rating = None
    d.feedback_sends = []
    d.feedback_responses = []
    d.notices = []
    if reg is not None:
        row = (await session.execute(
            select(CheckIns, Doors.name).outerjoin(Doors, Doors.id == CheckIns.door_id).where(CheckIns.registration_id == reg.id)
        )).first()
        if row is not None:
            d.check_in, d.door_name = row
        d.deliveries = list((await session.scalars(
            select(Deliveries).where(Deliveries.registration_id == reg.id).order_by(Deliveries.kind, Deliveries.channel)
        )).all())
        d.attempts = list((await session.execute(
            select(DeliveryAttempts, Deliveries.kind, Deliveries.channel)
            .join(Deliveries, Deliveries.id == DeliveryAttempts.delivery_id)
            .where(Deliveries.registration_id == reg.id)
            .order_by(DeliveryAttempts.created_at.desc()).limit(SOURCE_CAP)
        )).all())
        d.print_jobs = list((await session.scalars(
            select(PrintJobs).where(PrintJobs.registration_id == reg.id).order_by(PrintJobs.created_at.desc()).limit(SOURCE_CAP)
        )).all())
        d.print_requests = list((await session.scalars(
            select(CardPrintRequests).where(CardPrintRequests.registration_id == reg.id).order_by(CardPrintRequests.created_at.desc()).limit(SOURCE_CAP)
        )).all())
        d.merch = list((await session.execute(
            select(MerchCollections, MerchItems.name, MerchVariants.label)
            .join(MerchItems, MerchItems.id == MerchCollections.item_id)
            .outerjoin(MerchVariants, MerchVariants.id == MerchCollections.variant_id)
            .where(MerchCollections.registration_id == reg.id)
            .order_by(MerchCollections.collected_at.desc()).limit(SOURCE_CAP)
        )).all())
        if reg.payment_receipt:
            d.orders = list((await session.scalars(
                select(PaymentOrders).where(PaymentOrders.receipt == reg.payment_receipt)
                .order_by(PaymentOrders.created_at).limit(SOURCE_CAP).execution_options(include_deleted=True)
            )).all())
            order_ids = [o.id for o in d.orders]
            if order_ids:
                d.logs = list((await session.scalars(
                    select(PaymentLogs).where(PaymentLogs.order_id.in_(order_ids))
                    .order_by(PaymentLogs.created_at).limit(SOURCE_CAP).execution_options(include_deleted=True)
                )).all())
            log_ids = [lg.id for lg in d.logs]
            if log_ids:
                d.refunds = list((await session.scalars(
                    select(PaymentRefunds).where(PaymentRefunds.payment_log_id.in_(log_ids), PaymentRefunds.kind != "store_order")
                    .order_by(PaymentRefunds.created_at).limit(SOURCE_CAP)
                )).all())
        d.messages = list((await session.execute(
            select(MessageDeliveries, Messages.subject, Messages.kind, Messages.created_by)
            .join(Messages, Messages.id == MessageDeliveries.message_id)
            .where(MessageDeliveries.registration_id == reg.id)
            .order_by(MessageDeliveries.created_at.desc()).limit(SOURCE_CAP)
        )).all())
        d.cancellation = await session.scalar(select(TicketCancellations).where(TicketCancellations.registration_id == reg.id))
        if reg.user_id is not None:
            d.rating = await session.scalar(
                select(EventRatingsLink).where(EventRatingsLink.event_id == event.id, EventRatingsLink.user_id == reg.user_id)
                .order_by(EventRatingsLink.created_at.desc()).limit(1)
            )
        d.feedback_sends = list((await session.scalars(select(FeedbackSends).where(FeedbackSends.registration_id == reg.id).limit(SOURCE_CAP))).all())
        d.feedback_responses = list((await session.scalars(select(FeedbackResponses).where(FeedbackResponses.registration_id == reg.id).limit(SOURCE_CAP))).all())
        d.notices = list((await session.scalars(
            select(GuestNotices).where(GuestNotices.registration_id == reg.id).order_by(GuestNotices.created_at.desc()).limit(SOURCE_CAP)
        )).all())
    # The waiting-list entries that led to them: the registration a claim made, or a request's hold.
    holds = [r.waitlist_entry_id for r in requests if r.waitlist_entry_id is not None]
    where = [WaitlistEntries.id.in_(holds)] if holds else []
    if reg_id is not None:
        where.append(WaitlistEntries.registration_id == reg_id)
    if where:
        d.waitlist = list((await session.scalars(select(WaitlistEntries).where(or_(*where)).order_by(WaitlistEntries.created_at).limit(SOURCE_CAP))).all())
    if req_ids:
        d.review_sends = list((await session.execute(
            select(ReviewResendRecipients, ReviewResends.stage, ReviewResends.created_by_name, ReviewResends.created_by)
            .join(ReviewResends, ReviewResends.id == ReviewResendRecipients.resend_id)
            .where(ReviewResendRecipients.request_id.in_(req_ids))
            .order_by(ReviewResendRecipients.created_at.desc()).limit(SOURCE_CAP)
        )).all())
    # What they were asked for, and what became of it.
    people_where = [(AnswerAskPeople.ref_kind == "request") & AnswerAskPeople.ref_id.in_(req_ids)] if req_ids else []
    if reg_id is not None:
        people_where.append((AnswerAskPeople.ref_kind == "registration") & (AnswerAskPeople.ref_id == reg_id))
    if people_where:
        d.ask_people = list((await session.execute(
            select(AnswerAskPeople, AnswerAsks)
            .join(AnswerAsks, AnswerAsks.id == AnswerAskPeople.ask_id)
            .where(AnswerAskPeople.event_id == event.id, or_(*people_where))
            .order_by(AnswerAskPeople.added_at.desc()).limit(SOURCE_CAP)
        )).all())
        sends_where = [(AnswerAskSends.ref_kind == "request") & AnswerAskSends.ref_id.in_(req_ids)] if req_ids else []
        if reg_id is not None:
            sends_where.append((AnswerAskSends.ref_kind == "registration") & (AnswerAskSends.ref_id == reg_id))
        ask_ids = [a.id for _, a in d.ask_people]
        if ask_ids:
            d.ask_sends = list((await session.scalars(
                select(AnswerAskSends).where(AnswerAskSends.ask_id.in_(ask_ids), or_(*sends_where))
                .order_by(AnswerAskSends.created_at.desc()).limit(SOURCE_CAP)
            )).all())

    # The audit log: what was done to this registration, request or payment.
    entity = []
    if reg_id is not None:
        entity.append((AdminAuditLogs.entity_type == "registration") & (AdminAuditLogs.entity_id == str(reg_id)))
    if req_ids:
        entity.append((AdminAuditLogs.entity_type == "registration_request") & AdminAuditLogs.entity_id.in_([str(i) for i in req_ids]))
    if d.logs:
        entity.append((AdminAuditLogs.entity_type == "payment") & AdminAuditLogs.entity_id.in_([str(lg.id) for lg in d.logs]))
    d.audit = []
    if entity:
        d.audit = list((await session.scalars(
            select(AdminAuditLogs).where(or_(*entity)).order_by(AdminAuditLogs.created_at.desc(), AdminAuditLogs.id.desc()).limit(SOURCE_CAP)
        )).all())

    # Names for every id above, in two reads.
    user_ids = {x for x in (
        *(r.decided_by_id for r in requests),
        d.check_in.checked_in_by if d.check_in is not None else None,
        *(c.collected_by for c, _, _ in d.merch),
        *(j.requested_by for j in d.print_jobs),
        *(p.requested_by_user_id for p in d.print_requests),
        *(r.created_by for r in d.refunds),
        *(a.actor_id for a in d.audit),
        *(by for _, _, _, by in d.review_sends),
        *(by for _, _, _, by in d.messages),
        *(a.created_by_user_id for _, a in d.ask_people),
    ) if x is not None}
    d.user_names = {}
    if user_ids:
        d.user_names = {i: (n or e) for i, n, e in (await session.execute(
            select(Users.id, Users.full_name, Users.email).where(Users.id.in_(user_ids)).execution_options(include_deleted=True)
        )).all()}
    device_ids = {x for x in (
        d.check_in.kiosk_device_id if d.check_in is not None else None,
        *(p.requested_by_device_id for p in d.print_requests),
        *(p.printer_id for p in d.print_requests),
        *(p.claimed_by for p in d.print_requests),
    ) if x is not None}
    d.devices = {}
    if device_ids:
        d.devices = {i: (n, door) for i, n, door in (await session.execute(
            select(KioskDevices.id, KioskDevices.name, KioskDevices.door_label).where(KioskDevices.id.in_(device_ids))
        )).all()}
    return d
