"""A club's address: the handle before @mail.myotherapp.in.

3 to 30 characters of a-z, 0-9, dots and hyphens; it starts and ends with a
letter or a digit, and has no two dots or hyphens in a row. Lower case: what
the club types is lowered. Names that are the platform's own, or that people
expect a postmaster to read (RFC 2142's), are reserved for no club.

Unique across every club and every handle still forwarding (a changed one
forwards 90 days, then is free again). Changed at most once a month.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_, select

from app.api.club_mailbox.models import ClubMailboxAddresses
from app.response import CustomHTTPException

MIN_LENGTH = 3
MAX_LENGTH = 30
PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
#: A changed handle forwards into the same mailbox this long.
FORWARD_FOR = timedelta(days=90)
#: A handle changes at most once in this long.
CHANGE_EVERY = timedelta(days=30)

#: Never a club's: the mail standards' own (RFC 2142), the platform's, and
#: words people would trust as the platform speaking.
RESERVED = frozenset({
    "admin", "administrator", "postmaster", "abuse", "support", "info", "noreply", "no-reply", "donotreply",
    "do-not-reply", "billing", "security", "hostmaster", "webmaster", "mailer-daemon", "mailerdaemon", "root",
    "noc", "www", "mail", "email", "smtp", "imap", "pop", "ftp", "help", "helpdesk", "contact", "team", "staff",
    "sales", "marketing", "privacy", "legal", "compliance", "dmarc", "dkim", "spf", "bounce", "bounces",
    "notifications", "notification", "alerts", "system", "myotherapp", "moa", "finnet", "official", "verify",
    "verification", "accounts", "account", "payments", "payment", "refunds", "careers", "jobs", "press", "news",
    "test", "null", "undefined", "api", "ses", "amazonses", "unsubscribe", "list", "owner", "owners", "moderator",
})


def _refuse(status: int, message: str, code: str, **errors) -> CustomHTTPException:
    return CustomHTTPException(status_code=status, message=message, error_code=code, errors={"field": "handle", **errors})


def normal(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def problem(handle: str) -> Optional[tuple[str, str]]:
    """Why this handle can't be anyone's, as (code, words), or None."""
    if len(handle) < MIN_LENGTH or len(handle) > MAX_LENGTH:
        return "HANDLE_INVALID", f"An address is {MIN_LENGTH} to {MAX_LENGTH} characters."
    if not PATTERN.match(handle):
        return "HANDLE_INVALID", "Use letters, numbers, dots and hyphens, starting and ending with a letter or a number."
    if ".." in handle or "--" in handle or ".-" in handle or "-." in handle:
        return "HANDLE_INVALID", "No two dots or hyphens in a row."
    if handle in RESERVED or handle.replace(".", "").replace("-", "") in RESERVED:
        return "HANDLE_RESERVED", "That address is kept for the platform. Pick another."
    return None


def check(handle: str) -> str:
    """The handle, lowered, or 422 HANDLE_INVALID / HANDLE_RESERVED."""
    value = normal(handle)
    found = problem(value)
    if found is not None:
        raise _refuse(422, found[1], found[0])
    return value


async def holder(session, handle: str, now: Optional[datetime] = None) -> Optional[ClubMailboxAddresses]:
    """The address row that holds this handle now: a club's current one, or
    a changed one still forwarding. None when it is free."""
    now = now or datetime.now(timezone.utc)
    A = ClubMailboxAddresses
    return await session.scalar(
        select(A).where(A.handle == normal(handle), or_(A.retired_at.is_(None), A.forward_until > now))
    )


async def resolve(session, address: str, domain: str, now: Optional[datetime] = None) -> Optional[ClubMailboxAddresses]:
    """The address row mail to `address` belongs to, or None (not our
    domain, not a handle, nobody's, or forwarding has ended). Plus-addressing
    (`yukthi+events@...`) reaches yukthi."""
    value = normal(address)
    local, at, host = value.rpartition("@")
    if not at or host != normal(domain):
        return None
    local = local.split("+", 1)[0]
    if problem(local) is not None:
        return None
    return await holder(session, local, now)


def next_change_at(changed_at: Optional[datetime]) -> Optional[datetime]:
    if changed_at is None:
        return None
    changed_at = changed_at if changed_at.tzinfo else changed_at.replace(tzinfo=timezone.utc)
    return changed_at + CHANGE_EVERY
