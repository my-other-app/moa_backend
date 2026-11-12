"""Finding a club or an event by a slug it has now or had once, and an admin
changing one.

Every lookup by slug goes through `club_slug_matches` / `event_slug_matches`
(a WHERE condition, so the lookup stays one statement) or through
`resolve_club_slug` / `resolve_event_slug` (the id). Either finds the live
slug first and, failing that, the alias: the answer is always the canonical
row, whose `slug` is the current one, so a client that asked by an old one
sees the difference and redirects (the website 301s, the club-sites Worker
301s the old subdomain, the app just opens the event).
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.api.clubs.models import Clubs
from app.api.events.models import Events
from app.api.slugs.models import CLUB, EVENT, SlugAliases
from app.config import settings
from app.core import public_urls
from app.response import CustomHTTPException

MIN_LENGTH = 3
MAX_LENGTH = 60
#: Lower-case letters, digits and single inner hyphens; starts and ends with a letter or digit.
SHAPE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9]))*$")

#: Names a club's slug can never be: a club's slug is also its website's
#: subdomain, and these are ours. The club-sites Worker's own list
#: (moa_mobileapp infra/cloudflare/club-sites/routing.js RESERVED, and its
#: fallback origin `sites`), plus the platform's hosts.
RESERVED_CLUB_SLUGS = frozenset({
    # routing.js RESERVED
    "www", "api", "admin", "admin-next", "club", "clubs", "clubs-next", "event", "events", "r",
    "club-dashboard", "main-portal", "admin-dashboard",
    "mail", "email", "send", "rsend", "smtp", "imap", "pop", "mx", "app", "apps", "staging",
    "api-staging", "dev", "status", "help", "support", "docs", "blog", "static", "cdn", "assets",
    "m", "go", "link", "links", "auth", "login", "account", "accounts", "dashboard",
    "media",
    # routing.js FALLBACK
    "sites",
})

_TABLES = {CLUB: Clubs, EVENT: Events}
_NOUN = {CLUB: "Club", EVENT: "Event"}


# ─── Lookups ─────────────────────────────────────────────────────────────


def _matches(kind: str, ref: str):
    """`Model.id` is the row whose slug is `ref` now, else the row an alias
    `ref` names. One id at most: two scalar subqueries, the live one first
    (a live slug and an alias of the same kind are never the same string;
    if one ever were, the live one wins)."""
    model = _TABLES[kind]
    live = aliased(model)
    return model.id == func.coalesce(
        select(live.id).where(live.slug == ref).scalar_subquery(),
        select(SlugAliases.target_id).where(SlugAliases.kind == kind, SlugAliases.slug == ref).scalar_subquery(),
    )


def club_slug_matches(ref: str):
    """WHERE condition: the club `ref` is the slug of, now or once."""
    return _matches(CLUB, ref)


def event_slug_matches(ref: str):
    """WHERE condition: the event `ref` is the slug of, now or once."""
    return _matches(EVENT, ref)


async def resolve_club_slug(session, ref: str) -> Optional[int]:
    """The id of the club `ref` is the slug of, now or once (deleted or not); None for none."""
    return await session.scalar(select(Clubs.id).where(club_slug_matches(ref)))


async def resolve_event_slug(session, ref: str) -> Optional[int]:
    """The id of the event `ref` is the slug of, now or once (deleted or not); None for none."""
    return await session.scalar(select(Events.id).where(event_slug_matches(ref)))


async def aliases_of(session, kind: str, target_id: int) -> list[str]:
    """The old slugs of one club or event, the most recent first."""
    rows = await session.scalars(
        select(SlugAliases.slug)
        .where(SlugAliases.kind == kind, SlugAliases.target_id == target_id)
        .order_by(SlugAliases.created_at.desc(), SlugAliases.id.desc())
    )
    return list(rows.all())


# ─── Changing one ────────────────────────────────────────────────────────


def check(kind: str, raw) -> str:
    """The slug, lower-cased, or 422 SLUG_INVALID / SLUG_RESERVED."""
    slug = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if not (MIN_LENGTH <= len(slug) <= MAX_LENGTH) or not SHAPE.match(slug):
        raise CustomHTTPException(
            422,
            message=f"An address is {MIN_LENGTH} to {MAX_LENGTH} letters, digits and single hyphens, "
            "starting and ending with a letter or digit.",
            error_code="SLUG_INVALID",
        )
    if slug.isdigit():
        # A number is read as an id wherever an address may be an id or a slug.
        raise CustomHTTPException(422, message="An address needs at least one letter.", error_code="SLUG_INVALID")
    if kind == CLUB and slug in RESERVED_CLUB_SLUGS:
        raise CustomHTTPException(422, message="That address is kept for the platform. Pick another.", error_code="SLUG_RESERVED")
    return slug


def _taken() -> CustomHTTPException:
    return CustomHTTPException(409, message="That address is taken.", error_code="SLUG_TAKEN")


def url_of(kind: str, slug: str) -> str:
    """The public page at this slug: the event's page, or the club's page on the site."""
    if kind == EVENT:
        return public_urls.event_url(slug)
    return settings.PUBLIC_CLUB_URL.format(slug=slug)


def site_url_of(kind: str, slug: str) -> Optional[str]:
    """A club's website at its subdomain; None for an event."""
    if kind != CLUB:
        return None
    from app.api.sites.service import _LABEL

    # As sites.service.public_url: a slug that cannot be a subdomain has the site's page instead.
    return settings.PUBLIC_CLUB_SITE_URL.format(slug=slug) if _LABEL.match(slug) else url_of(CLUB, slug)


async def out(session, kind: str, row) -> dict:
    """What the change answers, and what the admin's Web address card shows."""
    return {
        "slug": row.slug,
        "aliases": await aliases_of(session, kind, row.id),
        "url": url_of(kind, row.slug),
        "site_url": site_url_of(kind, row.slug),
    }


async def change(session, kind: str, target_id: int, raw, actor, request=None) -> dict:
    """Give a club or an event a new slug. The old one becomes an alias (and
    keeps answering); an old one of the same target is taken back (its
    alias goes). Unique against every slug of the kind, live (deleted rows
    included: the column is unique over all of them) and old. Audited.
    Commits; the site's ISR pages and the discover cache follow from the
    row's change (app/core/site_revalidate.py), and a published club site
    is made again so its links carry the new address."""
    from app.api.audit import service as audit

    model = _TABLES[kind]
    row = await session.scalar(select(model).where(model.id == target_id).with_for_update())
    if row is None or row.is_deleted:
        raise CustomHTTPException(404, message=f"{_NOUN[kind]} not found")
    slug = check(kind, raw)
    old = row.slug
    if slug == old:
        return await out(session, kind, row)

    holder = await session.scalar(select(model.id).where(model.slug == slug))
    if holder is not None:
        raise _taken()
    alias = await session.scalar(select(SlugAliases).where(SlugAliases.kind == kind, SlugAliases.slug == slug))
    if alias is not None and alias.target_id != row.id:
        raise _taken()

    try:
        async with session.begin_nested():
            if alias is not None:
                # Changing back: the old address is the live one again.
                await session.execute(delete(SlugAliases).where(SlugAliases.id == alias.id))
            row.slug = slug
            session.add(SlugAliases(kind=kind, slug=old, target_id=row.id, created_by_user_id=getattr(actor, "id", None)))
            await session.flush()
    except IntegrityError:
        # Another change took it between the check and this write.
        raise _taken() from None

    name = getattr(row, "name", None) or f"#{row.id}"
    await audit.record(
        session,
        actor=actor,
        action=f"{kind}.slug_change",
        entity_type=kind,
        entity_id=row.id,
        summary=f"Changed the address of {kind} “{name}”: {old} → {slug}",
        details={"from": old, "to": slug, "reclaimed": alias is not None},
        request=request,
    )
    await session.commit()
    answer = await out(session, kind, row)

    # A published site lists the club's and its events' addresses: made again
    # (never raises; a failure leaves the sites sweep to catch it).
    from app.api.sites import service as sites

    if kind == CLUB:
        await sites.republish_for_club(session, row.id)
    else:
        await sites.republish_for_event(session, row.id)
    return answer
