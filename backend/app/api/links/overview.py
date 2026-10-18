"""Tracking links, as the Tracking links page needs them.

Every link the club made — to its page and to each of its events — with
what it did in the period: opened how often, by how many people, how many
of them followed or signed up, and what those did next. Above the links,
the period day by day, the channels they add up to, and what deserves a
look: links nobody has opened, and how much arrives with no link at all.
One link can be opened further: its days, the devices it was opened on,
and where the openings came from.

Everything is counted by the database, grouped by link and by day (or
month) in India, from the start of the period before: the page reads a row
per link and a row per day, not every click, follow and registration of the
period (a link shared widely has hundreds of thousands). The clicks are read
through ix_tracking_clicks_link_created.
"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import String, and_, cast, exists, func, select

from app.api.club_domains.lookup import active_host
from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.events.models import EventRegistrationsLink, Events
from app.api.links.models import TrackingClicks, TrackingLinks
from app.api.links.service import destination, short_url
from app.response import CustomHTTPException

IST = timezone(timedelta(hours=5, minutes=30))
IST_NAME = "Asia/Kolkata"
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


def _bucket(column, by_month: bool):
    """The day (or month) in India an instant falls in, as SQL: a plain timestamp at its start."""
    return func.date_trunc("month" if by_month else "day", func.timezone(IST_NAME, column))


def _key(d, by_month: bool) -> str:
    """What _bucket gives, as the page's key: "2026-10-18" or "2026-10"."""
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%Y-%m") if by_month else d.isoformat()


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


def _who():
    """A click's visitor; a click without one counts as its own."""
    return func.coalesce(TrackingClicks.visitor, cast(TrackingClicks.id, String))


async def _all_time_clicks(session, ids: list[UUID]) -> dict[UUID, tuple[int, Optional[datetime]]]:
    """Per link: openings ever, and the last one — to know a link nobody has
    opened. Read from the index alone (link_id, created_at)."""
    if not ids:
        return {}
    rows = (await session.execute(select(TrackingClicks.link_id, func.count(), func.max(TrackingClicks.created_at)).where(TrackingClicks.link_id.in_(ids)).group_by(TrackingClicks.link_id))).all()
    return {lid: (int(n), last) for lid, n, last in rows}


def _follows_where(club_id: int, since: datetime) -> list:
    return [ClubUsersLink.club_id == club_id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False, ClubUsersLink.created_at >= since]  # noqa: E712


def _regs_where(club_id: int, event: Optional[Events], since: datetime) -> list:
    conds = [Events.club_id == club_id, Events.is_deleted == False, EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.created_at >= since]  # noqa: E712
    if event is not None:
        conds.append(EventRegistrationsLink.event_id == event.id)
    return conds


def _target(link: TrackingLinks, events: dict[int, Events]) -> dict:
    if link.kind == "website":
        return dict(kind="website", id=None, name="Website", starts_at=None)
    if link.event_id is None:
        return dict(kind="club", id=None, name="Club page", starts_at=None)
    e = events.get(link.event_id)
    return dict(kind="event", id=link.event_id, name=e.name if e else "Event", starts_at=e.event_datetime if e else None)


def _buckets(since: datetime, now: datetime, days: int, by_month: bool) -> list[str]:
    if by_month:
        out = []
        m = since.astimezone(IST).replace(day=1)
        while m <= now.astimezone(IST):
            out.append(m.strftime("%Y-%m"))
            m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        return out
    today = now.astimezone(IST).date().isoformat()
    return [b for b in ((since.astimezone(IST) + timedelta(days=i)).date().isoformat() for i in range(days + 1)) if b <= today]


async def overview(session, club: Clubs, event: Optional[Events], days: int, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    since = now - timedelta(days=days)
    before = since - timedelta(days=days)
    by_month = days > 120
    cur = TrackingClicks.created_at >= since

    links = await _links(session, club, event)
    ids = [l.id for l in links]
    events = await _events_of(session, links)
    ever = await _all_time_clicks(session, ids)
    site_host = await active_host(session, club.id) if event is None else None

    # Clicks: per link, in the period and the one before; the period by day; the uniques overall.
    c_now: dict = defaultdict(int)
    c_prev: dict = defaultdict(int)
    uniques: dict = defaultdict(int)
    series: dict = defaultdict(lambda: dict(clicks=0, conversions=0))
    total_uniques = 0
    if ids:
        in_scope = [TrackingClicks.link_id.in_(ids), TrackingClicks.created_at >= before]
        for lid, n, u, p in (await session.execute(
            select(
                TrackingClicks.link_id, func.count().filter(cur), func.count(func.distinct(_who())).filter(cur), func.count().filter(~cur),
            ).where(*in_scope).group_by(TrackingClicks.link_id)
        )).all():
            c_now[lid], uniques[lid], c_prev[lid] = int(n), int(u), int(p)
        bucket = _bucket(TrackingClicks.created_at, by_month)
        for d, n in (await session.execute(select(bucket, func.count()).where(TrackingClicks.link_id.in_(ids), cur).group_by(bucket))).all():
            series[_key(d, by_month)]["clicks"] += int(n)
        total_uniques = int(await session.scalar(select(func.count(func.distinct(_who()))).where(TrackingClicks.link_id.in_(ids), cur)) or 0)

    # Conversions: follows (the club's page) and registrations, per link, now and before; all of them, tracked or not.
    v_now: dict = defaultdict(int)
    v_prev: dict = defaultdict(int)
    second: dict = defaultdict(int)
    tracked_follows = all_follows = tracked_regs = all_regs = 0
    if event is None:
        f_cur = ClubUsersLink.created_at >= since
        for lid, n, p in (await session.execute(
            select(ClubUsersLink.tracking_link_id, func.count().filter(f_cur), func.count().filter(~f_cur))
            .where(*_follows_where(club.id, before)).group_by(ClubUsersLink.tracking_link_id)
        )).all():
            all_follows += int(n)
            if lid is not None:
                v_now[lid] += int(n)
                v_prev[lid] += int(p)
                tracked_follows += int(n)
        # Of the period's tracked follows, the ones who then registered for one of the club's events.
        later = exists(
            select(EventRegistrationsLink.id).join(Events, Events.id == EventRegistrationsLink.event_id)
            .where(
                EventRegistrationsLink.user_id == ClubUsersLink.user_id, Events.club_id == club.id, Events.is_deleted == False,  # noqa: E712
                EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.created_at >= ClubUsersLink.created_at,  # noqa: E712
            )
        )
        for lid, n in (await session.execute(
            select(ClubUsersLink.tracking_link_id, func.count())
            .where(*_follows_where(club.id, since), ClubUsersLink.tracking_link_id.isnot(None), later)
            .group_by(ClubUsersLink.tracking_link_id)
        )).all():
            second[lid] += int(n)
        f_bucket = _bucket(ClubUsersLink.created_at, by_month)
        for d, n in (await session.execute(
            select(f_bucket, func.count()).where(*_follows_where(club.id, since), ClubUsersLink.tracking_link_id.isnot(None)).group_by(f_bucket)
        )).all():
            series[_key(d, by_month)]["conversions"] += int(n)

    r_cur = EventRegistrationsLink.created_at >= since
    regs = select(
        EventRegistrationsLink.tracking_link_id, func.count().filter(r_cur),
        func.count().filter(and_(r_cur, EventRegistrationsLink.is_attended == True)), func.count().filter(~r_cur),  # noqa: E712
    ).join(Events, Events.id == EventRegistrationsLink.event_id).where(*_regs_where(club.id, event, before)).group_by(EventRegistrationsLink.tracking_link_id)
    for lid, n, attended, p in (await session.execute(regs)).all():
        all_regs += int(n)
        if lid is not None:
            v_now[lid] += int(n)
            v_prev[lid] += int(p)
            second[lid] += int(attended)
            tracked_regs += int(n)
    r_bucket = _bucket(EventRegistrationsLink.created_at, by_month)
    for d, n in (await session.execute(
        select(r_bucket, func.count()).join(Events, Events.id == EventRegistrationsLink.event_id)
        .where(*_regs_where(club.id, event, since), EventRegistrationsLink.tracking_link_id.isnot(None)).group_by(r_bucket)
    )).all():
        series[_key(d, by_month)]["conversions"] += int(n)

    rows = []
    for l in links:
        n, c = c_now[l.id], v_now[l.id]
        rows.append(dict(
            id=l.id, name=l.name, code=l.code, url=short_url(l), destination=destination(l, getattr(club, "slug", None), getattr(events.get(l.event_id), "slug", None), site_host),
            utm_source=l.utm_source, utm_medium=l.utm_medium, utm_campaign=l.utm_campaign, utm_content=l.utm_content,
            target=_target(l, events), kind=l.kind or "custom",
            conversion_noun="registration" if l.event_id else ("sign-up" if l.kind == "website" else "follow"),
            second_noun="checked in" if (l.event_id or l.kind == "website") else "registered later",
            clicks=n, uniques=uniques[l.id], conversions=c, rate=_pct(c, n), second=second[l.id],
            clicks_ever=ever.get(l.id, (0, None))[0], last_click_at=ever.get(l.id, (0, None))[1], created_at=l.created_at,
        ))

    series_out = [dict(date=b, **series[b]) for b in _buckets(since, now, days, by_month)]

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
    # Every tracked conversion counts, a retired link's too (as the page always counted them).
    total_conv, prev_conv = sum(v_now.values()), sum(v_prev.values())
    rate_now, rate_before = _pct(total_conv, total_clicks), _pct(prev_conv, prev_clicks)
    all_conv = all_follows + all_regs

    never = [dict(id=r["id"], name=r["name"], target=r["target"]) for r in rows if r["clicks_ever"] == 0 and now - r["created_at"] >= NEVER_OPENED_AFTER]
    return dict(
        scope="event" if event is not None else "club", period_days=days, grain="month" if by_month else "day",
        links=rows, series=series_out, by_source=sources,
        totals=dict(
            links=len(rows), clicks=total_clicks, uniques=total_uniques, conversions=total_conv, rate=rate_now,
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
    """One link, opened further: its days, the devices, and where the openings
    came from -- counted by the database, the browsers and referrers grouped
    before they are read (there are far fewer of them than clicks)."""
    now = now or _now()
    since = now - timedelta(days=days)
    link = await session.scalar(select(TrackingLinks).where(TrackingLinks.id == link_id, TrackingLinks.club_id == club.id, TrackingLinks.is_deleted == False))  # noqa: E712
    if link is None or (event is not None and link.event_id != event.id):
        raise CustomHTTPException(404, message="No such link")
    by_month = days > 120
    scope = [TrackingClicks.link_id == link.id, TrackingClicks.created_at >= since]
    total, uniq, first, last = (await session.execute(
        select(func.count(), func.count(func.distinct(_who())), func.min(TrackingClicks.created_at), func.max(TrackingClicks.created_at)).where(*scope)
    )).one()
    bucket = _bucket(TrackingClicks.created_at, by_month)
    days_ = {_key(d, by_month): int(n) for d, n in (await session.execute(select(bucket, func.count()).where(*scope).group_by(bucket))).all()}
    devices: Counter = Counter()
    for ua, n in (await session.execute(select(TrackingClicks.user_agent, func.count()).where(*scope).group_by(TrackingClicks.user_agent))).all():
        devices[device_of(ua)] += int(n)
    referrers: Counter = Counter()
    for ref, n in (await session.execute(select(TrackingClicks.referrer, func.count()).where(*scope).group_by(TrackingClicks.referrer))).all():
        referrers[referrer_of(ref)] += int(n)
    hour = func.extract("hour", func.timezone(IST_NAME, TrackingClicks.created_at))
    hours = {int(h): int(n) for h, n in (await session.execute(select(hour, func.count()).where(*scope).group_by(hour))).all()}
    target = event if event is not None else (await _events_of(session, [link])).get(link.event_id)
    return dict(
        id=link.id, name=link.name, code=link.code, url=short_url(link), destination=destination(link, getattr(club, "slug", None), getattr(target, "slug", None), await active_host(session, club.id) if link.kind == "website" else None),
        clicks=int(total or 0), uniques=int(uniq or 0),
        first_click_at=first, last_click_at=last,
        series=[dict(date=b, clicks=days_.get(b, 0)) for b in _buckets(since, now, days, by_month)],
        devices=[dict(device=d, clicks=n) for d, n in devices.most_common()],
        referrers=[dict(referrer=r, clicks=n) for r, n in referrers.most_common(6)],
        hours=[dict(hour=h, clicks=hours.get(h, 0)) for h in range(24)],
        created_at=link.created_at, generated_at=now,
    )

