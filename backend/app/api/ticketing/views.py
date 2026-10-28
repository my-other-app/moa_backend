"""What the event page, the club's editor and the event's overview show of
an event's tickets: types with what is left, the waiting list, the sales."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select

from app.api.events.models import EventRegistrationsLink
from app.api.ticketing import places, waitlist
from app.api.ticketing import types as types_mod
from app.api.ticketing.models import DEFAULT_OFFER_HOURS, DiscountCodes
from app.core import money


def _now() -> datetime:
    return datetime.now(timezone.utc)


def type_status(event, ttype, left: Optional[int], now: datetime) -> str:
    """on_sale, sold_out, not_started, ended, or closed (the event's registration)."""
    if not waitlist.registration_open(event, now):
        return "closed"
    starts = max([d for d in (ttype.sales_start, event.reg_startdate) if d is not None], default=None)
    if starts is not None and now < starts:
        return "not_started"
    if ttype.sales_end is not None and now >= ttype.sales_end:
        return "ended"
    if left is not None and left <= 0:
        return "sold_out"
    return "on_sale"


async def public(session, event, *, include_hidden: Optional[int] = None, viewer=None) -> dict:
    """The event page's tickets. Hidden types are left out, but for the one
    a link names (`include_hidden`). Makes the event's default type when it
    has none (the caller commits)."""
    now = _now()
    await types_mod.ensure(session, event)
    types = await places.fresh(session, event)
    room = places.compute(event, types, await places.lines(session, event.id, now=now))
    wl_on = await waitlist.enabled(session, event)
    settings_row = await waitlist.settings_for(session, event.id) if wl_on else None
    shown = []
    for t in types:
        if t.is_hidden and t.id != include_hidden:
            continue
        left = room.for_type(t.id)
        status = type_status(event, t, left, now)
        price = types_mod.price_of(event, t, types)
        shown.append({
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "price": float(price),
            "currency": "INR",
            "is_free": price == 0,
            "capacity": types_mod.capacity_of(event, t, types),
            "available": left,
            "status": status,
            "sales_start": t.sales_start,
            "sales_end": t.sales_end,
            "per_person_limit": t.per_person_limit,
            "is_hidden": bool(t.is_hidden),
            "waitlist": wl_on and status == "sold_out",
        })
    visible = [t for t in types if not t.is_hidden]
    out = {
        "types": shown,
        # A registration must name its type (ticket_type_id) when this is true.
        "requires_choice": len(types) > 1 and len(visible) != 1,
        "available": room.event_public,
        "sold_out": bool(shown) and all(s["status"] == "sold_out" for s in shown),
        "waitlist": {
            "enabled": wl_on,
            "offer_hours": settings_row.offer_hours if settings_row is not None else DEFAULT_OFFER_HOURS,
        },
        "viewer_waitlist": [],
        "requires_approval": bool(getattr(event, "requires_approval", False)),
    }
    if viewer is not None:
        out["viewer_waitlist"] = [await entry_public(session, e) for e in await waitlist.mine(session, event.id, viewer)]
        # Their own registration request, while it is open (app/api/approvals).
        from app.api.approvals import service as approvals

        out["viewer_request"] = await approvals.viewer_request(session, event.id, viewer)
    return out


async def entry_public(session, entry) -> dict:
    names = await types_mod.names(session, [entry.ticket_type_id])
    return {
        "id": entry.id,
        "ticket_type_id": entry.ticket_type_id,
        "ticket_type_name": names.get(entry.ticket_type_id),
        "status": entry.status,
        "position": await waitlist.position(session, entry),
        "offer_expires_at": entry.offer_expires_at if entry.status == "offered" else None,
        "joined_at": entry.created_at,
    }


async def club_types(session, event) -> dict:
    """The editor's list: every live type, hidden ones too, with what sold."""
    from app.api.subscriptions import gates

    await types_mod.ensure(session, event)
    await session.commit()
    types = await places.fresh(session, event)
    lines_ = await places.lines(session, event.id)
    room = places.compute(event, types, lines_)
    held = await _registered(session, [t.id for t in types])
    return {
        "types": [
            {
                "id": t.id, "name": t.name, "description": t.description,
                "price": float(types_mod.price_of(event, t, types)),
                "capacity": types_mod.capacity_of(event, t, types) if not types_mod.follows(types) else event.max_participants,
                "sales_start": t.sales_start, "sales_end": t.sales_end, "is_hidden": bool(t.is_hidden),
                "per_person_limit": t.per_person_limit, "sort_order": t.sort_order, "is_default": bool(t.is_default),
                "sold": int(t.taken_count or 0), "available": room.for_type(t.id),
                "waiting": lines_.get(t.id, places.Line()).waiting,
                # The waiting list's offers; a confirmed request's hold is `held`.
                "offered": lines_.get(t.id, places.Line()).offered - lines_.get(t.id, places.Line()).held,
                "held": lines_.get(t.id, places.Line()).held,
                "has_registrations": held.get(t.id, False),
            }
            for t in types
        ],
        "follows_event": types_mod.follows(types),
        "event_capacity": event.max_participants,
        "event_available": room.event_public,
        "max_types": await gates.cap(session, event.club_id, "ticket_types_per_event"),
        "currency": "INR",
    }


async def _registered(session, type_ids: list[int]) -> dict[int, bool]:
    if not type_ids:
        return {}
    R = EventRegistrationsLink
    rows = await session.scalars(
        select(R.ticket_type_id).where(R.ticket_type_id.in_(type_ids), R.is_deleted == False).distinct()  # noqa: E712
    )
    return {i: True for i in rows.all()}


async def sales(session, event) -> dict:
    """The event's overview: per type, what sold and for how much, what is
    waiting for its payment, what is left, and who is in line; and the codes
    used on its tickets. `held`: places held for confirmed registration
    requests not paid yet (app/api/approvals), apart from the waiting list's
    `offered`; both hold their places."""
    await types_mod.ensure(session, event)
    await session.commit()
    types = await places.fresh(session, event)
    R = EventRegistrationsLink
    holding = or_(R.is_paid == True, R.actual_amount == 0)  # noqa: E712
    rows = (await session.execute(
        select(
            R.ticket_type_id,
            func.count().filter(holding),
            func.count().filter(R.is_paid == True),  # noqa: E712
            func.coalesce(func.sum(R.paid_amount).filter(R.is_paid == True), 0),  # noqa: E712
            func.count().filter(and_(R.is_paid == False, R.actual_amount > 0)),  # noqa: E712
            func.coalesce(func.sum(R.discount_amount).filter(holding), 0),
        )
        .where(R.event_id == event.id, R.is_deleted == False)  # noqa: E712
        .group_by(R.ticket_type_id)
    )).all()
    by_type = {r[0]: r for r in rows}
    lines_ = await places.lines(session, event.id)
    room = places.compute(event, types, lines_)
    out_types = []
    totals = {"sold": 0, "paid": 0, "revenue": 0.0, "pending": 0, "discount_total": 0.0, "waiting": 0, "offered": 0, "held": 0}
    for t in types:
        r = by_type.get(t.id)
        line = lines_.get(t.id, places.Line())
        item = {
            "id": t.id, "name": t.name, "price": float(types_mod.price_of(event, t, types)),
            "capacity": types_mod.capacity_of(event, t, types) if not types_mod.follows(types) else event.max_participants,
            "is_hidden": bool(t.is_hidden),
            "sold": int(r[1]) if r else 0,
            "paid": int(r[2]) if r else 0,
            "revenue": float(money.rupees(r[3])) if r else 0.0,
            "pending": int(r[4]) if r else 0,
            "discount_total": float(money.rupees(r[5])) if r else 0.0,
            "available": room.for_type(t.id),
            "waiting": line.waiting, "offered": line.offered - line.held, "held": line.held,
        }
        for key in totals:
            totals[key] += item[key]
        out_types.append(item)
    untyped = by_type.get(None)
    if untyped is not None:
        totals["sold"] += int(untyped[1])
        totals["paid"] += int(untyped[2])
        totals["revenue"] += float(money.rupees(untyped[3]))
        totals["pending"] += int(untyped[4])
    code_rows = (await session.execute(
        select(DiscountCodes.id, DiscountCodes.code, func.count(R.id), func.coalesce(func.sum(R.discount_amount), 0))
        .join(R, R.discount_code_id == DiscountCodes.id)
        .where(R.event_id == event.id, R.is_deleted == False, holding)  # noqa: E712
        .group_by(DiscountCodes.id, DiscountCodes.code)
        .order_by(func.count(R.id).desc())
        .execution_options(include_deleted=True)
    )).all()
    totals["revenue"] = round(totals["revenue"], 2)
    totals["discount_total"] = round(totals["discount_total"], 2)
    return {
        "types": out_types,
        "totals": totals,
        "codes": [{"id": i, "code": c, "uses": int(n), "discount_total": float(money.rupees(d))} for i, c, n, d in code_rows],
        "event_capacity": event.max_participants,
        "event_available": room.event_public,
        "waitlist_enabled": await waitlist.enabled(session, event),
        "currency": "INR",
    }
