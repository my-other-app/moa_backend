"""What the club pushed to phones, as the Announcements page needs it.

A send is one row here, whatever it was: an announcement from the console
or a live update from the room. Each says who it went to, how many opened
it and how fast. Above the list: how much the club sends, how far it
reaches, how much of it is opened, when it lands best, and how many of the
people registered for what is coming can be reached at all.

Pushes are stored one row per person, so a live update to 1,500 people is
1,500 rows. They are grouped back into sends in SQL (`_grouped`); before,
every row the club had ever pushed was read into Python and grouped there,
twice for each open of the page (the list and its figures), and the cost
grew with every update sent. The list is paged in SQL; the figures read
every send, kept 30 s (app/api/hot_reads) and forgotten when the club
pushes something new.
"""
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by, array_agg

from app.api import hot_reads
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


def _grouped(club_id: int):
    """One row per send, grouped from the per-person rows of the club's
    events: one kind, one title, one body, one event, one minute. Its time is
    the latest of its rows and its audience that row's, as when Python
    grouped them newest first."""
    N = Notifications
    minute = func.date_trunc("minute", N.created_at)
    read = N.status == NotificationStatus.read
    return (
        select(
            N.type.label("type"),
            N.title.label("title"),
            N.description.label("body"),
            N.event_id.label("event_id"),
            func.max(N.created_at).label("created_at"),
            func.count().label("sent"),
            func.count().filter(read).label("read"),
            func.count().filter(read, N.updated_at - N.created_at <= QUICK).label("read_quickly"),
            array_agg(aggregate_order_by(N.data["audience"].as_string(), N.created_at.desc()))[1].label("audience"),
        )
        .join(Events, Events.id == N.event_id)
        .where(Events.club_id == club_id, Events.is_deleted == False, N.type.in_(list(KINDS)), N.is_deleted == False)  # noqa: E712
        .group_by(N.type, N.title, N.description, N.event_id, minute)
    )


def _newest_first(g) -> tuple:
    # Then what tells two sends of one moment apart, so a send is on one page only.
    return (g.c.created_at.desc(), g.c.event_id.desc(), g.c.type, g.c.title, g.c.body)


def _with_event(g):
    return select(g, Events.name.label("event_name"), Events.event_datetime.label("event_starts_at")).join(Events, Events.id == g.c.event_id)


def _send(row) -> dict:
    """A grouped row, as the page shows a send."""
    minute = row.created_at.replace(second=0, microsecond=0)
    sent, read = int(row.sent), int(row.read)
    return dict(
        id=hashlib.sha1(f"{row.type}|{row.title}|{row.event_id}|{minute.isoformat()}".encode()).hexdigest()[:16],
        kind=KINDS[row.type], title=row.title, body=row.body, event_id=row.event_id, created_at=row.created_at,
        audience=row.audience, sent=sent, read=read, read_quickly=int(row.read_quickly),
        event=dict(id=row.event_id, name=row.event_name, starts_at=row.event_starts_at),
        read_pct=round(100 * read / sent) if sent else 0, slot=_slot(row.created_at),
    )


async def _every_send(session, club_id: int) -> list[dict]:
    g = _grouped(club_id).subquery("sends")
    return [_send(r) for r in (await session.execute(_with_event(g).order_by(*_newest_first(g)))).all()]


async def _sends(session, club_id: int) -> list[dict]:
    """Every send of the club, newest first, kept 30 s: the figures above the
    page read all of them. A new send forgets it (`forget`)."""
    return await hot_reads.kept(("announcements", club_id), hot_reads.ANNOUNCEMENTS_SECONDS, lambda: _every_send(session, club_id))


def forget(*club_ids: Optional[int]) -> None:
    """These clubs just pushed something: their figures are worked out again."""
    for club_id in {c for c in club_ids if c is not None}:
        hot_reads.forget(("announcements", club_id))


async def history(session, club_id: int, *, scope: str = "all", search: Optional[str] = None, limit: int = 20, offset: int = 0) -> dict:
    """A page of the sends, scoped and searched (title, body or event name),
    and how many of each kind there are in all."""
    g = _grouped(club_id).subquery("sends")
    counts = dict(all=0, announcements=0, live_updates=0)
    for kind, n in (await session.execute(select(g.c.type, func.count()).group_by(g.c.type))).all():
        counts["announcements" if KINDS.get(kind) == "announcement" else "live_updates"] += int(n)
        counts["all"] += int(n)
    where = []
    if scope == "announcements":
        where.append(g.c.type == "club_announcement")
    elif scope == "live_updates":
        where.append(g.c.type == "event_live_update")
    term = (search or "").strip().lower()
    if term:
        where.append(or_(*(func.strpos(func.lower(func.coalesce(col, "")), term) > 0 for col in (g.c.title, g.c.body, Events.name))))
    page = _with_event(g).add_columns(func.count().over().label("total")).where(*where).order_by(*_newest_first(g)).limit(limit).offset(offset)
    rows = (await session.execute(page)).all()
    if rows:
        total = int(rows[0].total)
    elif offset:  # past the end: the page says nothing of how many there are
        total = int(await session.scalar(select(func.count()).select_from(_with_event(g).where(*where).subquery())) or 0)
    else:
        total = 0
    return dict(items=[_send(r) for r in rows], total=total, limit=limit, offset=offset, counts=counts)


async def _reachable(session, event_ids: list[int]) -> dict[int, dict]:
    """Per event: registered, checked in, and how many of each hold a phone
    the app can reach. Each registration asks about its own check-in and its
    own person's devices (both indexed), rather than every check-in and
    device on the platform being read for the comparison."""
    out = {i: dict(registered=0, checked_in=0, reachable=0, reachable_checked_in=0) for i in event_ids}
    if not event_ids:
        return out
    Reg = EventRegistrationsLink
    came = exists().where(CheckIns.registration_id == Reg.id)
    tokened = exists().where(UserDeviceTokens.user_id == Reg.user_id, UserDeviceTokens.is_deleted == False)  # noqa: E712
    q = (
        select(
            Reg.event_id,
            func.count(),
            func.count().filter(came),
            func.count().filter(tokened),
            func.count().filter(tokened & came),
        )
        .where(Reg.event_id.in_(event_ids), Reg.is_deleted == False)  # noqa: E712
        .group_by(Reg.event_id)
    )
    for eid, regs, checked, reach, reach_came in (await session.execute(q)).all():
        out[eid].update(registered=regs, checked_in=checked, reachable=reach, reachable_checked_in=reach_came)
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
