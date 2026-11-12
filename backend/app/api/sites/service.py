"""The club's site: its choices, the data the page is made from, the
rendering, and the publish that stores the page.

`site_data` is the render input (feature-register/site-templates-contract.md,
"Changes"): one bounded query per list -- the next six events, the last six,
the counts, the team, the partners, the speakers, the photo library, the
topics -- never one per row.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, and_, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs import service as clubs
from app.api.clubs.models import ClubInterestsLink, Clubs, ClubSocials
from app.api.closeout.models import EventStatus
from app.api.events.models import EventCategories, Events, EventSpeakers
from app.api.interests.models import Interests
from app.api.links import service as links
from app.api.sites import layout as layouts
from app.api.sites import registry
from app.api.sites.models import DEFAULT_TEMPLATE, IMAGE_MB, MAX_IMAGES, ClubSiteImages, ClubSites
from app.api.sites.render import IST, image_srcset, image_url, render, titles_for
from app.api.speakers.models import ACTIVE, HIDDEN, SpeakerProfiles
from app.config import settings
from app.core import public_urls

#: Events on the page: those anyone may see.
SHOWN = (EventStatus.published, EventStatus.live, EventStatus.closed, EventStatus.archived)
EVENTS_ON_PAGE = 6
SPEAKERS_ON_PAGE = 8
TOPICS_ON_PAGE = 12


def _now() -> datetime:
    return datetime.now(timezone.utc)


# One DNS label: what can stand before .myotherapp.in.
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def public_url(club: Clubs) -> str:
    """The site's address: the club's own subdomain, or the events-site page
    when the slug cannot be a subdomain (longer than 63, odd characters)."""
    if club.slug and _LABEL.match(club.slug):
        return settings.PUBLIC_CLUB_SITE_URL.format(slug=club.slug)
    return settings.PUBLIC_CLUB_URL.format(slug=club.slug)


async def site_address(session: AsyncSession, club: Clubs) -> str:
    """Where the site is seen: the club's own domain while it is active
    (app/api/club_domains), else `public_url`. The page's canonical address,
    og:url and the dashboard's "Open site" all follow it."""
    from app.api.club_domains.lookup import active_host

    host = await active_host(session, club.id)
    return f"https://{host}" if host else public_url(club)


def _site_api_url(club: Clubs) -> str:
    return f"{settings.PUBLIC_API_URL}/api/v1/sites/{club.slug}"


async def get_or_create(session: AsyncSession, club: Clubs) -> ClubSites:
    s = await session.scalar(select(ClubSites).where(ClubSites.club_id == club.id))
    if s is None:
        s = ClubSites(club_id=club.id, template=registry.default_key() or DEFAULT_TEMPLATE, layout={})
        session.add(s)
        await session.commit()
        await session.refresh(s)
    return s


def choices(s: ClubSites) -> dict:
    """What the club chose, as stored (the layout normalised when it is read)."""
    return dict(template=s.template, headline=s.headline, subline=s.subline, layout=dict(s.layout or {}))


async def library_ids(session: AsyncSession, club_id: int) -> set[int]:
    return set((await session.scalars(select(ClubSiteImages.id).where(ClubSiteImages.club_id == club_id))).all())


async def apply_choices(session: AsyncSession, club_id: int, current: dict, body: dict) -> tuple[dict, dict]:
    """`body` (a PATCH or a preview: template, layout, headline, subline,
    only what was sent) over the `current` choices: (the new choices, what
    the layout check adjusted). Validates; saves nothing."""
    new = dict(current)
    old_pkg = registry.get(current.get("template"))
    if body.get("template"):
        pkg = registry.get(body["template"])
        if pkg is None:
            from app.response import CustomHTTPException

            raise CustomHTTPException(422, message="No such template", error_code="SITE_TEMPLATE_UNKNOWN")
        if pkg.key != current.get("template"):
            new["layout"] = layouts.switch(current.get("layout"), old_pkg, pkg)
        new["template"] = pkg.key
    pkg = registry.resolve(new["template"])
    adjusted: dict = {}
    if body.get("layout") is not None:
        new["layout"], adjusted = layouts.apply(new.get("layout"), body["layout"], pkg, await library_ids(session, club_id))
    for k in ("headline", "subline"):
        if k in body:
            new[k] = body[k]
    return new, adjusted


async def patch(session: AsyncSession, club: Clubs, s: ClubSites, body: dict) -> tuple[ClubSites, dict]:
    new, adjusted = await apply_choices(session, club.id, choices(s), body)
    s.template = new["template"]
    s.headline = new["headline"]
    s.subline = new["subline"]
    s.layout = new["layout"]
    s.settings_at = _now()
    await session.commit()
    await session.refresh(s)
    return s, adjusted


# ─── The render input ────────────────────────────────────────────────────


def _first_sentence(text: str | None, limit: int = 240) -> str | None:
    if not text:
        return None
    t = text.strip().split("\n")[0]
    for stop in (". ", "! ", "? "):
        if stop in t:
            t = t.split(stop)[0] + stop.strip()
            break
    return t[:limit] or None


def initials(name: str | None) -> str:
    parts = [p for p in (name or "").split() if p and p[0].isalnum()]
    return "".join(p[0] for p in parts[:2]).upper() or "C"


def safe_href(url: str | None) -> str | None:
    """An address the page may carry: https, mailto or tel (http made
    https); None for anything else -- javascript:, data:, a bare word."""
    u = (url or "").strip()
    low = u.lower()
    if low.startswith("https://") and len(u) > 8:
        return u
    if low.startswith("http://") and len(u) > 7:
        return "https://" + u[7:]
    if low.startswith(("mailto:", "tel:")) and len(u) > 7:
        return u
    return None


def _social_href(kind: str, raw: str) -> str:
    v = raw.strip().lstrip("@")
    if v.lower().startswith(("http://", "https://")):
        return v
    return {"instagram": f"https://instagram.com/{v}", "x": f"https://x.com/{v}", "linkedin": (f"https://{v}" if "/" in v else f"https://linkedin.com/in/{v}"), "youtube": (f"https://{v}" if "/" in v else f"https://youtube.com/@{v}")}.get(kind, f"https://{v}")


SOCIAL_LABELS = {"instagram": "Instagram", "linkedin": "LinkedIn", "youtube": "YouTube", "website": "Website", "x": "X"}

#: The shape each kind of picture is asked for (storage/shapes.py), for the
#: img's width and height when the stored file's own size is not kept.
SHAPE = {"poster": (1080, 1350), "cover": (1500, 500), "logo": (500, 500), "person": (400, 400), "partner": (400, 400), "speaker": (200, 200)}


def _variant_for(image, want: int) -> str:
    """The smallest of the picture's own sizes at least `want` px wide, else its largest; the original when it has none."""
    widths = getattr(image, "widths", None) or {}
    if not widths:
        return "original"
    wide = sorted(widths.items(), key=lambda kv: kv[1])
    return next((name for name, w in wide if w >= want), wide[-1][0])


def picture(base: str, image, *, alt: str = "", size: tuple[int, int], want: int) -> dict | None:
    """An image column's picture as the templates take it: {url, srcset, alt, w, h}."""
    if image is None:
        return None
    url = image_url(base, image, _variant_for(image, want))
    if not url:
        return None
    return {"url": url, "srcset": image_srcset(image), "alt": alt or "", "w": int(size[0]), "h": int(size[1])}


def library_picture(base: str, row: ClubSiteImages, want: int = 1440) -> dict | None:
    return picture(base, row.image, alt=row.alt or "", size=(row.width, row.height), want=want)


def resolve_slots(pkg: registry.Package, slots: dict, library: list[ClubSiteImages]) -> dict[str, ClubSiteImages]:
    """Which library photo each of `pkg`'s slots shows: the club's choice,
    and the gallery's gaps (photos.N) filled from the library in order with
    photos no other slot uses."""
    by_id = {r.id: r for r in library}
    chosen = {k: by_id[v] for k, v in slots.items() if k in pkg.slot_keys and v in by_id}
    used = {r.id for r in chosen.values()}
    rest = iter([r for r in library if r.id not in used])
    out = {k: r for k, r in chosen.items() if not k.startswith("photos.")}
    for s in pkg.photo_slots:
        r = chosen.get(s.key) or next(rest, None)
        if r is not None:
            out[s.key] = r
    return out


def _title_org(designation: str | None) -> tuple[str | None, str | None]:
    d = " ".join((designation or "").split())
    for sep in (" · ", " at ", " @ ", ", ", " | "):
        if sep in d:
            t, o = d.split(sep, 1)
            return t.strip() or None, o.strip() or None
    return d or None, None


def _event(base: str, e: Events, kind: str | None, ref_code: str | None, fallback: dict | None) -> dict:
    start = e.event_datetime.astimezone(IST)
    end = start + timedelta(hours=float(e.duration)) if e.duration and float(e.duration) > 0 else None
    poster = picture(base, e.poster, alt=e.name, size=SHAPE["poster"], want=1080)
    seats = max(0, e.max_participants - (e.going_count or 0)) if e.max_participants else None
    return {
        "id": e.id, "name": e.name,
        "url": public_urls.event_url(e.slug) + (f"?sref={ref_code}" if ref_code else ""),
        "starts_at": start.isoformat(), "ends_at": end.isoformat() if end else None,
        "day": str(start.day), "day2": f"{start.day:02d}", "month": start.strftime("%b"), "month_long": start.strftime("%B"),
        "year": str(start.year), "weekday": start.strftime("%a"), "weekday_long": start.strftime("%A"),
        "time": start.strftime("%I:%M %p").lstrip("0"), "end_time": end.strftime("%I:%M %p").lstrip("0") if end else None,
        "date_label": f"{start.strftime('%a')} {start.day} {start.strftime('%b')} {start.year}",
        "where": "Online" if e.is_online else (e.location_name or None), "is_online": bool(e.is_online),
        "fee": f"₹{int(e.reg_fee)}" if e.has_fee and e.reg_fee else "Free",
        "kind": kind, "blurb": _first_sentence(e.about, 160), "seats_left": seats, "going": int(e.going_count or 0),
        "poster": poster or fallback, "poster_is_fallback": poster is None and fallback is not None,
    }


async def _speakers(session: AsyncSession, base: str, shown) -> list[dict]:
    """The speakers of the club's events, most recent first, each person
    once (by their profile, else their name), never one whose profile was
    hidden or removed on their request. One query."""
    person = func.coalesce(literal("p") + cast(EventSpeakers.speaker_profile_id, String), literal("n") + func.lower(EventSpeakers.name))
    latest = (
        select(EventSpeakers.id.label("sid"), Events.event_datetime.label("at"))
        .join(Events, Events.id == EventSpeakers.event_id)
        .outerjoin(SpeakerProfiles, SpeakerProfiles.id == EventSpeakers.speaker_profile_id)
        .where(*shown, EventSpeakers.is_deleted == False, or_(SpeakerProfiles.id.is_(None), and_(SpeakerProfiles.status != HIDDEN, SpeakerProfiles.removed_at.is_(None))))  # noqa: E712
        .distinct(person).order_by(person, Events.event_datetime.desc(), EventSpeakers.display_order, EventSpeakers.id)
        .subquery()
    )
    rows = (await session.execute(
        select(EventSpeakers, SpeakerProfiles)
        .join(latest, latest.c.sid == EventSpeakers.id)
        .outerjoin(SpeakerProfiles, SpeakerProfiles.id == EventSpeakers.speaker_profile_id)
        .order_by(latest.c.at.desc(), EventSpeakers.id)
        .limit(SPEAKERS_ON_PAGE)
    )).all()
    out = []
    for sp, profile in rows:
        title, org = _title_org(sp.designation)
        url = public_urls.site_url(f"/speakers/{profile.slug}") if profile is not None and profile.status == ACTIVE else safe_href(sp.link)
        photo = sp.photo or (profile.photo if profile is not None else None)
        out.append({"name": sp.name, "title": title, "org": org, "photo": picture(base, photo, alt=sp.name, size=SHAPE["speaker"], want=200), "url": url})
    return out


async def site_data(session: AsyncSession, club: Clubs, choices: dict, ref_code: str | None = None) -> dict:
    """Everything the page is made from, with every image on the media CDN
    (each size in a srcset) when it is on, else pointed at the site's own
    image route.

    `ref_code` is the website's own tracking link: it rides on every event
    button as `sref`, so a sign-up that starts on the site is credited to it
    (unless the visitor came through another of the club's links first)."""
    from app.api.cohosts import access as cohosts

    pkg = registry.resolve(choices.get("template"))
    lay = layouts.normalise(choices.get("layout"), pkg)
    base = _site_api_url(club)
    now = _now()
    shown = (cohosts.hosted_or_cohosted(club.id), Events.is_deleted == False, Events.lifecycle.in_(SHOWN))  # noqa: E712
    ahead = (Events.event_datetime >= now, Events.lifecycle.in_((EventStatus.published, EventStatus.live)))

    library = list((await session.scalars(select(ClubSiteImages).where(ClubSiteImages.club_id == club.id).order_by(ClubSiteImages.position, ClubSiteImages.id))).all())
    placed = resolve_slots(pkg, lay["slots"], library)
    sizes = {s.key: s.size for s in pkg.slots}
    images: dict = {k: None for k in ("hero.cover", "hero.image", "about.image", "join.image", "event.fallback")}
    for k, row in placed.items():
        if not k.startswith("photos."):
            images[k] = library_picture(base, row, want=2 * sizes[k][0])
    images["photos"] = [library_picture(base, placed[s.key], want=2 * s.size[0]) for s in pkg.photo_slots if s.key in placed]
    cover = picture(base, club.cover, alt=club.name, size=SHAPE["cover"], want=3000)
    if images["hero.cover"] is None:
        images["hero.cover"] = cover
    fallback = images["event.fallback"]

    def events(rows) -> list[dict]:
        return [_event(base, e, kind, ref_code, fallback) for e, kind in rows]

    with_kind = select(Events, EventCategories.name).outerjoin(EventCategories, EventCategories.id == Events.category_id)
    upcoming = events((await session.execute(with_kind.where(*shown, *ahead).order_by(Events.event_datetime, Events.id).limit(EVENTS_ON_PAGE))).all())
    past = events((await session.execute(with_kind.where(*shown, Events.event_datetime < now).order_by(Events.event_datetime.desc(), Events.id.desc()).limit(EVENTS_ON_PAGE))).all())
    held, coming = (await session.execute(
        select(func.count().filter(Events.event_datetime < now), func.count().filter(and_(*ahead))).where(*shown)
    )).one()

    socials_row = await session.scalar(select(ClubSocials).where(ClubSocials.club_id == club.id, ClubSocials.is_deleted == False))  # noqa: E712
    socials = []
    for k in ("instagram", "linkedin", "youtube", "website"):
        raw = getattr(socials_row, k, None) if socials_row is not None else None
        href = safe_href(_social_href(k, raw)) if raw and raw.strip() else None
        if href:
            socials.append({"kind": k, "label": SOCIAL_LABELS[k], "href": href})

    team = []
    for m in await clubs.list_team(session, club.id):
        member_links = []
        for k in ("instagram", "linkedin", "x", "website"):
            raw = getattr(m, k)
            href = safe_href(_social_href(k, raw)) if raw and raw.strip() else None
            if href:
                member_links.append({"kind": k, "label": SOCIAL_LABELS[k], "href": href})
        team.append({"name": m.name, "role": m.role, "initials": initials(m.name), "photo": picture(base, m.photo, alt=m.name, size=SHAPE["person"], want=400), "links": member_links})
    sponsors = [
        {"name": p.name, "role": p.role, "initials": initials(p.name), "logo": picture(base, p.logo, alt=p.name, size=SHAPE["partner"], want=400)}
        for p in await clubs.list_partners(session, club.id)
    ]
    topics = list((await session.scalars(
        select(Interests.name).join(ClubInterestsLink, ClubInterestsLink.interest_id == Interests.id)
        .where(ClubInterestsLink.club_id == club.id, ClubInterestsLink.is_deleted == False, Interests.is_deleted == False)  # noqa: E712
        .order_by(Interests.sort_order, Interests.name).limit(TOPICS_ON_PAGE)
    )).all())
    speakers = await _speakers(session, base, shown)

    site_url = await site_address(session, club)
    default_headline = club.tagline or club.name
    default_subline = _first_sentence(club.about)
    headline = choices.get("headline") or default_headline
    subline = choices.get("subline") or default_subline
    logo = picture(base, club.logo, alt=club.name, size=SHAPE["logo"], want=500)
    email = (club.contact_email or "").strip()
    follow_url = f"{settings.PUBLIC_SITE_URL}/clubs/{club.slug}?follow=1"
    return dict(
        site=dict(template=pkg.key, headline=choices.get("headline"), subline=choices.get("subline"), accent=lay["accent"], title_overrides=lay["titles"], layout=lay),
        page=dict(title=club.name if headline == club.name else f"{club.name} · {headline}", description=subline or f"{club.name} on MyOtherApp.", url=site_url),
        headline=headline, subline=subline,
        club=dict(
            name=club.name, initials=initials(club.name), tagline=club.tagline, about=club.about, about_short=_first_sentence(club.about),
            location_name=club.location_name, contact_email=email or None, slug=club.slug,
            logo=logo, logo_url=logo["url"] if logo else None, logo_srcset=logo["srcset"] if logo else None, cover=cover,
            founded_year=None, topics=topics, brand_colour=layouts.hex_colour(club.brand_primary),
        ),
        images=images,
        stats=dict(followers=int(club.follower_count or 0), events_held=int(held or 0), upcoming=int(coming or 0), members=None),
        upcoming=upcoming, past=past, speakers=speakers, team=team, sponsors=sponsors, socials=socials,
        links=dict(
            follow_url=follow_url, events_url=f"{settings.PUBLIC_SITE_URL}/clubs/{club.slug}#events", join_url=follow_url,
            contact_mailto=f"mailto:{email}" if email and "@" in email and not re.search(r"[\s<>\"']", email) else None, site_url=site_url,
        ),
        # The platform's own, not the templates': the beacon's address and the console's defaults.
        site_url=site_url, visit_url=f"{settings.PUBLIC_API_URL}/api/v1/sites/{club.slug}/visit",
        default_headline=default_headline, default_subline=default_subline,
    )


def available(data: dict) -> dict:
    """Which sections have anything to show, so the console can say "no partners yet"."""
    club = data["club"]
    return dict(
        nav=True, hero=True, profile=True, footer=True, join=True, stats=True,
        about=bool(club.get("about")), expertise=bool(club.get("topics")),
        upcoming=bool(data["upcoming"]), past=bool(data["past"]), photos=bool(data["images"]["photos"]),
        speakers=bool(data["speakers"]), team=bool(data["team"]), sponsors=bool(data["sponsors"]),
    )


async def preview_html(session: AsyncSession, club: Clubs, chosen: dict) -> str:
    existing = await links.website_link(session, club.id)
    return render(await site_data(session, club, chosen, existing.code if existing else None))


async def publish(session: AsyncSession, club: Clubs, s: ClubSites) -> ClubSites:
    # Stamped with when the reading began, not when it ended: a change made
    # while the page was being made is then newer than it, and the sites
    # sweep (app/api/sites/sweep.py) makes the page again.
    started = _now()
    # Publishing is what gives the site its own tracking link, from the first time.
    link = await links.ensure_website_link(session, club)
    s.html = render(await site_data(session, club, choices(s), link.code))
    s.published_at = started
    await session.commit()
    await session.refresh(s)
    return s


# ─── The console's view ──────────────────────────────────────────────────


def template_meta(pkg: registry.Package) -> dict:
    def thumb(kind: str):
        from app.api.sites.router import STATIC_PREFIX

        p = pkg.thumb(kind)
        if p is None:
            return None
        name = f"{pkg.key}-mobile.png" if kind == "mobile" else f"{pkg.key}.png"
        return f"{settings.PUBLIC_API_URL}{STATIC_PREFIX}/{name}?v={registry.file_version(p)}"

    return {
        "key": pkg.key, "name": pkg.name, "blurb": pkg.blurb, "layout": pkg.layout,
        "sections": [{"key": s.key, "label": s.label, "default_title": s.default_title, "hideable": s.hideable} for s in pkg.sections],
        "fixed": {"top": list(pkg.fixed_top), "side": list(pkg.fixed_side), "bottom": list(pkg.fixed_bottom)},
        "slots": [{"key": s.key, "label": s.label, "size": list(s.size), "section": s.section} for s in pkg.slots],
        "accent": {"default": pkg.accent_default},
        "thumb_url": thumb("desktop"), "thumb_mobile_url": thumb("mobile"),
    }


def templates_meta() -> list[dict]:
    return [template_meta(p) for p in registry.packages().values()]


def image_out(base: str, row: ClubSiteImages, used_in: list[str]) -> dict:
    pic = library_picture(base, row, want=960) or {}
    return {
        "id": row.id, "url": pic.get("url"), "srcset": pic.get("srcset"), "alt": row.alt, "width": row.width, "height": row.height,
        "position": row.position, "used_in": used_in,
    }


async def library_view(session: AsyncSession, club: Clubs, s: ClubSites) -> list[dict]:
    """The library as the console lists it, each photo with the current
    template's slots it shows in (chosen, or filling the gallery)."""
    pkg = registry.resolve(s.template)
    lay = layouts.normalise(s.layout, pkg)
    rows = list((await session.scalars(select(ClubSiteImages).where(ClubSiteImages.club_id == club.id).order_by(ClubSiteImages.position, ClubSiteImages.id))).all())
    used: dict[int, list[str]] = {}
    for slot, r in resolve_slots(pkg, lay["slots"], rows).items():
        used.setdefault(r.id, []).append(slot)
    base = _site_api_url(club)
    return [image_out(base, r, used.get(r.id, [])) for r in rows]


async def out(session: AsyncSession, club: Clubs, s: ClubSites, adjusted: dict | None = None) -> dict:
    link = await links.website_link(session, club.id)
    if link is None and s.published_at:
        # Published before sites had a link: give it one now.
        link = await links.ensure_website_link(session, club)
        await session.commit()
    chosen = choices(s)
    pkg = registry.resolve(s.template)
    data = await site_data(session, club, chosen, link.code if link else None)
    lay = data["site"]["layout"]
    tracking = dict(link_id=str(link.id), code=link.code, short_url=links.short_url(link)) if link else None
    images_changed = bool(s.published_at) and bool(await session.scalar(
        select(ClubSiteImages.id).where(ClubSiteImages.club_id == club.id, ClubSiteImages.updated_at > s.published_at).limit(1)
    ))
    stale = s.published_at is not None and ((s.settings_at is not None and s.settings_at > s.published_at) or images_changed)
    return dict(
        template=pkg.key, headline=s.headline, subline=s.subline,
        default_headline=data["default_headline"], default_subline=data["default_subline"],
        layout=lay, titles=titles_for(pkg, lay["titles"]),
        sections={k: k not in lay["hidden"] for k in lay["order"]},
        available=available(data), club_colour=data["club"]["brand_colour"],
        images=await library_view(session, club, s),
        limits={"images": MAX_IMAGES, "image_mb": IMAGE_MB},
        published_at=s.published_at, stale=stale, url=data["site_url"], slug=club.slug,
        templates=templates_meta(), tracking=tracking, adjusted=adjusted or None,
    )


# ─── The public page ─────────────────────────────────────────────────────


async def published_html(session: AsyncSession, slug: str) -> str | None:
    """The stored page (`published_page` without the slug)."""
    found = await published_page(session, slug)
    return found[0] if found else None


async def published_page(session: AsyncSession, slug: str) -> tuple[str, str] | None:
    """(the stored page, the club's current slug), in one query: no club row
    loaded (it loaded the club and all its interests, which the page never
    used), only the HTML. `slug` may be one the club had once (app/api/slugs);
    the current one differs then, and the caller says so."""
    from app.api.slugs.service import club_slug_matches

    row = (await session.execute(
        select(ClubSites.html, Clubs.slug).join(Clubs, Clubs.id == ClubSites.club_id).where(club_slug_matches(slug), Clubs.is_deleted == False)  # noqa: E712
    )).first()
    if row is None or not row[0]:
        return None
    return row[0], row[1]


async def republish_for_club(session: AsyncSession, club_id: int) -> bool:
    """Called when the club's events change: a published site is made again so
    it never goes stale. Never raises; whether a page was made."""
    try:
        s = await session.scalar(select(ClubSites).where(ClubSites.club_id == club_id))
        if s is None or not s.published_at:
            return False
        club = await session.scalar(select(Clubs).where(Clubs.id == club_id))
        if club is None:
            return False
        await publish(session, club, s)
        return True
    except Exception:  # noqa: BLE001 — a site that fails to rebuild must never block an event save
        await session.rollback()
        return False


async def republish_for_event(session: AsyncSession, event_id: int, also: tuple = ()) -> None:
    """The same, from an event that just changed: the host's site, and each
    co-host's (their sites list it too, app/api/cohosts), and `also` (a club
    that just stopped co-hosting it)."""
    from app.api.cohosts import access as cohosts

    try:
        club_id = await session.scalar(select(Events.club_id).where(Events.id == event_id))
        club_ids = [club_id, *await cohosts.cohost_club_ids(session, event_id), *also]
    except Exception:  # noqa: BLE001
        await session.rollback()
        return
    for cid in dict.fromkeys(c for c in club_ids if c is not None):
        await republish_for_club(session, cid)


def on_page(html: str | None, row: ClubSiteImages) -> bool:
    """Whether the published page shows this library photo: its stored
    name (a fresh v-<hex> per upload, media.versioned_name) is in the page."""
    key = (getattr(row.image, "variations", None) or {}).get("original") or ""
    stem = key.rpartition("/")[2].rpartition(".")[0]
    return bool(html and stem and stem in html)
