"""Addresses we no longer mail: one list for the whole platform.

A hard bounce or a spam complaint used to mark one registration only, so the
same dead or unwilling address went on getting mail from every other event
and club -- and each of those bounces costs the sending domain's standing
with SES and Resend. Now the provider's word lands here, keyed by the
address in lower case, and every event mail looks here first:

- an audience (`emails.service.audience_query`) leaves the address out;
- a ticket, certificate or message about to go is skipped, with the reason.

Account mail -- a password reset, a verification code, a sign-in alert, an
invitation -- still goes. The person asked for it a moment ago, and holding
it back would lock them out of their own account with no way to correct the
address themselves; the provider's own suppression list still stands behind
it. The transport logs a warning when it sends one to an address on this
list (`warn_sync`), so a support thread can see why it may not have arrived.

Nothing here removes an address: that is a decision for a person (and a row
deleted by hand) once the mailbox is known to work again.

Whole domains are off the list too (settings.MAIL_SUPPRESS_DOMAINS): the
demo's invented people live at demo.myotherapp.in, and no mail of any kind,
account mail included, goes there. The transport drops those recipients
before anything is sent (`transport.deliver`), audiences leave them out,
and a ticket or message for one is skipped with its reason.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import CheckConstraint, Column, String, exists, false, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

logger = logging.getLogger(__name__)

BOUNCE = "bounce"
COMPLAINT = "complaint"
#: The reason a recipient at a suppressed domain carries (never stored).
DOMAIN = "domain"


class EmailSuppressions(AbstractSQLModel):
    __tablename__ = "email_suppressions"
    __table_args__ = (CheckConstraint("email = lower(email)", name="ck_email_suppressions_lower"),)

    # Lower case, trimmed: the key every lookup uses.
    email = Column(String(320), primary_key=True)
    # bounce | complaint
    reason = Column(String(20), nullable=False)
    # The provider's words, for whoever is asked why.
    detail = Column(String(300), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


def normal(address: Optional[str]) -> str:
    return (address or "").strip().lower()


async def add(session, address: Optional[str], reason: str, detail: str = "") -> bool:
    """Put an address on the list. The first reason stands; a second report is a no-op."""
    email = normal(address)
    if not email or "@" not in email:
        return False
    await session.execute(
        pg_insert(EmailSuppressions.__table__)
        .values(email=email[:320], reason=reason, detail=(detail or "")[:300] or None, created_at=datetime.now(timezone.utc))
        .on_conflict_do_nothing(index_elements=["email"])
    )
    return True


def domains() -> frozenset[str]:
    from app.config import settings

    return settings.mail_suppress_domains


def at_suppressed_domain(address: Optional[str]) -> bool:
    """Whether the address is at a domain no mail goes to, or a subdomain of one."""
    email = normal(address)
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    return any(domain == d or domain.endswith("." + d) for d in domains())


def domain_suppressed(column):
    """SQL: the address in `column` is at a domain no mail goes to, or a subdomain of one."""
    listed = sorted(domains())
    if not listed:
        return false()
    domain = func.lower(func.split_part(func.trim(column), "@", 2))
    return or_(domain.in_(listed), *(domain.like("%." + d.replace("%", "").replace("_", r"\_")) for d in listed))


async def reasons(session, addresses: Iterable[Optional[str]]) -> dict[str, EmailSuppressions]:
    """The suppressed ones among these addresses, by lower-case address. An
    address at a suppressed domain answers a row made here (reason DOMAIN),
    never stored."""
    wanted = sorted({normal(a) for a in addresses if normal(a)})
    if not wanted:
        return {}
    out = {a: EmailSuppressions(email=a, reason=DOMAIN, detail=a.rsplit("@", 1)[1]) for a in wanted if at_suppressed_domain(a)}
    rest = [a for a in wanted if a not in out]
    if rest:
        rows = await session.scalars(select(EmailSuppressions).where(EmailSuppressions.email.in_(rest)))
        out.update({r.email: r for r in rows})
    return out


def not_suppressed(column):
    """A WHERE clause: the address in `column` is not on the list (the
    primary key's lookup) and not at a suppressed domain."""
    return ~exists().where(EmailSuppressions.email == func.lower(func.trim(column))) & ~domain_suppressed(column)


def why(row: EmailSuppressions) -> str:
    """The skip reason a desk reads."""
    if row.reason == DOMAIN:
        return f"not sent: {row.detail or 'that domain'} is a demo address, which is never mailed"[:400]
    what = "marked our mail as spam" if row.reason == COMPLAINT else "bounced"
    when = row.created_at.strftime("%-d %b %Y") if row.created_at else ""
    return f"not sent: that address {what}{' on ' + when if when else ''} — use another address"[:400]


# ─── The transport's warning, from a worker thread ─────────────────────────
#
# The transport runs on a worker thread, with no event loop to ask the async
# pool from, so it asks on a small synchronous engine of its own: one
# connection kept, one more at a pinch. Only mail that did not look here
# already (account mail, alerts) pays the lookup.

_engine = None
_engine_lock = threading.Lock()


def _sync_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from sqlalchemy import create_engine

                from app.config import settings

                _engine = create_engine(
                    settings.DATABASE_URL_SYNC, pool_size=1, max_overflow=1, pool_pre_ping=True, pool_recycle=1800, pool_timeout=2,
                )
    return _engine


def lookup_sync(addresses: Iterable[str]) -> dict[str, str]:
    """{address: reason} for the suppressed ones. Blocking; for a worker thread."""
    wanted = sorted({normal(a) for a in addresses if normal(a)})
    if not wanted:
        return {}
    with _sync_engine().connect() as conn:
        rows = conn.execute(
            select(EmailSuppressions.email, EmailSuppressions.reason).where(EmailSuppressions.email.in_(wanted))
        ).all()
    return {email: reason for email, reason in rows}


def masked(address: str) -> str:
    local, _, domain = normal(address).partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def warn_sync(addresses: Iterable[str], subject: str) -> dict[str, str]:
    """Log a warning for each suppressed address this mail is about to reach. Never raises."""
    try:
        found = lookup_sync(addresses)
    except Exception:  # noqa: BLE001 - a warning must not stop a mail
        logger.debug("suppression lookup failed", exc_info=True)
        return {}
    for address, reason in found.items():
        logger.warning("mail %r goes to %s although it is suppressed (%s)", (subject or "")[:80], masked(address), reason)
    return found
