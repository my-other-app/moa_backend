"""Links to the private developer docs (feature-register/private-dev-docs-contract.md).

Only https://myotherapp.in/developers is public. Every other page of the
docs, the OpenAPI document and the Postman file need one of these signed
links: `https://myotherapp.in/developers/access?t=moa_docs_...`. The website
asks GET /api/v1/developers/access whether the token is good
(developers_router.py), keeps it in a cookie on /developers, and asks again
at most every 5 minutes, so a link taken away ends within 5 minutes.

The token is `moa_docs_` and 32 random bytes, kept only as a sha256 digest
(as a club's API keys are, keys.py): it is in the one answer that made it
and nowhere else.

Who makes them, each stored so it can be listed and revoked:

- a club's owner or admin, while the club is allocated API access
  (/clubs/integrations/docs-links; club_access.policy's `_INTEGRATIONS`):
  for themselves, 7 days, or to share with their developer, 30 days; at
  most MAX_LIVE_PER_CLUB live at once;
- a platform admin with manage_content (/admin/integrations/docs-links):
  their own, 7 days, or one for a partner app's developers, 30 days.

A link stops working, checked on every use (`check`), when it expires or is
revoked; a club's when the club's API access is off, the club is no longer
active (deleted, not approved, its account closed) or the person who made it
is no longer its owner or an admin of it; a partner app's when the app is
suspended or gone; an admin's own when that person is no longer an admin
who may manage content; any of them while PARTNER_API_ENABLED is off. Why is
never said: every dead link is the same 404.
"""
from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, or_, select

from app.api.partner import allocation
from app.api.partner.keys import digest
from app.api.partner.models import DOCS_LINK_KINDS, DeveloperDocsLinks, PartnerApps
from app.config import settings
from app.response import CustomHTTPException

PREFIX = "moa_docs_"
SELF_DAYS = 7
SHARE_DAYS = 30
MAX_LIVE_PER_CLUB = 20
MAX_LABEL = 80
#: A link's "last used" and use count are written at most this often.
TOUCH_EVERY = timedelta(minutes=1)
#: How long an expired or revoked link stays in the club's list.
KEEP_ENDED = timedelta(days=30)
#: Where a docs token may also come with a request for the OpenAPI document.
HEADER = "MOA-Docs-Token"
QUERY = "docs_token"
_MAX_TOKEN = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """In UTC, however the column handed it back, so one time reads the same everywhere."""
    return _aware(dt).astimezone(timezone.utc).isoformat() if dt is not None else None


def _refuse(status: int, message: str, code: str, **errors) -> CustomHTTPException:
    return CustomHTTPException(status, message=message, error_code=code, errors=errors or None)


def new_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def looks_like(raw: Optional[str]) -> bool:
    return bool(raw) and raw.startswith(PREFIX) and len(raw) <= _MAX_TOKEN


def url_for(raw: str) -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/developers/access?t={raw}"


def token_of(request) -> Optional[str]:
    """A docs token sent with a request: the MOA-Docs-Token header, else ?docs_token=."""
    raw = request.headers.get(HEADER) or request.query_params.get(QUERY) or ""
    return raw.strip() or None


def _label(value: Optional[str]) -> Optional[str]:
    text = " ".join((value or "").split())
    if len(text) > MAX_LABEL:
        raise _refuse(422, f"Keep the label to {MAX_LABEL} characters.", "VALIDATION", label=f"At most {MAX_LABEL} characters.")
    return text or None


def _expired(row: DeveloperDocsLinks) -> bool:
    return _aware(row.expires_at) <= _now()


def status_of(row: DeveloperDocsLinks) -> str:
    if row.revoked_at is not None:
        return "revoked"
    return "expired" if _expired(row) else "active"


# ─── Making them ─────────────────────────────────────────────────────────


async def live_count(session, club_id: int) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(DeveloperDocsLinks).where(
            DeveloperDocsLinks.kind == "club", DeveloperDocsLinks.club_id == club_id,
            DeveloperDocsLinks.revoked_at.is_(None), DeveloperDocsLinks.expires_at > _now(),
        )
    ) or 0)


async def _create(
    session, *, kind: str, purpose: str, days: int, club_id: Optional[int] = None, partner_app_id=None,
    label: Optional[str] = None, user_id: Optional[int] = None, member_id: Optional[int] = None, by_name: Optional[str] = None,
) -> tuple[DeveloperDocsLinks, str]:
    raw = new_token()
    row = DeveloperDocsLinks(
        token_hash=digest(raw), kind=kind, purpose=purpose, club_id=club_id, partner_app_id=partner_app_id, label=_label(label),
        created_by_user_id=user_id, created_by_member_id=member_id, created_by_name=(by_name or "")[:120] or None,
        expires_at=_now() + timedelta(days=days), use_count=0,
    )
    session.add(row)
    await session.flush()
    return row, raw


async def create_for_club(
    session, club_id: int, *, purpose: str, label: Optional[str] = None, user_id: Optional[int] = None,
    member_id: Optional[int] = None, by_name: Optional[str] = None,
) -> tuple[DeveloperDocsLinks, str]:
    """A club's link: `self` (7 days) or `share` (30 days). Flushed, not committed."""
    if purpose not in ("self", "share"):
        raise _refuse(422, "Say whether the link is for you (self) or to share (share).", "VALIDATION", purpose="self or share.")
    if not (await allocation.of(session, club_id)).allocated:
        raise _refuse(403, "API access isn't turned on for this club. Ask the MyOtherApp team.", "NOT_ALLOCATED")
    if await live_count(session, club_id) >= MAX_LIVE_PER_CLUB:
        raise _refuse(409, f"This club can have {MAX_LIVE_PER_CLUB} docs links at once. Revoke one first.", "DOCS_LINK_LIMIT",
                      max_links=MAX_LIVE_PER_CLUB)
    return await _create(
        session, kind="club", purpose=purpose, days=SELF_DAYS if purpose == "self" else SHARE_DAYS, club_id=club_id,
        label=label, user_id=user_id, member_id=member_id, by_name=by_name,
    )


async def create_for_admin(
    session, user, *, partner_app_id=None, label: Optional[str] = None,
) -> tuple[DeveloperDocsLinks, str, Optional[PartnerApps]]:
    """An admin's own link (7 days), or with `partner_app_id` one for that
    app's developers (30 days). Flushed, not committed."""
    by_name = getattr(user, "full_name", None) or getattr(user, "email", None)
    if partner_app_id is None:
        row, raw = await _create(session, kind="admin", purpose="self", days=SELF_DAYS, label=label, user_id=user.id, by_name=by_name)
        return row, raw, None
    app = await _app(session, partner_app_id)
    if app is None:
        raise _refuse(404, "No such partner app.", "APP_NOT_FOUND")
    if app.status != "active":
        raise _refuse(409, "This app is suspended: resume it first.", "APP_SUSPENDED")
    row, raw = await _create(
        session, kind="partner_app", purpose="share", days=SHARE_DAYS, partner_app_id=app.id, label=label, user_id=user.id, by_name=by_name,
    )
    return row, raw, app


async def _app(session, app_id) -> Optional[PartnerApps]:
    try:
        pk = app_id if isinstance(app_id, UUID) else UUID(str(app_id))
    except (TypeError, ValueError):
        return None
    return await session.get(PartnerApps, pk)


# ─── Listing and revoking ────────────────────────────────────────────────


async def link_of(session, link_id, *, club_id: Optional[int] = None) -> DeveloperDocsLinks:
    """One link by its id; with `club_id`, only that club's own."""
    try:
        pk = UUID(str(link_id))
    except (TypeError, ValueError):
        pk = None
    row = await session.get(DeveloperDocsLinks, pk) if pk else None
    if row is None or (club_id is not None and (row.kind != "club" or row.club_id != club_id)):
        raise _refuse(404, "No such docs link.", "DOCS_LINK_NOT_FOUND")
    return row


async def revoke(session, row: DeveloperDocsLinks, *, by_user_id: Optional[int] = None) -> DeveloperDocsLinks:
    if row.revoked_at is None:
        row.revoked_at = _now()
        row.revoked_by_user_id = by_user_id
    await session.flush()
    return row


async def club_links(session, club_id: int) -> list[DeveloperDocsLinks]:
    """The club's links, newest first: the live ones, and those that ended
    (expired or revoked) in the last 30 days."""
    cutoff = _now() - KEEP_ENDED
    q = select(DeveloperDocsLinks).where(
        DeveloperDocsLinks.kind == "club", DeveloperDocsLinks.club_id == club_id, DeveloperDocsLinks.expires_at > cutoff,
        or_(DeveloperDocsLinks.revoked_at.is_(None), DeveloperDocsLinks.revoked_at > cutoff),
    )
    return list((await session.scalars(q.order_by(DeveloperDocsLinks.created_at.desc()))).all())


async def all_links(
    session, *, partner_app_id=None, kind: Optional[str] = None, club_id: Optional[int] = None, limit: int = 500,
) -> list[DeveloperDocsLinks]:
    """Every link (the admin's list), newest first."""
    q = select(DeveloperDocsLinks)
    if partner_app_id is not None:
        try:
            app_pk = UUID(str(partner_app_id))
        except (TypeError, ValueError):
            return []
        q = q.where(DeveloperDocsLinks.partner_app_id == app_pk)
    if kind is not None:
        if kind not in DOCS_LINK_KINDS:
            return []
        q = q.where(DeveloperDocsLinks.kind == kind)
    if club_id is not None:
        q = q.where(DeveloperDocsLinks.club_id == club_id)
    return list((await session.scalars(q.order_by(DeveloperDocsLinks.created_at.desc()).limit(limit))).all())


async def names(session, rows: list[DeveloperDocsLinks]) -> tuple[dict, dict]:
    """The clubs and partner apps the rows name, by id: ({id: club}, {id: app})."""
    from app.api.clubs.models import Clubs

    club_ids = sorted({r.club_id for r in rows if r.club_id is not None})
    app_ids = sorted({r.partner_app_id for r in rows if r.partner_app_id is not None}, key=str)
    clubs = {c.id: c for c in (await session.scalars(select(Clubs).where(Clubs.id.in_(club_ids)))).all()} if club_ids else {}
    apps = {a.id: a for a in (await session.scalars(select(PartnerApps).where(PartnerApps.id.in_(app_ids)))).all()} if app_ids else {}
    return clubs, apps


def out(row: DeveloperDocsLinks, *, club=None, app=None) -> dict:
    """A link as the dashboards show it: never its token."""
    return {
        "id": str(row.id),
        "label": row.label,
        "kind": row.kind,
        "purpose": row.purpose,
        "status": status_of(row),
        "club": {"id": club.id, "name": club.name} if club is not None else ({"id": row.club_id, "name": None} if row.club_id else None),
        "partner_app": {"id": str(app.id), "name": app.name} if app is not None else (
            {"id": str(row.partner_app_id), "name": None} if row.partner_app_id else None),
        "created_by": {"id": row.created_by_user_id, "name": row.created_by_name, "member_id": row.created_by_member_id},
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "last_used_at": _iso(row.last_used_at),
        "use_count": int(row.use_count or 0),
        "revoked_at": _iso(row.revoked_at),
    }


def made(row: DeveloperDocsLinks, raw: str, *, club=None, app=None) -> dict:
    """The answer that made a link: the link, and its url carrying the token (shown once)."""
    return {"link": out(row, club=club, app=app), "url": url_for(raw)}


# ─── Checking them ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Access:
    link: DeveloperDocsLinks
    club: object = None
    app: Optional[PartnerApps] = None


async def _club_alive(session, row: DeveloperDocsLinks):
    """The link's club, when the club is active, allocated, and whoever made
    the link is still its owner or one of its admins; else None."""
    from app.api.club_access.models import ClubMembers
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users

    club = await session.get(Clubs, row.club_id) if row.club_id is not None else None
    if club is None or club.is_deleted or not club.is_verified:
        return None
    owner = await session.get(Users, club.user_id) if club.user_id else None
    if owner is None or owner.is_deleted or getattr(owner, "is_active", True) is False:
        return None
    if not (await allocation.of(session, club.id)).allocated:
        return None
    if row.created_by_member_id is not None:
        member = await session.get(ClubMembers, row.created_by_member_id)
        if member is None or member.club_id != club.id or member.status != "active" or member.role != "admin":
            return None
    elif row.created_by_user_id is None or row.created_by_user_id != club.user_id:
        return None
    return club


async def _admin_alive(session, row: DeveloperDocsLinks) -> bool:
    from app.api.users.models import Users
    from app.core.auth.permissions import MANAGE_CONTENT, can

    user = await session.get(Users, row.created_by_user_id) if row.created_by_user_id is not None else None
    if user is None or user.is_deleted or getattr(user, "is_active", True) is False:
        return False
    if getattr(user.user_type, "value", user.user_type) != "admin":
        return False
    return can(user, MANAGE_CONTENT)


async def check(session, raw: Optional[str]) -> Optional[Access]:
    """What a docs token gives access to, now; None for any token that does not."""
    if not settings.PARTNER_API_ENABLED:
        return None
    raw = (raw or "").strip()
    if not looks_like(raw):
        return None
    wanted = digest(raw)
    row = await session.scalar(select(DeveloperDocsLinks).where(DeveloperDocsLinks.token_hash == wanted))
    if row is None or not hmac.compare_digest(row.token_hash, wanted):
        return None
    if row.revoked_at is not None or _expired(row):
        return None
    if row.kind == "club":
        club = await _club_alive(session, row)
        return Access(link=row, club=club) if club is not None else None
    if row.kind == "partner_app":
        app = await _app(session, row.partner_app_id) if row.partner_app_id is not None else None
        return Access(link=row, app=app) if app is not None and app.status == "active" else None
    if row.kind == "admin":
        return Access(link=row) if await _admin_alive(session, row) else None
    return None


async def touch(session, row: DeveloperDocsLinks) -> None:
    """The link was used: `last_used_at` and `use_count`, at most once a minute."""
    now = _now()
    seen = _aware(row.last_used_at)
    if seen is None or now - seen > TOUCH_EVERY:
        row.last_used_at = now
        row.use_count = int(row.use_count or 0) + 1
        await session.commit()


def access_out(found: Access) -> dict:
    """GET /developers/access's answer for a good token."""
    data = {"ok": True, "kind": found.link.kind, "expires_at": _iso(found.link.expires_at)}
    if found.club is not None:
        data["club"] = {"id": found.club.id, "name": found.club.name}
    if found.app is not None:
        data["partner_app"] = {"id": str(found.app.id), "name": found.app.name}
    return data
