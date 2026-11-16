"""A club's address: the handle before @myotherapp.in (settings.MAILBOX_DOMAIN).

3 to 30 characters of a-z, 0-9, dots and hyphens; it starts and ends with a
letter or a digit, and has no two dots or hyphens in a row. Lower case: what
the club types is lowered. Names that are the platform's own, or that people
expect a postmaster to read (RFC 2142's), are reserved for no club. The
domain is the platform's own, so every address the platform itself uses on
it (info@, support@, the desk's inbound ones, ...) is reserved too, read from
the settings each time.

Unique across every club, for good: a changed one forwards into the same
mailbox for 90 days, then stops receiving mail, and is never another
club's (the club itself may take it back). Changed at most once a month.

A new or changed handle (`new_problem`, service.new_handle_problem) must
also not read as the platform speaking -- no `LOOKALIKE` word in it, dots,
hyphens and look-alike digits aside (`supp0rt`, `my-other-app`) -- and not be
the address of an account or a team member on the domain (`account_locals`).
Handles clubs already have keep working: `problem` alone decides where mail
goes.

Every club has one from the start (`from_name`, service.ensure_address):
the letters and digits of its name, lower case -- "GDG Cloud Trivandrum"
is `gdgcloudtrivandrum` -- or `club<id>` when its name has fewer than three
of them or reads as the platform; taken or reserved, `gdgcloudtrivandrum2`,
`...3`, each cut to fit 30.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from sqlalchemy import func, or_, select, union

from app.api.club_mailbox.models import ClubMailboxAddresses
from app.response import CustomHTTPException

MIN_LENGTH = 3
MAX_LENGTH = 30
PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
#: A changed handle forwards into the same mailbox this long (then it
#: receives nothing, and stays the club's).
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
    # The platform's own on myotherapp.in: its people, its products, its stores.
    "appreview", "app-review", "review", "reviews", "developer", "developers", "dev", "devs", "hello", "hi", "partner",
    "partners", "partnerships", "events", "event", "clubs", "club", "community", "founder", "founders", "ceo", "cto",
    "coo", "hr", "people", "finance", "accounting", "invoice", "invoices", "receipt", "receipts", "ticket", "tickets",
    "order", "orders", "status", "updates", "newsletter", "digest", "reply", "replies", "inbox", "mailbox", "dashboard",
    "portal", "app", "apps", "ios", "android", "store", "shop", "merch", "docs", "blog", "media", "cdn", "static",
    "assets", "feedback", "report", "reports", "trust", "safety", "ops", "devops", "it", "tech", "google", "apple",
    "razorpay", "whatsapp", "meta", "facebook", "instagram", "linkedin", "twitter", "domains", "domain", "dns",
    "hostmaster", "certificates", "certificate", "speakers", "speaker", "volunteer", "volunteers", "sponsor", "sponsors",
    "myotherapp-team", "theteam", "mail-daemon", "auto", "automated", "robot", "bot",
})


#: No new handle contains one of these: they read as the platform speaking.
LOOKALIKE = (
    "myotherapp", "finnet", "support", "security", "verif", "noreply", "donotreply", "billing", "payment", "admin",
    "postmaster", "mailerdaemon", "helpdesk", "password", "refund",
)
#: Ordinary words that hold one of those, taken out before looking.
INNOCENT = ("badminton",)
#: Digits read as the letters they stand in for (1 as both i and l).
_AS_LETTERS = (str.maketrans("013457", "oieast"), str.maketrans("013457", "oleast"))
RESERVED_WORDS = "That address is kept for the platform. Pick another."


def lookalike(handle: str) -> bool:
    """Whether the handle reads as the platform: a LOOKALIKE word in it."""
    bare = normal(handle).replace(".", "").replace("-", "")
    for word in INNOCENT:
        bare = bare.replace(word, "")
    return any(word in form for form in (bare, *(bare.translate(t) for t in _AS_LETTERS)) for word in LOOKALIKE)


#: No new sender name holds these: a club's mail can't be signed as the platform.
PLATFORM_NAMES = ("myotherapp", "finnet")


def platform_name(name: Optional[str]) -> bool:
    """Whether a sender name reads as the platform's ("MyOtherApp Support",
    "My Other App", "My0therApp"): its letters and digits, lower case."""
    bare = "".join(ch for ch in unicodedata.normalize("NFKD", name or "").lower() if ch.isascii() and ch.isalnum())
    return any(word in form for form in (bare, *(bare.translate(t) for t in _AS_LETTERS)) for word in PLATFORM_NAMES)


def _platform_locals() -> frozenset[str]:
    """The local parts of every address the platform itself uses on the
    mailbox domain: who its mail is from, the desk's inbound addresses, the
    admin inbox's (PLATFORM_INBOX_ADDRESSES)."""
    from app.config import settings

    domain = (settings.MAILBOX_DOMAIN or "").strip().lower()
    addresses = {
        settings.MAIL_FROM_ADDRESS, settings.SUPPORT_FROM_ADDRESS, settings.SALES_FROM_ADDRESS,
        *settings.support_inbound_addresses, *settings.sales_inbound_addresses, *settings.platform_inbox_addresses,
    }
    out = set()
    for address in addresses:
        local, at, host = (address or "").strip().lower().rpartition("@")
        if at and host == domain and local:
            out.add(local.split("+", 1)[0])
    return frozenset(out)


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
    if handle in RESERVED or handle.replace(".", "").replace("-", "") in RESERVED or handle in _platform_locals():
        return "HANDLE_RESERVED", RESERVED_WORDS
    return None


def new_problem(handle: str) -> Optional[tuple[str, str]]:
    """Why this handle can't be given now, as (code, words), or None: the
    rules every handle keeps (`problem`) and, for a new one, no look-alike."""
    found = problem(handle)
    if found is not None:
        return found
    if lookalike(handle):
        return "HANDLE_RESERVED", RESERVED_WORDS
    return None


def check(handle: str) -> str:
    """The handle, lowered, or 422 HANDLE_INVALID / HANDLE_RESERVED."""
    value = normal(handle)
    found = problem(value)
    if found is not None:
        raise _refuse(422, found[1], found[0])
    return value


async def holder(session, handle: str, now: Optional[datetime] = None) -> Optional[ClubMailboxAddresses]:
    """The address row mail to this handle reaches now: a club's current
    one, or a changed one still forwarding. None when no mail reaches it
    (nobody's, or its forwarding is over: `taken` says whose it was)."""
    now = now or datetime.now(timezone.utc)
    A = ClubMailboxAddresses
    return await session.scalar(
        select(A).where(A.handle == normal(handle), or_(A.retired_at.is_(None), A.forward_until > now))
    )


async def taken(session, handle: str) -> Optional[ClubMailboxAddresses]:
    """The address row with this handle, whatever its state: a handle that
    was ever a club's is that club's for good."""
    return await session.scalar(select(ClubMailboxAddresses).where(ClubMailboxAddresses.handle == normal(handle)))


async def account_locals(session, domain: str, *, except_user_id: Optional[int] = None) -> frozenset[str]:
    """The part before the @ (and before any +tag) of every account's and
    team member's address on `domain`: mail for those must never reach a
    club's mailbox. `except_user_id`: the club's own account, whose address
    may be the club's handle."""
    from app.api.club_access.models import ClubMembers
    from app.api.users.models import Users

    host = normal(domain)
    if not host:
        return frozenset()
    pattern = "%@" + host.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    user_email = func.lower(func.btrim(Users.email))
    member_email = func.lower(func.btrim(ClubMembers.email))
    users = select(user_email.label("email")).where(user_email.like(pattern, escape="\\"))
    if except_user_id is not None:
        users = users.where(Users.id != except_user_id)
    members = select(member_email.label("email")).where(member_email.like(pattern, escape="\\"))
    rows = (await session.scalars(union(users, members))).all()
    return frozenset(local.split("+", 1)[0] for local, _, _ in (str(r).rpartition("@") for r in rows) if local)


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


# ─── The automatic address ───────────────────────────────────────────────

#: Suffixes tried after the bare name: 2, 3, ... up to this one.
MAX_SUFFIX = 999
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def base_of(name: Optional[str], club_id: int) -> str:
    """The letters and digits of `name` (accents dropped, NFKD to ASCII),
    lower case, cut to 30; `club<id>` when fewer than three are left (a name
    in a script with no ASCII form: Malayalam, say)."""
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    base = _NOT_ALNUM.sub("", ascii_name.lower())[:MAX_LENGTH]
    return base if len(base) >= MIN_LENGTH else f"club{int(club_id)}"


def automatic_base(name: Optional[str], club_id: int) -> str:
    """What a club's automatic address starts from: `base_of` its name, or
    `club<id>` when that reads as the platform ("Support Squad")."""
    base = base_of(name, club_id)
    return f"club{int(club_id)}" if lookalike(base) else base


def candidates(base: str) -> Iterator[str]:
    """`base`, `base2`, `base3`, ... each cut so it fits 30, never one that
    can't be given (`new_problem`). Free or not is the caller's to ask."""
    for n in range(1, MAX_SUFFIX + 1):
        suffix = "" if n == 1 else str(n)
        handle = base[: MAX_LENGTH - len(suffix)] + suffix
        if new_problem(handle) is None:
            yield handle


def from_name(name: Optional[str], club_id: int) -> str:
    """The first address a club named `name` would be offered (free or not):
    "GDG Cloud Trivandrum" -> gdgcloudtrivandrum; a reserved one ("Info")
    -> info2; one that reads as the platform ("Support") or a
    Malayalam-only name -> club<id>."""
    return next(candidates(automatic_base(name, club_id)), f"club{int(club_id)}")
