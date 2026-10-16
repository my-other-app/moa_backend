"""The club's site: its choices, the data the page is made from, the
rendering, and the publish that stores the page."""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.clubs import service as clubs
from app.api.clubs.models import Clubs, ClubSocials, ClubUsersLink
from app.api.closeout.models import EventStatus
from app.api.events.models import Events
from app.api.links import service as links
from app.api.sites.models import FONTS, SECTIONS, TEMPLATES, THEMES, ClubSites
from app.api.sites.render import image_url, render
from app.config import settings
from app.response import CustomHTTPException

TEMPLATE_META = [
    {"key": "launchpad", "name": "Launchpad", "blurb": "Big centred hero, event cards"},
    {"key": "chapter", "name": "Chapter", "blurb": "Editorial, left-aligned list"},
    {"key": "campus", "name": "Campus", "blurb": "Cover photo and gallery grid"},
    {"key": "arena", "name": "Arena", "blurb": "Countdown hero for competitions"},
]
FONT_META = [{"key": k, "name": v[0]} for k, v in FONTS.items()]
SECTION_META = [
    ("hero", "Hero"), ("upcoming", "Upcoming events"), ("about", "About and numbers"), ("past", "Past events"),
    ("team", "Team"), ("partners", "Partners"), ("follow", "Follow banner"), ("contact", "Contact footer"),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def public_url(club: Clubs) -> str:
    return settings.PUBLIC_CLUB_URL.format(slug=club.slug)


def _site_api_url(club: Clubs) -> str:
    return f"{settings.PUBLIC_API_URL}/api/v1/sites/{club.slug}"


async def get_or_create(session: AsyncSession, club: Clubs) -> ClubSites:
    s = await session.scalar(select(ClubSites).where(ClubSites.club_id == club.id))
    if s is None:
        s = ClubSites(club_id=club.id, sections={k: True for k in SECTIONS})
        session.add(s)
        await session.commit()
        await session.refresh(s)
    return s


async def patch(session: AsyncSession, s: ClubSites, data: dict) -> ClubSites:
    if data.get("template") and data["template"] not in TEMPLATES:
        raise CustomHTTPException(422, message="No such template")
    if data.get("theme") and data["theme"] not in THEMES:
        raise CustomHTTPException(422, message="No such theme")
    if data.get("font") and data["font"] not in FONTS:
        raise CustomHTTPException(422, message="No such font")
    if data.get("sections") is not None:
        data["sections"] = {k: bool(data["sections"].get(k, True)) for k in SECTIONS}
    for k, v in data.items():
        if k in ("headline", "subline") or v is not None:
            setattr(s, k, v)
    s.settings_at = _now()
    await session.commit()
    await session.refresh(s)
    return s


def _choices(s: ClubSites) -> dict:
    return dict(template=s.template, theme=s.theme, font=s.font, headline=s.headline, subline=s.subline, sections={k: bool((s.sections or {}).get(k, True)) for k in SECTIONS})


def _first_sentence(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().split("\n")[0]
    for stop in (". ", "! ", "? "):
        if stop in t:
            t = t.split(stop)[0] + stop.strip()
            break
    return t[:240]


async def site_data(session: AsyncSession, club: Clubs, choices: dict, ref_code: str | None = None) -> dict:
    """Everything the page is made from, with every image pointed at the site's own image route.

    `ref_code` is the website's own tracking link: it rides on every event
    button as `sref`, so a sign-up that starts on the site is credited to it
    (unless the visitor came through another of the club's links first)."""
    base = _site_api_url(club)
    now = _now()
    socials = await session.scalar(select(ClubSocials).where(ClubSocials.club_id == club.id, ClubSocials.is_deleted == False))  # noqa: E712
    followers = await session.scalar(select(func.count()).where(ClubUsersLink.club_id == club.id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False)) or 0  # noqa: E712
    shown = (EventStatus.published, EventStatus.live, EventStatus.closed, EventStatus.archived)
    events = list((await session.scalars(select(Events).where(Events.club_id == club.id, Events.is_deleted == False, Events.lifecycle.in_(shown)).order_by(Events.event_datetime.desc()))).all())  # noqa: E712

    def ev(e: Events, upcoming: bool) -> dict:
        fee = f"₹{int(e.reg_fee)}" if e.has_fee and e.reg_fee else "Free"
        where = "Online" if e.is_online else (e.location_name or None)
        url = f"{settings.PUBLIC_SITE_URL}/{e.id}" + (f"?sref={ref_code}" if ref_code else "")
        return dict(id=e.id, name=e.name, starts_at=e.event_datetime, where=where, fee=fee, poster=image_url(base, e.poster, "medium"), url=url, upcoming=upcoming)

    upcoming = [ev(e, True) for e in sorted((e for e in events if e.event_datetime >= now and e.lifecycle in (EventStatus.published, EventStatus.live)), key=lambda e: e.event_datetime)][:6]
    past = [ev(e, False) for e in events if e.event_datetime < now][:6]
    team = [
        dict(name=m.name, role=m.role, photo=image_url(base, m.photo, "medium"), links=[(k, _social_href(k, getattr(m, k))) for k in ("instagram", "linkedin", "x", "website") if getattr(m, k)])
        for m in await clubs.list_team(session, club.id)
    ]
    partners = [dict(name=p.name, role=p.role, logo=image_url(base, p.logo, "thumbnail")) for p in await clubs.list_partners(session, club.id)]
    social_links = [(k.capitalize() if k != "youtube" else "YouTube", _social_href(k, getattr(socials, k))) for k in ("instagram", "linkedin", "youtube", "website") if socials is not None and getattr(socials, k)]
    default_headline = club.tagline or club.name
    default_subline = _first_sentence(club.about) if club.tagline else (_first_sentence(club.about) or None)
    return dict(
        site=choices, site_url=public_url(club),
        club=dict(name=club.name, about=club.about, tagline=club.tagline, location_name=club.location_name, contact_email=club.contact_email, brand_primary=club.brand_primary, logo_url=image_url(base, club.logo, "medium"), cover_url=image_url(base, club.cover, "original")),
        default_headline=default_headline, default_subline=default_subline,
        upcoming=upcoming, past=past, team=team, partners=partners, socials=social_links,
        numbers=dict(followers=followers, events=sum(1 for e in events if e.event_datetime < now)),
        follow_url=f"{settings.PUBLIC_SITE_URL}/clubs/{club.slug}?follow=1", events_url=f"{settings.PUBLIC_SITE_URL}/clubs/{club.slug}#events",
        visit_url=f"{settings.PUBLIC_API_URL}/api/v1/sites/{club.slug}/visit",
    )


def _social_href(kind: str, raw: str) -> str:
    v = raw.strip().lstrip("@")
    if v.lower().startswith(("http://", "https://")):
        return v
    return {"instagram": f"https://instagram.com/{v}", "x": f"https://x.com/{v}", "linkedin": (f"https://{v}" if "/" in v else f"https://linkedin.com/in/{v}"), "youtube": (f"https://{v}" if "/" in v else f"https://youtube.com/@{v}")}.get(kind, f"https://{v}")


def available_sections(data: dict) -> dict:
    return dict(hero=True, upcoming=True, about=True, past=bool(data["past"]), team=bool(data["team"]), partners=bool(data["partners"]), follow=bool(data.get("follow_url")), contact=True)


async def preview_html(session: AsyncSession, club: Clubs, choices: dict) -> str:
    existing = await links.website_link(session, club.id)
    return render(await site_data(session, club, choices, existing.code if existing else None))


async def publish(session: AsyncSession, club: Clubs, s: ClubSites) -> ClubSites:
    # Publishing is what gives the site its own tracking link, from the first time.
    link = await links.ensure_website_link(session, club)
    s.html = render(await site_data(session, club, _choices(s), link.code))
    s.published_at = _now()
    await session.commit()
    await session.refresh(s)
    return s


async def out(session: AsyncSession, club: Clubs, s: ClubSites) -> dict:
    link = await links.website_link(session, club.id)
    if link is None and s.published_at:
        # Published before sites had a link: give it one now.
        link = await links.ensure_website_link(session, club)
        await session.commit()
    data = await site_data(session, club, _choices(s), link.code if link else None)
    tracking = dict(link_id=str(link.id), code=link.code, short_url=links.short_url(link)) if link else None
    stale = s.published_at is not None and s.settings_at is not None and s.settings_at > s.published_at
    return dict(**_choices(s), default_headline=data["default_headline"], default_subline=data["default_subline"], available=available_sections(data), published_at=s.published_at, stale=stale, url=public_url(club), slug=club.slug, templates=TEMPLATE_META, fonts=FONT_META, tracking=tracking)


async def published_html(session: AsyncSession, slug: str) -> str | None:
    club = await session.scalar(select(Clubs).options(selectinload(Clubs.interests)).where(Clubs.slug == slug, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        return None
    s = await session.scalar(select(ClubSites).where(ClubSites.club_id == club.id))
    return s.html if s is not None and s.html else None


async def republish_for_club(session: AsyncSession, club_id: int) -> None:
    """Called when the club's events change: a published site is made again so it never goes stale. Never raises."""
    try:
        s = await session.scalar(select(ClubSites).where(ClubSites.club_id == club_id))
        if s is None or not s.published_at:
            return
        club = await session.scalar(select(Clubs).where(Clubs.id == club_id))
        if club is None:
            return
        await publish(session, club, s)
    except Exception:  # noqa: BLE001 — a site that fails to rebuild must never block an event save
        await session.rollback()


async def republish_for_event(session: AsyncSession, event_id: int) -> None:
    """The same, from an event that just changed."""
    try:
        club_id = await session.scalar(select(Events.club_id).where(Events.id == event_id))
    except Exception:  # noqa: BLE001
        await session.rollback()
        return
    if club_id is not None:
        await republish_for_club(session, club_id)
