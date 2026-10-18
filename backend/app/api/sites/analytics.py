"""What the club's website brought: visits, who sent them, and the sign-ups after.

Every visit to the site is an opening of one of the club's club-page links:
either one the club made (the Instagram bio, the poster QR — those land on
the site) or the website's own link, which counts what came some other way.
So the site's visits are the clicks of those links, and its sign-ups are the
registrations credited to them; nothing is counted twice.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from app.api.club_domains.lookup import active_host
from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.events.models import EventRegistrationsLink, Events
from app.api.links import service as links
from app.api.links.models import TrackingClicks, TrackingLinks
from app.api.links.overview import device_of, referrer_of


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _delta(now: int, before: int) -> Optional[float]:
    return None if not before else round(100.0 * (now - before) / before, 1)


async def site_analytics(session, club: Clubs, days: int = 30, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    since = now - timedelta(days=days)
    before = since - timedelta(days=days)
    published = await links.site_published(session, club.id)
    own = await links.website_link(session, club.id)

    page_links = list((await session.scalars(
        select(TrackingLinks).where(TrackingLinks.club_id == club.id, TrackingLinks.event_id.is_(None), TrackingLinks.is_deleted == False)  # noqa: E712
    )).all())
    ids = [l.id for l in page_links]
    by_id = {l.id: l for l in page_links}

    clicks = list((await session.scalars(
        select(TrackingClicks).where(TrackingClicks.link_id.in_(ids), TrackingClicks.created_at >= before)
    )).all()) if ids else []
    regs = list((await session.execute(
        select(EventRegistrationsLink, Events.name)
        .join(Events, Events.id == EventRegistrationsLink.event_id)
        .where(EventRegistrationsLink.tracking_link_id.in_(ids), EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.created_at >= before)  # noqa: E712
    )).all()) if ids else []
    follows = list((await session.scalars(
        select(ClubUsersLink).where(ClubUsersLink.tracking_link_id.in_(ids), ClubUsersLink.is_deleted == False, ClubUsersLink.created_at >= since)  # noqa: E712
    )).all()) if ids else []

    cur = [c for c in clicks if c.created_at >= since]
    prev = [c for c in clicks if c.created_at < since]
    regs_cur = [(r, name) for r, name in regs if r.created_at >= since]
    regs_prev = [r for r, _ in regs if r.created_at < since]

    def day(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).date().isoformat()

    series: dict = {}
    for i in range(days, -1, -1):
        series[day(now - timedelta(days=i))] = dict(visits=0, signups=0)
    for c in cur:
        series.setdefault(day(c.created_at), dict(visits=0, signups=0))["visits"] += 1
    for r, _ in regs_cur:
        series.setdefault(day(r.created_at), dict(visits=0, signups=0))["signups"] += 1

    v_link: Counter = Counter(c.link_id for c in cur)
    s_link: Counter = Counter(r.tracking_link_id for r, _ in regs_cur)
    sources = []
    for lid in set(v_link) | set(s_link):
        l = by_id.get(lid)
        if l is None:
            continue
        website = l.kind == links.WEBSITE
        sources.append(dict(
            kind="website" if website else "link", link_id=str(l.id),
            name="Found the site on their own" if website else l.name,
            visits=v_link[lid], signups=s_link[lid], rate=_pct(s_link[lid], v_link[lid]),
        ))
    sources.sort(key=lambda s: (-s["visits"], -s["signups"], s["name"]))

    own_cur = [c for c in cur if own is not None and c.link_id == own.id]
    referrers = Counter(referrer_of(c.referrer) for c in own_cur)
    devices = Counter(device_of(c.user_agent) for c in cur)
    by_event: dict = defaultdict(lambda: dict(name="", signups=0, came=0))
    for r, name in regs_cur:
        row = by_event[r.event_id]
        row["name"] = name
        row["signups"] += 1
        row["came"] += 1 if r.is_attended else 0

    visits, signups = len(cur), len(regs_cur)
    return dict(
        published=published, days=days,
        link=dict(id=str(own.id), code=own.code, short_url=links.short_url(own), destination=links.destination(own, club.slug, site_host=await active_host(session, club.id))) if own else None,
        visits=visits, visitors=len({c.visitor or str(c.id) for c in cur}), signups=signups,
        came=sum(1 for r, _ in regs_cur if r.is_attended), follows=len(follows), rate=_pct(signups, visits),
        deltas=dict(visits=_delta(visits, len(prev)), signups=_delta(signups, len(regs_prev))),
        series=[dict(day=k, **v) for k, v in sorted(series.items())],
        sources=sources,
        referrers=[dict(referrer=k, visits=n) for k, n in referrers.most_common(6)],
        devices=[dict(device=k, visits=n) for k, n in devices.most_common()],
        events=sorted(({"event_id": k, **v} for k, v in by_event.items()), key=lambda e: -e["signups"])[:8],
    )
