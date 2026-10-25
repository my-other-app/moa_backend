"""The steps of a registration that are about its ticket (registration.service
calls them): pricing it again when someone back to pay picks another type or
adds a code, what is kept with it once it is in, and what its answer says
about its price."""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select

from app.api.ticketing import places
from app.api.ticketing import types as types_mod
from app.api.ticketing.models import DiscountCodes
from app.core import money

logger = logging.getLogger(__name__)


async def _book_comp(session, registration) -> None:
    """A ticket confirmed on a paid event with nothing to pay: in the ledger
    as one given at nothing, as the finance page counts paid tickets."""
    from app.api.ledger import book as ledger_book
    from app.api.ledger import service as ledger

    await ledger.sync_registration(session, registration, kind=ledger_book.COMP, channel=ledger._channel_of(registration))


async def after_insert(session, event, registration, priced) -> None:
    """In the registration's own transaction, once it is in: the waiting-list
    offer it claims (claimed now when it owes nothing, else once its payment
    lands), and the ledger for a paid event's ticket that owes nothing."""
    if priced.offer is not None:
        from app.api.ticketing import waitlist

        priced.offer.registration_id = registration.id
        if priced.holding:
            waitlist.mark_claimed(priced.offer)
    if event.has_fee and priced.holding:
        await _book_comp(session, registration)


async def requote(session, event, registration, priced) -> bool:
    """A registration not paid for yet, priced again: another type, or a
    code. When it now owes nothing it is confirmed, taking its place as a
    new one would (holding the event's row, then the type's). Commits.
    True when it became confirmed (its ticket goes out)."""
    if registration.is_paid or money.rupees(registration.paid_amount) > 0:
        return False
    await types_mod.lock_event(session, event.id)
    if priced.holding:
        await places.take_room(session, event, priced, going_now=int(event.going_count or 0))
    priced.apply(registration, event)
    confirmed = bool(event.has_fee and priced.holding)
    if confirmed:
        await _book_comp(session, registration)
    await session.commit()
    return confirmed


async def summary_for(session, registration) -> dict:
    """What a registration's answer says about its ticket and price."""
    names = await types_mod.names(session, [registration.ticket_type_id])
    code = None
    if registration.discount_code_id is not None:
        code = await session.scalar(
            select(DiscountCodes.code).where(DiscountCodes.id == registration.discount_code_id)
            .execution_options(include_deleted=True)
        )
    discount = money.rupees(registration.discount_amount) if registration.discount_amount is not None else Decimal("0.00")
    return {
        "ticket_type": (
            {"id": registration.ticket_type_id, "name": names.get(registration.ticket_type_id)}
            if registration.ticket_type_id is not None else None
        ),
        "price": float(money.rupees(registration.actual_amount) + discount),
        "discount": {"code": code, "amount": float(discount)} if code is not None else None,
    }
