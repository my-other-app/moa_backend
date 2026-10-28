"""The club's events, as the list page needs them.

A row is the event plus what an organiser checks it for: how many are in
(and of how many), how many came, what it has brought in, whether sign-up is
open right now, whether the door is. Above the list sit the figures for the
season and the count of things waiting on somebody.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.orm import joinedload

from app.api.checkins.models import CheckIns
from app.api.checkins.service import door_open
from app.api.closeout.models import EventStatus
from app.api.events.models import EventRegistrationsLink, Events
from app.api.finance.service import _per_event

SCOPES = ("upcoming", "now", "draft", "past", "all")
SORTS = ("soonest", "latest", "registered", "viewed")
CLOSING_SOON = timedelta(hours=48)


def _ends_at_sql():
    return Events.event_datetime + func.cast(func.concat(Events.duration, " HOURS"), INTERVAL)


def ends_at(event: Events) -> datetime:
    return event.event_datetime + timedelta(hours=event.duration or 0)


def door_is_open(event: Events, now: datetime) -> bool:
    """The gate admits now: its switch says so, and the event is within a day either side."""
    if not door_open(event, now):
        return False
    return event.event_datetime - timedelta(hours=24) <= now <= ends_at(event) + timedelta(minutes=event.close_after_minutes or 240)


def registration_open(event: Events, regs: int, now: datetime) -> bool:
    """Whether someone could sign up this minute."""
    if (event.lifecycle or EventStatus.published) not in (EventStatus.published, EventStatus.live):
        return False
    if ends_at(event) <= now:
        return False
    if event.reg_startdate and event.reg_startdate > now:
        return False
    if event.reg_enddate and event.reg_enddate < now:
        return False
    if event.max_participants and regs >= event.max_participants:
        return False
    return True


def _counts():
    reg = select(func.count(EventRegistrationsLink.id)).where(EventRegistrationsLink.event_id == Events.id, EventRegistrationsLink.is_deleted == False).correlate(Events).scalar_subquery()  # noqa: E712
    attended = select(func.count(EventRegistrationsLink.id)).where(EventRegistrationsLink.event_id == Events.id, EventRegistrationsLink.is_attended == True, EventRegistrationsLink.is_deleted == False).correlate(Events).scalar_subquery()  # noqa: E712
    checked = select(func.count(CheckIns.id)).where(CheckIns.event_id == Events.id).correlate(Events).scalar_subquery()
    return reg, attended, checked


def _scoped(query, scope: str, now):
    ends = _ends_at_sql()
    if scope == "upcoming":
        return query.where(ends >= now, Events.lifecycle != EventStatus.draft)
    if scope == "now":
        return query.where(Events.lifecycle == EventStatus.live)
    if scope == "draft":
        return query.where(Events.lifecycle == EventStatus.draft)
    if scope == "past":
        return query.where(ends < now)
    return query


async def list_events(session, club_id: int, *, scope: str = "all", sort: Optional[str] = None, search: Optional[str] = None, limit: int = 20, offset: int = 0, is_ended: Optional[bool] = None) -> tuple[list, int]:
    now = func.now()
    reg, attended, checked = _counts()
    base = select(Events).where(Events.club_id == club_id, Events.is_deleted == False)  # noqa: E712
    # The old switch still works: is_ended maps onto the scopes.
    if is_ended is True and scope == "all":
        scope = "past"
    elif is_ended is False and scope == "all":
        scope = "upcoming"
    base = _scoped(base, scope, now)
    if search and search.strip():
        base = base.where(Events.name.ilike(f"%{search.strip()}%"))

    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0

    order = {
        "soonest": (Events.event_datetime.asc(),),
        "latest": (Events.event_datetime.desc(),),
        "registered": (reg.desc(), Events.event_datetime.desc()),
        "viewed": (Events.page_views.desc().nulls_last(), Events.event_datetime.desc()),
    }.get(sort or ("soonest" if scope in ("upcoming", "now") else "latest"), (Events.event_datetime.desc(),))

    q = select(Events, reg, attended, checked).where(Events.id.in_(select(base.subquery().c.id))).options(joinedload(Events.category), joinedload(Events.club)).order_by(*order).limit(limit).offset(offset)
    rows = (await session.execute(q)).all()
    money = await _per_event(session, [e.id for e, *_ in rows])
    # Registration requests waiting for the club (app/api/approvals).
    from app.api.approvals import service as approvals

    # One grouped statement for the page, never a subquery a row.
    asked = await approvals.totals(session, [e.id for e, *_ in rows])
    at = datetime.now(timezone.utc)
    items = []
    for event, regs, attended_n, checked_n in rows:
        m = money[event.id]
        items.append(
            jsonable_encoder(event)
            | {
                "requires_approval": bool(event.requires_approval),
                "high_demand_label": bool(event.high_demand_label),
                "requests_pending": asked.get(event.id, {}).get("pending", 0),
                "requests_total": asked.get(event.id, {}).get("total", 0),
                "registration_count": regs, "attended_count": attended_n, "checked_in": checked_n,
                "status": event.status, "max_participants": event.max_participants,
                "collected": float(m["tickets"] + m["merch"]), "outstanding": float(m["outstanding"]),
                "door_open": door_is_open(event, at), "registration_open": registration_open(event, regs, at),
                "ends_at": ends_at(event),
            }
        )
    return items, total


async def figures(session, club_id: int) -> dict:
    """The season above the list: what is coming, who is in, what it brought, and what waits."""
    now = datetime.now(timezone.utc)
    reg, attended, checked = _counts()
    live = select(Events, reg, attended, checked).where(Events.club_id == club_id, Events.is_deleted == False, Events.lifecycle != EventStatus.archived)  # noqa: E712
    rows = (await session.execute(live.options(joinedload(Events.category)))).all()
    upcoming = [(e, r) for e, r, _a, _c in rows if ends_at(e) > now and e.lifecycle is not EventStatus.draft]
    money = await _per_event(session, [e.id for e, _ in upcoming])
    next_event = min((e for e, _ in upcoming), key=lambda e: e.event_datetime, default=None)
    closing_soon = [e for e, r in upcoming if e.reg_enddate and now < e.reg_enddate <= now + CLOSING_SOON and registration_open(e, r, now)]
    doors_open = [e for e, _r in upcoming if door_is_open(e, now)]
    unmarked = [e for e, r, a, c in rows if ends_at(e) <= now and r > 0 and a == 0 and c == 0 and e.lifecycle is not EventStatus.draft]
    stale_drafts = [e for e, *_ in rows if e.lifecycle is EventStatus.draft and e.event_datetime <= now + timedelta(days=7)]
    from app.api.approvals import service as approvals

    asked = await approvals.totals(session, [e.id for e, *_ in rows])
    waiting = {e: t["pending"] for e, t in asked.items() if t["pending"]}
    return {
        "upcoming": len(upcoming),
        "next_event": {"id": next_event.id, "name": next_event.name, "starts_at": next_event.event_datetime} if next_event else None,
        "registered": sum(r for _, r in upcoming),
        # Registration requests on the events to come (app/api/approvals).
        "requires_approval": any(e.requires_approval for e, _ in upcoming),
        "requests_pending": sum(asked.get(e.id, {}).get("pending", 0) for e, _ in upcoming),
        "requests_total": sum(asked.get(e.id, {}).get("total", 0) for e, _ in upcoming),
        "capacity": sum(e.max_participants for e, _ in upcoming if e.max_participants) or None,
        "collected": float(sum((money[e.id]["tickets"] + money[e.id]["merch"] for e, _ in upcoming), 0)),
        "outstanding": float(sum((money[e.id]["outstanding"] for e, _ in upcoming), 0)),
        "attention": {
            "closing_soon": [{"id": e.id, "name": e.name, "at": e.reg_enddate} for e in sorted(closing_soon, key=lambda e: e.reg_enddate)],
            "doors_open": [{"id": e.id, "name": e.name} for e in doors_open],
            "unmarked": [{"id": e.id, "name": e.name} for e in sorted(unmarked, key=lambda e: e.event_datetime, reverse=True)],
            "stale_drafts": [{"id": e.id, "name": e.name, "at": e.event_datetime} for e in stale_drafts],
            # Registration requests waiting for the club (app/api/approvals).
            "requests_pending": [
                {"id": e.id, "name": e.name, "count": waiting[e.id]} for e, *_ in rows if waiting.get(e.id)
            ],
        },
        "past": sum(1 for e, *_ in rows if ends_at(e) <= now and e.lifecycle is not EventStatus.draft),
        "drafts": sum(1 for e, *_ in rows if e.lifecycle is EventStatus.draft),
        "generated_at": now,
    }


__all__ = ["list_events", "figures", "SCOPES", "SORTS"]
