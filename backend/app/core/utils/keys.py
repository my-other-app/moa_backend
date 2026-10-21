import hashlib
import re
import secrets
import time
import unicodedata

#: What a ticket id is made of: no 0/O, 1/I/L, so it reads back off paper.
TICKET_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
#: Two groups of five: 31**10 ~ 8.2e14 ids, about 49.5 bits. With ten crore
#: tickets out, a new id meets an old one about once in eight million
#: registrations, and that one is retried (registration.service). The old
#: ids had about 25 bits and no retry: they began to fail near 10**8.
TICKET_GROUP = 5
TICKET_ID = re.compile(rf"^MOA-[{TICKET_ALPHABET}]{{{TICKET_GROUP}}}-[{TICKET_ALPHABET}]{{{TICKET_GROUP}}}$")
#: The unique index a duplicate ticket id runs into.
TICKET_ID_INDEX = "ix_event_registrations_link_ticket_id"
#: The unique index a duplicate certificate number runs into.
CERTIFICATE_NO_INDEX = "uq_registrations_certificate_no"
#: What a certificate's number can be, upper case: CERT- and two groups of
#: the ticket alphabet, or, for a number made from a ticket id minted before
#: October 2026 (MOA-<hex clock>-XXXXX), one to seven hex digits first. The
#: ticket id an old certificate's QR carried (MOA-...) has the same body.
CERTIFICATE_CODE = re.compile(
    rf"^(?:CERT|MOA)-((?:[{TICKET_ALPHABET}]{{{TICKET_GROUP}}}|[0-9A-F]{{1,7}})-[{TICKET_ALPHABET}]{{{TICKET_GROUP}}})$"
)


def _group() -> str:
    return "".join(secrets.choice(TICKET_ALPHABET) for _ in range(TICKET_GROUP))


def generate_certificate_no() -> str:
    """A certificate's number: CERT-XXXXX-XXXXX, ten characters from the
    OS's CSPRNG, drawn on its own (about 49.5 bits, like a ticket id).

    The number used to be the ticket id with its prefix changed, so anyone
    reading a certificate could write down the ticket's id, which is what
    the door scans. Drawn separately, it says nothing about the ticket.
    """
    return f"CERT-{_group()}-{_group()}"


def generate_ticket_id() -> str:
    """A ticket id: MOA-XXXXX-XXXXX, ten characters from the OS's CSPRNG.

    Upper case, so the door looks one up on the unique index after
    upper-casing what it read. Ids minted before this (MOA-<hex time>-XXXXX)
    keep working: they are looked up the same way and never rewritten, and
    they are upper case too.
    """
    return f"MOA-{_group()}-{_group()}"


def violated_constraint(exc: BaseException) -> str | None:
    """The name of the constraint or unique index an IntegrityError broke,
    when the driver says (asyncpg does, as does psycopg2 through `.diag`)."""
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = getattr(cur, "constraint_name", None)
        if name:
            return name
        diag = getattr(cur, "diag", None)
        if diag is not None and getattr(diag, "constraint_name", None):
            return diag.constraint_name
        cur = getattr(cur, "orig", None) or cur.__cause__
    return None


def generate_slug(name):
    """Generate a unique slug from a given name."""
    # Normalize the name (remove accents, convert to lowercase)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("utf-8")
    name = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip().lower()  # Remove special chars
    name = re.sub(
        r"[\s-]+", "-", name
    )  # Replace spaces and multiple dashes with a single dash

    # Create a unique hash from timestamp
    unique_hash = hashlib.sha1(str(time.time()).encode()).hexdigest()[:6]

    return f"{name}-{unique_hash}"
