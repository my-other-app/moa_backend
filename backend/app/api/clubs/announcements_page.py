"""What the club pushed to phones, as the Announcements page needs it.

A send is one row here, whatever it was: an announcement from the console
or a live update from the room. Each says who it went to, how many opened
it and how fast. Above the list: how much the club sends, how far it
reaches, how much of it is opened, when it lands best, and how many of the
people registered for what is coming can be reached at all.
"""
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from app.api.checkins.models import CheckIns
from app.api.events.models import EventRegistrationsLink, Events
from app.api.notifications.models import NotificationStatus, Notifications
from app.api.users.models import UserDeviceTokens

KINDS = {"club_announcement": "announcement", "event_live_update": "live_update"}
SCOPES = ("all", "announcements", "live_updates")
WINDOW = timedelta(days=30)
QUICK = timedelta(hours=1)
SLOTS = (("morning", 6, 12, "Morning · 6–12"), ("afternoon", 12, 17, "Afternoon · 12–5"), ("evening", 17, 21, "Evening · 5–9"), ("night", 21, 30, "Night · 9–6"))
IST = timezone(timedelta(hours=5, minutes=30))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slot(dt: datetime) -> str:
    h = dt.astimezone(IST).hour
    for key, lo, hi, _label in SLOTS:
        if lo <= h < hi or (key == "night" and h < 6):
            return key
    return "night"


async def _events(session, club_id: int) -> dict[int, Events]:
    return {e.id: e for e in (await session.scalars(select(Events).where(Events.club_id == club_id, Events.is_deleted == False))).all()}  # noqa: E712


async def _sends(session, club_id: int) -> list[dict]:
    """Every push, grouped from the per-person rows: one send is one title, one body, one event, one minute."""
    events = await _events(session, club_id)
    if not events:
        return []
    rows = (await session.scalars(
        select(Notifications).where(Notifications.event_id.in_(list(events)), Notifications.type.in_(list(KINDS)), Notifications.is_deleted == False).order_by(Notifications.created_at.desc())  # noqa: E712
    )).all()
    groups: dict = {}
    for n in rows:
        minute = n.created_at.replace(second=0, microsecond=0)
        key = (n.type, n.title, n.description, n.event_id, minute)
        g = groups.get(key)
        if g is None:
            g = groups[key] = dict(
                id=hashlib.sha1(f"{n.type}|{n.title}|{n.event_id}|{minute.isoformat()}".encode()).hexdigest()[:16],
                kind=KINDS[n.type], title=n.title, body=n.description, event_id=n.event_id, created_at=n.created_at,
                audience=(n.data or {}).get("audience") if isinstance(n.data, dict) else None, sent=0, read=0, read_quickly=0,
            )
        g["sent"] += 1
        if n.status == NotificationStatus.read:
            g["read"] += 1
            if n.updated_at and n.updated_at - n.created_at <= QUICK:
                g["read_quickly"] += 1
    out = []
    for g in groups.values():
        e = events.get(g["event_id"])
        g["event"] = dict(id=e.id, name=e.name, starts_at=e.event_datetime) if e else None
        g["read_pct"] = round(100 * g["read"] / g["sent"]) if g["sent"] else 0
        g["slot"] = _slot(g["created_at"])
        out.append(g)
    out.sort(key=lambda g: g["created_at"], reverse=True)
    return out


def _matches(g: dict, term: str) -> bool:
    t = term.lower()
    return t in (g["title"] or "").lower() or t in (g["body"] or "").lower() or t in ((g["event"] or {}).get("name") or "").lower()


async def history(session, club_id: int, *, scope: str = "all", search: Optional[str] = None, limit: int = 20, offset: int = 0) -> dict:
    sends = await _sends(session, club_id)
    counts = dict(all=len(sends), announcements=sum(1 for g in sends if g["kind"] == "announcement"), live_updates=sum(1 for g in sends if g["kind"] == "live_update"))
    if scope == "announcements":
        sends = [g for g in sends if g["kind"] == "announcement"]
    elif scope == "live_updates":
        sends = [g for g in sends if g["kind"] == "live_update"]
    if search and search.strip():
        sends = [g for g in sends if _matches(g, search.strip())]
    return dict(items=sends[offset : offset + limit], total=len(sends), limit=limit, offset=offset, counts=counts)


async def _reachable(session, event_ids: list[int]) -> dict[int, dict]:
    """Per event: registered, checked in, and how many of each hold a phone the app can reach."""
    out = {i: dict(registered=0, checked_in=0, reachable=0, reachable_checked_in=0) for i in event_ids}
    if not event_ids:
        return out
    Reg = EventRegistrationsLink
    tokened = select(UserDeviceTokens.user_id).where(UserDeviceTokens.is_deleted == False).distinct().subquery()  # noqa: E712
    checked = select(CheckIns.registration_id).subquery()
    q = (
        select(
            Reg.event_id,
            func.count(),
            func.count().filter(Reg.id.in_(select(checked.c.registration_id))),
            func.count().filter(Reg.user_id.in_(select(tokened.c.user_id))),
            func.count().filter(Reg.user_id.in_(select(tokened.c.user_id)) & Reg.id.in_(select(checked.c.registration_id))),
        )
        .where(Reg.event_id.in_(event_ids), Reg.is_deleted == False)  # noqa: E712
        .group_by(Reg.event_id)
    )
    for eid, regs, came, reach, reach_came in (await session.execute(q)).all():
        out[eid].update(registered=regs, checked_in=came, reachable=reach, reachable_checked_in=reach_came)
    return out


async def audience(session, event: Events) -> dict:
    """What a send to this event would reach, by audience."""
    r = (await _reachable(session, [event.id]))[event.id]
    return dict(
        event=dict(id=event.id, name=event.name, starts_at=event.event_datetime),
        registered=r["registered"], checked_in=r["checked_in"], reachable=r["reachable"],
        by_audience=dict(
            all=dict(people=r["registered"], reachable=r["reachable"]),
            attendees=dict(people=r["checked_in"], reachable=r["reachable_checked_in"]),
            non_attendees=dict(people=r["registered"] - r["checked_in"], reachable=r["reachable"] - r["reachable_checked_in"]),
        ),
    )


async def insights(session, club_id: int) -> dict:
    now = _now()
    sends = await _sends(session, club_id)
    cur = [g for g in sends if now - g["created_at"] <= WINDOW]
    prev = [g for g in sends if WINDOW < now - g["created_at"] <= 2 * WINDOW]

    def totals(gs):
        sent = sum(g["sent"] for g in gs)
        read = sum(g["read"] for g in gs)
        quick = sum(g["read_quickly"] for g in gs)
        return dict(sends=len(gs), reached=sent, opened=read, open_rate=round(100 * read / sent) if sent else None, quick_rate=round(100 * quick / sent) if sent else None)

    c, p = totals(cur), totals(prev)
    # When it lands: open rate by time of day, over every send with a result.
    by_slot: dict = defaultdict(lambda: dict(sent=0, read=0, sends=0))
    for g in sends:
        s = by_slot[g["slot"]]
        s["sent"] += g["sent"]
        s["read"] += g["read"]
        s["sends"] += 1
    slots = [dict(key=k, label=label, sends=by_slot[k]["sends"], open_rate=round(100 * by_slot[k]["read"] / by_slot[k]["sent"]) if by_slot[k]["sent"] else None) for k, _lo, _hi, label in SLOTS]
    best = max((s for s in slots if s["sends"] >= 2 and s["open_rate"] is not None), key=lambda s: s["open_rate"], default=None)
    # Who can be reached for what is coming.
    upcoming = list((await session.scalars(select(Events).where(Events.club_id == club_id, Events.is_deleted == False, Events.event_datetime > now).order_by(Events.event_datetime.asc()).limit(5))).all())  # noqa: E712
    reach = await _reachable(session, [e.id for e in upcoming])
    coming = [dict(id=e.id, name=e.name, starts_at=e.event_datetime, registered=reach[e.id]["registered"], reachable=reach[e.id]["reachable"]) for e in upcoming]
    registered_ahead = sum(x["registered"] for x in coming)
    reachable_ahead = sum(x["reachable"] for x in coming)
    best_send = max(cur, key=lambda g: (g["read_pct"], g["sent"]), default=None)
    return dict(
        sends=c["sends"], sends_delta=(c["sends"] - p["sends"]) if (cur or prev) else None,
        reached=c["reached"], reached_delta=(c["reached"] - p["reached"]) if (cur or prev) else None,
        open_rate=c["open_rate"], open_rate_prev=p["open_rate"], quick_rate=c["quick_rate"],
        last_sent_at=sends[0]["created_at"] if sends else None,
        best=dict(id=best_send["id"], title=best_send["title"], read_pct=best_send["read_pct"], sent=best_send["sent"]) if best_send else None,
        slots=slots, best_slot=best["key"] if best else None,
        coming=coming, registered_ahead=registered_ahead, reachable_ahead=reachable_ahead,
        all_time=dict(sends=len(sends), reached=sum(g["sent"] for g in sends), opened=sum(g["read"] for g in sends)),
        generated_at=now,
    )
