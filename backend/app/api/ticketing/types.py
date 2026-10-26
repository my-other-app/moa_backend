"""An event's ticket types: made, changed, listed, and kept in step with the
event's own price and capacity.

While an event has one type and it is the default, the type *follows the
event*: what it costs is the event's fee (`has_fee`, `reg_fee`) and how many
it has is the event's `max_participants`, read from the event at every
registration, as before ticket types existed. Whatever writes the event's
fee or cap (the event form, the app, an old client) keeps working, and the
type is kept showing the same (`follow_event_edit`).

With a second type the types decide: the event's `has_fee` and `reg_fee`
become what its types charge (any paid; the lowest price) so lists and old
clients still read a sensible price, and `max_participants` is the event's
total across its types (null: only each type's own cap).

Writes take the event's row first (`lock_event`), then the types': the
order a registration takes them in (registration.service.take_place, then
places.take_room), so the two never wait on each other in a circle.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.events.models import EventRegistrationsLink, Events
from app.api.ticketing.models import TicketTypes
from app.core import money
from app.response import CustomHTTPException

#: The name an event's first type is given.
DEFAULT_NAME = "General admission"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def event_price(event) -> Decimal:
    """What the event charges by its own fields: its fee when it has one."""
    return money.rupees(event.reg_fee) if event.has_fee and event.reg_fee else Decimal("0.00")


def follows(types: list) -> bool:
    """One type, the default: its price and capacity are the event's."""
    return len(types) == 1 and bool(types[0].is_default)


def price_of(event, ttype, types: list) -> Decimal:
    return event_price(event) if follows(types) else money.rupees(ttype.price)


def capacity_of(event, ttype, types: list) -> Optional[int]:
    """The type's own cap, the one its own count is held to. A type that
    follows the event has none: the event's cap is the one that holds."""
    return None if follows(types) else ttype.capacity


async def live(session, event_id: int, *, fresh: bool = False) -> list[TicketTypes]:
    stmt = (
        select(TicketTypes)
        .where(TicketTypes.event_id == event_id, TicketTypes.is_deleted == False)  # noqa: E712
        .order_by(TicketTypes.sort_order, TicketTypes.id)
    )
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    return list((await session.scalars(stmt)).all())


async def adopt(session, event_id: int, default_id: int) -> None:
    """The event's registrations that have no type (made before it had one,
    or by a server from before types) are of its default."""
    regs = EventRegistrationsLink.__table__
    await session.execute(
        update(regs).where(regs.c.event_id == event_id, regs.c.ticket_type_id.is_(None)).values(ticket_type_id=default_id)
    )


async def ensure(session, event, *, adopt_untyped: bool = False) -> list[TicketTypes]:
    """The event's live types, its default made first when it has none (an
    event made since the migration, or by a path that does not make one).
    The registrations it already had are of that default from then on, so
    its count starts right; with `adopt_untyped`, any registration still
    without a type is too (the club's own writes ask for it). Writes only
    then; flushes, never commits. Never call it holding the event's row: the
    registrations it adopts are taken before the type's row, as a payment's
    capture takes a registration's row before the event's."""
    rows = await live(session, event.id)
    if rows:
        if adopt_untyped:
            default = next((t for t in rows if t.is_default), rows[0])
            await adopt(session, event.id, default.id)
            rows = await live(session, event.id, fresh=True)
        return rows
    now = _now()
    table = TicketTypes.__table__
    await session.execute(
        pg_insert(table)
        .values(
            event_id=event.id, name=DEFAULT_NAME, description=None, price=event_price(event),
            capacity=event.max_participants, is_hidden=False, sort_order=0, is_default=True, taken_count=0,
            created_at=now, updated_at=now, is_deleted=False,
        )
        .on_conflict_do_nothing(index_elements=["event_id"], index_where=table.c.is_default & ~table.c.is_deleted)
    )
    rows = await live(session, event.id, fresh=True)
    default = next((t for t in rows if t.is_default), rows[0] if rows else None)
    if default is not None:
        await adopt(session, event.id, default.id)
        rows = await live(session, event.id, fresh=True)
    return rows


#: What the ticketing reads of an event, read again once its row is held.
EVENT_FIELDS = [
    "has_fee", "reg_fee", "max_participants", "going_count", "paid_count", "club_id", "is_deleted",
    "event_datetime", "duration", "reg_startdate", "reg_enddate", "lifecycle", "created_at",
]


async def lock_event(session, event_id: int) -> Events:
    """The event's row, held to the end of the transaction, and its fields
    as they are now. Only the columns are read again: the relationships the
    caller loaded (to answer with) stay loaded."""
    held = await session.scalar(
        select(Events.id).where(Events.id == event_id, Events.is_deleted == False).with_for_update()  # noqa: E712
    )
    if held is None:
        raise CustomHTTPException(404, message="Event not found")
    event = await session.get(Events, event_id)
    await session.refresh(event, attribute_names=EVENT_FIELDS)
    return event


def sync_event_fee(event, types: list) -> None:
    """The event's own fee fields, as its types say: for lists, cards and
    every client that reads the event's price rather than its types."""
    if follows(types):
        t = types[0]
        price = money.rupees(t.price)
        event.has_fee = price > 0
        event.reg_fee = price if price > 0 else None
        event.max_participants = t.capacity
        return
    priced = sorted(money.rupees(t.price) for t in types if money.rupees(t.price) > 0)
    event.has_fee = bool(priced)
    event.reg_fee = priced[0] if priced else None


async def confirm_free_registrations(session, event) -> None:
    """A paid event counts its paid registrations: the ones that owe nothing
    (a free type, a code that took it all) are confirmed as paid, so the
    door admits them and they count against the event's places. Called when
    an event starts charging. One event's rows, only those owing nothing --
    never a row a payment's capture could be holding."""
    if not event.has_fee:
        return
    regs = EventRegistrationsLink.__table__
    await session.execute(
        update(regs)
        .where(
            regs.c.event_id == event.id, regs.c.is_deleted == False,  # noqa: E712
            regs.c.is_paid == False, regs.c.actual_amount == 0,  # noqa: E712
        )
        .values(is_paid=True, updated_at=_now())
    )


async def follow_event_edit(session, event) -> None:
    """After the event itself was saved (made, edited, copied): a single
    default type shows the event's price and cap again; with several, the
    event's fee is what its types charge. Commits."""
    await ensure(session, event, adopt_untyped=True)
    event = await lock_event(session, event.id)
    types = await live(session, event.id, fresh=True)
    if follows(types):
        t = types[0]
        t.price = event_price(event)
        t.capacity = event.max_participants
    else:
        was_paid = bool(event.has_fee)
        sync_event_fee(event, types)
        if event.has_fee and not was_paid:
            await confirm_free_registrations(session, event)
    await session.commit()


# ─── What the club writes ───────────────────────────────────────────────


FIELDS = ("name", "description", "price", "capacity", "sales_start", "sales_end", "is_hidden", "per_person_limit", "sort_order")


def _check_window(start, end) -> None:
    if start is not None and end is not None and end <= start:
        raise CustomHTTPException(
            422, message="The sales end must be after they start.", error_code="INVALID_SALES_WINDOW",
            errors={"sales_end": "Must be after the sales start."},
        )


async def _gate_new_type(session, event, types: list, *, adding: int = 1, actor=None) -> None:
    from app.api.subscriptions import gates

    await gates.require_within(
        session, event.club_id, "ticket_types_per_event", used=len(types), adding=adding, actor=actor, event=event,
    )


async def _gate_paid(session, event, price, *, actor=None) -> None:
    """Selling paid tickets on an event that has been free is the `tickets`
    feature, as switching the event's own fee on is (gates.check_event_update)."""
    if money.rupees(price) > 0 and not event.has_fee:
        from app.api.subscriptions import gates

        await gates.require_feature(session, event.club_id, "tickets", actor=actor, event=event)


async def _leave_following(session, event, types: list) -> None:
    """A second type is coming: the default, until now the event's price and
    cap, keeps them as its own. When the event's cap was only that type's
    (the two were the same number), it is no longer a total over every type."""
    if not follows(types):
        return
    t = types[0]
    t.price = event_price(event)
    t.capacity = event.max_participants
    if event.max_participants is not None and t.capacity == event.max_participants:
        event.max_participants = None


async def _after_change(session, event, types_before_paid: bool) -> list[TicketTypes]:
    await session.flush()
    types = await live(session, event.id, fresh=True)
    if follows(types):
        # Back to one: the event's own cap is this type's again.
        pass
    sync_event_fee(event, types)
    if event.has_fee and not types_before_paid:
        await confirm_free_registrations(session, event)
    return types


async def create(session, event, data: dict, *, actor=None) -> TicketTypes:
    await ensure(session, event, adopt_untyped=True)
    event = await lock_event(session, event.id)
    types = await live(session, event.id, fresh=True)
    await _gate_new_type(session, event, types, actor=actor)
    await _gate_paid(session, event, data.get("price") or 0, actor=actor)
    _check_window(data.get("sales_start"), data.get("sales_end"))
    was_paid = bool(event.has_fee)
    await _leave_following(session, event, types)
    order = data.get("sort_order")
    if order is None:
        order = (max((t.sort_order for t in types), default=0) + 1) if types else 0
    row = TicketTypes(
        event_id=event.id, name=data["name"].strip(), description=(data.get("description") or None),
        price=money.rupees(data.get("price") or 0), capacity=data.get("capacity"),
        sales_start=data.get("sales_start"), sales_end=data.get("sales_end"),
        is_hidden=bool(data.get("is_hidden")), per_person_limit=data.get("per_person_limit"),
        sort_order=order, is_default=False, taken_count=0,
    )
    session.add(row)
    await _after_change(session, event, was_paid)
    await session.commit()
    from app.api.ticketing import waitlist

    await waitlist.fill_soon(session, event.id)
    return row


async def update_type(session, event, ttype: TicketTypes, data: dict, *, actor=None) -> TicketTypes:
    event = await lock_event(session, event.id)
    ttype = await session.scalar(
        select(TicketTypes).where(TicketTypes.id == ttype.id).with_for_update().execution_options(populate_existing=True)
    )
    if "price" in data and data["price"] is not None:
        await _gate_paid(session, event, data["price"], actor=actor)
    start = data["sales_start"] if "sales_start" in data else ttype.sales_start
    end = data["sales_end"] if "sales_end" in data else ttype.sales_end
    _check_window(start, end)
    was_paid = bool(event.has_fee)
    types = await live(session, event.id)
    single = follows(types)
    for key in FIELDS:
        if key not in data:
            continue
        value = data[key]
        if key == "name":
            value = (value or "").strip() or ttype.name
        elif key == "price":
            value = money.rupees(value or 0)
        elif key == "is_hidden":
            value = bool(value)
        elif key == "sort_order" and value is None:
            continue
        setattr(ttype, key, value)
    if single:
        # The event's own price and cap are this type's: written back to it.
        sync_event_fee(event, [ttype])
    await _after_change(session, event, was_paid)
    await session.commit()
    from app.api.ticketing import waitlist

    await waitlist.fill_soon(session, event.id)
    return ttype


async def has_registrations(session, ticket_type_id: int) -> bool:
    return bool(await session.scalar(
        select(EventRegistrationsLink.id).where(
            EventRegistrationsLink.ticket_type_id == ticket_type_id,
            EventRegistrationsLink.is_deleted == False,  # noqa: E712
        ).limit(1)
    ))


async def delete_type(session, event, ttype: TicketTypes) -> None:
    """Only a type nobody holds a ticket of; one that sold is hidden instead,
    so its tickets keep their type. The event always keeps one."""
    event = await lock_event(session, event.id)
    types = await live(session, event.id, fresh=True)
    if len(types) <= 1:
        raise CustomHTTPException(
            409, message="An event keeps at least one ticket type.", error_code="LAST_TICKET_TYPE",
        )
    if await has_registrations(session, ttype.id):
        raise CustomHTTPException(
            409, message="People hold tickets of this type. Hide it instead, so nobody else can buy it.",
            error_code="TICKET_TYPE_HAS_REGISTRATIONS",
        )
    from app.api.ticketing.models import OPEN_STATUSES, REMOVED, WaitlistEntries

    was_paid = bool(event.has_fee)
    ttype.soft_delete()
    if ttype.is_default:
        # The event's default passes to the first type left.
        ttype.is_default = False
        await session.flush()
        rest = [t for t in types if t.id != ttype.id]
        rest[0].is_default = True
    await session.execute(
        update(WaitlistEntries)
        .where(
            WaitlistEntries.ticket_type_id == ttype.id, WaitlistEntries.status.in_(OPEN_STATUSES),
            # A confirmed request's hold runs its course (app/api/approvals):
            # it lapses when its time is up, and its request with it.
            WaitlistEntries.via_request == False,  # noqa: E712
        )
        .values(status=REMOVED, closed_at=_now(), offer_token=None)
    )
    types = await _after_change(session, event, was_paid)
    if follows(types) and event.max_participants is None:
        event.max_participants = types[0].capacity
    await session.commit()


async def reorder(session, event, ids: list[int]) -> list[TicketTypes]:
    event = await lock_event(session, event.id)
    types = await live(session, event.id, fresh=True)
    by_id = {t.id: t for t in types}
    if set(ids) != set(by_id):
        raise CustomHTTPException(
            422, message="Name every ticket type of the event once.", error_code="INVALID_ORDER",
            errors={"ids": "Every ticket type of the event, once each."},
        )
    for n, type_id in enumerate(ids):
        by_id[type_id].sort_order = n
    await session.commit()
    return await live(session, event.id, fresh=True)


async def save_all(session, event, items: list[dict], *, event_capacity="unchanged", actor=None) -> list[TicketTypes]:
    """The event form's editor, saved at once: the list as it should be.
    A type with an `id` is changed, one without is made, one left out is
    deleted (refused, as `delete_type`, when people hold it). The order is
    the list's. `event_capacity` sets the event's total when given."""
    if not items:
        raise CustomHTTPException(
            422, message="An event keeps at least one ticket type.", error_code="LAST_TICKET_TYPE",
            errors={"types": "At least one ticket type."},
        )
    await ensure(session, event, adopt_untyped=True)
    event = await lock_event(session, event.id)
    types = await live(session, event.id, fresh=True)
    by_id = {t.id: t for t in types}
    unknown = [i.get("id") for i in items if i.get("id") is not None and i["id"] not in by_id]
    if unknown:
        raise CustomHTTPException(
            422, message="A ticket type in the list is not this event's.", error_code="TICKET_TYPE_NOT_FOUND",
            errors={"types": f"Not this event's: {unknown}"},
        )
    keep = {i["id"] for i in items if i.get("id") is not None}
    dropped = [t for t in types if t.id not in keep]
    adding = sum(1 for i in items if i.get("id") is None)
    if adding:
        await _gate_new_type(session, event, [t for t in types if t.id in keep], adding=adding, actor=actor)
    for item in items:
        _check_window(item.get("sales_start"), item.get("sales_end"))
        current = by_id.get(item.get("id"))
        if current is None or money.rupees(item.get("price") or 0) != money.rupees(current.price):
            await _gate_paid(session, event, item.get("price") or 0, actor=actor)
    for t in dropped:
        if await has_registrations(session, t.id):
            raise CustomHTTPException(
                409, message=f"People hold tickets of “{t.name}”. Hide it instead of removing it.",
                error_code="TICKET_TYPE_HAS_REGISTRATIONS", errors={"ticket_type_id": t.id},
            )
    was_paid = bool(event.has_fee)
    single_before = follows(types)
    from app.api.ticketing.models import OPEN_STATUSES, REMOVED, WaitlistEntries

    for t in dropped:
        t.soft_delete()
        t.is_default = False
        await session.execute(
            update(WaitlistEntries)
            .where(WaitlistEntries.ticket_type_id == t.id, WaitlistEntries.status.in_(OPEN_STATUSES))
            .values(status=REMOVED, closed_at=_now(), offer_token=None)
        )
    await session.flush()
    saved: list[TicketTypes] = []
    for n, item in enumerate(items):
        row = by_id.get(item.get("id"))
        if row is None:
            row = TicketTypes(event_id=event.id, is_default=False, taken_count=0)
            session.add(row)
        row.name = (item.get("name") or "").strip() or row.name or DEFAULT_NAME
        row.description = item.get("description") or None
        row.price = money.rupees(item.get("price") or 0)
        row.capacity = item.get("capacity")
        row.sales_start = item.get("sales_start")
        row.sales_end = item.get("sales_end")
        row.is_hidden = bool(item.get("is_hidden"))
        row.per_person_limit = item.get("per_person_limit")
        row.sort_order = n
        saved.append(row)
    await session.flush()
    if not any(t.is_default for t in saved):
        saved[0].is_default = True
        await session.flush()
    if event_capacity != "unchanged":
        event.max_participants = event_capacity
    elif single_before and len(saved) > 1 and event.max_participants is not None:
        # Was the one type's cap; now each type has its own.
        default = next((t for t in saved if t.is_default), None)
        if default is not None and default.capacity == event.max_participants:
            event.max_participants = None
    types = await live(session, event.id, fresh=True)
    if follows(types):
        sync_event_fee(event, types)
    else:
        sync_event_fee(event, types)
    if event.has_fee and not was_paid:
        await confirm_free_registrations(session, event)
    await session.commit()
    from app.api.ticketing import waitlist

    await waitlist.fill_soon(session, event.id)
    return await live(session, event.id, fresh=True)


async def copy_from(session, source, new_event_id: int, *, allow_more: bool = True) -> int:
    """A copied event's types: the source's, as they are set up -- names,
    prices, caps, limits, order -- with nothing sold, and no sales window
    (the source's dates are its own). Only the default when the plan allows
    one type (`allow_more` false). Commits. Returns how many."""
    source_types = await live(session, source.id)
    if not source_types:
        return 0
    if not allow_more:
        source_types = [next((t for t in source_types if t.is_default), source_types[0])]
    for n, t in enumerate(source_types):
        session.add(TicketTypes(
            event_id=new_event_id, name=t.name, description=t.description, price=t.price, capacity=t.capacity,
            is_hidden=bool(t.is_hidden), per_person_limit=t.per_person_limit, sort_order=n,
            is_default=bool(t.is_default) or (not allow_more), taken_count=0,
        ))
    await session.flush()
    copy = await session.get(Events, new_event_id)
    if copy is not None:
        sync_event_fee(copy, await live(session, new_event_id, fresh=True))
    await session.commit()
    return len(source_types)


async def get_type(session, event_id: int, type_id: int) -> TicketTypes:
    row = await session.scalar(
        select(TicketTypes).where(
            TicketTypes.id == type_id, TicketTypes.event_id == event_id, TicketTypes.is_deleted == False,  # noqa: E712
        )
    )
    if row is None:
        raise CustomHTTPException(404, message="Ticket type not found", error_code="TICKET_TYPE_NOT_FOUND")
    return row


async def names(session, type_ids) -> dict[int, str]:
    """Types' names by id, deleted ones too (a ticket keeps its type's name)."""
    ids = sorted({int(i) for i in type_ids if i is not None})
    if not ids:
        return {}
    rows = await session.execute(
        select(TicketTypes.id, TicketTypes.name).where(TicketTypes.id.in_(ids)).execution_options(include_deleted=True)
    )
    return {i: n for i, n in rows.all()}


async def count_live(session, event_id: int) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(TicketTypes).where(
            TicketTypes.event_id == event_id, TicketTypes.is_deleted == False,  # noqa: E712
        )
    ) or 0)
