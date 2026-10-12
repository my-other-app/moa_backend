"""The event store.

An item has a price and a stock (or sizes with a stock each), who may buy
it and who gets it free. A guest picks at registration; the picks become
an order that is pending until the same Razorpay payment as the ticket
captures, then paid. Stock is sold on payment, not on picking. At the
door, a paid order is handed over once.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select

from app.api.events.models import EventRegistrationsLink as Reg, Events
from app.api.store.models import EventStoreItems, EventStoreOrderLines, EventStoreOrders, EventStoreVariants
from app.core.storage import objects
from app.response import CustomHTTPException


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


def item_out(m: EventStoreItems, earned: Decimal = Decimal(0)) -> dict:
    total, sold = _total(m), _sold(m)
    return dict(
        id=str(m.id), event_id=m.event_id, name=m.name, description=m.description, photo_key=m.photo_key,
        photo_url=objects.presigned_url(m.photo_key) if m.photo_key else None, price=_f(m.price), stock=m.stock, sold=sold, total=total,
        left=max(0, total - sold), earned=_f(earned), available_roles=m.available_roles, free_roles=list(m.free_roles or []), on_sale=m.on_sale,
        sold_out=total > 0 and sold >= total, sort_order=m.sort_order,
        variants=[dict(id=str(v.id), label=v.label, stock=v.stock, sold=v.sold_count, left=max(0, v.stock - v.sold_count)) for v in m.variants],
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
    rows = list((await session.scalars(select(EventStoreItems).where(EventStoreItems.event_id == event.id, EventStoreItems.is_deleted == False).order_by(EventStoreItems.sort_order, EventStoreItems.created_at))).all())  # noqa: E712
    earned = await _earned(session, event.id)
    return [item_out(m, earned.get(m.id, Decimal(0))) for m in rows]


async def item_for(session, event: Events, item_id) -> EventStoreItems:
    m = await session.scalar(select(EventStoreItems).where(EventStoreItems.id == _uuid(item_id), EventStoreItems.event_id == event.id, EventStoreItems.is_deleted == False))  # noqa: E712
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


async def update_item(session, event: Events, m: EventStoreItems, body) -> dict:
    data = body.model_dump(exclude_unset=True)
    if "variants" in data and data["variants"] is not None:
        keep = {v.label.strip().lower(): v for v in body.variants}
        for existing in list(m.variants):
            if existing.label.lower() in keep:
                new_stock = keep.pop(existing.label.lower()).stock
                if new_stock < existing.sold_count:
                    raise CustomHTTPException(422, message=f"{existing.label}: {existing.sold_count} already sold; stock cannot go below that")
                existing.stock = new_stock
            elif existing.sold_count > 0:
                raise CustomHTTPException(422, message=f"{existing.label} has been sold; it cannot be removed")
            else:
                m.variants.remove(existing)
        for i, v in enumerate(keep.values()):
            m.variants.append(EventStoreVariants(label=v.label.strip(), stock=v.stock, sort_order=len(m.variants) + i))
        m.stock = None
    if "stock" in data and data["stock"] is not None:
        if m.variants:
            raise CustomHTTPException(422, message="This item has sizes; set the stock per size")
        if data["stock"] < m.sold_count:
            raise CustomHTTPException(422, message=f"{m.sold_count} already sold; stock cannot go below that")
        m.stock = data["stock"]
    for k in ("name", "description", "photo_key", "price", "available_roles", "free_roles", "on_sale", "sort_order"):
        if k in data and (data[k] is not None or k in ("description", "photo_key", "available_roles")):
            setattr(m, k, data[k].strip() if k == "name" else data[k])
    await session.commit()
    await session.refresh(m)
    return item_out(m, (await _earned(session, event.id)).get(m.id, Decimal(0)))


async def delete_item(session, m: EventStoreItems) -> None:
    if _sold(m) > 0:
        # Paid for by someone: it stays on their order. It leaves the shelf instead.
        m.on_sale = False
    m.is_deleted = True
    m.deleted_at = _now()
    await session.commit()


# ─── The catalogue a guest sees ──────────────────────────────────────────


async def catalog(session, event: Events) -> dict:
    if not getattr(event, "merch_enabled", False):
        return dict(enabled=False, items=[])
    rows = list((await session.scalars(select(EventStoreItems).where(EventStoreItems.event_id == event.id, EventStoreItems.is_deleted == False, EventStoreItems.on_sale == True).order_by(EventStoreItems.sort_order))).all())  # noqa: E712
    items = []
    for m in rows:
        if m.available_roles and "attendee" not in m.available_roles:
            continue  # the public form only ever registers attendees
        total, sold = _total(m), _sold(m)
        if total > 0 and sold >= total:
            continue
        items.append(dict(
            id=str(m.id), name=m.name, description=m.description, photo_url=objects.presigned_url(m.photo_key) if m.photo_key else None,
            price=_f(Decimal(0) if "attendee" in (m.free_roles or []) else m.price), left=None if m.variants else max(0, total - sold),
            variants=[dict(id=str(v.id), label=v.label, left=max(0, v.stock - v.sold_count)) for v in m.variants if v.stock - v.sold_count > 0],
        ))
    return dict(enabled=True, items=items)


# ─── Orders ──────────────────────────────────────────────────────────────


async def pending_for(session, registration_id) -> EventStoreOrders | None:
    return await session.scalar(select(EventStoreOrders).where(EventStoreOrders.registration_id == registration_id, EventStoreOrders.status == "pending"))


async def pending_total(session, registration_id) -> Decimal:
    o = await pending_for(session, registration_id)
    return Decimal(o.total) if o else Decimal(0)


async def place_order(session, event: Events, reg: Reg, picks, role: str = "attendee") -> EventStoreOrders | None:
    """The picks become one order. Replaces any earlier pending order for the same registration."""
    earlier = await pending_for(session, reg.id)
    if earlier is not None:
        earlier.status = "cancelled"
    if not picks:
        await session.flush()
        return None
    if not getattr(event, "merch_enabled", False):
        raise CustomHTTPException(422, message="Merchandise is not on sale for this event")
    order = EventStoreOrders(event_id=event.id, registration_id=reg.id, user_id=reg.user_id, status="pending", total=Decimal(0))
    total = Decimal(0)
    seen = set()
    for p in picks:
        m = await session.scalar(select(EventStoreItems).where(EventStoreItems.id == p.item_id, EventStoreItems.event_id == event.id, EventStoreItems.is_deleted == False, EventStoreItems.on_sale == True))  # noqa: E712
        if m is None:
            raise CustomHTTPException(422, message="One of the items is no longer on sale")
        if m.available_roles and role not in m.available_roles:
            raise CustomHTTPException(422, message=f"{m.name} is not available to {role}s")
        variant = None
        if m.variants:
            variant = next((v for v in m.variants if p.variant_id is not None and v.id == p.variant_id), None)
            if variant is None:
                raise CustomHTTPException(422, message=f"Pick a size for {m.name}")
            key = (m.id, variant.id)
            left = variant.stock - variant.sold_count
        else:
            key = (m.id, None)
            left = (m.stock or 0) - m.sold_count
        if key in seen:
            raise CustomHTTPException(422, message=f"{m.name} is in the order twice")
        seen.add(key)
        if p.qty > left:
            raise CustomHTTPException(422, message=f"{m.name}{' ' + variant.label if variant else ''}: only {left} left" if left > 0 else f"{m.name}{' ' + variant.label if variant else ''} is sold out")
        free = role in (m.free_roles or [])
        unit = Decimal(0) if free else Decimal(m.price)
        line_total = unit * p.qty
        total += line_total
        order.lines.append(EventStoreOrderLines(item_id=m.id, variant_id=variant.id if variant else None, item_name=m.name, variant_label=variant.label if variant else None, qty=p.qty, unit_price=unit, line_total=line_total, is_free=free))
    order.total = total
    session.add(order)
    await session.flush()
    if total == 0:
        await mark_paid(session, order)
    return order


async def _bump(session, order: EventStoreOrders, sign: int) -> None:
    for line in order.lines:
        if line.variant_id is not None:
            v = await session.get(EventStoreVariants, line.variant_id)
            if v is not None:
                v.sold_count = max(0, v.sold_count + sign * line.qty)
        m = await session.get(EventStoreItems, line.item_id)
        if m is not None and line.variant_id is None:
            m.sold_count = max(0, m.sold_count + sign * line.qty)


async def mark_paid(session, order: EventStoreOrders) -> None:
    if order.status == "paid":
        return
    order.status = "paid"
    order.paid_at = _now()
    await _bump(session, order, +1)
    await session.flush()


async def mark_refunded(session, registration_id) -> int:
    n = 0
    for o in (await session.scalars(select(EventStoreOrders).where(EventStoreOrders.registration_id == registration_id, EventStoreOrders.status == "paid"))).all():
        o.status = "refunded"
        await _bump(session, o, -1)
        n += 1
    await session.flush()
    return n


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
        summary=_summary_line(o), paid_at=_iso(o.paid_at), handed_over_at=_iso(o.handed_over_at), created_at=_iso(o.created_at),
    )


async def orders(session, event: Events, *, q: str | None, status: str | None, page: int, page_size: int) -> dict:
    base = select(EventStoreOrders, Reg).join(Reg, Reg.id == EventStoreOrders.registration_id).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status.in_(["paid", "refunded"] if status is None else [status]))
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


async def hand_over(session, o: EventStoreOrders, by_user_id: int | None, undo: bool = False) -> dict:
    if o.status != "paid":
        raise CustomHTTPException(422, message="Only a paid order is handed over")
    if undo:
        o.handed_over_at, o.handed_over_by = None, None
    else:
        if o.handed_over_at is None:
            o.handed_over_at, o.handed_over_by = _now(), by_user_id
    await session.commit()
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
    ticket_revenue = await session.scalar(select(func.coalesce(func.sum(Reg.paid_amount), 0)).where(Reg.event_id == event.id, Reg.is_deleted == False, Reg.is_paid == True)) or 0  # noqa: E712
    sized = max((i for i in items if i["variants"]), key=lambda i: i["sold"], default=None)
    latest = (await session.execute(select(EventStoreOrders, Reg).join(Reg, Reg.id == EventStoreOrders.registration_id).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status == "paid").order_by(EventStoreOrders.paid_at.desc()).limit(8))).all()
    return dict(
        enabled=bool(getattr(event, "merch_enabled", False)), items_total=len(items), items_on_sale=sum(1 for i in items if i["on_sale"] and not i["sold_out"]), sold_out=sum(1 for i in items if i["sold_out"]),
        units_sold=int(units), orders_paid=int(orders_paid), revenue=_f(revenue), ticket_revenue=_f(ticket_revenue), to_hand_over=int(to_hand_over),
        sizes=[dict(label=v["label"], sold=v["sold"]) for v in (sized["variants"] if sized else [])], sizes_item=sized["name"] if sized else None,
        latest_orders=[order_out(o, r) for o, r in latest],
    )
