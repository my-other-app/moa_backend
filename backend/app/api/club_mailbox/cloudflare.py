"""Mail that Cloudflare Email Routing received for the mailbox domain.

The domain's MX is Cloudflare's; its catch-all rule hands every mail to the
`club-mail` Worker (infra/cloudflare/club-mail in the app repo), which posts
the raw MIME here, one recipient at a time (Email Routing runs the Worker
once per envelope recipient), and does what the answer says: accept, or
reject in the SMTP session (the sender's own server tells its sender, so a
forged sender gets no backscatter). Anything else -- a 5xx, no answer -- and
the Worker throws, Cloudflare gives the sending server a temporary error,
and the mail comes again later; nothing is kept twice.

The delivery is signed: HMAC-SHA256, with the key both sides hold
(settings.MAILBOX_HOOK_SECRET; the Worker's MAILBOX_HOOK_SECRET secret), over

    v1 \\n <unix time> \\n <envelope to> \\n <envelope from> \\n <size> \\n <auth results>

(each value as the Worker sent it in its header: percent-encoded), so it is
checked before the body is read, and a delivery older than five minutes is
refused. The body's length must be the signed size.

Cloudflare gives no spam or virus verdict. It does check SPF, DKIM and DMARC
and prepends its own Authentication-Results (authserv-id mx.cloudflare.net)
before the Worker runs (Cloudflare's "Workers API" page); the Worker passes
that header on, and a mail that failed DMARC goes to the club's Spam.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from email import policy
from email.parser import BytesHeaderParser
from typing import Optional
from urllib.parse import unquote

from app.api.club_mailbox import providers
from app.config import settings

CLOUDFLARE = "cloudflare"
VERSION = "v1"
#: How old a signed delivery may be (seconds), either way.
WINDOW = 300
#: Cloudflare's own Authentication-Results (its authserv-id).
AUTHSERV = "mx.cloudflare.net"

H_TIME = "x-moa-timestamp"
H_TO = "x-moa-envelope-to"
H_FROM = "x-moa-envelope-from"
H_SIZE = "x-moa-size"
H_AUTH = "x-moa-auth-results"
H_SIGNATURE = "x-moa-signature"


def secret() -> str:
    return (settings.MAILBOX_HOOK_SECRET or "").strip()


def signed_text(timestamp: str, to: str, sender: str, size: str, auth: str) -> str:
    return "\n".join((VERSION, timestamp, to, sender, size, auth))


def sign(key: str, timestamp: str, to: str, sender: str, size: str, auth: str) -> str:
    return hmac.new(key.encode(), signed_text(timestamp, to, sender, size, auth).encode(), hashlib.sha256).hexdigest()


class Delivery:
    """One signed delivery's envelope, as the Worker sent it."""

    def __init__(self, headers) -> None:
        self.timestamp = (headers.get(H_TIME) or "").strip()
        self.to_raw = headers.get(H_TO) or ""
        self.from_raw = headers.get(H_FROM) or ""
        self.size_raw = (headers.get(H_SIZE) or "").strip()
        self.auth_raw = headers.get(H_AUTH) or ""
        self.signature = (headers.get(H_SIGNATURE) or "").strip().lower()

    @property
    def size(self) -> int:
        return int(self.size_raw) if self.size_raw.isdigit() else -1

    @property
    def to(self) -> str:
        return unquote(self.to_raw).strip().lower()[:320]

    @property
    def sender(self) -> str:
        return unquote(self.from_raw).strip().lower()[:320]

    @property
    def auth(self) -> str:
        return unquote(self.auth_raw)[:4000]

    def verify(self, now: Optional[float] = None) -> Optional[str]:
        """None when it is signed with our key and recent; else why not."""
        key = secret()
        if not key:
            return "not_set_up"
        if not self.timestamp.isdigit() or not self.signature or self.size < 0 or "@" not in self.to:
            return "unsigned"
        now = time.time() if now is None else now
        if abs(now - int(self.timestamp)) > WINDOW:
            return "stale"
        expected = sign(key, self.timestamp, self.to_raw, self.from_raw, self.size_raw, self.auth_raw)
        if not hmac.compare_digest(expected, self.signature):
            return "signature"
        return None


_COMMENT = re.compile(r"\([^()]*\)")
_NEXT_HEADER = re.compile(r",\s*[A-Za-z0-9][A-Za-z0-9.-]*\s*;")
_RESULT = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.I)


def verdicts_from(auth: str) -> dict:
    """{"spf": "PASS", "dkim": "FAIL", "dmarc": "PASS"} from Cloudflare's own
    Authentication-Results (the first; the Worker's Headers joins any others
    after it with commas, and those, the sender's, are not read). {} when
    the first isn't Cloudflare's."""
    value = _COMMENT.sub("", auth or "").strip()
    head, sep, rest = value.partition(";")
    if not sep or head.strip().lower() != AUTHSERV:
        return {}
    cut = _NEXT_HEADER.search(rest)
    ours = rest[: cut.start()] if cut else rest
    found: dict[str, str] = {}
    for method, result in _RESULT.findall(ours):
        method, result = method.lower(), result.upper()[:24]
        # Several DKIM signatures: one that passes is enough.
        if method == "dkim" and found.get("dkim") == "PASS":
            continue
        found[method] = result
    return found


def message_key(raw: bytes, sender: str) -> str:
    """The provider's id for a mail: its Message-ID with the envelope's
    sender, so the same mail sent again (the sending server retrying after a
    temporary error) is kept once; without a Message-ID, its bytes."""
    try:
        headers = BytesHeaderParser(policy=policy.compat32).parsebytes(raw[: 256 * 1024])
        message_id = str(headers.get("Message-ID") or "").strip()
    except Exception:  # noqa: BLE001 - a mail with headers we can't read: its bytes, then
        message_id = ""
    basis = f"{message_id}|{sender}".encode() if message_id else raw
    return "cf-" + hashlib.sha256(basis).hexdigest()[:48]


def received(delivery: Delivery, raw: bytes) -> providers.Received:
    verdicts = verdicts_from(delivery.auth)
    return providers.Received(
        provider=CLOUDFLARE, provider_message_id=message_key(raw, delivery.sender), recipients=[delivery.to],
        spam=verdicts.get("dmarc") == "FAIL", virus=False, verdicts=verdicts,
    )


def reader_for(raw: bytes):
    """The inbound pipeline's reader: the mail is already here."""
    limit = int(settings.MAILBOX_INBOUND_MAX_MB or 40) * 1024 * 1024

    def read(_received: providers.Received) -> bytes:
        if len(raw) > limit:
            raise providers.TooLarge(f"{len(raw)} bytes")
        return raw

    return read


#: What the Worker says to the sending server, by why the mail wasn't kept.
REJECT_TEXT = {
    "unknown": "No mailbox here by that name",
    "closed": "This mailbox is not receiving mail",
    "off": "This address does not receive mail",
    "too_large": "Message too large for this mailbox",
    "unparseable": "Message could not be read",
    "virus": "Message refused",
}


def decision(desk: dict, outcome) -> tuple[str, Optional[str]]:
    """("accept", None) when the desk or a club kept it (or had it already);
    else ("reject", why)."""
    desk_kept = any(k in desk for k in ("support", "sales"))
    if desk_kept or outcome.kept or outcome.duplicate:
        return "accept", None
    reasons = list(outcome.dropped) + ([desk["dropped"]] if desk.get("dropped") else [])
    for why in ("too_large", "unparseable", "virus", "closed", "off", "unknown"):
        if why in reasons:
            return "reject", why
    return "reject", "unknown"
