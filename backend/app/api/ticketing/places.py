"""Places: what is left of each ticket type and of the event, who may take
it, and taking it without ever selling one place twice.

Three counts meet here:

- a type's `taken_count` (the database keeps it: registrations holding a
  place of the type) against its `capacity`;
- the event's own count against its `max_participants`, as before ticket
  types: on a free event every live registration (`going_count`), on a paid
  event the paid ones (`paid_count`);
- the waiting list: an offer that has not run out holds a place for the
  person it went to, and the people still waiting for a type come before
  anyone new for that type's places. Where it is the event's total that is
  full (a type with room of its own), the people waiting who that room could
  serve come first for the event's places too.

A registration that holds a place now (free, or made free by a code) takes it
under the event's row (registration.service.take_place, and for a paid
event capacity.take_paid_place) and then the type's row (`take_room`); a
paid ticket takes its place when its payment lands (`capture_room`, after
capacity.take_paid_place). Every check that must be exact is made holding
both rows, reading counts the holders' own writes keep, so two can never
both take the last place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select

from app.api.events.models import Events
from app.api.ticketing import types as types_mod
from app.api.ticketing.models import OFFERED, WAITING, TicketTypes, WaitlistEntries
from app.response import CustomHTTPException


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Line:
    """One type's open waiting-list entries: offers still standing, and the
    people still waiting. `held`: those of the offers that are a club's
    confirmations of registration requests (app/api/approvals), counted in
    `offered` too -- every standing offer holds its place."""

    offered: int = 0
    waiting: int = 0
    held: int = 0


def _open_entries(now: datetime):
    W = WaitlistEntries
    return or_(W.status == WAITING, and_(W.status == OFFERED, W.offer_expires_at > now))


def _add(line: Line, status: str, via_request: bool, n: int) -> None:
    if status == OFFERED:
        line.offered += n
        if via_request:
            line.held += n
    else:
        line.waiting += n


async def lines(session, event_id: int, *, exclude=None, now: Optional[datetime] = None) -> dict[int, Line]:
    """The event's waiting lists by type. An offer that ran out holds
    nothing, whether or not the sweep has closed it yet. `exclude`: an entry
    left out (the one being claimed: its place is the claimer's)."""
    now = now or _now()
    W = WaitlistEntries
    stmt = (
        select(W.ticket_type_id, W.status, W.via_request, func.count())
        .where(W.event_id == event_id, _open_entries(now))
        .group_by(W.ticket_type_id, W.status, W.via_request)
    )
    if exclude is not None:
        stmt = stmt.where(W.id != exclude)
    out: dict[int, Line] = {}
    for type_id, status, via, n in (await session.execute(stmt)).all():
        _add(out.setdefault(type_id, Line()), status, bool(via), int(n))
    return out




def event_cap(event) -> Optional[int]:
    """The organiser's cap on the whole event (0, as always, is none)."""
    return event.max_participants or None


def event_count(event) -> int:
    """What the event's cap counts, as it always has."""
    return int((event.paid_count if event.has_fee else event.going_count) or 0)


@dataclass
class Room:
    """What is left, worked out from the counts (pure)."""

    #: Per type: places left for someone new (None: no cap of its own).
    public: dict[int, Optional[int]] = field(default_factory=dict)
    #: Per type: places free for the people waiting for it (None: no cap).
    free: dict[int, Optional[int]] = field(default_factory=dict)
    #: The event's places left for someone new (None: no cap).
    event_public: Optional[int] = None
    #: The event's places free for offers (None: no cap).
    event_free: Optional[int] = None

    def for_type(self, type_id: int) -> Optional[int]:
        """Places of this type someone new may take now: the lower of the
        type's and the event's (None: no limit)."""
        values = [v for v in (self.public.get(type_id), self.event_public) if v is not None]
        return max(0, min(values)) if values else None

    def refusal(self, event, ttype, *, claiming: bool) -> Optional[str]:
        """Why a registration of `ttype` cannot have a place now: "event" (the
        event's total) or "type", else None. Someone claiming an offer has its
        place (left out of the counts by the caller) and comes before those
        still waiting; anyone else comes after them."""
        event_left = self.event_free if claiming else self.event_public
        type_left = (self.free if claiming else self.public).get(ttype.id)
        if event_left is not None and event_left <= 0:
            return "event"
        if type_left is not None and type_left <= 0:
            return "type"
        return None


def _left(cap: Optional[int], used: int) -> Optional[int]:
    return None if cap is None else cap - used


def compute(event, types: list, lines_: dict[int, Line], *, count: Optional[int] = None) -> Room:
    """The places left, from the event (its cap and count, or `count` read
    under its row), its types (their caps and counts) and its waiting lists.

    An event whose club confirms each registration has no waiting list
    (waitlist.enabled): the people left waiting from before the switch are
    offered nothing, so they hold no place ahead of anyone. Its standing
    offers -- the holds of paid confirmations among them -- still do."""
    room = Room()
    reserved_event = 0
    line_off = bool(getattr(event, "requires_approval", False))
    for t in types:
        line = lines_.get(t.id, Line())
        waiting = 0 if line_off else line.waiting
        cap = types_mod.capacity_of(event, t, types)
        free = _left(cap, int(t.taken_count or 0) + line.offered)
        room.free[t.id] = free
        room.public[t.id] = None if free is None else free - waiting
        # Those waiting whom this type's own room could serve come first for
        # the event's places too.
        servable = waiting if free is None else max(0, min(waiting, free))
        reserved_event += line.offered + servable
    for type_id, line in lines_.items():
        if type_id not in room.free:
            # A type deleted since: its offers still hold their places.
            reserved_event += line.offered
    cap_e = event_cap(event)
    used = event_count(event) if count is None else count
    offered_event = sum(line.offered for line in lines_.values())
    room.event_free = _left(cap_e, used + offered_event)
    room.event_public = _left(cap_e, used + reserved_event)
    return room


#: What the counts read of the event.
COUNTED = ["going_count", "paid_count", "max_participants", "has_fee", "reg_fee"]


async def fresh(session, event) -> list:
    """The event's counts and its types as the database holds them now (not
    as this session first read them)."""
    await session.refresh(event, attribute_names=COUNTED)
    return await types_mod.live(session, event.id, fresh=True)


async def room_now(session, event, types: Optional[list] = None) -> Room:
    """What is left right now, for a page to show (no lock)."""
    types = await fresh(session, event)
    return compute(event, types, await lines(session, event.id))


@dataclass
class Places:
    """The event's places as one figure, the ticket picker's own sums:
    `capacity` (None: no cap), `taken` (what the cap counts: paid tickets on
    a paid event, every registration on a free one), `held` (standing offers:
    confirmed requests' holds and waiting-list offers) and `left` (what
    someone new could have now, never below 0; None: no cap). Pending
    registration requests hold nothing, so they are in none of them."""

    capacity: Optional[int]
    taken: int
    held: int
    left: Optional[int]

    def as_dict(self) -> dict:
        return {"capacity": self.capacity, "taken": self.taken, "held": self.held, "left": self.left}


def places_of(event, types: list, lines_: dict[int, Line], room: Optional[Room] = None) -> Places:
    """The event's total when it has one (the picker's `available`); with no
    total but a cap on every type, the types' together; else no cap."""
    room = room or compute(event, types, lines_)
    held = sum(line.offered for line in lines_.values())
    cap = event_cap(event)
    if cap is not None:
        return Places(capacity=cap, taken=event_count(event), held=held, left=max(0, room.event_public))
    caps = [types_mod.capacity_of(event, t, types) for t in types]
    if types and all(c is not None for c in caps):
        return Places(
            capacity=sum(caps), taken=sum(int(t.taken_count or 0) for t in types), held=held,
            left=sum(max(0, room.public.get(t.id) or 0) for t in types),
        )
    return Places(capacity=None, taken=event_count(event), held=held, left=None)


async def summary(session, event) -> Places:
    """The event's places now, as the ticket picker counts them (no lock)."""
    types = await fresh(session, event)
    lines_ = await lines(session, event.id)
    return places_of(event, types, lines_)


async def places_many(session, events: list, *, now: Optional[datetime] = None) -> dict[int, Places]:
    """`summary` for a page of events in one statement, whatever their
    number: their ticket types (ix_ticket_types_event), each with its open
    waiting-list entries counted (ix_waitlist_line). Deleted types come too,
    only for their offers, which still hold their places. The events' own
    counts are the rows as the caller read them."""
    ids = [e.id for e in events]
    if not ids:
        return {}
    now = now or _now()
    W = WaitlistEntries
    line = (
        select(W.ticket_type_id.label("type_id"), W.status, W.via_request, func.count().label("n"))
        .where(W.event_id.in_(ids), _open_entries(now))
        .group_by(W.ticket_type_id, W.status, W.via_request)
        .subquery("line")
    )
    rows = (await session.execute(
        select(TicketTypes, line.c.status, line.c.via_request, line.c.n)
        .outerjoin(line, line.c.type_id == TicketTypes.id)
        .where(TicketTypes.event_id.in_(ids))
        .order_by(TicketTypes.event_id, TicketTypes.sort_order, TicketTypes.id)
        .execution_options(include_deleted=True)
    )).all()
    types: dict[int, dict[int, TicketTypes]] = {}
    lines_: dict[int, dict[int, Line]] = {}
    for t, status, via, n in rows:
        if not t.is_deleted:
            types.setdefault(t.event_id, {})[t.id] = t
        if status is not None:
            _add(lines_.setdefault(t.event_id, {}).setdefault(t.id, Line()), status, bool(via), int(n))
    return {e.id: places_of(e, list(types.get(e.id, {}).values()), lines_.get(e.id, {})) for e in events}


def full(*, waitlist: bool, max_participants=None) -> CustomHTTPException:
    """The event has no place left (its total)."""
    return CustomHTTPException(
        400, message="Event is full", error_code="EVENT_FULL",
        errors={"max_participants": max_participants, "waitlist": waitlist},
    )


def sold_out(ttype, *, waitlist: bool) -> CustomHTTPException:
    return CustomHTTPException(
        409, message=f"“{ttype.name}” is sold out.", error_code="TICKET_TYPE_SOLD_OUT",
        errors={"ticket_type_id": ttype.id, "waitlist": waitlist},
    )


async def check_early(session, event, quote) -> None:
    """An early answer, without a lock, for a registration about to be made:
    nothing left of its type, or of the event when people wait for it.
    The exact check is `take_room` (or `capture_room`, for a paid ticket)."""
    lines_ = await lines(session, event.id, exclude=quote.offer.id if quote.offer is not None else None)
    if not lines_ and quote.capacity is None:
        return
    room = compute(event, await fresh(session, event), lines_)
    await _refuse(session, event, quote.ticket_type, room.refusal(event, quote.ticket_type, claiming=quote.offer is not None))


async def _refuse(session, event, ttype, why: Optional[str]) -> None:
    if why is None:
        return
    from app.api.ticketing import waitlist

    joinable = await waitlist.joinable(session, event)
    if why == "event":
        raise full(waitlist=joinable, max_participants=event.max_participants)
    raise sold_out(ttype, waitlist=joinable)


async def _lock_type(session, type_id: int) -> Optional[TicketTypes]:
    return await session.scalar(
        select(TicketTypes).where(TicketTypes.id == type_id).with_for_update().execution_options(populate_existing=True)
    )


async def _held_room(session, event, ttype_id: int, *, exclude=None, count: int) -> tuple[Room, list]:
    """The room, with the event's row already held by the caller and the
    type's row taken here, counted fresh."""
    await _lock_type(session, ttype_id)
    types = await types_mod.live(session, event.id, fresh=True)
    lines_ = await lines(session, event.id, exclude=exclude)
    return compute(event, types, lines_, count=count), types


async def take_room(session, event, quote, *, going_now: int) -> None:
    """A registration that holds its place now (it owes nothing): its place,
    taken holding the event's row (taken by take_place before this) and the
    type's. On a paid event a free ticket is a paid place, and takes one as a
    payment would (capacity.take_paid_place). `going_now` is the event's
    going_count before this registration, as take_place read it. Raises
    EVENT_FULL or TICKET_TYPE_SOLD_OUT; nothing is kept then."""
    if not quote.holding:
        return
    from app.api.payments import capacity as paid_capacity
    from app.api.ticketing import waitlist

    count = going_now
    if event.has_fee:
        if not await paid_capacity.take_paid_place(session, event):
            raise full(waitlist=await waitlist.joinable(session, event), max_participants=event.max_participants)
        count = int(await session.scalar(select(Events.paid_count).where(Events.id == event.id)) or 0)
    exclude = quote.offer.id if quote.offer is not None else None
    lines_ = await lines(session, event.id, exclude=exclude)
    if not lines_ and quote.capacity is None:
        return
    room, _types = await _held_room(session, event, quote.ticket_type.id, exclude=exclude, count=count)
    await _refuse(session, event, quote.ticket_type, room.refusal(event, quote.ticket_type, claiming=quote.offer is not None))


async def capture_room(session, event, registration) -> bool:
    """A paid ticket's place, as its payment lands: after the event's paid
    place was taken (capacity.take_paid_place, which holds the event's row).
    False when its type, or the event once the waiting list is counted, has
    nothing left -- the payment then goes back, as for a full event. A
    waiting-list offer the registration was made from is claimed with it."""
    if registration.ticket_type_id is None:
        return True
    entry = await _offer_of(session, registration)
    exclude = entry.id if _standing(entry) else None
    lines_ = await lines(session, event.id, exclude=exclude)
    types = await types_mod.live(session, event.id)
    ttype = next((t for t in types if t.id == registration.ticket_type_id), None)
    cap = types_mod.capacity_of(event, ttype, types) if ttype is not None else None
    if lines_ or cap is not None:
        # The paid flip has not been flushed: paid_count is the count before this one.
        count = int(await session.scalar(select(Events.paid_count).where(Events.id == event.id)) or 0)
        room, _types = await _held_room(session, event, registration.ticket_type_id, exclude=exclude, count=count)
        if room.refusal(event, ttype or SimpleNamespace(id=registration.ticket_type_id), claiming=exclude is not None):
            return False
    if entry is not None:
        from app.api.ticketing import waitlist

        waitlist.mark_claimed(entry)
        if entry.via_request:
            # A club's confirmation, paid for (app/api/approvals).
            from app.api.approvals import service as approvals

            await approvals.on_paid(session, entry, registration)
    return True


def _standing(entry) -> bool:
    """An offer that still holds its place (not run out)."""
    return (
        entry is not None and entry.status == OFFERED
        and (entry.offer_expires_at is None or entry.offer_expires_at > _now())
    )


async def _offer_of(session, registration) -> Optional[WaitlistEntries]:
    """The waiting-list offer a registration was made from (standing, or run out since)."""
    W = WaitlistEntries
    return await session.scalar(
        select(W).where(W.registration_id == registration.id, W.status.in_((OFFERED, "expired"))).limit(1)
    )


async def check_payable(session, event, registration) -> None:
    """Before a checkout opens for a ticket not paid yet: an early answer
    (no lock) when its type, or the event once the waiting list is counted,
    has nothing left, so nobody pays for a place that is gone. The capture
    decides exactly (`capture_room`)."""
    if registration.ticket_type_id is None or registration.is_paid:
        return
    entry = await _offer_of(session, registration)
    exclude = entry.id if _standing(entry) else None
    lines_ = await lines(session, event.id, exclude=exclude)
    types = await types_mod.live(session, event.id)
    ttype = next((t for t in types if t.id == registration.ticket_type_id), None)
    if ttype is None or (not lines_ and types_mod.capacity_of(event, ttype, types) is None):
        return
    room = compute(event, types, lines_)
    await _refuse(session, event, ttype, room.refusal(event, ttype, claiming=exclude is not None))
