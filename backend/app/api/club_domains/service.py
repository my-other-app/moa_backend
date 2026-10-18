"""A club's own domain: connect it, check it, take it away; the moves the
sweep makes; what the Worker asks; the admins' list.

The rules (the states are in models.py):

- Connecting a domain makes it at Cloudflare first. If Cloudflare refuses,
  nothing changes here and the club keeps what it had. A club that already
  has a domain and connects another gets the new one pending and the old one
  removed, at Cloudflare too (best effort).
- A domain is `active` once Cloudflare says both the hostname and its
  certificate are active and the club's plan includes `custom_domain`. The
  first time, the club is mailed "Your site is live at ..." and the admins
  are alerted (`club_domain_active`).
- Not entitled any more: `paused` (the Worker redirects it to the club's
  subdomain). Entitled again: `active`. Thirty days paused: removed at
  Cloudflare, and the club is mailed.
- Pending for seven days: `failed`, and gone from Cloudflare (which deletes
  an unvalidated hostname after seven days anyway).
- Whenever a domain starts or stops being active, the published page is made
  again, so its canonical address and og:url follow.

Cloudflare is only ever called while `cloudflare.available()`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.club_domains import cloudflare as cf
from app.api.club_domains import hosts
from app.api.club_domains.lookup import _LABEL, active_host, club_site_suffix, default_host
from app.api.club_domains.models import ACTIVE, FAILED, LIVE, PAUSED, PENDING, REMOVED, STATUSES, ClubDomains
from app.api.clubs.models import Clubs
from app.config import settings
from app.core.after_commit import run_after_commit
from app.core.email import plain
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FEATURE = "custom_domain"
#: How long a domain may wait for its records before it fails.
PENDING_FOR = timedelta(days=7)
#: How often an active domain is checked with Cloudflare again.
RECHECK_ACTIVE = timedelta(days=1)
#: How long a paused domain is kept before it is removed.
PAUSED_FOR = timedelta(days=30)
#: Cloudflare adds the certificate's TXT record a few seconds after the
#: hostname is made; connecting waits this long once for it.
SSL_RECORDS_WAIT = 2.0

#: Cloudflare hostname states that will never become active on their own.
BLOCKED = frozenset({"blocked", "pending_blocked", "test_blocked", "test_failed"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ─── Refusals ────────────────────────────────────────────────────────────


def unavailable() -> CustomHTTPException:
    return CustomHTTPException(
        503,
        message="Custom domains aren't available right now. Try again later.",
        error_code="DOMAINS_UNAVAILABLE",
    )


def cloudflare_error(exc: cf.CloudflareError) -> CustomHTTPException:
    return CustomHTTPException(
        502,
        message=f"Cloudflare couldn't set this domain up: {exc.message}",
        error_code="CLOUDFLARE_ERROR",
        errors={"cloudflare": exc.message},
    )


def host_taken(host: str) -> CustomHTTPException:
    return CustomHTTPException(
        409,
        message=f"{host} is already connected to another club's site.",
        error_code="HOST_TAKEN",
        errors={"host": host},
    )


# ─── Reading ─────────────────────────────────────────────────────────────


async def entitled(session, club_id: int) -> bool:
    """Whether the club's own plan includes its own domain. Who is asking
    does not matter here: an admin acting as the club is let through the
    gate, but the domain still follows the club's plan."""
    from app.api.subscriptions import gates

    if not gates.enforcing():
        return True
    return bool((await gates.entitlements(session, club_id)).allows(FEATURE))


async def current(session, club_id: int, *, lock: bool = False) -> ClubDomains | None:
    """The club's domain as the dashboard shows it: the live one, or the one
    that failed (until the club tries again or dismisses it)."""
    q = (
        select(ClubDomains)
        .where(ClubDomains.club_id == club_id, ClubDomains.status != REMOVED)
        .order_by(ClubDomains.created_at.desc(), ClubDomains.id.desc())
        .limit(1)
    )
    if lock:
        q = q.with_for_update()
    return await session.scalar(q)


def domain_out(row: ClubDomains | None) -> dict | None:
    if row is None:
        return None
    return {
        "host": row.host,
        "status": row.status,
        "records": list(row.records or []),
        "ssl_status": row.ssl_status,
        "last_error": row.last_error,
        "verified_at": row.verified_at,
        "created_at": row.created_at,
    }


async def out(session, club: Clubs, row: ClubDomains | None = None) -> dict:
    row = row if row is not None else await current(session, club.id)
    return {
        "available": cf.available(),
        "entitled": await entitled(session, club.id),
        "default_host": default_host(club.slug),
        "domain": domain_out(row if row is not None and row.status != REMOVED else None),
    }


# ─── Applying what Cloudflare said ───────────────────────────────────────


def _apply(row: ClubDomains, result: dict, now: datetime) -> None:
    row.cf_hostname_id = (result or {}).get("id") or row.cf_hostname_id
    row.hostname_status, row.ssl_status = cf.statuses(result)
    fresh = cf.records(result, row.host)
    if row.status == PENDING or len(fresh) > 1:
        row.records = fresh
    row.last_error = None if cf.is_live(result) else cf.problem(result)
    row.checked_at = now


async def _forget(hostname_id: str | None) -> bool:
    """Delete it at Cloudflare. Best effort: logged, never raised."""
    if not hostname_id or not cf.available():
        return False
    try:
        return await cf.delete(hostname_id)
    except cf.CloudflareError:
        logger.warning("club domain: Cloudflare would not delete custom hostname %s", hostname_id, exc_info=True)
        return False


async def _fail(row: ClubDomains, now: datetime, reason: str) -> None:
    await _forget(row.cf_hostname_id)
    row.status = FAILED
    row.last_error = reason[:300]
    row.cf_hostname_id = None
    row.paused_at = None
    row.checked_at = now


def _remove(row: ClubDomains, now: datetime) -> None:
    row.status = REMOVED
    row.removed_at = now
    row.paused_at = None


async def _club(session, club_id: int) -> Clubs | None:
    return await session.get(Clubs, club_id)


async def _owner_address(session, club_id: int) -> str | None:
    from app.api.subscriptions.notices import owner_address

    return await owner_address(session, club_id)


def send(to: str, subject: str, paragraphs: list[str], button, footer: str | None) -> None:
    """The mail, raising when the provider did not take it: what the durable
    job runs (app.core.after_commit), so an outage is retried."""
    from app.core.email import email as email_mod

    email_mod.send_email(
        recipients=[to], subject=subject,
        text=plain.text(paragraphs, button=button, footer=footer),
        html=plain.render(paragraphs, button=button, footer=footer),
    )


def _send(to: str, subject: str, paragraphs: list[str], button, footer: str | None) -> None:
    """What callers queue after commit: a job that runs `send` when there is a
    loop, else this, in line. Logged, never raised."""
    try:
        send(to, subject, paragraphs, button, footer)
    except Exception:  # noqa: BLE001
        logger.warning("club domain email (%s) to %s could not be sent", subject, to, exc_info=True)


async def _mail(session, club_id: int, subject: str, paragraphs: list[str], button=None, footer: str | None = None) -> bool:
    to = await _owner_address(session, club_id)
    if not to:
        return False
    run_after_commit(session, _send, to, subject, paragraphs, button, footer)
    return True


async def _went_live(session, row: ClubDomains, now: datetime) -> None:
    """Pending to active, the first time: mail the club, alert the admins."""
    from app.api.admin_alerts import notify

    row.status = ACTIVE
    row.paused_at = None
    row.verified_at = row.verified_at or now
    club = await _club(session, row.club_id)
    name = club.name if club is not None else f"Club {row.club_id}"
    await _mail(
        session, row.club_id,
        subject=f"Your site is live at {row.host}",
        paragraphs=[
            f"Hi {name},",
            f"Your club's website is live at https://{row.host}, with its own secure certificate.",
            f"Keep the CNAME record pointing {row.host} to {settings.SITES_CNAME_TARGET} for as long as you use the domain. "
            f"Your {default_host(club.slug) if club is not None else 'MyOtherApp'} address now forwards there too.",
        ],
        button=("Open your site", f"https://{row.host}"),
    )
    await notify(
        session, "club_domain_active",
        title=f"{name}'s site is live at {row.host}",
        body=f"Verified by Cloudflare: https://{row.host} serves the club's website.",
        club=club if club is not None else row.club_id,
        data={"domain_id": row.id, "host": row.host},
    )


async def sync_entitlement(session, row: ClubDomains, now: datetime) -> str | None:
    """Pause an active domain whose club lost the feature; resume a paused one
    whose club has it again. Returns "paused", "resumed" or None."""
    if row.status == ACTIVE and not await entitled(session, row.club_id):
        row.status = PAUSED
        row.paused_at = now
        return "paused"
    if row.status == PAUSED and row.cf_hostname_id and await entitled(session, row.club_id):
        row.status = ACTIVE
        row.paused_at = None
        return "resumed"
    return None


async def refresh(session, row: ClubDomains, now: datetime | None = None, *, nudge: bool = False) -> str | None:
    """Ask Cloudflare about a live domain and move it on. With `nudge`,
    Cloudflare is also asked to look at the records now rather than at its
    next retry. Returns what changed ("verified", "paused", "resumed",
    "failed") or None. Raises CloudflareError when Cloudflare could not be
    asked -- always before anything here has changed, so there is nothing
    to undo."""
    now = now or _now()
    if row.status not in LIVE:
        return None
    if not row.cf_hostname_id:
        await _fail(row, now, "This domain isn't set up at Cloudflare. Add it again to start over.")
        return "failed"
    try:
        result = await cf.get(row.cf_hostname_id)
        if nudge and not cf.is_live(result):
            try:
                result = await cf.refresh(row.cf_hostname_id) or result
            except cf.NotFound:
                raise
            except cf.CloudflareError:
                logger.info("club domain: Cloudflare would not recheck %s now; it will at its next retry", row.host)
    except cf.NotFound:
        row.cf_hostname_id = None
        await _fail(row, now, "Cloudflare no longer has this domain. Add it again to start over.")
        return "failed"
    _apply(row, result, now)
    hostname_status, _ = cf.statuses(result)
    if hostname_status in BLOCKED:
        reason = cf.problem(result) or "Cloudflare won't serve this domain."
        await _fail(row, now, f"Cloudflare blocked {row.host}: {reason}")
        return "failed"
    if row.status == PENDING:
        if cf.is_live(result):
            if await entitled(session, row.club_id):
                await _went_live(session, row, now)
                return "verified"
            row.verified_at = row.verified_at or now
            row.status = PAUSED
            row.paused_at = now
            return "paused"
        if now - _aware(row.created_at) > PENDING_FOR:
            await _fail(
                row, now,
                f"We couldn't verify {row.host} within 7 days. Check the records at your DNS provider, then add the domain again.",
            )
            return "failed"
        return None
    return await sync_entitlement(session, row, now)


async def republish(session, club_id: int) -> None:
    """The published page again, so its canonical address follows the domain. Never raises."""
    from app.api.sites import service as sites

    await sites.republish_for_club(session, club_id)


async def after_change(session, club_id: int, before: str, after: str) -> None:
    """Once committed: a page that just started or stopped being served at the domain is made again."""
    if before != after and ACTIVE in (before, after):
        await republish(session, club_id)


# ─── The club's own actions ──────────────────────────────────────────────


async def _create_at_cloudflare(session, host: str) -> dict:
    """The custom hostname for `host`, made now -- or, when Cloudflare
    already has one that nothing live here holds (a delete that failed),
    that one."""
    try:
        return await cf.create(host)
    except cf.CloudflareError as exc:
        if not cf.is_duplicate(exc):
            raise cloudflare_error(exc) from exc
        duplicate = exc
    try:
        found = await cf.find(host)
    except cf.CloudflareError as exc:
        raise cloudflare_error(exc) from exc
    if not found or not found.get("id"):
        raise cloudflare_error(duplicate)
    held = await session.scalar(
        select(ClubDomains.id).where(ClubDomains.cf_hostname_id == found["id"], ClubDomains.status.in_(LIVE)).limit(1)
    )
    if held:
        raise host_taken(host)
    return found


async def connect(session, club: Clubs, raw_host: str, user, *, request=None) -> ClubDomains:
    """Connect `raw_host` to the club's site. Same live domain again: nothing to do."""
    from app.api.audit import service as audit

    if not cf.available():
        raise unavailable()
    host = hosts.normalise(raw_host)
    existing = await current(session, club.id, lock=True)
    if existing is not None and existing.host == host and existing.status in LIVE:
        return existing
    taken = await session.scalar(
        select(ClubDomains.id).where(ClubDomains.host == host, ClubDomains.status.in_(LIVE), ClubDomains.club_id != club.id).limit(1)
    )
    if taken:
        raise host_taken(host)

    # Cloudflare first: if it refuses, the club keeps what it had.
    result = await _create_at_cloudflare(session, host)
    now = _now()
    before = existing.status if existing is not None else REMOVED
    old_hostname = existing.cf_hostname_id if existing is not None and existing.status in LIVE else None
    if existing is not None:
        _remove(existing, now)
        await session.flush()
    row = ClubDomains(club_id=club.id, host=host, status=PENDING, records=[], created_by=getattr(user, "id", None), created_at=now, updated_at=now)
    _apply(row, result, now)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        if result.get("id") and result.get("id") != old_hostname:
            await _forget(result["id"])
        raise host_taken(host) from None
    await audit.record(
        session, actor=user, action="club_domain.connect", entity_type="club", entity_id=club.id,
        summary=f"Connected {host} to the club's website" + (f" (replacing {existing.host})" if existing is not None else ""),
        details={"domain_id": row.id, "host": host, "replaced": existing.host if existing is not None else None},
        request=request,
    )
    await session.commit()
    if old_hostname and old_hostname != row.cf_hostname_id:
        await _forget(old_hostname)
    await after_change(session, club.id, before, REMOVED)

    if row.cf_hostname_id and not any(r.get("purpose") == "ssl" for r in row.records or []):
        # The certificate's record follows the hostname by a few seconds.
        await asyncio.sleep(SSL_RECORDS_WAIT)
        try:
            again = await cf.get(row.cf_hostname_id)
        except cf.CloudflareError:
            again = None
        if again:
            await session.refresh(row)
            _apply(row, again, _now())
            await session.commit()
    return row


async def check(session, club: Clubs) -> ClubDomains:
    """Ask Cloudflare now, for the club's Check button."""
    if not cf.available():
        raise unavailable()
    row = await current(session, club.id, lock=True)
    if row is None:
        raise CustomHTTPException(404, message="This club hasn't added a domain.", error_code="NO_DOMAIN")
    before = row.status
    try:
        await refresh(session, row, _now(), nudge=True)
    except cf.CloudflareError as exc:
        # Cloudflare is asked before anything changes: nothing to undo.
        raise cloudflare_error(exc) from exc
    await session.commit()
    await after_change(session, club.id, before, row.status)
    return row


async def disconnect(session, club: Clubs, user, *, request=None) -> None:
    """Take the club's domain away, here and at Cloudflare (best effort)."""
    from app.api.audit import service as audit

    row = await current(session, club.id, lock=True)
    if row is None:
        return
    before, hostname_id = row.status, row.cf_hostname_id
    _remove(row, _now())
    await audit.record(
        session, actor=user, action="club_domain.remove", entity_type="club", entity_id=club.id,
        summary=f"Removed {row.host} from the club's website",
        details={"domain_id": row.id, "host": row.host, "was": before},
        request=request,
    )
    await session.commit()
    if before in LIVE:
        await _forget(hostname_id)
    await after_change(session, club.id, before, REMOVED)


# ─── The Worker's question ───────────────────────────────────────────────


def _bare(raw: str | None) -> str:
    host = (raw or "").strip().lower().rstrip(".")
    if host.startswith("[") or host.count(":") > 1:
        return host
    return host.split(":", 1)[0]


async def resolve(session, raw_host: str | None) -> dict | None:
    """Whose site `host` is: {club_slug, status, canonical_host}, or None.

    - `www.theirclub.org`, active: serve the site; paused: redirect to the
      club's subdomain.
    - `{slug}.myotherapp.in`: the club's site; canonical_host is its own
      domain while that is active, so the Worker can redirect there.
    """
    host = _bare(raw_host)
    if not host or len(host) > 253:
        return None
    suffix = club_site_suffix()
    if suffix and host.endswith(suffix):
        slug = host[: -len(suffix)]
        if not _LABEL.match(slug):
            return None
        club = await session.scalar(select(Clubs.id).where(Clubs.slug == slug, Clubs.is_deleted == False))  # noqa: E712
        if club is None:
            return None
        return {"club_slug": slug, "status": "active", "canonical_host": await active_host(session, club) or host}
    row = (
        await session.execute(
            select(ClubDomains.status, Clubs.slug)
            .join(Clubs, Clubs.id == ClubDomains.club_id)
            .where(ClubDomains.host == host, ClubDomains.status.in_((ACTIVE, PAUSED)), Clubs.is_deleted == False)  # noqa: E712
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    status, slug = row
    if status == ACTIVE:
        return {"club_slug": slug, "status": "active", "canonical_host": host}
    # A slug that cannot be a subdomain has none to go back to: the public site, never the domain itself.
    fallback = default_host(slug) or urlsplit(settings.PUBLIC_SITE_URL).hostname
    return {"club_slug": slug, "status": "paused", "canonical_host": fallback}


# ─── The admins ──────────────────────────────────────────────────────────


async def admin_list(session, *, status: str | None, q: str | None, offset: int, limit: int):
    criteria = []
    if status:
        if status not in STATUSES:
            raise CustomHTTPException(422, message=f"status is one of {', '.join(STATUSES)}")
        criteria.append(ClubDomains.status == status)
    else:
        criteria.append(ClubDomains.status != REMOVED)
    query = select(ClubDomains).join(Clubs, Clubs.id == ClubDomains.club_id)
    if q and q.strip():
        like = f"%{q.strip().lower().replace('%', '').replace('_', '')}%"
        criteria.append(or_(ClubDomains.host.ilike(like), Clubs.name.ilike(like), Clubs.slug.ilike(like)))
    total = await session.scalar(select(func.count()).select_from(query.where(*criteria).subquery())) or 0
    rows = (
        await session.scalars(
            query.where(*criteria).options(selectinload(ClubDomains.club))
            .order_by(ClubDomains.created_at.desc(), ClubDomains.id.desc()).offset(offset).limit(limit)
        )
    ).all()
    return list(rows), int(total)


def admin_item(row: ClubDomains) -> dict:
    club = row.club
    return {
        "id": row.id,
        "club": {"id": club.id, "name": club.name, "slug": club.slug} if club is not None else None,
        "host": row.host,
        "status": row.status,
        "ssl_status": row.ssl_status,
        "last_error": row.last_error,
        "verified_at": row.verified_at,
        "created_at": row.created_at,
    }


async def _admin_row(session, domain_id: int) -> ClubDomains:
    row = await session.scalar(
        select(ClubDomains).where(ClubDomains.id == domain_id).with_for_update().execution_options(populate_existing=True)
    )
    if row is None:
        raise CustomHTTPException(404, message="No such domain")
    return row


async def admin_recheck(session, domain_id: int, admin, *, request=None) -> ClubDomains:
    from app.api.audit import service as audit

    if not cf.available():
        raise unavailable()
    row = await _admin_row(session, domain_id)
    before = row.status
    if row.status in LIVE:
        try:
            await refresh(session, row, _now(), nudge=True)
        except cf.CloudflareError as exc:
            raise cloudflare_error(exc) from exc
    await audit.record(
        session, actor=admin, action="club_domain.admin_recheck", entity_type="club", entity_id=row.club_id,
        summary=f"Rechecked {row.host} with Cloudflare: {before} -> {row.status}",
        details={"domain_id": row.id, "host": row.host, "before": before, "after": row.status},
        request=request,
    )
    await session.commit()
    await after_change(session, row.club_id, before, row.status)
    return row


async def admin_remove(session, domain_id: int, admin, *, request=None) -> ClubDomains:
    from app.api.audit import service as audit

    row = await _admin_row(session, domain_id)
    before, hostname_id = row.status, row.cf_hostname_id
    if before != REMOVED:
        _remove(row, _now())
        row.last_error = "Removed by MyOtherApp."
    await audit.record(
        session, actor=admin, action="club_domain.admin_remove", entity_type="club", entity_id=row.club_id,
        summary=f"Removed {row.host} from the club's website",
        details={"domain_id": row.id, "host": row.host, "was": before},
        request=request,
    )
    await session.commit()
    if before in LIVE:
        await _forget(hostname_id)
    await after_change(session, row.club_id, before, row.status)
    return row


# ─── The sweep's own moves ───────────────────────────────────────────────


async def expire_paused(session, row: ClubDomains, now: datetime) -> None:
    """Thirty days paused: removed, at Cloudflare too, and the club told."""
    club = await _club(session, row.club_id)
    name = club.name if club is not None else f"Club {row.club_id}"
    since = _aware(row.paused_at) or now
    hostname_id = row.cf_hostname_id
    _remove(row, now)
    row.last_error = "Removed after 30 days paused: the club's plan no longer includes its own domain."
    await _forget(hostname_id)
    fallback = default_host(club.slug) if club is not None else None
    await _mail(
        session, row.club_id,
        subject=f"{row.host} is no longer connected to your club's site",
        paragraphs=[
            f"Hi {name},",
            f"Your plan stopped including your own domain on {since:%d %b %Y}, so {row.host} has been forwarding to your MyOtherApp address since then. "
            "After 30 days we've now disconnected it.",
            (f"Your site is still at https://{fallback}. " if fallback else "Your site is still on MyOtherApp. ")
            + "To use your domain again, move to a plan that includes it and add it on the Website page.",
        ],
        button=("See plans", f"{settings.PUBLIC_WEB_URL.rstrip('/')}/billing"),
        footer=f"You can remove the {row.host} records at your DNS provider now.",
    )
