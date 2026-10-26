from datetime import datetime, time, timedelta, timezone
import logging
import uuid
from fastapi import BackgroundTasks
from pandas import DataFrame
from sqlalchemy import DateTime, Interval, Time, and_, cast, func, literal_column, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.orm.attributes import set_committed_value

logger = logging.getLogger(__name__)

from app.api.users.models import Users
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.registration import answers
from app.api.service import update_background_task_log
from app.api.users.models import UserProfiles, UserTypes, Users
from app.api.admission import service as admission
from app.core import jobs
from app.core.utils.keys import CERTIFICATE_NO_INDEX, TICKET_ID_INDEX, generate_certificate_no, generate_ticket_id, violated_constraint
from app.core.validations.schema import validate_relations
from app.response import CustomHTTPException
from app.api.events.access import ensure_event_access
from app.api.templates_audience import RegistrationRole
from app.api.users import service as user_service
from app.api.models import BackgroundTaskLogs


ALREADY_REGISTERED = (
    "This email is already registered for this event. Your ticket was sent to it; "
    "sign in to the MyOtherApp app with this email to see it."
)
AWAITING_PAYMENT = (
    "This email already has a registration for this event that is waiting for payment. "
    "If you left the payment unfinished, you can start again here in about 30 minutes, "
    "or sign in to the MyOtherApp app with this email to finish paying."
)


#: How long an unpaid registration from the public form holds its address
#: before the address may start again. A checkout left open (a UPI request, a
#: slow bank page) finishes well inside it; one walked away from no longer
#: locks the person out of the form.
ABANDONED_AFTER = timedelta(minutes=30)


async def retire_if_abandoned(session: AsyncSession, event: Events, reg: EventRegistrationsLink) -> bool:
    """Retire an unpaid registration a guest walked away from, so the same
    address can register again from the public form. Only a guest's, only on
    a paid event, never once money moved and never while a payment may still
    land on it. True when it was retired."""
    from app.api.payments.models import OrderStatus, PaymentOrders

    if not event.has_fee or reg.is_paid or (reg.paid_amount or 0) > 0 or reg.is_attended:
        return False
    since = datetime.now(timezone.utc) - ABANDONED_AFTER
    if reg.created_at is not None and reg.created_at > since:
        return False
    holder = await session.get(Users, reg.user_id)
    if holder is None or holder.user_type != UserTypes.guest:
        return False
    if reg.payment_receipt:
        money_or_recent = await session.scalar(
            select(PaymentOrders.id)
            .where(
                PaymentOrders.receipt == reg.payment_receipt,
                or_(
                    PaymentOrders.status.in_([OrderStatus.paid, OrderStatus.refunded]),
                    PaymentOrders.created_at > since,
                ),
            )
            .limit(1)
        )
        if money_or_recent is not None:
            return False
    # Only a live registration is unique per (event, user)
    # (uq_registrations_event_user_live), so any number may be retired.
    # The caller holds the address's lock already (hold_address): this
    # flush takes the event's row (the going_count trigger), and every
    # registration takes the address first and the event's row second.
    reg.is_deleted = True
    # What they picked from the shelf goes back on it.
    from app.api.store import service as store

    pending = await store.pending_for(session, reg.id)
    if pending is not None:
        await store.release(session, pending, status="cancelled")
    await session.flush()
    return True


async def resolve_event(session: AsyncSession, event_id: int | str) -> Events:
    """The live event an id or a slug names."""
    if isinstance(event_id, int) or (isinstance(event_id, str) and event_id.isdigit()):
        where = Events.id == int(event_id)
    else:
        where = Events.slug == event_id
    event = await session.scalar(select(Events).where(where, Events.is_deleted == False))
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    return event


async def live_registration_for_email(
    session: AsyncSession, event_id: int, email: str
) -> EventRegistrationsLink | None:
    """A live registration for this event under this address: made with it,
    or held by any account that has it. Case and stray spaces are ignored.

    Two lookups, each on its own index (ix_registrations_event_email; the
    holders by ix_users_email_normalised, then the event's unique (event,
    user) key), the earlier of the two answers kept. As one OR, Postgres read
    every registration of the event."""
    holders = select(Users.id).where(
        user_service.same_email(Users.email, email), Users.is_deleted == False
    )
    live = (
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
        .order_by(EventRegistrationsLink.created_at)
        .limit(1)
    )
    found = [
        reg
        for reg in (
            await session.scalar(live.where(user_service.same_email(EventRegistrationsLink.email, email))),
            await session.scalar(live.where(EventRegistrationsLink.user_id.in_(holders))),
        )
        if reg is not None
    ]
    return min(found, key=lambda r: r.created_at) if found else None


async def existing_registration(
    session: AsyncSession, event_id: int, user_id: int, email: str
) -> EventRegistrationsLink | None:
    """This user's live registration for the event, else one made with this
    address, case and stray spaces ignored -- as the address's lock is
    taken (hold_address), so "A@x.com" and "a@x.com" sent together are one
    sign-up, not two. Two lookups, each an index probe (the event's unique
    (event, user) key; ix_registrations_event_email). As one
    `user_id = ? OR email = ?` it read every registration of the event."""
    live = select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )
    mine = await session.scalar(live.where(EventRegistrationsLink.user_id == user_id).limit(1))
    if mine is not None:
        return mine
    return await session.scalar(
        live.where(user_service.same_email(EventRegistrationsLink.email, email))
        .order_by(EventRegistrationsLink.created_at)
        .limit(1)
    )


async def places_taken(session: AsyncSession, event: Events) -> int:
    """What the organiser's `max_participants` counts: on a paid event the
    paid registrations (from ix_registrations_event_created alone), on a free
    one all of them -- its going_count, which the database keeps equal to
    that count in the writer's own transaction."""
    if event.has_fee:
        # Soft deletes spelled out, the session's `IS false` form switched
        # off: Postgres cannot match it to the partial index (gates.count).
        return int(await session.scalar(
            select(func.count()).select_from(EventRegistrationsLink).where(
                EventRegistrationsLink.event_id == event.id,
                EventRegistrationsLink.is_deleted == False,
                EventRegistrationsLink.is_paid == True,
            ).execution_options(include_deleted=True)
        ) or 0)
    return int(event.going_count or 0)


async def take_place(session: AsyncSession, event: Events, cap: int | None):
    """The registration takes the event's row: one UPDATE that bumps the
    admission sequence and, on a free event with a cap, only does so while
    there is room (`going_count < cap`). Postgres re-reads the row after any
    registration ahead of this one commits, so two can never both take the
    last place. Returns (admission number's sequence, going_count before
    this registration), or None when the event is full or gone.

    The row stays held until the transaction ends: the insert's own trigger
    updates going_count on it anyway. So this comes last, just before the
    insert, and everything that can be checked first is."""
    table = Events.__table__
    stmt = (
        update(table)
        .where(table.c.id == event.id, table.c.is_deleted == False)
        .values(admission_seq=func.coalesce(table.c.admission_seq, 0) + 1)
        .returning(table.c.admission_seq, table.c.going_count)
    )
    if cap is not None:
        stmt = stmt.where(table.c.going_count < cap)
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    # The event in this session says what the row says now (as the ORM
    # update this replaces did), without writing it again at the commit.
    set_committed_value(event, "admission_seq", int(row[0]))
    return int(row[0]), int(row[1] or 0)


async def hold_address(session: AsyncSession, event_id: int, email: str) -> None:
    """A transaction-scoped advisory lock on this event and address (case and
    spaces ignored), held to the commit. Taken first, before anything that
    writes the event's row (take_place, or a retired registration's
    going_count), so every registration takes the two in one order.
    Re-entrant: the same transaction may take it again."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"registration:{event_id}:{(email or '').strip().lower()}", 0)))
    )


#: Fresh ticket ids tried before a registration gives up. One retry is
#: already a one-in-millions event (keys.py).
TICKET_ATTEMPTS = 5
#: The partial unique index on a live registration's (event, user).
LIVE_REGISTRATION_INDEX = "uq_registrations_event_user_live"
#: An admission number is one live guest's on its event.
ADMISSION_INDEX = "uq_registrations_event_admission"


async def insert_registration(session: AsyncSession, registration: EventRegistrationsLink) -> None:
    """INSERT the registration in a SAVEPOINT, so a ticket id that is
    already taken costs a fresh id and another try, not the registration
    (the event's place and the address's lock stay held); so do an
    admission number and a certificate number someone already holds. A second live
    registration for the same account -- two requests racing past the
    checks with different addresses -- is the 400 a repeat always got."""
    for attempt in range(TICKET_ATTEMPTS):
        try:
            async with session.begin_nested():
                session.add(registration)
                await session.flush()
            return
        except IntegrityError as exc:
            name = violated_constraint(exc)
            if name == LIVE_REGISTRATION_INDEX:
                raise CustomHTTPException(400, message="Already registered for this event", error_code="ALREADY_REGISTERED")
            if attempt == TICKET_ATTEMPTS - 1 or name not in (TICKET_ID_INDEX, ADMISSION_INDEX, CERTIFICATE_NO_INDEX):
                raise
            if name == CERTIFICATE_NO_INDEX:
                logger.warning("certificate number %s was taken; drawing another", registration.certificate_no)
                registration.certificate_no = generate_certificate_no()
            elif name == ADMISSION_INDEX:
                # A number given out by hand or by an import sat where the
                # sequence was going: the next one in the sequence instead.
                logger.warning("admission number %s was taken; numbering again", registration.admission_no)
                event = await session.get(Events, registration.event_id)
                registration.admission_no = admission.number_for(event, await admission.next_seq(session, registration.event_id))
            else:
                logger.warning("ticket id %s was taken; minting another", registration.ticket_id)
                registration.ticket_id = generate_ticket_id()


async def _no_place(session: AsyncSession, event: Events, room, *, organiser: bool) -> CustomHTTPException:
    """Why take_place found no room: the event gone, the organiser's own cap
    (checked first, with its own answer), or the plan's per-event cap."""
    going = await session.scalar(
        select(Events.going_count).where(Events.id == event.id, Events.is_deleted == False)
    )
    if going is None:
        return CustomHTTPException(404, message="Event not found")
    if room is None or room.event_cap is None or (event.max_participants and going >= event.max_participants):
        from app.api.ticketing import places as ticketing_places
        from app.api.ticketing import waitlist as ticketing_waitlist

        return ticketing_places.full(
            waitlist=await ticketing_waitlist.joinable(session, event), max_participants=event.max_participants,
        )
    from app.api.subscriptions import gates

    return await gates.refuse_registration(
        session, event, room.ent, "registrations_per_event", cap=room.event_cap, used=int(going), organiser=organiser
    )


async def add_registration(
    session: AsyncSession,
    db_event: Events,
    priced,
    *,
    full_name: str,
    email: str,
    phone: str | None,
    user_id: int,
    additional_details: dict | None,
    whatsapp_opt_in: bool | None = None,
    role: RegistrationRole | None = None,
    tracking_ref: str | None = None,
    tracking_link_id=None,
    by_organiser: bool = False,
    now: datetime | None = None,
) -> EventRegistrationsLink:
    """A new registration, priced (`priced`: ticketing.quote's answer), its
    place taken and the row inserted, in the caller's transaction: flushed,
    never committed. Every check of the event itself (open, not cancelled,
    the answers) is the caller's; this takes the plan's caps, the event's
    row (take_place) and its ticket type's (take_room), and refuses with
    EVENT_FULL, TICKET_TYPE_SOLD_OUT or the plan's limit when there is no
    place, leaving the event's row as it was.

    register_event makes every registration through this; so does a club
    confirming registration requests (app/api/approvals), one SAVEPOINT a
    request, many in one transaction. `tracking_link_id`: a link already
    resolved (a request kept the one its page carried), else `tracking_ref`."""
    from app.api.subscriptions import gates
    from app.api.ticketing import places as ticketing_places
    from app.api.ticketing import service as ticketing

    now = now or datetime.now(timezone.utc)
    # The club's plan caps registrations per event and per month, on top
    # of the organiser's own max_participants (never for an event made
    # before the caps began).
    room = await gates.check_registration(session, db_event, organiser=by_organiser)

    registration = EventRegistrationsLink(
        event_id=db_event.id,
        user_id=user_id,
        ticket_id=generate_ticket_id(),
        actual_amount=db_event.reg_fee,
        paid_amount=0,
        additional_details=additional_details,
        full_name=full_name,
        email=email,
        phone=phone,
        whatsapp_opt_in=bool(whatsapp_opt_in),
        whatsapp_opt_in_at=now if whatsapp_opt_in else None,
        # Omitted means attendee, which is the column default -- passing
        # None explicitly would violate the not-null constraint.
        **({"role": role} if role is not None else {}),
    )
    # Its type, what it owes, and on a paid event confirmed now when that is nothing.
    priced.apply(registration, db_event)
    # Credited to the tracking link the page carried, if it carried one of this club's.
    if tracking_link_id is not None:
        registration.tracking_link_id = tracking_link_id
    else:
        from app.api.links import service as links

        await links.attribute_registration(session, registration, db_event, tracking_ref)

    # Now the event's row, and the place, in one statement. On a free
    # event the cap is the lower of the organiser's and the plan's; a paid
    # event's places are its paid registrations, which no registration
    # changes, so the checks above stand.
    caps = [] if db_event.has_fee else [c for c in (db_event.max_participants, room.event_cap if room else None) if c]
    taken = await take_place(session, db_event, min(caps) if caps else None)
    if taken is None:
        # Nothing was updated, so nothing is held.
        raise await _no_place(session, db_event, room, organiser=by_organiser)
    seq, going_now = taken
    await gates.recheck_month(session, db_event, room, going_now=going_now, organiser=by_organiser)
    # A ticket that owes nothing holds its place now: its type's, and on a
    # paid event a paid place, the waiting list counted (ticketing.places).
    await ticketing_places.take_room(session, db_event, priced, going_now=going_now)
    # Its admission number, from the event's own counter, before the row is kept.
    registration.admission_no = admission.number_for(db_event, seq)
    await insert_registration(session, registration)
    await ticketing.after_insert(session, db_event, registration, priced)
    return registration


async def register_event(
    session: AsyncSession,
    full_name: str,
    email: str,
    event_id: int | str,
    user_id: int,
    phone: str | None = None,
    background_tasks: BackgroundTasks | None = None,
    additional_details: dict | None = None,
    background_task_log_id: str | None = None,
    role: RegistrationRole | None = None,
    notify: bool = True,
    relaxed_fields: bool = False,
    tracking_ref: str | None = None,
    store_picks: list | None = None,
    claim_existing: bool = True,
    by_organiser: bool = False,
    whatsapp_opt_in: bool | None = None,
    ticket_type_id: int | None = None,
    discount_code: str | None = None,
    offer=None,
):
    """`relaxed_fields` is for a guest registered at the door: nothing is
    required there, and what they answer goes on the record.

    `ticket_type_id`, `discount_code`: the ticket type it is for and the code
    typed for it (app/api/ticketing). Without a type, the event's only one;
    an event selling several refuses it (422 TICKET_TYPE_REQUIRED), but for
    the organiser adding a guest, who gets the default. What it costs is
    worked out here alone (ticketing.quote). `offer`: the waiting-list offer
    it claims (ticketing.waitlist), whose place it takes.

    `whatsapp_opt_in` is the guest's own tick on the form, "Send my ticket
    and event reminders on WhatsApp": True asks for it (and needs a number
    WhatsApp can reach, or the registration is refused with a 422 saying
    so), False or None does not. Never set for a guest the organiser adds:
    consent is the guest's to give. On a registration already made (back
    to pay) a tick adds the consent and no tick leaves it as it was.

    `by_organiser` is for the club adding a guest itself (by hand, by import,
    at the door): when the club's plan caps registrations it is told the
    plan's reason, where a guest on the public form is only told that
    registrations are closed.

    `notify=False` is for a guest the organiser adds or imports: the
    confirmation goes out later through Send tickets, where it is recorded,
    rather than from here where it is not.

    `claim_existing=False` is for a fresh guest from the public form: a
    registration already made under the same address by someone else is
    refused rather than handed back, ticket id and all."""
    is_event_id = (isinstance(event_id, str) and event_id.isdigit()) or isinstance(
        event_id, int
    )

    await validate_relations(
        session,
        (
            {
                "event": (Events, int(event_id)),
                "user": (Users, user_id),
            }
            if is_event_id
            else {
                "slug": (Events, event_id, "slug"),
                "user": (Users, user_id),
            }
        ),
    )
    # The event is read without a lock, and every check below is made before
    # the registration takes the event's row (take_place, just before the
    # insert). The row used to be locked here (SELECT ... FOR UPDATE) and held
    # through the checks, the duplicate lookup and the plan's counts, so one
    # event took its registrations one at a time for as long as all of that
    # took, and longer the bigger the event (docs/SCALING.md). The one check
    # that must be exact -- room left -- is made again by take_place itself.
    if is_event_id:
        event_id = int(event_id)
        db_event = await session.scalar(select(Events).filter(Events.id == event_id))
    else:
        db_event = await session.scalar(select(Events).filter(Events.slug == event_id))

    if not db_event:
        raise CustomHTTPException(404, message="Event not found")
    
    # Check if event is deleted
    if db_event.is_deleted:
        raise CustomHTTPException(404, message="Event not found")
    # Called off by the club (app/api/club_ops): nobody new comes in.
    if getattr(db_event.lifecycle, "value", db_event.lifecycle) == "cancelled":
        raise CustomHTTPException(400, message="This event was cancelled", error_code="REGISTRATIONS_CLOSED", errors={"closed_reason": "cancelled"})
    
    # Check if registration deadline has passed
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    # (A waiting-list offer made in time may be claimed until it runs out.)
    if db_event.reg_enddate and db_event.reg_enddate < now and offer is None:
        raise CustomHTTPException(400, message="Registration deadline has passed", error_code="REGISTRATIONS_CLOSED", errors={"closed_reason": "deadline_passed", "closed_at": db_event.reg_enddate.isoformat()})
    
    # Check if registration hasn't started yet
    if db_event.reg_startdate and db_event.reg_startdate > now:
        raise CustomHTTPException(400, message="Registration has not started yet", error_code="REGISTRATIONS_CLOSED", errors={"closed_reason": "not_started", "opens_at": db_event.reg_startdate.isoformat()})

    # Room left, as max_participants counts it: on a paid event the paid
    # registrations, on a free one all of them. An early answer; on a free
    # event take_place makes it exact.
    if db_event.max_participants:
        if await places_taken(session, db_event) >= db_event.max_participants:
            # Saying whether its waiting list can be joined instead (app/api/ticketing).
            from app.api.ticketing import places as ticketing_places
            from app.api.ticketing import waitlist as ticketing_waitlist

            raise ticketing_places.full(
                waitlist=await ticketing_waitlist.joinable(session, db_event), max_participants=db_event.max_participants,
            )

    # The answers: bounded always, and checked against the event's own
    # questions when it asks any (answers.py). Before any lock is taken.
    additional_details = answers.check(db_event.additional_details, additional_details, relaxed=relaxed_fields)

    # Asked for WhatsApp: a number it can reach, or say so now rather than
    # take the consent and never be able to use it.
    if whatsapp_opt_in:
        from app.core import whatsapp

        number, why = whatsapp.normalise(phone)
        if number is None:
            raise CustomHTTPException(
                422,
                message="To get your ticket on WhatsApp, enter a mobile number WhatsApp can reach, or untick the box.",
                error_code="WHATSAPP_NUMBER",
                # Keyed by the field, as the website reads a registration's field errors.
                errors={"phone": f"WhatsApp can't reach this number: {why}."},
            )

    # An existing registration: this user's, or one made with this address.
    # Two requests with the same address (a double submit) are taken one
    # after the other from here, so the second finds the first's; nobody
    # else waits on it, as everyone waited on the event's row before.
    await hold_address(session, db_event.id, email)
    registration = await existing_registration(session, db_event.id, user_id, email)
    if registration:
        if not claim_existing and registration.user_id != user_id:
            raise CustomHTTPException(409, message=ALREADY_REGISTERED, error_code="ALREADY_REGISTERED")
        from app.api.store import service as store

        # A shelf order still waiting for its payment may be changed, or
        # dropped with `store: []`, on a free event or a paid-for ticket too:
        # the payment it waits for is the order's own.
        changing_picks = store_picks is not None and await store.pending_for(session, registration.id) is not None
        if (not db_event.has_fee or registration.is_paid) and not changing_picks:
            # Still a 400, as the app expects; the code lets every client tell it apart.
            raise CustomHTTPException(400, message="Already registered for this event", error_code="ALREADY_REGISTERED")
        # Back for the payment: the shelf picks are taken fresh, under the
        # event's row as a new registration's are (free picks count against
        # the stock at once). `store: []` drops the earlier order and gives
        # back what it held, so the payment is the ticket's alone.
        if store_picks is not None:
            await session.execute(select(Events.id).where(Events.id == db_event.id).with_for_update())
            await store.place_order(session, db_event, registration, store_picks, role=(registration.role.value if getattr(registration, "role", None) else "attendee"))
            await session.commit()
        if whatsapp_opt_in and not registration.whatsapp_opt_in:
            registration.whatsapp_opt_in = True
            registration.whatsapp_opt_in_at = now
            await session.commit()
        # Back to pay, asking for another ticket type or with a code: priced
        # again (it keeps its own type when it names none).
        if (
            (ticket_type_id is not None and ticket_type_id != registration.ticket_type_id) or discount_code
        ) and not registration.is_paid:
            from app.api.ticketing import quote as ticketing_quote
            from app.api.ticketing import service as ticketing

            priced = await ticketing_quote.quote(
                session, db_event, ticket_type_id=ticket_type_id if ticket_type_id is not None else registration.ticket_type_id,
                discount_code=discount_code, email=email, phone=phone, user_id=user_id, exclude=registration.id,
            )
            if await ticketing.requote(session, db_event, registration, priced) and notify:
                from app.api.deliveries import service as deliveries

                await deliveries.send_ticket(session, db_event, registration, background_tasks)
    else:
        # Its ticket type and what it costs, worked out by the server alone,
        # and every check of them that can refuse it before anything is
        # written (app/api/ticketing/quote.py).
        from app.api.ticketing import quote as ticketing_quote

        priced = await ticketing_quote.quote(
            session, db_event, ticket_type_id=ticket_type_id, discount_code=discount_code, email=email, phone=phone,
            user_id=user_id, organiser=by_organiser, offer=offer,
        )
        registration = await add_registration(
            session, db_event, priced, full_name=full_name, email=email, phone=phone, user_id=user_id,
            additional_details=additional_details, whatsapp_opt_in=whatsapp_opt_in, role=role,
            tracking_ref=tracking_ref, by_organiser=by_organiser, now=now,
        )

        if store_picks:
            from app.api.store import service as store

            # Before the registration is kept, not after: a pick refused here
            # (sold out, no size) must leave nothing behind, or the guest
            # could not try again from the public form, which refuses an
            # address already on the list.
            await store.place_order(session, db_event, registration, store_picks, role=(role.value if role is not None else "attendee"))
        await session.commit()
        await session.refresh(registration)
        db_event = await session.scalar(
            select(Events)
            .filter(Events.id == registration.event_id)
            .options(joinedload(Events.club))
        )
        if (not db_event.has_fee or registration.is_paid) and notify:
            # The ticket goes out as the event's invite email, through the
            # same queue as Send tickets, so the attendee list shows it land.
            # (On a paid event, a ticket that owed nothing: a free type, or a code.)
            try:
                from app.api.deliveries import service as deliveries

                await deliveries.send_ticket(session, db_event, registration, background_tasks)
            except Exception as e:
                logger.exception("Error sending the ticket email")
                if background_task_log_id:
                    await update_background_task_log(
                        session,
                        background_task_log_id,
                        new_logs=[
                            {
                                "level": "ERROR",
                                "message": f"Failed to send email to {email}",
                                "metadata": {"error": str(e)},
                            }
                        ],
                    )

    # By id, not by address: two accounts can register under one address,
    # and the answer must be this caller's own registration.
    data = await session.scalar(
        select(EventRegistrationsLink)
        .where(EventRegistrationsLink.id == registration.id)
        .options(
            selectinload(EventRegistrationsLink.event),
            selectinload(EventRegistrationsLink.user),
        )
    )
    from app.api.store import service as store

    pending_store = await store.pending_for(session, data.id)
    payment_remining = max(0, (data.actual_amount or 0) - (data.paid_amount or 0)) + (pending_store.total if pending_store else 0)
    from app.api.ticketing import service as ticketing_service

    ticket = await ticketing_service.summary_for(session, data)

    # Check for new user badges after registration
    try:
        from app.api.badges.service import check_and_award_user_badges
        await check_and_award_user_badges(session, user_id=data.user_id)
    except Exception as e:
        logging.getLogger(__name__).error(f"Badge check failed after registration: {e}")

    return {
        "event_name": db_event.name,
        "event_id": db_event.id,
        "event_registration_id": data.id,
        "pay_amount": payment_remining,
        "store_order_id": str(pending_store.id) if pending_store else None,
        "store_total": float(pending_store.total) if pending_store else 0.0,
        "event_datetime": db_event.event_datetime,
        "ticket_id": data.ticket_id,
        "full_name": data.full_name,
        "email": data.email,
        "phone": data.phone,
        "additional_details": data.additional_details,
        "whatsapp_opt_in": bool(data.whatsapp_opt_in),
        **ticket,
        "is_paid": bool(data.is_paid),
    }


async def list_event_registrations(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    is_attended: bool | None = None,
    is_paid: bool | None = None,  # None: everyone
    limit: int = 10,
    offset: int = 0,
    search: str | None = None,
    as_admin: bool = False,
    role: RegistrationRole | None = None,
    delivery: str | None = None,
):
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(event, user_id, as_admin)

    # The filters are built once and applied to both the page and the count,
    # so the two can never disagree.
    conditions = []
    if is_attended is not None:
        conditions.append(EventRegistrationsLink.is_attended == is_attended)
    # The guest list's Paid and Unpaid filters: the parameter was taken and
    # never applied, so both showed every guest.
    if is_paid is not None:
        conditions.append(EventRegistrationsLink.is_paid == is_paid)
    if role is not None:
        conditions.append(EventRegistrationsLink.role == role)
    if search:
        search_pattern = f"%{search}%"
        conditions.append(
            (EventRegistrationsLink.full_name.ilike(search_pattern))
            | (EventRegistrationsLink.email.ilike(search_pattern))
            | (EventRegistrationsLink.phone.ilike(search_pattern))
            | (EventRegistrationsLink.ticket_id.ilike(search_pattern))
        )
    if delivery == "not_delivered":
        # Nothing sent, nothing that worked, or something that came back. A
        # guest reached on any channel is not here.
        from app.api.deliveries.models import Deliveries, DeliveryKind, DeliveryOutcome, DeliveryStatus

        reached = select(Deliveries.registration_id).where(
            Deliveries.event_id == event_id,
            Deliveries.kind == DeliveryKind.ticket,
            Deliveries.status == DeliveryStatus.sent,
            (Deliveries.outcome.is_(None)) | (Deliveries.outcome.in_((DeliveryOutcome.delivered, DeliveryOutcome.read))),
        )
        conditions.append(EventRegistrationsLink.id.not_in(reached))

    query = (
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
        .options(
            joinedload(EventRegistrationsLink.user).options(
                joinedload(Users.profile).options(
                    joinedload(UserProfiles.org),
                    joinedload(UserProfiles.avatar)
                )
            ),
            joinedload(EventRegistrationsLink.volunteer),
        )
    )

    for condition in conditions:
        query = query.where(condition)
    # By name, as a list somebody reads from top to bottom: lower(name), id,
    # which ix_registrations_event_name walks in order, so a page reads its
    # rows rather than sorting the whole event first.
    query = query.order_by(func.lower(EventRegistrationsLink.full_name), EventRegistrationsLink.id)

    # Get total count before pagination
    # Use a separate count query to avoid issues with joinedload in subqueries
    count_query = select(func.count()).select_from(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )
    for condition in conditions:
        count_query = count_query.where(condition)

    total = await session.scalar(count_query) or 0

    if limit is not None and offset is not None:
        query = query.limit(limit).offset(offset)

    scalar_result = await session.scalars(query)
    registrations = list(scalar_result)
    # What was sent to each of them, so the list can say who was reached.
    from app.api.deliveries import service as deliveries
    from app.api.deliveries.models import DeliveryKind

    sent = await deliveries.by_registration(
        session, event_id, DeliveryKind.ticket, [r.id for r in registrations]
    )
    from app.api.ticketing import types as ticket_types

    type_names = await ticket_types.names(session, [r.ticket_type_id for r in registrations])
    
    # Manually serialize to avoid Pydantic validation errors with nested SQLAlchemy models
    serialized_results = []
    for reg in registrations:
        try:
            # Serialize user profile
            user_data = None
            if reg.user:
                profile_data = None
                if reg.user.profile:
                    org_data = None
                    if reg.user.profile.org:
                        org_data = {
                            "id": reg.user.profile.org.id,
                            "name": reg.user.profile.org.name,
                            "type": reg.user.profile.org.type.value if hasattr(reg.user.profile.org.type, 'value') else str(reg.user.profile.org.type),
                            "logo": reg.user.profile.org.logo,
                        }
                    avatar_data = None
                    if reg.user.profile.avatar:
                        avatar_data = {
                            "id": reg.user.profile.avatar.id,
                            "name": reg.user.profile.avatar.name,
                            "image": reg.user.profile.avatar.image,
                        }
                    profile_data = {
                        "org": org_data,
                        "avatar": avatar_data,
                        "profile_pic": reg.user.profile.profile_pic,
                    }
                user_data = {
                    "id": reg.user.id,
                    "full_name": reg.user.full_name or "",
                    "email": reg.user.email or "",
                    "phone": reg.user.phone,
                    "username": reg.user.username or reg.user.email or "",  # Fallback to email if no username
                    "user_type": reg.user.user_type.value if hasattr(reg.user.user_type, 'value') else str(reg.user.user_type),
                    "profile": profile_data,
                }
            
            # Serialize volunteer - skip if soft-deleted
            volunteer_data = None
            if reg.volunteer and not getattr(reg.volunteer, 'is_deleted', False):
                volunteer_data = {
                    "email": reg.volunteer.email or "",
                    "is_approved": reg.volunteer.is_approved,
                    "user_id": reg.volunteer.user_id,
                    "profile_pic": None,
                    "full_name": None,
                }
            
            # Get institution from profile.org
            institution = None
            if reg.user and reg.user.profile and reg.user.profile.org:
                institution = reg.user.profile.org.name
            
            serialized_results.append({
                "id": reg.id,
                "ticket_id": reg.ticket_id or "",
                "event_id": reg.event_id,
                "is_paid": reg.is_paid,
                "full_name": reg.full_name or "",
                "email": reg.email or "",
                "phone": reg.phone,
                "actual_amount": float(reg.actual_amount or 0),
                "paid_amount": float(reg.paid_amount or 0),
                "is_attended": reg.is_attended,
                "is_won": reg.is_won,
                # The placing itself, not just whether they placed -- the
                # dashboard shows 1st/2nd/3rd and the badge metrics key on it.
                "position": reg.position,
                # Which ticket and certificate variant this person is issued.
                # This dict is built by hand, so a column left out of it is
                # silently replaced by the response schema's default -- which
                # reported every speaker as an attendee.
                "role": getattr(reg.role, "value", reg.role),
                "user": user_data,
                "volunteer": volunteer_data,
                "institution": institution,
                "name": reg.full_name or "",
                # Answers to the event's custom questions. Previously omitted,
                # which silently dropped them from the registrations export --
                # the export code spreads this key and had been producing
                # nothing for it.
                "additional_details": reg.additional_details,
                "created_at": reg.created_at,
                "ticket_revoked_at": reg.ticket_revoked_at,
                "ticket_deliveries": sent.get(reg.id, []),
                "email_bounced_at": reg.email_bounced_at,
                "email_bounce_reason": reg.email_bounce_reason,
                "admission_no": reg.admission_no,
                "ticket_type_id": reg.ticket_type_id,
                "ticket_type_name": type_names.get(reg.ticket_type_id),
            })
        except Exception as e:
            logger.error(f"Error serializing registration {reg.id}: {e}")
            # Continue with other registrations instead of failing entirely
            continue
    
    return serialized_results, total


async def get_registration(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    as_admin: bool = False,
):
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(event, user_id, as_admin)

    query = select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )

    if registration_id.startswith("MOA"):
        query = query.where(EventRegistrationsLink.ticket_id == registration_id)
    else:
        try:
            uuid_obj = uuid.UUID(registration_id)
            query = query.where(EventRegistrationsLink.id == uuid_obj)
        except ValueError:
            raise CustomHTTPException(404, "Registration not found")

    scalar_result = await session.scalar(
        query
        .options(
            joinedload(EventRegistrationsLink.event).options(
                joinedload(Events.club), joinedload(Events.category)
            ),
            joinedload(EventRegistrationsLink.user),
        )
    )
    if not scalar_result:
        raise CustomHTTPException(404, "Registration not found")
    return scalar_result
    if not scalar_result:
        raise CustomHTTPException(404, "Registration not found")
    return scalar_result


def _cell(value):
    """A spreadsheet cell as an answer: numpy's numbers as Python's, a date
    as its ISO text, a blank cell (NaN) as nothing -- what the answers'
    bounds (answers.py) and the JSON column take."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value != value:
        return None
    return value


async def enqueue_import(session: AsyncSession, event_id: int, df: DataFrame, background_log: BackgroundTaskLogs, notify: bool = True) -> None:
    """The import as a job (`registrations.import`), run after the caller's
    commit -- by the worker when there is one -- on a session of its own. The
    sheet's rows travel in the job as JSON (pandas writes NaN as null and
    dates as ISO text), so a deploy mid-import does not lose the file: the
    job is taken again and carries on."""
    import json as _json

    rows = _json.loads(df.to_json(orient="records", date_format="iso"))
    await jobs.enqueue(
        session, "registrations.import",
        {"event_id": int(event_id), "log_id": str(background_log.id), "notify": bool(notify), "rows": rows, "columns": list(map(str, df.columns))},
        dedupe_key=f"registrations:import:{background_log.id}",
    )


@jobs.handler("registrations.import", max_attempts=3, timeout=3600.0, concurrency=1, priority=jobs.LOW)
async def _import_job(p: dict) -> None:
    """Run once: an import whose log is no longer STARTED has finished.
    Cut off half way, it is run again on the same log, and carries on."""
    from uuid import UUID

    from app.api.models import BackgroundTaskLogs

    async with jobs.scope() as session:
        log = await session.get(BackgroundTaskLogs, UUID(p["log_id"]))
        if log is None or log.status != "STARTED":
            return
        df = DataFrame(p.get("rows") or [], columns=p.get("columns") or None)
        await bulk_import_event_registrations(session, int(p["event_id"]), df, log, bool(p.get("notify", True)))


async def bulk_import_event_registrations(
    session: AsyncSession,
    event_id: Events,
    df: DataFrame,
    background_log: BackgroundTaskLogs,
    notify: bool = True,
):
    try:
        # Use select to properly load the event with joined club
        event_query = (
            select(Events)
            .filter(Events.id == event_id)
            .options(joinedload(Events.club))
        )
        event = await session.scalar(event_query)

        if not event:
            await update_background_task_log(
                session,
                background_log.id,
                new_logs=[
                    {
                        "level": "ERROR",
                        "message": f"Event with ID {event_id} not found",
                    }
                ],
                new_status="FAILED",
            )
            return

        # Prepare registration logs
        registration_logs = []

        for index, row in df.iterrows():
            full_name = row.get("full_name")
            email = row.get("email")
            # A blank cell arrives as NaN, not "". Said plainly, before any
            # query runs: a bad value reaching the database aborts the
            # transaction and takes every later row down with it.
            if not isinstance(email, str) or not email.strip() or not isinstance(full_name, str) or not full_name.strip():
                registration_logs.append(
                    {"level": "ERROR", "message": f"Row {int(index) + 2}: a name and an email are both required"}
                )
                continue
            email = email.strip()
            full_name = full_name.strip()
            try:
                phone_cell = row.get("phone")
                phone = str(phone_cell).strip() if isinstance(phone_cell, str) and phone_cell.strip() else None
                role = _role_from_sheet(row.get("role"))

                # Process additional details
                additional_details = {}
                additional_details_fields = row.get("additional_details_fields", "")
                if additional_details_fields:
                    for key in str(additional_details_fields).split(","):
                        if key in row:
                            value = _cell(row[key])
                            if value is not None:
                                additional_details[key] = value

                # Find or create user
                user = await user_service.get_non_club_user_by_email(session, email)

                if not user:
                    # Create new user if not exists
                    user = await user_service.create_user(
                        session=session,
                        full_name=full_name,
                        email=email,
                        phone=phone,
                        provider="email",
                        user_type=UserTypes.guest,
                    )

                if not user:
                    registration_logs.append(
                        {
                            "level": "ERROR",
                            "message": f"Failed to create or find user for '{full_name}' <{email}>",
                        }
                    )
                    continue

                # Register for event
                await register_event(
                    session=session,
                    full_name=full_name,
                    email=email,
                    phone=phone,
                    user_id=user.id,
                    event_id=event.id,
                    additional_details=additional_details,
                    background_task_log_id=background_log.id,
                    role=role,
                    notify=notify,
                    by_organiser=True,
                )

                registration_logs.append(
                    {
                        "level": "INFO",
                        "message": f"Registered '{full_name}' <{email}> for '{event.name}'",
                    }
                )

            except Exception as row_error:
                # The failed statement left the transaction aborted; roll it
                # back so the next row, and the log itself, can be written.
                # Rows already registered were committed and stay.
                await session.rollback()
                registration_logs.append(
                    {
                        "level": "ERROR",
                        "message": f"Row {int(index) + 2}: '{full_name}' <{email}> could not be registered — {str(row_error)[:200]}",
                        "metadata": {"error": str(row_error)},
                    }
                )

        # Update background log with all collected logs
        if registration_logs:
            await update_background_task_log(
                session,
                background_log.id,
                new_logs=registration_logs,
                new_status=(
                    "COMPLETED"
                    if all(log["level"] == "INFO" for log in registration_logs)
                    else "PARTIAL"
                ),
            )

        # Commit the session
        await session.commit()

    except Exception as global_error:
        # Handle any global unexpected errors
        await update_background_task_log(
            session,
            background_log.id,
            new_logs=[
                {
                    "level": "CRITICAL",
                    "message": "Bulk import failed",
                    "metadata": {"error": str(global_error)},
                }
            ],
            new_status="FAILED",
        )

        # Re-raise the error for upper-level handling
        # raise


async def get_event_analytics(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    as_admin: bool = False,
):
    # 1. Verify Event Access
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(event, user_id, as_admin)

    # 2. Counted by the database: it used to load every registration of the
    # event as an object to count them in Python.
    R = EventRegistrationsLink
    live = (R.event_id == event_id, R.is_deleted == False)  # noqa: E712
    total_registrations, paid_count, attended_count, total_revenue, morning, afternoon, evening, night = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(R.is_paid == True),  # noqa: E712
                func.count().filter(R.is_attended == True),  # noqa: E712
                func.coalesce(func.sum(R.paid_amount), 0),
                *(func.count().filter(cond) for cond in _time_of_day(R.created_at)),
            ).where(*live)
        )
    ).one()
    unpaid_count = total_registrations - paid_count
    absent_count = total_registrations - attended_count

    page_views = event.page_views or 0
    conversion_rate = (total_registrations / page_views * 100) if page_views > 0 else 0.0
    attendance_rate = (attended_count / total_registrations * 100) if total_registrations > 0 else 0.0

    # 3. Institutions: the college question's answer, else "Unknown".
    institution = func.coalesce(
        *(func.nullif(R.additional_details[key].as_string(), "") for key in ("College Name", "Institution", "college")),
        "Unknown",
    ).label("institution")
    by_institution = (
        await session.execute(select(institution, func.count()).where(*live).group_by(institution).order_by(func.count().desc(), institution))
    ).all()
    top_institutions = [{"name": name, "value": n} for name, n in by_institution[:5]]
    # Distinct institutions that turned up, not the top five; "Unknown" is
    # the bucket for no answer, not an institution.
    total_institutions = len([name for name, _ in by_institution if name != "Unknown"])

    # 4. Check-ins by the hour, in IST.
    hour = func.date_trunc("hour", func.timezone("UTC", R.attended_on, type_=DateTime) + IST)
    attendance_over_time = [
        {"time": h.strftime("%I %p"), "count": n}
        for h, n in (
            await session.execute(
                select(hour, func.count()).where(*live, R.is_attended == True, R.attended_on.isnot(None)).group_by(hour).order_by(hour)  # noqa: E712
            )
        ).all()
    ]

    return {
        "total_registrations": total_registrations,
        "total_revenue": total_revenue,
        "attendance_rate": round(attendance_rate, 1),
        "conversion_rate": round(conversion_rate, 1),
        "payment_status": {"paid": paid_count, "unpaid": unpaid_count},
        "attendance_status": {"attended": attended_count, "absent": absent_count},
        "top_institutions": top_institutions,
        "total_institutions": total_institutions,
        "registration_time": {
            "morning": morning,
            "afternoon": afternoon,
            "evening": evening,
            "night": night,
        },
        "attendance_over_time": attendance_over_time,
    }


#: The analytics read times in IST, as the organisers do.
IST = literal_column("interval '5 hours 30 minutes'", type_=Interval)


def _time_of_day(column):
    """Morning 6-12, afternoon 12-17, evening 17-21, night the rest, in IST."""
    t = cast(func.timezone("UTC", column, type_=DateTime) + IST, Time)
    return (
        and_(t >= time(6), t < time(12)),
        and_(t >= time(12), t < time(17)),
        and_(t >= time(17), t < time(21)),
        and_(column.isnot(None), or_(t < time(6), t >= time(21))),
    )


async def _load_registration_for_event(
    session: AsyncSession, event_id: int, registration_id: str
):
    """
    Resolve a registration by uuid or by ticket id.

    mark_attendance accepts either form -- organisers paste a scanned ticket
    id as readily as a uuid -- so the winner endpoints accept both too rather
    than being the one place that insists on a uuid.
    """
    query = select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )
    if registration_id.startswith("MOA"):
        query = query.where(EventRegistrationsLink.ticket_id == registration_id)
    else:
        try:
            query = query.where(
                EventRegistrationsLink.id == uuid.UUID(registration_id)
            )
        except ValueError:
            raise CustomHTTPException(404, "Registration not found")

    registration = await session.scalar(query.options(
        joinedload(EventRegistrationsLink.user)
    ))
    if not registration:
        raise CustomHTTPException(404, message="Registration not found")
    return registration


async def set_winner(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    position: int | None,
    as_admin: bool = False,
):
    """
    Record or clear a placing for one registration.

    `is_won` and `position` have been columns on every registration since the
    table was written, and the badge checker reads `position` for three of its
    metrics -- events_won (1st), runner_up_count (2nd) and podium_count (3rd).
    Nothing in the API ever wrote either column, so those three badges could
    never be earned by anyone. This is what writes them.

    The two columns are kept consistent: `is_won` means "placed at all" and is
    true exactly when a position is set. Passing None clears both.

    Positions are not required to be unique within an event. Shared places are
    real -- joint winners, two thirds -- and rejecting them would be a
    surprise with no upside; the dashboard shows the placings as given.
    """
    if position is not None and position < 1:
        raise CustomHTTPException(400, message="Position must be 1 or higher")

    event = await session.scalar(
        select(Events).where(Events.id == event_id).options(joinedload(Events.club))
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(
        event, user_id, as_admin, message="Not authorized to manage this event"
    )

    registration = await _load_registration_for_event(
        session, event_id, registration_id
    )

    registration.position = position
    registration.is_won = position is not None

    await session.commit()
    # No refresh: sessions are created with expire_on_commit=False, so the
    # instance keeps both the values just written and the eagerly-loaded
    # `user` the response schema requires. Refreshing would reload the columns
    # and drop the relationship, which the response then rejects.

    # The three placing badges can only be earned once a position exists, so
    # re-check here. Mirrors mark_attendance: a badge failure must not fail
    # the write the organiser actually asked for.
    if registration.user_id:
        try:
            from app.api.badges.service import check_and_award_user_badges

            await check_and_award_user_badges(session, user_id=registration.user_id)
        except Exception as e:
            import logging

            logging.getLogger(__name__).error(f"Badge check failed after placing: {e}")

    return registration


async def list_winners(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    as_admin: bool = False,
):
    """Everyone who placed, best placing first."""
    event = await session.scalar(
        select(Events).where(Events.id == event_id).options(joinedload(Events.club))
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(event, user_id, as_admin)

    rows = await session.scalars(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
            EventRegistrationsLink.is_won == True,
        )
        .options(joinedload(EventRegistrationsLink.user))
        .order_by(
            EventRegistrationsLink.position.asc().nulls_last(),
            EventRegistrationsLink.full_name.asc(),
        )
    )
    return list(rows)


async def mark_attendance(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    is_attended: bool,
    as_admin: bool = False,
    background=None,
):
    """Check a guest in by hand from the attendees page, or take it back.
    Recorded as an admission at the desk, like every other way in."""
    from app.api.checkins import service as checkins
    from app.api.checkins.gate_auth import GateActor

    event = await session.scalar(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(
        event, user_id, as_admin, message="Not authorized to manage this event"
    )

    query = select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )
    if registration_id.startswith("MOA"):
        query = query.where(EventRegistrationsLink.ticket_id == registration_id)
    else:
        try:
            uuid_obj = uuid.UUID(registration_id)
            query = query.where(EventRegistrationsLink.id == uuid_obj)
        except ValueError:
            raise CustomHTTPException(404, "Registration not found")
    query = query.options(joinedload(EventRegistrationsLink.user))
    registration = await session.scalar(query)
    if not registration:
        raise CustomHTTPException(404, message="Registration not found")

    actor_user = await session.get(Users, user_id)
    actor = GateActor(kind="user", name=(actor_user.full_name if actor_user else None) or "Organiser", user=actor_user, full=True)
    if is_attended:
        if registration.ticket_revoked_at is not None:
            raise CustomHTTPException(
                409, message="This ticket was revoked. Reinstate it before checking them in."
            )
        if not registration.is_attended:
            result = await checkins.check_in_by_hand(session, event, registration.id, "Desk", actor, background=background)
            if result["outcome"] == "UNPAID":
                raise CustomHTTPException(409, message="This guest has not paid. Record the payment first.")
            if result["outcome"] == "EVENT_NOT_OPEN":
                raise CustomHTTPException(409, message="The door is closed. Open it under Team & doors to check guests in.")
    elif registration.is_attended:
        await checkins.undo(session, event, registration.id, actor)

    # Named attributes, not a bare refresh: a bare refresh expires the joined
    # `user` too and does not load it back, and the response schema requires
    # it -- so a check-in on a registration already in the session answered
    # 500 after the write had been made.
    await session.refresh(registration, attribute_names=["is_attended", "attended_on", "updated_at", "user"])
    return registration


async def set_role(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    role: RegistrationRole | None,
    as_admin: bool = False,
):
    """Record which role one registration is attending as.

    This is what makes the role-specific ticket and certificate designs
    reachable. An event can hold a separate design for speakers or staff,
    but the renderer picks one by the registration's role -- so without a
    way to set it, every variant an organiser designed would sit unused
    while everyone received the event default.

    Passing None resets the registration to `attendee`, which is what every
    registration starts as: the column is not nullable, because "attending
    as nobody" is not a state an event has. An attendee still receives the
    event's default design unless the club has drawn an attendee variant,
    since resolution falls through from the role to the event default.
    """
    if role is None:
        role = RegistrationRole.attendee
    event = await session.scalar(
        select(Events).where(Events.id == event_id).options(joinedload(Events.club))
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(
        event, user_id, as_admin, message="Not authorized to manage this event"
    )

    registration = await _load_registration_for_event(
        session, event_id, registration_id
    )

    registration.role = role
    await session.commit()
    # No refresh, for the same reason as set_winner: expire_on_commit is off,
    # so the instance keeps the eagerly-loaded `user` the response needs.

    return registration


def _role_from_sheet(value) -> RegistrationRole | None:
    """Read a `role` cell from a bulk-import sheet.

    Optional, so a sheet written before this column existed imports exactly
    as it did. Deliberately forgiving about case and stray spaces, because
    the alternative is failing somebody's whole row over "Speaker " -- and
    an unreadable value falls back to attendee rather than rejecting the
    registration, since the point of the import is to get people in.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in {"nan", "none"}:
        return None
    try:
        return RegistrationRole(text)
    except ValueError:
        logger.warning("Unknown role %r in bulk import, importing as attendee", value)
        return None


# ---------------------------------------------------------------------------
# The organiser's own changes to the list: a correction, a removal, a guest
# added by hand, and the record of what was imported. Ported from Muster's
# attendees module onto registrations.
# ---------------------------------------------------------------------------


async def update_registration(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    *,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    role: RegistrationRole | None = None,
    additional_details: dict | None = None,
    as_admin: bool = False,
):
    """A correction: the registration's own name, contact and answers. The
    linked user account is left alone -- it is theirs, not the event's."""
    registration = await get_registration(session, user_id, event_id, registration_id, as_admin=as_admin)
    if full_name is not None:
        registration.full_name = full_name.strip()
    if email is not None:
        taken = await session.scalar(
            select(EventRegistrationsLink).where(
                EventRegistrationsLink.event_id == event_id,
                EventRegistrationsLink.is_deleted == False,
                user_service.same_email(EventRegistrationsLink.email, email),
                EventRegistrationsLink.id != registration.id,
            )
        )
        if taken is not None:
            raise CustomHTTPException(409, message="Another registration on this event already uses that email.")
        if (registration.email or "").strip().lower() != email.strip().lower():
            registration.email_bounced_at = None
            registration.email_bounce_reason = None
        registration.email = email
    if phone is not None:
        if (registration.phone or "").strip() != phone.strip():
            # A corrected number may reach WhatsApp where the old one did not.
            registration.wa_bounced_at = None
            registration.wa_bounce_reason = None
        registration.phone = phone.strip() or None
    if role is not None:
        registration.role = role
    if additional_details is not None:
        current = registration.additional_details if isinstance(registration.additional_details, dict) else {}
        registration.additional_details = answers.bound({**current, **additional_details})
    await session.commit()
    await session.refresh(registration, attribute_names=["full_name", "email", "phone", "role", "additional_details", "updated_at", "user"])
    return registration


async def remove_registration(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    *,
    confirm_slug: str,
    as_admin: bool = False,
):
    """Takes them off the list: the record, the ticket and the check-in with
    it. Soft deleted like everything else, but it is the answer to "take me
    off the list", so it asks for the event's slug to be typed."""
    registration = await get_registration(session, user_id, event_id, registration_id, as_admin=as_admin)
    if (confirm_slug or "").strip() != registration.event.slug:
        raise CustomHTTPException(409, message="Type the event's slug exactly to confirm.", errors={"fields": {"confirm_slug": registration.event.slug}})
    registration.soft_delete()
    await session.commit()
    return registration


async def add_attendee(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    *,
    full_name: str,
    email: str,
    phone: str | None,
    role: RegistrationRole,
    additional_details: dict | None,
    as_admin: bool = False,
):
    """A guest the organiser adds by hand. Finds or makes the guest account
    the way the public form does, registers them, and sends nothing: the
    ticket goes out through Send tickets, where it is recorded."""
    from app.api.users import service as user_service
    from app.api.users.models import UserTypes

    event = await session.scalar(select(Events).where(Events.id == event_id).options(joinedload(Events.club)))
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(event, user_id, as_admin, message="Not authorized to manage this event")

    user = await user_service.get_non_club_user_by_email(session, email)
    if user is None:
        user = await user_service.create_user(
            session=session, full_name=full_name, email=email, phone=phone, provider="email", user_type=UserTypes.guest
        )
    if user is None:
        raise CustomHTTPException(400, message="The guest account could not be created")
    data = await register_event(
        session=session, full_name=full_name, email=email, phone=phone, user_id=user.id, event_id=event_id,
        additional_details=additional_details, role=role, notify=False, by_organiser=True,
    )
    return await get_registration(session, user_id, event_id, str(data["event_registration_id"]), as_admin=as_admin)


def summarise_import(log) -> dict:
    """What an import did, in the words the panel shows."""
    entries = log.logs or []
    imported = sum(1 for e in entries if e.get("level") == "INFO")
    failed = sum(1 for e in entries if e.get("level") in ("ERROR", "CRITICAL"))
    issues = [e.get("message", "") for e in entries if e.get("level") in ("ERROR", "CRITICAL")][:50]
    return {
        "id": log.id, "task_name": log.task_name, "status": log.status, "created_at": log.created_at,
        "imported": imported, "failed": failed, "issues": issues,
    }


async def list_imports(session: AsyncSession, user_id: int, event_id: int, as_admin: bool = False) -> list[dict]:
    from app.api.models import BackgroundTaskLogs

    event = await session.scalar(select(Events).where(Events.id == event_id).options(joinedload(Events.club)))
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(event, user_id, as_admin)
    logs = await session.scalars(
        select(BackgroundTaskLogs)
        .where(BackgroundTaskLogs.event_id == event_id, BackgroundTaskLogs.task_type == "event_registrations_bulk_import")
        .order_by(BackgroundTaskLogs.created_at.desc())
        .limit(20)
    )
    return [summarise_import(log) for log in logs]
