"""Which domain a club's site answers at. Small and free of the rest of the
feature, so the site, the tracking links and the discover pages can ask
without importing Cloudflare."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from sqlalchemy import select

from app.api.club_domains.models import ACTIVE, ClubDomains
from app.config import settings

# One DNS label: what can stand before .myotherapp.in.
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def default_host(slug: str | None) -> str | None:
    """`{slug}.myotherapp.in`, or None when the slug cannot be a DNS label."""
    if not slug or not _LABEL.match(slug):
        return None
    return urlsplit(settings.PUBLIC_CLUB_SITE_URL.format(slug=slug)).hostname


def club_site_suffix() -> str:
    """`.myotherapp.in`: what a club's own subdomain ends with."""
    host = urlsplit(settings.PUBLIC_CLUB_SITE_URL.replace("{slug}", "x")).hostname or ""
    return host[1:] if host.startswith("x.") else ""


async def active_host(session, club_id: int) -> str | None:
    """The club's own domain while it is active, else None."""
    return await session.scalar(
        select(ClubDomains.host).where(ClubDomains.club_id == club_id, ClubDomains.status == ACTIVE).limit(1)
    )


async def active_hosts(session, club_ids) -> dict[int, str]:
    ids = list(club_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(ClubDomains.club_id, ClubDomains.host).where(ClubDomains.club_id.in_(ids), ClubDomains.status == ACTIVE)
    )
    return {club_id: host for club_id, host in rows.all()}
