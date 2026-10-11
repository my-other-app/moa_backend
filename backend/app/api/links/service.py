"""Tracking links: made, opened, and reconciled with the sign-ups they brought."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import joinedload

from app.api.clubs.models import ClubUsersLink
from app.api.events.models import EventRegistrationsLink, Events
from app.api.links.models import TrackingClicks, TrackingLinks
from app.config import settings
from app.response import CustomHTTPException

# No vowels, no look-alikes: a code read off a poster cannot be misread.
ALPHABET = "bcdfghjkmnpqrstvwxz23456789"
CODE_LENGTH = 8
TAG_CLEAN = re.compile(r"[^a-z0-9_\-.]+")


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def tag(value: Optional[str]) -> Optional[str]:
    """A UTM value as the analytics tools expect it: lower case, no spaces."""
    if value is None:
        return None
    cleaned = TAG_CLEAN.sub("_", value.strip().lower()).strip("_")
    return cleaned or None


def short_url(link: TrackingLinks) -> str:
    return f"{settings.PUBLIC_API_URL}/l/{link.code}"


def destination(link: TrackingLinks, club_slug: Optional[str] = None) -> str:
    base = f"{settings.PUBLIC_SITE_URL}/{link.event_id}" if link.event_id else settings.PUBLIC_CLUB_URL.format(slug=club_slug or link.club_id)
    params = {"utm_source": link.utm_source, "utm_medium": link.utm_medium}
    if link.utm_campaign:
        params["utm_campaign"] = link.utm_campaign
    if link.utm_content:
        params["utm_content"] = link.utm_content
    params["ref"] = link.code
    return f"{base}{'&' if '?' in base else '?'}{urlencode(params)}"


async def by_code(session, code: str) -> Optional[TrackingLinks]:
    if not code:
        return None
    return await session.scalar(
        select(TrackingLinks).options(joinedload(TrackingLinks.club)).where(TrackingLinks.code == code.strip().lower(), TrackingLinks.is_deleted == False)  # noqa: E712
    )


def visitor_of(request) -> Optional[str]:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ip = forwarded or (request.client.host if request.client else "")
    ua = request.headers.get("user-agent") or ""
    if not ip and not ua:
        return None
    return hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:64]


async def record_click(session, link: TrackingLinks, request) -> None:
    session.add(TrackingClicks(
        link_id=link.id, visitor=visitor_of(request),
        referrer=(request.headers.get("referer") or None) and request.headers.get("referer")[:300],
        user_agent=(request.headers.get("user-agent") or None) and request.headers.get("user-agent")[:200],
    ))
    await session.commit()


async def attribute_registration(session, registration: EventRegistrationsLink, event: Events, ref: Optional[str]) -> None:
    """Credit a sign-up to the link whose code it carried — only a link of
    this event's club, and only an event link for this very event."""
    link = await by_code(session, ref) if ref else None
    if link is None or link.club_id != event.club_id:
        return
    if link.event_id is not None and link.event_id != event.id:
        return
    registration.tracking_link_id = link.id


async def attribute_follow(session, follow: ClubUsersLink, club_id: int, ref: Optional[str]) -> None:
    link = await by_code(session, ref) if ref else None
    if link is None or link.club_id != club_id:
        return
    follow.tracking_link_id = link.id


# ─── Making and keeping links ─────────────────────────────────────────────


async def create(session, club, event, body, user=None) -> TrackingLinks:
    source, medium = tag(body.utm_source), tag(body.utm_medium)
    if not source or not medium:
        raise CustomHTTPException(422, message="Give the link a source and a medium.")
    for _ in range(5):
        link = TrackingLinks(
            club_id=club.id, event_id=event.id if event is not None else None, name=body.name.strip(), code=new_code(),
            utm_source=source, utm_medium=medium, utm_campaign=tag(body.utm_campaign), utm_content=tag(body.utm_content),
            created_by=getattr(user, "id", None),
        )
        if await session.scalar(select(func.count()).where(TrackingLinks.code == link.code)) == 0:
            session.add(link)
            await session.commit()
            await session.refresh(link)
            return link
    raise CustomHTTPException(500, message="Could not mint a link code; try again.")


async def link_for(session, club, event, link_id: UUID) -> TrackingLinks:
    link = await session.scalar(
        select(TrackingLinks).where(TrackingLinks.id == link_id, TrackingLinks.club_id == club.id, TrackingLinks.is_deleted == False)  # noqa: E712
    )
    if link is None or (event is not None and link.event_id != event.id) or (event is None and link.event_id is not None):
        raise CustomHTTPException(404, message="No such link")
    return link


async def update(session, link: TrackingLinks, body) -> TrackingLinks:
    if body.name is not None:
        link.name = body.name.strip()
    for field in ("utm_source", "utm_medium"):
        value = getattr(body, field)
        if value is not None:
            cleaned = tag(value)
            if not cleaned:
                raise CustomHTTPException(422, message=f"{field} cannot be empty.")
            setattr(link, field, cleaned)
    for field in ("utm_campaign", "utm_content"):
        if field in body.model_fields_set:
            setattr(link, field, tag(getattr(body, field)))
    await session.commit()
    await session.refresh(link)
    return link


async def delete(session, link: TrackingLinks) -> None:
    link.is_deleted = True
    await session.commit()


# ─── The figures ──────────────────────────────────────────────────────────


def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _delta(now: float, before: float) -> Optional[float]:
    if before == 0:
        return None
    return round((now - before) / before * 100, 1)


async def summary(session, club, event, days: int = 30, now: Optional[datetime] = None) -> dict:
    """Every link in scope with its clicks and conversions in the period,
    the channels they add up to, the totals against the period before, and
    how many conversions came with no link at all."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    before = since - timedelta(days=days)
    in_period = lambda col: col >= since  # noqa: E731
    in_previous = lambda col: and_(col >= before, col < since)  # noqa: E731

    scope = [TrackingLinks.club_id == club.id, TrackingLinks.is_deleted == False]  # noqa: E712
    scope.append(TrackingLinks.event_id == event.id if event is not None else TrackingLinks.event_id.is_(None))
    links = list((await session.scalars(select(TrackingLinks).where(*scope).order_by(TrackingLinks.created_at.desc()))).all())
    ids = [l.id for l in links]

    clicks: dict = {}
    if ids:
        rows = (
            await session.execute(
                select(
                    TrackingClicks.link_id,
                    func.count().filter(in_period(TrackingClicks.created_at)),
                    func.count(func.distinct(TrackingClicks.visitor)).filter(in_period(TrackingClicks.created_at)),
                    func.count().filter(in_previous(TrackingClicks.created_at)),
                ).where(TrackingClicks.link_id.in_(ids)).group_by(TrackingClicks.link_id)
            )
        ).all()
        clicks = {link_id: (int(n), int(u), int(p)) for link_id, n, u, p in rows}

    conversions: dict = {}
    if event is not None:
        if ids:
            rows = (
                await session.execute(
                    select(
                        EventRegistrationsLink.tracking_link_id,
                        func.count().filter(in_period(EventRegistrationsLink.created_at)),
                        func.count().filter(and_(in_period(EventRegistrationsLink.created_at), EventRegistrationsLink.is_attended == True)),  # noqa: E712
                        func.count().filter(in_previous(EventRegistrationsLink.created_at)),
                    )
                    .where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.tracking_link_id.in_(ids), EventRegistrationsLink.is_deleted == False)  # noqa: E712
                    .group_by(EventRegistrationsLink.tracking_link_id)
                )
            ).all()
            conversions = {link_id: (int(n), int(s), int(p)) for link_id, n, s, p in rows}
        all_now, all_before = (
            await session.execute(
                select(func.count().filter(in_period(EventRegistrationsLink.created_at)), func.count().filter(in_previous(EventRegistrationsLink.created_at)))
                .where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
            )
        ).one()
        nouns = ("registration", "checked in")
    else:
        if ids:
            rows = (
                await session.execute(
                    select(
                        ClubUsersLink.tracking_link_id,
                        func.count().filter(in_period(ClubUsersLink.created_at)),
                        func.count().filter(in_previous(ClubUsersLink.created_at)),
                    )
                    .where(ClubUsersLink.club_id == club.id, ClubUsersLink.is_following == True, ClubUsersLink.tracking_link_id.in_(ids), ClubUsersLink.is_deleted == False)  # noqa: E712
                    .group_by(ClubUsersLink.tracking_link_id)
                )
            ).all()
            conversions = {link_id: (int(n), 0, int(p)) for link_id, n, p in rows}
            # Followers who went on to register for one of the club's events after following.
            later = (
                await session.execute(
                    select(ClubUsersLink.tracking_link_id, func.count(func.distinct(ClubUsersLink.user_id)))
                    .select_from(ClubUsersLink)
                    .join(EventRegistrationsLink, and_(EventRegistrationsLink.user_id == ClubUsersLink.user_id, EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.created_at >= ClubUsersLink.created_at))  # noqa: E712
                    .join(Events, and_(Events.id == EventRegistrationsLink.event_id, Events.club_id == club.id))
                    .where(ClubUsersLink.club_id == club.id, ClubUsersLink.is_following == True, ClubUsersLink.tracking_link_id.in_(ids), in_period(ClubUsersLink.created_at))  # noqa: E712
                    .group_by(ClubUsersLink.tracking_link_id)
                )
            ).all()
            for link_id, n in later:
                c = conversions.get(link_id, (0, 0, 0))
                conversions[link_id] = (c[0], int(n), c[2])
        all_now, all_before = (
            await session.execute(
                select(func.count().filter(in_period(ClubUsersLink.created_at)), func.count().filter(in_previous(ClubUsersLink.created_at)))
                .where(ClubUsersLink.club_id == club.id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False)  # noqa: E712
            )
        ).one()
        nouns = ("follow", "registered later")

    out_links = []
    by_source: dict = {}
    total_clicks = total_prev_clicks = total_conv = total_prev_conv = 0
    for link in links:
        n, u, pc = clicks.get(link.id, (0, 0, 0))
        c, s, pcv = conversions.get(link.id, (0, 0, 0))
        total_clicks += n
        total_prev_clicks += pc
        total_conv += c
        total_prev_conv += pcv
        agg = by_source.setdefault(link.utm_source, [0, 0])
        agg[0] += n
        agg[1] += c
        out_links.append({
            "id": link.id, "name": link.name, "code": link.code, "url": short_url(link), "destination": destination(link, getattr(club, "slug", None)),
            "utm_source": link.utm_source, "utm_medium": link.utm_medium, "utm_campaign": link.utm_campaign, "utm_content": link.utm_content,
            "clicks": n, "uniques": u, "conversions": c, "rate": _pct(c, n), "second": s, "created_at": link.created_at.isoformat(),
        })
    sources = sorted(
        ({"source": src, "clicks": n, "conversions": c, "rate": _pct(c, n)} for src, (n, c) in by_source.items()),
        key=lambda r: (-r["conversions"], -r["clicks"], r["source"]),
    )
    best = sources[0] if sources and sources[0]["conversions"] > 0 else None
    rate_now, rate_before = _pct(total_conv, total_clicks), _pct(total_prev_conv, total_prev_clicks)
    return {
        "scope": "event" if event is not None else "club",
        "period_days": days,
        "conversion_noun": nouns[0],
        "second_noun": nouns[1],
        "links": out_links,
        "by_source": sources,
        "totals": {
            "clicks": total_clicks, "conversions": total_conv, "rate": rate_now,
            "share_of_all": _pct(total_conv, int(all_now or 0)), "best_source": best["source"] if best else None, "best_conversions": best["conversions"] if best else 0,
        },
        "deltas": {
            "clicks": _delta(total_clicks, total_prev_clicks), "conversions": _delta(total_conv, total_prev_conv),
            "rate_pts": round(rate_now - rate_before, 1) if total_prev_clicks else None,
        },
        "untracked": {"conversions": max(0, int(all_now or 0) - total_conv), "total": int(all_now or 0)},
        "generated_at": now.isoformat(),
    }
