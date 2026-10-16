"""Tracking links, as the Tracking links page needs them.

Every link the club made — to its page and to each of its events — with
what it did in the period: opened how often, by how many people, how many
of them followed or signed up, and what those did next. Above the links,
the period day by day, the channels they add up to, and what deserves a
look: links nobody has opened, and how much arrives with no link at all.
One link can be opened further: its days, the devices it was opened on,
and where the openings came from.
"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import and_, select

from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.events.models import EventRegistrationsLink, Events
from app.api.links.models import TrackingClicks, TrackingLinks
from app.api.links.service import destination, short_url
from app.response import CustomHTTPException

IST = timezone(timedelta(hours=5, minutes=30))
PHONE = re.compile(r"iPhone|Android.*Mobile|Windows Phone|Mobile Safari|Opera Mini", re.I)
TABLET = re.compile(r"iPad|Tablet|Android(?!.*Mobile)", re.I)
# Hosts an in-app browser or a share sheet reports; the rest keep their own name.
HOST_NAMES = {"instagram.com": "Instagram", "facebook.com": "Facebook", "whatsapp.com": "WhatsApp", "linkedin.com": "LinkedIn", "t.co": "X / Twitter", "twitter.com": "X / Twitter", "x.com": "X / Twitter", "youtube.com": "YouTube", "google.com": "Google", "telegram.org": "Telegram"}
NEVER_OPENED_AFTER = timedelta(days=3)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _delta(now: float, before: float) -> Optional[float]:
    return round((now - before) / before * 100, 1) if before else None


def _day(dt: datetime) -> str:
    return dt.astimezone(IST).date().isoformat()


def _month(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m")


def device_of(ua: Optional[str]) -> str:
    if not ua:
        return "unknown"
    if TABLET.search(ua):
        return "tablet"
    if PHONE.search(ua):
        return "phone"
    return "desktop"


def referrer_of(ref: Optional[str]) -> str:
    if not ref:
        return "Direct · app or typed"
    host = (urlsplit(ref).hostname or "").lower()
    if not host:
        return "Direct · app or typed"
    for prefix in ("www.", "l.", "lm.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return HOST_NAMES.get(host, host)


async def _links(session, club: Clubs, event: Optional[Events]) -> list[TrackingLinks]:
    where = [TrackingLinks.club_id == club.id, TrackingLinks.is_deleted == False]  # noqa: E712
    if event is not None:
        where.append(TrackingLinks.event_id == event.id)
    return list((await session.scalars(select(TrackingLinks).where(*where).order_by(TrackingLinks.created_at.desc()))).all())


async def _events_of(session, links: list[TrackingLinks]) -> dict[int, Events]:
    ids = {l.event_id for l in links if l.event_id}
    if not ids:
        return {}
    return {e.id: e for e in (await session.scalars(select(Events).where(Events.id.in_(ids)))).all()}


async def _clicks(session, ids: list[UUID], since: datetime) -> list[TrackingClicks]:
    if not ids:
        return []
    return list((await session.scalars(select(TrackingClicks).where(TrackingClicks.link_id.in_(ids), TrackingClicks.created_at >= since).order_by(TrackingClicks.created_at.asc()))).all())


async def _all_time_clicks(session, ids: list[UUID]) -> dict[UUID, tuple[int, Optional[datetime]]]:
    """Per link: openings ever, and the last one — to know a link nobody has opened."""
    from sqlalchemy import func

    if not ids:
        return {}
    rows = (await session.execute(select(TrackingClicks.link_id, func.count(), func.max(TrackingClicks.created_at)).where(TrackingClicks.link_id.in_(ids)).group_by(TrackingClicks.link_id))).all()
    return {lid: (int(n), last) for lid, n, last in rows}


async def _follows(session, club_id: int, since: datetime) -> list[ClubUsersLink]:
    return list((await session.scalars(select(ClubUsersLink).where(ClubUsersLink.club_id == club_id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False, ClubUsersLink.created_at >= since))).all())  # noqa: E712


async def _registrations(session, club_id: int, event: Optional[Events], since: datetime) -> list[EventRegistrationsLink]:
    q = select(EventRegistrationsLink).join(Events, Events.id == EventRegistrationsLink.event_id).where(Events.club_id == club_id, Events.is_deleted == False, EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.created_at >= since)  # noqa: E712
    if event is not None:
        q = q.where(EventRegistrationsLink.event_id == event.id)
    return list((await session.scalars(q)).all())


async def _registered_later(session, club_id: int, follows: list[ClubUsersLink]) -> set[int]:
    """Of these followers, the ones who registered for one of the club's events after following."""
    if not follows:
        return set()
    by_user = {f.user_id: f.created_at for f in follows}
    rows = (await session.execute(
        select(EventRegistrationsLink.user_id, EventRegistrationsLink.created_at)
        .join(Events, and_(Events.id == EventRegistrationsLink.event_id, Events.club_id == club_id))
        .where(EventRegistrationsLink.user_id.in_(list(by_user)), EventRegistrationsLink.is_deleted == False)  # noqa: E712
    )).all()
    return {uid for uid, at in rows if at >= by_user[uid]}


def _target(link: TrackingLinks, events: dict[int, Events]) -> dict:
    if link.kind == "website":
        return dict(kind="website", id=None, name="Website", starts_at=None)
    if link.event_id is None:
        return dict(kind="club", id=None, name="Club page", starts_at=None)
    e = events.get(link.event_id)
    return dict(kind="event", id=link.event_id, name=e.name if e else "Event", starts_at=e.event_datetime if e else None)


async def overview(session, club: Clubs, event: Optional[Events], days: int, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    since = now - timedelta(days=days)
    before = since - timedelta(days=days)
    by_month = days > 120

    links = await _links(session, club, event)
    ids = [l.id for l in links]
    events = await _events_of(session, links)
    clicks = await _clicks(session, ids, before)
    ever = await _all_time_clicks(session, ids)
    follows = [] if event is not None else await _follows(session, club.id, before)
    regs = await _registrations(session, club.id, event, before)
    later = await _registered_later(session, club.id, [f for f in follows if f.tracking_link_id and f.created_at >= since])

    cur = lambda at: at >= since  # noqa: E731
    # Per link, in the period and the one before.
    c_now: dict = defaultdict(int)
    c_prev: dict = defaultdict(int)
    uniques: dict = defaultdict(set)
    last_click: dict = {}
    for c in clicks:
        if cur(c.created_at):
            c_now[c.link_id] += 1
            uniques[c.link_id].add(c.visitor or str(c.id))
            last_click[c.link_id] = c.created_at
        else:
            c_prev[c.link_id] += 1
    v_now: dict = defaultdict(int)
    v_prev: dict = defaultdict(int)
    second: dict = defaultdict(int)
    for f in follows:
        if not f.tracking_link_id:
            continue
        if cur(f.created_at):
            v_now[f.tracking_link_id] += 1
            if f.user_id in later:
                second[f.tracking_link_id] += 1
        else:
            v_prev[f.tracking_link_id] += 1
    for r in regs:
        if not r.tracking_link_id:
            continue
        if cur(r.created_at):
            v_now[r.tracking_link_id] += 1
            if r.is_attended:
                second[r.tracking_link_id] += 1
        else:
            v_prev[r.tracking_link_id] += 1

    rows = []
    for l in links:
        n, c = c_now[l.id], v_now[l.id]
        rows.append(dict(
            id=l.id, name=l.name, code=l.code, url=short_url(l), destination=destination(l, getattr(club, "slug", None)),
            utm_source=l.utm_source, utm_medium=l.utm_medium, utm_campaign=l.utm_campaign, utm_content=l.utm_content,
            target=_target(l, events), kind=l.kind or "custom",
            conversion_noun="registration" if l.event_id else ("sign-up" if l.kind == "website" else "follow"),
            second_noun="checked in" if (l.event_id or l.kind == "website") else "registered later",
            clicks=n, uniques=len(uniques[l.id]), conversions=c, rate=_pct(c, n), second=second[l.id],
            clicks_ever=ever.get(l.id, (0, None))[0], last_click_at=ever.get(l.id, (0, None))[1], created_at=l.created_at,
        ))

    # The period, day by day (month by month over a long one): opened, and what it brought.
    key = _month if by_month else _day
    series: dict = defaultdict(lambda: dict(clicks=0, conversions=0))
    for c in clicks:
        if cur(c.created_at):
            series[key(c.created_at)]["clicks"] += 1
    for f in follows:
        if f.tracking_link_id and cur(f.created_at):
            series[key(f.created_at)]["conversions"] += 1
    for r in regs:
        if r.tracking_link_id and cur(r.created_at):
            series[key(r.created_at)]["conversions"] += 1
    if by_month:
        buckets = []
        m = since.astimezone(IST).replace(day=1)
        while m <= now.astimezone(IST):
            buckets.append(m.strftime("%Y-%m"))
            m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        buckets = [(since.astimezone(IST) + timedelta(days=i)).date().isoformat() for i in range(days + 1)]
        buckets = [b for b in buckets if b <= now.astimezone(IST).date().isoformat()]
    series_out = [dict(date=b, **series[b]) for b in buckets]

    # Channels: the sources the links add up to.
    by_source: dict = defaultdict(lambda: dict(links=0, clicks=0, conversions=0))
    for r in rows:
        s = by_source[r["utm_source"]]
        s["links"] += 1
        s["clicks"] += r["clicks"]
        s["conversions"] += r["conversions"]
    sources = sorted((dict(source=k, rate=_pct(v["conversions"], v["clicks"]), **v) for k, v in by_source.items()), key=lambda s: (-s["conversions"], -s["clicks"], s["source"]))
    best_source = sources[0] if sources and sources[0]["conversions"] > 0 else None
    best_link = max((r for r in rows if r["conversions"] > 0), key=lambda r: (r["conversions"], r["rate"]), default=None)

    total_clicks, prev_clicks = sum(c_now.values()), sum(c_prev.values())
    total_conv, prev_conv = sum(v_now.values()), sum(v_prev.values())
    rate_now, rate_before = _pct(total_conv, total_clicks), _pct(prev_conv, prev_clicks)
    tracked_follows = sum(1 for f in follows if f.tracking_link_id and cur(f.created_at))
    all_follows = sum(1 for f in follows if cur(f.created_at))
    tracked_regs = sum(1 for r in regs if r.tracking_link_id and cur(r.created_at))
    all_regs = sum(1 for r in regs if cur(r.created_at))
    all_conv = all_follows + all_regs

    never = [dict(id=r["id"], name=r["name"], target=r["target"]) for r in rows if r["clicks_ever"] == 0 and now - r["created_at"] >= NEVER_OPENED_AFTER]
    return dict(
        scope="event" if event is not None else "club", period_days=days, grain="month" if by_month else "day",
        links=rows, series=series_out, by_source=sources,
        totals=dict(
            links=len(rows), clicks=total_clicks, uniques=len({v for s in uniques.values() for v in s}), conversions=total_conv, rate=rate_now,
            follows=tracked_follows, registrations=tracked_regs, second=sum(second.values()), share_of_all=_pct(total_conv, all_conv),
            best_source=dict(source=best_source["source"], conversions=best_source["conversions"], rate=best_source["rate"]) if best_source else None,
            best_link=dict(id=best_link["id"], name=best_link["name"], conversions=best_link["conversions"], rate=best_link["rate"], source=best_link["utm_source"]) if best_link else None,
            last_click_at=max((r["last_click_at"] for r in rows if r["last_click_at"]), default=None),
        ),
        deltas=dict(clicks=_delta(total_clicks, prev_clicks), conversions=_delta(total_conv, prev_conv), rate_pts=round(rate_now - rate_before, 1) if prev_clicks else None),
        untracked=dict(follows=dict(tracked=tracked_follows, total=all_follows), registrations=dict(tracked=tracked_regs, total=all_regs), conversions=all_conv - total_conv, total=all_conv),
        attention=dict(never_opened=never),
        all_time=dict(links=len(rows), clicks=sum(v[0] for v in ever.values())),
        generated_at=now,
    )


async def detail(session, club: Clubs, event: Optional[Events], link_id: UUID, days: int, now: Optional[datetime] = None) -> dict:
    """One link, opened further: its days, the devices, and where the openings came from."""
    now = now or _now()
    since = now - timedelta(days=days)
    link = await session.scalar(select(TrackingLinks).where(TrackingLinks.id == link_id, TrackingLinks.club_id == club.id, TrackingLinks.is_deleted == False))  # noqa: E712
    if link is None or (event is not None and link.event_id != event.id):
        raise CustomHTTPException(404, message="No such link")
    clicks = await _clicks(session, [link.id], since)
    by_month = days > 120
    key = _month if by_month else _day
    days_: dict = defaultdict(int)
    for c in clicks:
        days_[key(c.created_at)] += 1
    if by_month:
        buckets = []
        m = since.astimezone(IST).replace(day=1)
        while m <= now.astimezone(IST):
            buckets.append(m.strftime("%Y-%m"))
            m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        today = now.astimezone(IST).date().isoformat()
        buckets = [b for b in ((since.astimezone(IST) + timedelta(days=i)).date().isoformat() for i in range(days + 1)) if b <= today]
    devices = Counter(device_of(c.user_agent) for c in clicks)
    referrers = Counter(referrer_of(c.referrer) for c in clicks)
    hours = Counter(c.created_at.astimezone(IST).hour for c in clicks)
    return dict(
        id=link.id, name=link.name, code=link.code, url=short_url(link), destination=destination(link, getattr(club, "slug", None)),
        clicks=len(clicks), uniques=len({c.visitor or str(c.id) for c in clicks}),
        first_click_at=clicks[0].created_at if clicks else None, last_click_at=clicks[-1].created_at if clicks else None,
        series=[dict(date=b, clicks=days_[b]) for b in buckets],
        devices=[dict(device=d, clicks=n) for d, n in devices.most_common()],
        referrers=[dict(referrer=r, clicks=n) for r, n in referrers.most_common(6)],
        hours=[dict(hour=h, clicks=hours.get(h, 0)) for h in range(24)],
        created_at=link.created_at, generated_at=now,
    )
