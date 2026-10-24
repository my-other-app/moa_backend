"""Tracking links: made, opened, and reconciled with the sign-ups they brought."""
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import joinedload

from app.api.club_domains.lookup import active_host
from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.events.models import EventRegistrationsLink, Events
from app.api.discover import visitors
from app.api.links.models import TrackingClicks, TrackingLinks
from app.config import settings
from app.core import public_urls
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


def destination(link: TrackingLinks, club_slug: Optional[str] = None, event_slug: Optional[str] = None, site_host: Optional[str] = None) -> str:
    """Where the link lands, tagged: the event's page or the club's. Every caller
    passes the event's slug; the page answers to the bare id too, should one not.

    The website's own link lands on the website itself at the club's own
    domain while that is active (`site_host`, from club_domains.lookup):
    the site counts the visit there with `ref` and `via=l`, as it does on
    any address."""
    if link.event_id:
        base = public_urls.event_url(event_slug or link.event_id)
    elif site_host and link.kind == WEBSITE:
        base = f"https://{site_host}/"
    else:
        base = settings.PUBLIC_CLUB_URL.format(slug=club_slug or link.club_id)
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
        select(TrackingLinks).options(joinedload(TrackingLinks.club), joinedload(TrackingLinks.event)).where(TrackingLinks.code == code.strip().lower(), TrackingLinks.is_deleted == False)  # noqa: E712
    )


def visitor_of(request) -> Optional[str]:
    """Today's salted hash of the opener's address (as the proxy saw it, not
    the first X-Forwarded-For entry, which the client writes) and browser
    (discover.visitors): a person opening a link twice in a day counts once
    among the uniques, and no stored hash can be turned back into an address.
    Uniques over a period are therefore visitor-days."""
    return visitors.anonymous(request, length=64)


# Link previews and crawlers: they open the link to draw a card, not because a person did.
_BOT = re.compile(
    r"facebookexternalhit|facebot|whatsapp|slackbot|slack-imgproxy|linkedinbot|twitterbot|discordbot|telegrambot|"
    r"skypeuripreview|googlebot|google-inspectiontool|bingbot|applebot|duckduckbot|yandex|baiduspider|embedly|"
    r"pinterestbot|redditbot|iframely|vkshare|bitlybot|[a-z]+bot/|\b(?:bot|crawler|spider|preview)\b|"
    r"headlesschrome|python-requests|curl/|wget/|go-http-client|axios/",
    re.I,
)


def is_person(request) -> bool:
    """An opening worth a click: a GET from something that is not a link
    preview or a crawler (a HEAD, or a WhatsApp, Slack, LinkedIn, X, Discord,
    Telegram or search engine fetch, is not)."""
    if request.method != "GET":
        return False
    ua = request.headers.get("user-agent") or ""
    return bool(ua) and not _BOT.search(ua)


async def record_click(session, link: TrackingLinks, request) -> None:
    """Count an opening, unless it was a preview, a crawler or a HEAD."""
    if not is_person(request):
        return
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
    if link.kind == WEBSITE:
        raise CustomHTTPException(409, message="This is your website's own link; it stays while the site is up.")
    link.is_deleted = True
    await session.commit()


# ─── The club website's own link ──────────────────────────────────────────

WEBSITE = "website"
# A visitor who reloads or comes back within this counts once.
VISIT_GAP = timedelta(minutes=30)


async def website_link(session, club_id: int) -> Optional[TrackingLinks]:
    return await session.scalar(
        select(TrackingLinks).where(TrackingLinks.club_id == club_id, TrackingLinks.kind == WEBSITE, TrackingLinks.is_deleted == False)  # noqa: E712
    )


async def ensure_website_link(session, club) -> TrackingLinks:
    """The club website's link, made the first time it is needed. Flushed, not
    committed: the caller's commit keeps it."""
    link = await website_link(session, club.id)
    if link is not None:
        return link
    for _ in range(5):
        code = new_code()
        if await session.scalar(select(func.count()).where(TrackingLinks.code == code)) == 0:
            link = TrackingLinks(
                club_id=club.id, event_id=None, kind=WEBSITE, name="Your website", code=code,
                utm_source="website", utm_medium="club_site",
            )
            session.add(link)
            await session.flush()
            return link
    raise CustomHTTPException(500, message="Could not mint a link code; try again.")


async def site_published(session, club_id: int) -> bool:
    from app.api.sites.models import ClubSites

    return bool(await session.scalar(select(ClubSites.published_at).where(ClubSites.club_id == club_id)))


def _clean_referrer(value) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return value.strip()[:300]


async def record_site_visit(session, slug: str, request, *, ref=None, via=None, referrer=None) -> Optional[str]:
    """A visit to a club's published website. It came through one of the
    club's own links, or it is the website's own: what reached the site some
    other way (a search, a bio, a link someone pasted). Returns what it was
    counted as, or None when it was not counted."""
    club = await session.scalar(select(Clubs).where(Clubs.slug == slug, Clubs.is_deleted == False))  # noqa: E712
    if club is None or not await site_published(session, club.id):
        return None
    link = await by_code(session, ref) if isinstance(ref, str) and ref else None
    if link is not None and link.club_id == club.id and link.event_id is None:
        if via == "l":
            return None  # the short link already counted this opening on its way here
        target = link  # the full link, opened without the short one
    else:
        target = await ensure_website_link(session, club)
    visitor = visitor_of(request)
    if visitor:
        recent = await session.scalar(
            select(func.count()).where(
                TrackingClicks.link_id == target.id, TrackingClicks.visitor == visitor,
                TrackingClicks.created_at >= datetime.now(timezone.utc) - VISIT_GAP,
            )
        )
        if recent:
            await session.commit()  # keep a just-made website link
            return None
    session.add(TrackingClicks(
        link_id=target.id, visitor=visitor, referrer=_clean_referrer(referrer),
        user_agent=(request.headers.get("user-agent") or None) and request.headers.get("user-agent")[:200],
    ))
    await session.commit()
    return target.kind


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
    site_host = await active_host(session, club.id) if event is None else None

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
            "id": link.id, "name": link.name, "code": link.code, "url": short_url(link), "destination": destination(link, getattr(club, "slug", None), getattr(event, "slug", None), site_host),
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
