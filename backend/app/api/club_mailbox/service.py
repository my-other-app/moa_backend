"""A club's mailbox: setting it up, its address, what its plan allows, and
what the club and the admins are shown of it.

The plan (flag `mailbox`, Pro and Enterprise): setting up, changing the
address and sending answer 403 PLAN_FEATURE_LOCKED on other plans, and the
page says what to upgrade to. When a club's plan stops including it, the
mailbox lapses (`lapsed_at`, found by the sweep or the first look after):

- nothing new is accepted -- mail to its address is dropped (never bounced,
  so no backscatter), and counted -- and nothing can be sent;
- the mail already there stays readable for LAPSE_READABLE (30 days);
- then it can only be exported (an mbox file) or deleted, by the club's own
  account; reading a thread answers 403 MAILBOX_LAPSED;
- the owner is told at the lapse (bell and mail) and when the 30 days end.

Upgrading again opens it as it was. The feature switch (APP_CLUB_MAILBOX)
off hides it all: every route but the status answers 404 MAILBOX_OFF.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from app.api.club_mailbox import handles
from app.api.club_mailbox.models import (
    DEFAULT_RETENTION_MONTHS,
    IN,
    OUT,
    SPAM,
    ClubMailboxAddresses,
    ClubMailboxes,
    ClubMailMessages,
    ClubMailThreads,
)
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FEATURE = "mailbox"
#: After its plan lapses, the mail stays readable this long; then export or delete.
LAPSE_READABLE = timedelta(days=30)
ACTION = "club_mailbox"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


def on() -> bool:
    return bool(settings.CLUB_MAILBOX)


def domain() -> str:
    return (settings.MAILBOX_DOMAIN or "mail.myotherapp.in").strip().lower()


def address_of(handle: str) -> str:
    return f"{handle}@{domain()}"


def require_on() -> None:
    if not on():
        raise _refuse(404, "Club mailboxes aren't open yet.", "MAILBOX_OFF")


# ─── Reading ─────────────────────────────────────────────────────────────


async def club_of(session, user):
    from app.api.clubs.models import Clubs

    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id))
    if club is None:
        raise _refuse(404, "No club for this account", "NOT_A_CLUB")
    return club


async def mailbox_of(session, club_id: int) -> Optional[ClubMailboxes]:
    return await session.scalar(select(ClubMailboxes).where(ClubMailboxes.club_id == club_id))


async def current_address(session, club_id: int) -> Optional[ClubMailboxAddresses]:
    A = ClubMailboxAddresses
    return await session.scalar(select(A).where(A.club_id == club_id, A.retired_at.is_(None)))


async def forwarding(session, club_id: int, now: Optional[datetime] = None) -> list[ClubMailboxAddresses]:
    """The club's earlier addresses still forwarding into its mailbox."""
    now = now or _now()
    A = ClubMailboxAddresses
    return list((await session.scalars(
        select(A).where(A.club_id == club_id, A.retired_at.isnot(None), A.forward_until > now).order_by(A.retired_at.desc())
    )).all())


async def own_addresses(session, club_id: int) -> list[str]:
    """Every address of the club's that mail still reaches (current and forwarding)."""
    current = await current_address(session, club_id)
    out = [address_of(current.handle)] if current else []
    out += [address_of(a.handle) for a in await forwarding(session, club_id)]
    return out


async def plan_allows(session, club_id: int, actor=None) -> bool:
    from app.api.subscriptions import gates

    return await gates.allows(session, club_id, FEATURE, actor=actor)


async def require_plan(session, club_id: int, actor=None) -> None:
    from app.api.subscriptions import gates

    await gates.require_feature(session, club_id, FEATURE, actor=actor)


async def upgrade_payload(session) -> dict:
    from app.api.subscriptions import gates

    return (await gates.feature_locked(session, FEATURE)).errors or {}


def lapse_state(mailbox: Optional[ClubMailboxes], allowed: bool, now: Optional[datetime] = None) -> str:
    """`open` (the plan has it), `lapsed` (it doesn't; the mail readable for
    30 days from the lapse), `expired` (past those: export or delete)."""
    if allowed or mailbox is None:
        return "open"
    since = _aware(mailbox.lapsed_at) or (now or _now())
    return "expired" if (now or _now()) >= since + LAPSE_READABLE else "lapsed"


async def require_readable(session, club_id: int, mailbox: Optional[ClubMailboxes]) -> None:
    """403 MAILBOX_LAPSED once a lapsed mailbox's 30 days are over."""
    if mailbox is None:
        return
    allowed = await plan_allows(session, club_id)
    if lapse_state(mailbox, allowed) == "expired":
        raise _refuse(
            403, "Your plan no longer includes the mailbox and its 30 days are over. The club's owner can export the mail, "
                 "or delete it, from Mail settings.", "MAILBOX_LAPSED",
        )


async def require_mailbox(session, club_id: int) -> ClubMailboxes:
    mailbox = await mailbox_of(session, club_id)
    if mailbox is None:
        raise _refuse(409, "Pick the club's address first.", "NO_MAILBOX")
    return mailbox


# ─── The address ─────────────────────────────────────────────────────────


async def handle_free(session, handle: str, club_id: Optional[int] = None) -> bool:
    """Whether the handle can be this club's (free, or already its own)."""
    row = await handles.holder(session, handle)
    return row is None or (club_id is not None and row.club_id == club_id)


async def set_address(session, club, user, handle: str, *, member=None, request=None) -> ClubMailboxes:
    """Set the club up with `handle`, or change its address to it.

    422 HANDLE_INVALID / HANDLE_RESERVED, 409 HANDLE_TAKEN, 429
    HANDLE_CHANGED_RECENTLY (`errors.next_change_at`), 403
    PLAN_FEATURE_LOCKED. A changed address forwards 90 days. Audited."""
    from sqlalchemy.exc import IntegrityError

    from app.api.audit import service as audit

    require_on()
    value = handles.check(handle)
    await require_plan(session, club.id, actor=user)
    now = _now()
    mailbox = await mailbox_of(session, club.id)
    current = await current_address(session, club.id) if mailbox is not None else None
    if current is not None and current.handle == value:
        return mailbox
    if current is not None:
        next_at = handles.next_change_at(mailbox.handle_changed_at)
        if next_at is not None and now < next_at:
            raise _refuse(
                429, f"The address can change once a month. The next change can be made on {next_at.strftime('%-d %B %Y')}.",
                "HANDLE_CHANGED_RECENTLY", {"field": "handle", "next_change_at": next_at.isoformat()},
            )
    holder = await handles.holder(session, value, now)
    if holder is not None and holder.club_id != club.id:
        raise _refuse(409, "That address is taken. Pick another.", "HANDLE_TAKEN", {"field": "handle"})
    try:
        async with session.begin_nested():
            if mailbox is None:
                mailbox = ClubMailboxes(
                    club_id=club.id, created_by=getattr(user, "id", None) if member is None else None,
                    created_by_member_id=getattr(member, "id", None), retention_months=DEFAULT_RETENTION_MONTHS,
                )
                session.add(mailbox)
                await session.flush()
            if current is not None:
                current.retired_at = now
                current.forward_until = now + handles.FORWARD_FOR
                mailbox.handle_changed_at = now
            await session.flush()
            stale = await session.scalar(select(ClubMailboxAddresses).where(ClubMailboxAddresses.handle == value))
            if stale is not None and stale.club_id == club.id:
                # The club's own old handle, still forwarding, becomes its address again.
                stale.retired_at, stale.forward_until = None, None
            else:
                if stale is not None:
                    # Another club's, whose forwarding has ended (the sweep had not let it go yet).
                    await session.delete(stale)
                    await session.flush()
                session.add(ClubMailboxAddresses(club_id=club.id, mailbox_id=mailbox.id, handle=value, created_at=now))
            await session.flush()
    except IntegrityError:
        # Taken by another club between the check and the write.
        raise _refuse(409, "That address is taken. Pick another.", "HANDLE_TAKEN", {"field": "handle"}) from None
    await audit.record(
        session, actor=user, action=f"{ACTION}.address", entity_type="club", entity_id=club.id,
        summary=(f"Changed the club's address to {address_of(value)}" if current is not None
                 else f"Set up the club's mailbox at {address_of(value)}"),
        details={"handle": value, "previous": current.handle if current is not None else None,
                 "member_id": getattr(member, "id", None)},
        request=request,
    )
    await session.commit()
    return mailbox


async def update_settings(session, club, user, *, display_name: Optional[str], retention_months: Optional[int],
                          request=None) -> ClubMailboxes:
    """The name the club sends as and how long its mail is kept. Audited (what changed, not the words)."""
    from app.api.audit import service as audit

    require_on()
    mailbox = await require_mailbox(session, club.id)
    changed = {}
    if display_name is not None:
        value = " ".join(display_name.split())[:80] or None
        if value != mailbox.display_name:
            mailbox.display_name = value
            changed["display_name"] = True
    if retention_months is not None and retention_months != mailbox.retention_months:
        changed["retention_months"] = {"before": mailbox.retention_months, "after": retention_months}
        mailbox.retention_months = retention_months
    if changed:
        await audit.record(
            session, actor=user, action=f"{ACTION}.settings", entity_type="club", entity_id=club.id,
            summary="Changed the club mailbox's settings", details={"changed": changed}, request=request,
        )
    await session.commit()
    return mailbox


async def set_signature(session, club, signature: Optional[str]) -> ClubMailboxes:
    require_on()
    mailbox = await require_mailbox(session, club.id)
    value = (signature or "").replace("\r\n", "\n").strip()
    mailbox.signature = value[:2000] or None
    await session.commit()
    return mailbox


def sender_name(mailbox: ClubMailboxes, club) -> str:
    return (mailbox.display_name or getattr(club, "name", None) or "Club")[:80]


# ─── What the club is shown ──────────────────────────────────────────────


async def credits_left(session, club_id: int) -> Optional[int]:
    from app.api.club_mailbox import credits

    try:
        return await credits.remaining(session, club_id)
    except Exception:  # noqa: BLE001 - the page shows nothing rather than failing
        logger.warning("club %s: could not read its email credits", club_id, exc_info=True)
        return None


async def status_out(session, club_id: int, *, manage: bool, mail: bool, owner: bool, impersonated: bool, actor=None) -> dict:
    """The Mail section's status: whether it is open on this server, what
    the plan allows, the address and its old ones, the lapse, and what the
    viewer may do. No mail content."""
    allowed = await plan_allows(session, club_id, actor=actor)
    mailbox = await mailbox_of(session, club_id) if on() else None
    current = await current_address(session, club_id) if mailbox is not None else None
    state = lapse_state(mailbox, allowed)
    lapsed_at = _aware(mailbox.lapsed_at) if mailbox is not None else None
    if mailbox is not None and not allowed and lapsed_at is None:
        lapsed_at = _now()
    out = {
        "available": on(),
        "domain": domain(),
        "plan_allows": allowed,
        "upgrade": None if allowed else await upgrade_payload(session),
        "set_up": current is not None,
        "address": address_of(current.handle) if current is not None else None,
        "handle": current.handle if current is not None else None,
        "previous": [],
        "display_name": mailbox.display_name if mailbox is not None else None,
        "signature": (mailbox.signature if mailbox is not None else None) if mail else None,
        "retention_months": mailbox.retention_months if mailbox is not None else DEFAULT_RETENTION_MONTHS,
        "next_change_at": None,
        "state": state,
        "lapsed_at": lapsed_at if state != "open" else None,
        "readable_until": (lapsed_at + LAPSE_READABLE) if (state != "open" and lapsed_at) else None,
        "can_manage": bool(manage and on()),
        "can_mail": bool(mail and on()),
        "is_owner": bool(owner),
        "impersonated": bool(impersonated),
        "credits_left": None,
        "limits": {
            "per_hour": int(settings.MAILBOX_SEND_PER_HOUR), "per_day": int(settings.MAILBOX_SEND_PER_DAY),
            "recipients": int(settings.MAILBOX_MAX_RECIPIENTS), "attachments_mb": 10,
        },
    }
    if mailbox is not None:
        next_at = handles.next_change_at(mailbox.handle_changed_at)
        out["next_change_at"] = next_at if next_at and next_at > _now() else None
        out["previous"] = [
            {"address": address_of(a.handle), "forward_until": _aware(a.forward_until)} for a in await forwarding(session, club_id)
        ]
    if mail and on():
        out["credits_left"] = await credits_left(session, club_id)
    return out


async def admin_out(session, club_id: int) -> dict:
    """A platform admin's card: the address, whether it is open, counts.
    Never a subject, an address someone wrote from, or a word of any mail."""
    allowed = await plan_allows(session, club_id)
    mailbox = await mailbox_of(session, club_id)
    current = await current_address(session, club_id) if mailbox is not None else None
    out = {
        "club_id": club_id,
        "available": on(),
        "plan_allows": allowed,
        "set_up": current is not None,
        "address": address_of(current.handle) if current is not None else None,
        "state": lapse_state(mailbox, allowed),
        "lapsed_at": _aware(mailbox.lapsed_at) if mailbox is not None else None,
        "created_at": _aware(mailbox.created_at) if mailbox is not None else None,
        "retention_months": mailbox.retention_months if mailbox is not None else None,
        "forwarding": 0,
        "threads": 0,
        "received": 0,
        "sent": 0,
        "spam": 0,
        "failed": 0,
        "received_this_month": 0,
        "sent_this_month": 0,
        "dropped_while_closed": mailbox.dropped_count if mailbox is not None else 0,
        "last_received_at": _aware(mailbox.last_received_at) if mailbox is not None else None,
        "last_sent_at": _aware(mailbox.last_sent_at) if mailbox is not None else None,
    }
    if mailbox is None:
        return out
    out["forwarding"] = len(await forwarding(session, club_id))
    M, T = ClubMailMessages, ClubMailThreads
    month = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = (await session.execute(
        select(
            func.count().filter(M.direction == IN),
            func.count().filter(M.direction == OUT, M.status.in_(("sent", "bounced", "complained"))),
            func.count().filter(M.direction == OUT, M.status == "failed"),
            func.count().filter(M.direction == IN, M.created_at >= month),
            func.count().filter(M.direction == OUT, M.status.in_(("sent", "bounced", "complained")), M.created_at >= month),
        ).where(M.club_id == club_id)
    )).one()
    out.update(received=int(row[0]), sent=int(row[1]), failed=int(row[2]), received_this_month=int(row[3]), sent_this_month=int(row[4]))
    t = (await session.execute(
        select(func.count(), func.count().filter(T.folder == SPAM)).where(T.club_id == club_id)
    )).one()
    out.update(threads=int(t[0]), spam=int(t[1]))
    return out


# ─── The lapse ───────────────────────────────────────────────────────────


async def tell_owner(session, club_id: int, kind: str, *, subject: str, paragraphs: list[str], now: Optional[datetime] = None) -> bool:
    """A notice in the club's bell and a mail to its owner, once for this
    kind and day (the notice's dedupe key). No mail content. True when a mail went."""
    from app.api.audit import service as audit
    from app.api.club_alerts import notify
    from app.api.club_whatsapp.alerts import _send
    from app.api.subscriptions.notices import owner_address
    from app.core.after_commit import run_after_commit

    now = now or _now()
    made = await notify(
        session, club_id, f"mailbox_{kind}", title=subject[:200], body=" ".join(paragraphs[:1]),
        link="/mail/settings", data={"kind": kind}, dedupe_key=f"mailbox_{kind}:{now.date().isoformat()}",
    )
    if made is None:
        return False
    to = await owner_address(session, club_id)
    await audit.record(
        session, actor=None, action=f"{ACTION}.notice:{kind}", entity_type="club", entity_id=club_id,
        summary=subject[:255], details={"kind": kind, "emailed": bool(to)},
    )
    if not to:
        return False
    url = f"{settings.MCP_DASHBOARD_URL.rstrip('/')}/mail/settings"
    run_after_commit(session, _send, to, subject, paragraphs, ("Open Mail settings", url), None)
    return True


async def check_lapse(session, mailbox: ClubMailboxes, now: Optional[datetime] = None) -> Optional[str]:
    """Bring the mailbox's lapse in line with its plan: found lapsed (the
    owner told), opened again, or its 30 days just ended (told again).
    Returns what changed, or None. The caller commits."""
    now = now or _now()
    allowed = await plan_allows(session, mailbox.club_id)
    if allowed:
        if mailbox.lapsed_at is not None:
            mailbox.lapsed_at = mailbox.lapse_told_at = mailbox.expiry_told_at = None
            return "reopened"
        return None
    if mailbox.lapsed_at is None:
        mailbox.lapsed_at = now
        readable = (now + LAPSE_READABLE).strftime("%-d %B %Y")
        await tell_owner(
            session, mailbox.club_id, "lapsed", now=now,
            subject="Your plan no longer includes the club mailbox",
            paragraphs=[
                "Mail sent to the club's address is no longer accepted, and nothing can be sent from it.",
                f"The mail already there stays readable in Mail until {readable}. After that you can export it, "
                "or delete it, from Mail settings.",
                "Upgrade to Pro or Enterprise to open the mailbox again, as it was.",
            ],
        )
        mailbox.lapse_told_at = now
        return "lapsed"
    if mailbox.expiry_told_at is None and now >= _aware(mailbox.lapsed_at) + LAPSE_READABLE:
        await tell_owner(
            session, mailbox.club_id, "expired", now=now,
            subject="The club mailbox's 30 days are over: export or delete its mail",
            paragraphs=[
                "The mailbox's mail can no longer be read in the dashboard.",
                "The club's own account can download all of it (an mbox file any mail program opens), or delete it, "
                "from Mail settings. Otherwise it is deleted as its retention period ends.",
            ],
        )
        mailbox.expiry_told_at = now
        return "expired"
    return None
