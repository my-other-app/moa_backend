"""A paid event's places, taken when the money lands.

A paid event counts its paid registrations against its cap: the lower of the
organiser's `max_participants` and, for an event the plans apply to (not
grandfathered, enforcement on), the plan's `registrations_per_event`. It used
to be checked without a lock when registering and when the order was made,
and never at capture, so everyone at checkout when the last place went could
pay for it.

Now the capture takes the place: `take_paid_place` is one conditional UPDATE
of the event's row (`paid_count < cap`), which holds the row until the
capture's transaction ends. Flipping the registration to paid in that same
transaction bumps `paid_count` (the trigger in events.models, which also
keeps it right for the desk, the organiser marking someone paid, refunds and
merges), so a second capture waiting on the row re-reads the count with the
first one in it. A capture that finds no place is refunded in full
(payments.service).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import update

from app.api.events.models import Events


async def paid_cap(session, event: Events) -> Optional[int]:
    """How many paid places the event has; None for no limit."""
    from app.api.subscriptions import gates

    caps = [event.max_participants] if event.max_participants else []
    if gates.enforcing() and event.club_id is not None and not gates.grandfathered(event):
        ent = await gates.entitlements(session, event.club_id)
        limit = ent.limit("registrations_per_event")
        if limit is not None:
            caps.append(limit)
    return min(caps) if caps else None


async def take_paid_place(session, event: Events) -> bool:
    """Take a paid place on `event`, or say there is none. Holds the event's
    row to the end of the transaction, in which the caller marks the
    registration paid (and the trigger counts it)."""
    cap = await paid_cap(session, event)
    table = Events.__table__
    stmt = (
        update(table)
        .where(table.c.id == event.id, table.c.is_deleted == False)  # noqa: E712
        # A write that changes nothing but takes the row: the paid flip in
        # the same transaction is what adds one, through the trigger.
        .values(paid_count=table.c.paid_count)
        .returning(table.c.paid_count)
    )
    if cap is not None:
        stmt = stmt.where(table.c.paid_count < cap)
    return (await session.execute(stmt)).first() is not None


async def full(session, event: Events) -> bool:
    """Whether every paid place is taken now (no lock: an early answer)."""
    cap = await paid_cap(session, event)
    if cap is None:
        return False
    from sqlalchemy import select

    taken = await session.scalar(select(Events.paid_count).where(Events.id == event.id))
    return int(taken or 0) >= cap
