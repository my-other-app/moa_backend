"""The event store.

An item has a price and a stock (or sizes with a stock each), who may buy
it and who gets it free. A guest picks at registration; the picks become
an order that is pending until the same Razorpay payment as the ticket
captures, then paid. At the door, a paid order is handed over once.

Stock is held, then sold, each step one conditional UPDATE of the item's
(or size's) row, so two guests can never both get the last piece:

- picking reserves: `reserved_count += q` only while
  `sold_count + reserved_count + q <= stock` (else SOLD_OUT, before any
  money moves). The order holds its pieces until `held_until`, HOLD_FOR
  after it was placed or its checkout last opened.
- the capture sells: a held order moves its pieces from reserved to sold
  (`sold_count + q <= stock`); one whose hold was given back takes them
  afresh only while `sold_count + reserved_count + q <= stock`, so it never
  takes a piece someone else is holding. No pieces left: the order is
  `oversold`, the organiser and the admins are told, and the organiser
  decides -- hand something over anyway once stock is added (`fulfil`), or
  refund. Nothing is refunded on its own.
- dropping the order (picks changed, the registration abandoned) or the
  hold running out (sweep.expire_holds, every minute) gives the pieces back.
- a refund puts sold pieces back on the shelf -- unless the order was
  already handed over: those pieces have left with the guest.

Why counters on the rows rather than a table of holds: the order and its
lines already are the hold (what, how many, until when), so a second table
would copy them; and with the count on the row, reserving is one statement
that cannot oversell, where a holds table needs the row locked and its live
holds summed on every pick and every catalogue view. The price is that an
expired hold still counts until the sweep gives it back (at most a minute
late) -- which errs on the side of never selling a piece twice.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select, update

from app.api.events.models import EventRegistrationsLink as Reg, Events
from app.api.store.models import EventStoreItems, EventStoreOrderLines, EventStoreOrders, EventStoreVariants
from app.core.storage import objects
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: How long a pending order holds its pieces. The same 30 minutes after which
#: an unpaid registration counts as walked away from
#: (events.registration.service.ABANDONED_AFTER): a checkout left open
#: finishes well inside it.
HOLD_FOR = timedelta(minutes=30)

#: Orders whose money was taken for pieces: sold, or paid for when there were none.
TAKEN = ("paid", "oversold")


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _f(x) -> float:
    return float(round(Decimal(x or 0), 2))


def _uuid(v) -> UUID:
    try:
        return UUID(str(v))
    except (ValueError, TypeError):
        raise CustomHTTPException(404, message="Not found")


# ─── Items ───────────────────────────────────────────────────────────────


def _total(m: EventStoreItems) -> int:
    return sum(v.stock for v in m.variants) if m.variants else (m.stock or 0)


def _sold(m: EventStoreItems) -> int:
    return sum(v.sold_count for v in m.variants) if m.variants else m.sold_count


def _held(m: EventStoreItems) -> int:
    return sum(v.reserved_count or 0 for v in m.variants) if m.variants else (m.reserved_count or 0)


def _left(stock, sold, held) -> int:
    return max(0, (stock or 0) - (sold or 0) - (held or 0))


def item_out(m: EventStoreItems, earned: Decimal = Decimal(0)) -> dict:
    total, sold, held = _total(m), _sold(m), _held(m)
    return dict(
        id=str(m.id), event_id=m.event_id, name=m.name, description=m.description, photo_key=m.photo_key,
        photo_url=objects.photo_url(m.photo_key), price=_f(m.price), stock=m.stock, sold=sold, held=held, total=total,
        left=_left(total, sold, held), earned=_f(earned), available_roles=m.available_roles, free_roles=list(m.free_roles or []), on_sale=m.on_sale,
        sold_out=total > 0 and sold + held >= total, sort_order=m.sort_order,
        variants=[
            dict(id=str(v.id), label=v.label, stock=v.stock, sold=v.sold_count, held=v.reserved_count or 0, left=_left(v.stock, v.sold_count, v.reserved_count))
            for v in m.variants
        ],
        created_at=_iso(m.created_at),
    )


async def _earned(session, event_id: int) -> dict[UUID, Decimal]:
    rows = (await session.execute(
        select(EventStoreOrderLines.item_id, func.sum(EventStoreOrderLines.line_total))
        .join(EventStoreOrders, EventStoreOrders.id == EventStoreOrderLines.order_id)
        .where(EventStoreOrders.event_id == event_id, EventStoreOrders.status == "paid")
        .group_by(EventStoreOrderLines.item_id)
    )).all()
    return {i: Decimal(t or 0) for i, t in rows}


async def list_items(session, event: Events) -> list[dict]:
    rows = list((await session.scalars(select(EventStoreItems).where(EventStoreItems.event_id == event.id, EventStoreItems.is_deleted == False).order_by(EventStoreItems.sort_order, EventStoreItems.created_at).execution_options(populate_existing=True))).all())  # noqa: E712
    earned = await _earned(session, event.id)
    return [item_out(m, earned.get(m.id, Decimal(0))) for m in rows]


async def item_for(session, event: Events, item_id) -> EventStoreItems:
    m = await session.scalar(select(EventStoreItems).where(EventStoreItems.id == _uuid(item_id), EventStoreItems.event_id == event.id, EventStoreItems.is_deleted == False).execution_options(populate_existing=True))  # noqa: E712
    if m is None:
        raise CustomHTTPException(404, message="Item not found")
    return m


async def create_item(session, event: Events, body) -> dict:
    last = await session.scalar(select(func.max(EventStoreItems.sort_order)).where(EventStoreItems.event_id == event.id))
    m = EventStoreItems(
        event_id=event.id, name=body.name, description=(body.description or "").strip() or None, photo_key=body.photo_key, price=body.price, stock=body.stock,
        available_roles=body.available_roles, free_roles=body.free_roles, on_sale=body.on_sale, sort_order=(last if last is not None else -1) + 1,
    )
    for i, v in enumerate(body.variants):
        m.variants.append(EventStoreVariants(label=v.label.strip(), stock=v.stock, sort_order=i))
    session.add(m)
    await session.commit()
    await session.refresh(m)
    return item_out(m)


def _taken_message(label: str, sold: int, held: int) -> str:
    if held:
        return f"{label}: {sold} already sold and {held} held for checkouts; stock cannot go below {sold + held}"
    return f"{label}: {sold} already sold; stock cannot go below that"


async def update_item(session, event: Events, m: EventStoreItems, body) -> dict:
    data = body.model_dump(exclude_unset=True)
    if "variants" in data and data["variants"] is not None:
        keep = {v.label.strip().lower(): v for v in body.variants}
        for existing in list(m.variants):
            taken = existing.sold_count + (existing.reserved_count or 0)
            if existing.label.lower() in keep:
                new_stock = keep.pop(existing.label.lower()).stock
                if new_stock < taken:
                    raise CustomHTTPException(422, message=_taken_message(existing.label, existing.sold_count, existing.reserved_count or 0))
                existing.stock = new_stock
            elif taken > 0:
                raise CustomHTTPException(422, message=f"{existing.label} has been sold or is held for a checkout; it cannot be removed")
            else:
                m.variants.remove(existing)
        for i, v in enumerate(keep.values()):
            m.variants.append(EventStoreVariants(label=v.label.strip(), stock=v.stock, sort_order=len(m.variants) + i))
        m.stock = None
    if "stock" in data and data["stock"] is not None:
        if m.variants:
            raise CustomHTTPException(422, message="This item has sizes; set the stock per size")
        if data["stock"] < m.sold_count + (m.reserved_count or 0):
            raise CustomHTTPException(422, message=_taken_message(m.name, m.sold_count, m.reserved_count or 0))
        m.stock = data["stock"]
    for k in ("name", "description", "photo_key", "price", "available_roles", "free_roles", "on_sale", "sort_order"):
        if k in data and (data[k] is not None or k in ("description", "photo_key", "available_roles")):
            setattr(m, k, data[k].strip() if k == "name" else data[k])
    await session.commit()
    await session.refresh(m)
    return item_out(m, (await _earned(session, event.id)).get(m.id, Decimal(0)))


async def delete_item(session, m: EventStoreItems) -> None:
    if _sold(m) > 0 or _held(m) > 0:
        # Paid for (or being paid for) by someone: it stays on their order. It leaves the shelf instead.
        m.on_sale = False
    m.is_deleted = True
    m.deleted_at = _now()
    await session.commit()


# ─── The catalogue a guest sees ──────────────────────────────────────────


async def catalog(session, event: Events) -> dict:
    if not getattr(event, "merch_enabled", False):
        return dict(enabled=False, items=[])
    rows = list((await session.scalars(select(EventStoreItems).where(EventStoreItems.event_id == event.id, EventStoreItems.is_deleted == False, EventStoreItems.on_sale == True).order_by(EventStoreItems.sort_order).execution_options(populate_existing=True))).all())  # noqa: E712
    items = []
    for m in rows:
        if m.available_roles and "attendee" not in m.available_roles:
            continue  # the public form only ever registers attendees
        total, sold, held = _total(m), _sold(m), _held(m)
        if total > 0 and sold + held >= total:
            continue
        items.append(dict(
            id=str(m.id), name=m.name, description=m.description, photo_url=objects.photo_url(m.photo_key),
            price=_f(Decimal(0) if "attendee" in (m.free_roles or []) else m.price), left=None if m.variants else _left(total, sold, held),
            variants=[
                dict(id=str(v.id), label=v.label, left=_left(v.stock, v.sold_count, v.reserved_count))
                for v in m.variants if _left(v.stock, v.sold_count, v.reserved_count) > 0
            ],
        ))
    return dict(enabled=True, items=items)


# ─── Stock, one conditional UPDATE per line ──────────────────────────────

_I = EventStoreItems.__table__
_V = EventStoreVariants.__table__


def _in_lock_order(order: EventStoreOrders) -> list[EventStoreOrderLines]:
    """The lines in one fixed order, so two orders taking the same rows
    take them in the same order and cannot deadlock."""
    return sorted(order.lines, key=lambda line: (str(line.item_id), str(line.variant_id or "")))


def _row(line):
    """The table and row id a line's stock lives on: its size's, or the item's."""
    if line.variant_id is not None:
        return _V, _V.c.id == line.variant_id
    return _I, _I.c.id == line.item_id


async def _move(session, line, *, where=None, **deltas) -> bool:
    """One conditional UPDATE of the line's stock row; whether it applied."""
    table, which = _row(line)
    stmt = update(table).where(which)
    if where is not None:
        stmt = stmt.where(where(table))
    stmt = stmt.values(**{k: fn(table) for k, fn in deltas.items()}).returning(table.c.id)
    return (await session.execute(stmt)).first() is not None


def _stock(t):
    return func.coalesce(t.c.stock, 0)


async def _reserve(session, line) -> bool:
    q = line.qty
    return await _move(
        session, line,
        where=lambda t: t.c.sold_count + t.c.reserved_count + q <= _stock(t),
        reserved_count=lambda t: t.c.reserved_count + q,
    )


async def _unreserve(session, line) -> None:
    q = line.qty
    await _move(session, line, reserved_count=lambda t: func.greatest(t.c.reserved_count - q, 0))


async def _sell_held(session, line) -> bool:
    q = line.qty
    return await _move(
        session, line,
        where=lambda t: t.c.sold_count + q <= _stock(t),
        sold_count=lambda t: t.c.sold_count + q,
        reserved_count=lambda t: func.greatest(t.c.reserved_count - q, 0),
    )


async def _sell_fresh(session, line) -> bool:
    q = line.qty
    return await _move(
        session, line,
        where=lambda t: t.c.sold_count + t.c.reserved_count + q <= _stock(t),
        sold_count=lambda t: t.c.sold_count + q,
    )


async def _unsell(session, line) -> None:
    q = line.qty
    await _move(session, line, sold_count=lambda t: func.greatest(t.c.sold_count - q, 0))


async def _available(session, line) -> int:
    table, which = _row(line)
    row = (await session.execute(select(_stock(table), table.c.sold_count, table.c.reserved_count).where(which))).first()
    return _left(*row) if row else 0


async def _locked(session, order: EventStoreOrders) -> EventStoreOrders:
    """The order's row, locked to the end of the transaction, as it is now."""
    return await session.scalar(
        select(EventStoreOrders).where(EventStoreOrders.id == order.id).with_for_update().execution_options(populate_existing=True)
    )


async def _take_all(session, order: EventStoreOrders, take) -> bool:
    """`take` for every line, all or none (a SAVEPOINT undoes the lines
    taken before one that could not be)."""
    savepoint = await session.begin_nested()
    for line in _in_lock_order(order):
        if not await take(session, line):
            await savepoint.rollback()
            return False
    await savepoint.commit()
    return True


# ─── Orders ──────────────────────────────────────────────────────────────


async def pending_for(session, registration_id) -> EventStoreOrders | None:
    return await session.scalar(select(EventStoreOrders).where(EventStoreOrders.registration_id == registration_id, EventStoreOrders.status == "pending"))


async def pending_total(session, registration_id) -> Decimal:
    o = await pending_for(session, registration_id)
    return Decimal(o.total) if o else Decimal(0)


def _sold_out(m: EventStoreItems, variant, left: int) -> CustomHTTPException:
    name = f"{m.name}{' ' + variant.label if variant else ''}"
    return CustomHTTPException(
        422,
        message=f"{name}: only {left} left" if left > 0 else f"{name} is sold out",
        error_code="SOLD_OUT",
        errors={"item_id": str(m.id), "variant_id": str(variant.id) if variant else None, "available": max(0, left)},
    )


async def place_order(session, event: Events, reg: Reg, picks, role: str = "attendee") -> EventStoreOrders | None:
    """The picks become one order, holding its pieces for HOLD_FOR. Replaces
    any earlier pending order for the same registration (whose pieces go
    back first). SOLD_OUT when a piece is not there to hold."""
    earlier = await pending_for(session, reg.id)
    if earlier is not None:
        await release(session, earlier, status="cancelled")
    if not picks:
        await session.flush()
        return None
    if not getattr(event, "merch_enabled", False):
        raise CustomHTTPException(422, message="Merchandise is not on sale for this event", error_code="STORE_CLOSED")
    order = EventStoreOrders(event_id=event.id, registration_id=reg.id, user_id=reg.user_id, status="pending", total=Decimal(0))
    total = Decimal(0)
    seen = set()
    found = {}
    for p in picks:
        m = await session.scalar(select(EventStoreItems).where(EventStoreItems.id == p.item_id, EventStoreItems.event_id == event.id, EventStoreItems.is_deleted == False, EventStoreItems.on_sale == True))  # noqa: E712
        if m is None:
            raise CustomHTTPException(422, message="One of the items is no longer on sale", error_code="ITEM_UNAVAILABLE", errors={"item_id": str(p.item_id)})
        if m.available_roles and role not in m.available_roles:
            raise CustomHTTPException(422, message=f"{m.name} is not available to {role}s", error_code="ITEM_UNAVAILABLE", errors={"item_id": str(m.id)})
        variant = None
        if m.variants:
            variant = next((v for v in m.variants if p.variant_id is not None and v.id == p.variant_id), None)
            if variant is None:
                raise CustomHTTPException(422, message=f"Pick a size for {m.name}", error_code="SIZE_REQUIRED", errors={"item_id": str(m.id)})
            key = (m.id, variant.id)
        else:
            key = (m.id, None)
        if key in seen:
            raise CustomHTTPException(422, message=f"{m.name} is in the order twice", error_code="ITEM_REPEATED", errors={"item_id": str(m.id)})
        seen.add(key)
        found[key] = (m, variant)
        free = role in (m.free_roles or [])
        unit = Decimal(0) if free else Decimal(m.price)
        line_total = unit * p.qty
        total += line_total
        order.lines.append(EventStoreOrderLines(item_id=m.id, variant_id=variant.id if variant else None, item_name=m.name, variant_label=variant.label if variant else None, qty=p.qty, unit_price=unit, line_total=line_total, is_free=free))
    order.total = total
    session.add(order)
    await session.flush()
    # Held now, one statement a line: a piece someone else is holding or has
    # bought is not there. The caller's transaction rolls back on SOLD_OUT,
    # and the lines held before it with it.
    for line in _in_lock_order(order):
        if not await _reserve(session, line):
            m, variant = found[(line.item_id, line.variant_id)]
            raise _sold_out(m, variant, await _available(session, line))
    order.held_until = _now() + HOLD_FOR
    await session.flush()
    if total == 0:
        # Given free (a role's free pick): sold at nothing, and in the
        # ledger as a zero-rupee comp, so the pieces are still counted.
        from app.api.ledger import book as ledger_book
        from app.api.ledger import service as ledger

        await mark_paid(session, order)
        await ledger.sync_registration(session, reg, kind=ledger_book.COMP, accounts=(ledger_book.STORE, ledger_book.STORE_HELD))
    return order


async def hold_for_checkout(session, order: EventStoreOrders) -> None:
    """A checkout is about to open for the order: its pieces are held for
    HOLD_FOR from now. A hold already given back is taken again, or SOLD_OUT
    -- before any money moves."""
    o = await _locked(session, order)
    if o is None or o.status != "pending":
        return
    if o.held_until is not None:
        o.held_until = _now() + HOLD_FOR
        await session.flush()
        return
    for line in _in_lock_order(o):
        if not await _reserve(session, line):
            m = await session.get(EventStoreItems, line.item_id)
            variant = await session.get(EventStoreVariants, line.variant_id) if line.variant_id else None
            raise _sold_out(m, variant, await _available(session, line))
    o.held_until = _now() + HOLD_FOR
    await session.flush()


async def release(session, order: EventStoreOrders, *, status: str | None = None) -> bool:
    """Give the order's held pieces back; with `status`, the order becomes
    it (a pending order dropped: "cancelled"). Whether anything was held."""
    o = await _locked(session, order)
    if o is None:
        return False
    held = o.held_until is not None
    if held:
        for line in _in_lock_order(o):
            await _unreserve(session, line)
        o.held_until = None
    if status is not None and o.status == "pending":
        o.status = status
    await session.flush()
    return held


async def mark_paid(session, order: EventStoreOrders) -> str:
    """The order's money landed: its pieces are sold. Held pieces move from
    reserved to sold; an order whose hold was given back takes them afresh
    from what nobody is holding. None left: `oversold`, and the organiser and
    the admins are told. Returns the order's status."""
    o = await _locked(session, order)
    if o is None:
        return "missing"
    if o.status != "pending":
        return o.status
    sold = False
    if o.held_until is not None:
        sold = await _take_all(session, o, _sell_held)
        if not sold:
            # Cannot happen while sold + reserved <= stock holds; if it ever
            # does, the hold is given back and the pieces taken afresh.
            for line in _in_lock_order(o):
                await _unreserve(session, line)
        o.held_until = None
    if not sold:
        sold = await _take_all(session, o, _sell_fresh)
    o.paid_at = _now()
    o.status = "paid" if sold else "oversold"
    await session.flush()
    if not sold:
        await _tell_oversold(session, o)
    return o.status


async def fulfil(session, order: EventStoreOrders) -> dict:
    """An oversold order the organiser has found the pieces for (stock
    raised): taken from the shelf now, and paid. SOLD_OUT while the pieces
    still are not there. In the ledger, its money moves from held to sold,
    dated when it was paid (the finance page's month for it)."""
    from app.api.ledger import book as ledger_book
    from app.api.ledger import service as ledger

    # The registration before the order, as a refund takes them: two that
    # meet wait for one another rather than deadlock.
    reg = await session.scalar(select(Reg).where(Reg.id == order.registration_id).with_for_update())
    o = await _locked(session, order)
    if o is None or o.status != "oversold":
        raise CustomHTTPException(409, message="Only an oversold order is fulfilled", error_code="NOT_OVERSOLD")
    if not await _take_all(session, o, _sell_fresh):
        raise CustomHTTPException(409, message="The pieces are still not in stock. Raise the stock first, or refund.", error_code="SOLD_OUT")
    if reg is not None:
        await ledger.ensure_history(session, reg)
    o.status = "paid"
    if reg is not None:
        await ledger.sync_registration(session, reg, kind=ledger_book.FULFIL, at=_now(), origin_at=o.paid_at,
                                       accounts=(ledger_book.STORE, ledger_book.STORE_HELD))
    await session.commit()
    reg = await session.get(Reg, o.registration_id)
    return order_out(o, reg)


async def _tell_oversold(session, o: EventStoreOrders) -> None:
    """The organiser (by mail) and the admins (an alert): money was taken for
    pieces that are gone. Who gets what is theirs to decide; nothing is
    refunded on its own. Sent once the capture commits."""
    from app.api.admin_alerts import notify
    from app.core.after_commit import run_after_commit

    event = await session.get(Events, o.event_id)
    reg = await session.get(Reg, o.registration_id)
    what = _summary_line(o)
    guest = reg.full_name if reg else "A guest"
    title = f"Oversold at {event.name if event else 'an event'}: {guest} paid for {what}, which had sold out"
    body = (
        f"₹{o.total} was paid for {what} when the last pieces had gone (the checkout's hold had run out). "
        "Hand something over anyway once you add stock (Fulfil), or refund the payment."
    )
    await notify(
        session, "store_oversold", title=title, body=body, club=event.club_id if event else None,
        data={"order_id": str(o.id), "event_id": o.event_id, "registration_id": str(o.registration_id), "total": str(o.total)},
        dedupe_on={"order_id": str(o.id)},
    )
    if event is not None and event.club_id is not None:
        from app.api.subscriptions import notices

        to = await notices.owner_address(session, event.club_id)
        if to:
            run_after_commit(session, notices._send, to, title, [body], None, None)


async def mark_refunded(session, registration_id) -> int:
    """The registration's payment was refunded: its orders are refunded.
    Pieces sold go back on the shelf -- unless they were handed over, and
    have left with the guest."""
    n = 0
    rows = (await session.scalars(
        select(EventStoreOrders.id).where(EventStoreOrders.registration_id == registration_id, EventStoreOrders.status.in_(TAKEN))
    )).all()
    for order_id in rows:
        o = await session.scalar(
            select(EventStoreOrders).where(EventStoreOrders.id == order_id).with_for_update().execution_options(populate_existing=True)
        )
        if o is None or o.status not in TAKEN:
            continue
        if o.status == "paid" and o.handed_over_at is None:
            for line in _in_lock_order(o):
                await _unsell(session, line)
        o.status = "refunded"
        n += 1
    await session.flush()
    return n


async def expire_holds(session, now: datetime | None = None, *, limit: int = 500) -> int:
    """Give back the pieces of pending orders whose hold has run out. One
    order at a time, each committed: an order a capture or a checkout is
    working on right now is skipped (it is locked) and looked at next time."""
    now = now or _now()
    ids = (await session.scalars(
        select(EventStoreOrders.id)
        .where(EventStoreOrders.held_until.is_not(None), EventStoreOrders.held_until < now, EventStoreOrders.status == "pending")
        .order_by(EventStoreOrders.held_until)
        .limit(limit)
    )).all()
    done = 0
    for order_id in ids:
        try:
            o = await session.scalar(
                select(EventStoreOrders)
                .where(EventStoreOrders.id == order_id, EventStoreOrders.status == "pending", EventStoreOrders.held_until < now)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if o is None:
                continue
            for line in _in_lock_order(o):
                await _unreserve(session, line)
            o.held_until = None
            await session.commit()
            done += 1
        except Exception:  # noqa: BLE001
            logger.exception("store hold sweep: order %s failed", order_id)
            await session.rollback()
    return done


def _summary_line(o: EventStoreOrders) -> str:
    parts = []
    for line in o.lines:
        name = line.item_name + (f" ({line.variant_label})" if line.variant_label else "")
        parts.append(f"{name} ×{line.qty}" if line.qty > 1 else name)
    return ", ".join(parts)


def order_out(o: EventStoreOrders, reg: Reg | None) -> dict:
    return dict(
        id=str(o.id), registration_id=str(o.registration_id), guest=(reg.full_name if reg else "—"), ticket_id=(reg.ticket_id if reg else None), status=o.status, total=_f(o.total),
        lines=[dict(item_id=str(line.item_id), item_name=line.item_name, variant_label=line.variant_label, qty=line.qty, unit_price=_f(line.unit_price), line_total=_f(line.line_total), is_free=line.is_free) for line in o.lines],
        summary=_summary_line(o), paid_at=_iso(o.paid_at), handed_over_at=_iso(o.handed_over_at), handed_over_by=o.handed_over_by, created_at=_iso(o.created_at),
    )


#: What the organiser's list shows by default: everything money was taken for.
_LISTED = ["paid", "refunded", "oversold"]


async def orders(session, event: Events, *, q: str | None, status: str | None, page: int, page_size: int) -> dict:
    base = select(EventStoreOrders, Reg).join(Reg, Reg.id == EventStoreOrders.registration_id).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status.in_(_LISTED if status is None else [status]))
    if q:
        like = f"%{q.strip()}%"
        base = base.where(or_(Reg.full_name.ilike(like), Reg.email.ilike(like), Reg.ticket_id.ilike(like)))
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (await session.execute(base.order_by(EventStoreOrders.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    return dict(items=[order_out(o, r) for o, r in rows], total=total or 0, page=page, page_size=page_size)


async def order_for(session, event: Events, order_id) -> EventStoreOrders:
    o = await session.scalar(select(EventStoreOrders).where(EventStoreOrders.id == _uuid(order_id), EventStoreOrders.event_id == event.id))
    if o is None:
        raise CustomHTTPException(404, message="Order not found")
    return o


async def _who(session, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    from app.api.users.models import Users

    row = (await session.execute(select(Users.full_name, Users.email).where(Users.id == user_id))).first()
    return (row[0] or row[1]) if row else None


async def hand_over(session, o: EventStoreOrders, by_user_id: int | None, undo: bool = False) -> dict:
    """Handed over once: one conditional UPDATE, so two doors scanning the
    same order cannot both hand it over. The second hears who did, and when
    (409 ALREADY_HANDED_OVER)."""
    table = EventStoreOrders.__table__
    if undo:
        done = (await session.execute(
            update(table).where(table.c.id == o.id, table.c.status == "paid")
            .values(handed_over_at=None, handed_over_by=None).returning(table.c.id)
        )).first()
    else:
        done = (await session.execute(
            update(table).where(table.c.id == o.id, table.c.status == "paid", table.c.handed_over_at.is_(None))
            .values(handed_over_at=_now(), handed_over_by=by_user_id).returning(table.c.id)
        )).first()
    if done is None:
        now = (await session.execute(
            select(table.c.status, table.c.handed_over_at, table.c.handed_over_by).where(table.c.id == o.id)
        )).first()
        if now is None or now.status != "paid":
            raise CustomHTTPException(422, message="Only a paid order is handed over")
        raise CustomHTTPException(
            409,
            message="This order was already handed over.",
            error_code="ALREADY_HANDED_OVER",
            errors={"handed_over_at": _iso(now.handed_over_at), "handed_over_by": now.handed_over_by, "handed_over_by_name": await _who(session, now.handed_over_by)},
        )
    await session.commit()
    o = await session.scalar(select(EventStoreOrders).where(EventStoreOrders.id == o.id).execution_options(populate_existing=True))
    reg = await session.get(Reg, o.registration_id)
    return order_out(o, reg)


async def orders_for_registration(session, event: Events, registration_id) -> list[dict]:
    rows = (await session.scalars(select(EventStoreOrders).where(EventStoreOrders.event_id == event.id, EventStoreOrders.registration_id == _uuid(registration_id), EventStoreOrders.status == "paid"))).all()
    reg = await session.get(Reg, _uuid(registration_id))
    return [order_out(o, reg) for o in rows]


# ─── The figures ─────────────────────────────────────────────────────────


async def summary(session, event: Events) -> dict:
    items = await list_items(session, event)
    paid = select(EventStoreOrders).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status == "paid")
    orders_paid = await session.scalar(select(func.count()).select_from(paid.subquery())) or 0
    revenue = await session.scalar(select(func.coalesce(func.sum(EventStoreOrders.total), 0)).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status == "paid")) or 0
    units = await session.scalar(
        select(func.coalesce(func.sum(EventStoreOrderLines.qty), 0)).join(EventStoreOrders, EventStoreOrders.id == EventStoreOrderLines.order_id).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status == "paid")
    ) or 0
    to_hand_over = await session.scalar(select(func.count()).select_from(EventStoreOrders).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status == "paid", EventStoreOrders.handed_over_at.is_(None))) or 0
    oversold = await session.scalar(select(func.count()).select_from(EventStoreOrders).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status == "oversold")) or 0
    ticket_revenue = await session.scalar(select(func.coalesce(func.sum(Reg.paid_amount), 0)).where(Reg.event_id == event.id, Reg.is_deleted == False, Reg.is_paid == True)) or 0  # noqa: E712
    sized = max((i for i in items if i["variants"]), key=lambda i: i["sold"], default=None)
    latest = (await session.execute(select(EventStoreOrders, Reg).join(Reg, Reg.id == EventStoreOrders.registration_id).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status == "paid").order_by(EventStoreOrders.paid_at.desc()).limit(8))).all()
    return dict(
        enabled=bool(getattr(event, "merch_enabled", False)), items_total=len(items), items_on_sale=sum(1 for i in items if i["on_sale"] and not i["sold_out"]), sold_out=sum(1 for i in items if i["sold_out"]),
        units_sold=int(units), orders_paid=int(orders_paid), revenue=_f(revenue), ticket_revenue=_f(ticket_revenue), to_hand_over=int(to_hand_over), oversold=int(oversold),
        sizes=[dict(label=v["label"], sold=v["sold"]) for v in (sized["variants"] if sized else [])], sizes_item=sized["name"] if sized else None,
        latest_orders=[order_out(o, r) for o, r in latest],
    )
