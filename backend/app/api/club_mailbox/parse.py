"""A received mail's raw MIME, read into what the mailbox keeps.

Python's own parser (email, policy.default) decodes the headers -- RFC 2047
words, in any charset -- and each part's transfer encoding. A part whose
charset is unknown or lies is read as UTF-8 with replacement characters,
never refused: a mail is kept even when part of it is garbled.

What comes out: the threading headers (Message-ID, In-Reply-To, References),
who from and to, the subject, the date, the text (or the HTML's words when
there is no text part), the HTML sanitised (sanitise.py), and every file --
attached ones, and pictures the HTML shows by Content-ID.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

from app.api.club_mailbox import sanitise

logger = logging.getLogger(__name__)

#: At most this much text is kept of a mail's body.
MAX_TEXT = 1_000_000
#: At most this many files are kept of one mail.
MAX_ATTACHMENTS = 50
_MESSAGE_ID = re.compile(r"<[^<>\s]{1,398}>")


@dataclass
class Part:
    filename: Optional[str]
    content_type: str
    content: bytes
    content_id: Optional[str] = None
    inline: bool = False


@dataclass
class Parsed:
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: list[str] = field(default_factory=list)
    from_address: Optional[str] = None
    from_name: Optional[str] = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    reply_to: Optional[str] = None
    subject: str = ""
    date: Optional[datetime] = None
    text: str = ""
    html: str = ""
    attachments: list[Part] = field(default_factory=list)
    #: Parts left out: past MAX_ATTACHMENTS.
    skipped: int = 0


def _header(msg, name: str) -> str:
    """A header as text, never raising on a malformed one."""
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 - a broken header is an empty one
        return ""
    return str(value or "").strip()


def message_ids(value: Optional[str]) -> list[str]:
    """The <...> ids in a Message-ID, In-Reply-To or References header, in order."""
    return _MESSAGE_ID.findall(value or "")


def _addresses(msg, name: str) -> list[tuple[str, str]]:
    """(name, address) pairs, the address lower-cased; malformed ones skipped."""
    try:
        values = msg.get_all(name) or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for display, address in getaddresses([str(v) for v in values]):
        address = (address or "").strip().lower()
        if "@" in address and len(address) <= 320 and " " not in address:
            out.append(((display or "").strip()[:200], address))
    return out


def _text_of(part) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ""
    except Exception:  # noqa: BLE001 - an unknown or lying charset
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")


def _bytes_of(part) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        payload = None
    if payload is None:
        content = part.get_payload()
        if isinstance(content, str):
            return content.encode("utf-8", errors="replace")
        if isinstance(content, list):
            # A message attached whole (message/rfc822): kept as it was sent.
            try:
                return part.as_bytes(policy=policy.default)
            except Exception:  # noqa: BLE001
                return b""
        return b""
    return payload


def _filename(part) -> Optional[str]:
    try:
        name = part.get_filename()
    except Exception:  # noqa: BLE001
        name = None
    if not name:
        return None
    name = "".join(ch for ch in str(name) if ch.isprintable() and ch not in "\\/\r\n").strip()
    return name[:255] or None


def _date(msg) -> Optional[datetime]:
    raw = _header(msg, "Date")
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
    except Exception:  # noqa: BLE001
        return None
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse(raw: bytes) -> Parsed:
    """The mail in `raw`. Never raises on what a sender wrote: a part that
    can't be read is left out."""
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
    out = Parsed()
    ids = message_ids(_header(msg, "Message-ID"))
    out.message_id = ids[0] if ids else None
    reply = message_ids(_header(msg, "In-Reply-To"))
    out.in_reply_to = reply[0] if reply else None
    out.references = message_ids(_header(msg, "References"))[-50:]
    sender = _addresses(msg, "From")
    if sender:
        out.from_name, out.from_address = sender[0][0] or None, sender[0][1]
    out.to = [a for _, a in _addresses(msg, "To")][:200]
    out.cc = [a for _, a in _addresses(msg, "Cc")][:200]
    replies = _addresses(msg, "Reply-To")
    out.reply_to = replies[0][1] if replies else None
    out.subject = " ".join(_header(msg, "Subject").split())[:300]
    out.date = _date(msg)

    text_part = html_part = None
    try:
        text_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
    except Exception:  # noqa: BLE001
        pass
    if text_part is not None:
        out.text = _text_of(text_part)[:MAX_TEXT]
    if html_part is not None:
        html = _text_of(html_part)
        out.html = sanitise.clean(html)
        if not out.text:
            out.text = sanitise.to_text(html, MAX_TEXT)

    bodies = {id(p) for p in (text_part, html_part) if p is not None}
    for part in msg.walk():
        if part.is_multipart() or id(part) in bodies:
            continue
        ctype = (part.get_content_type() or "application/octet-stream").lower()
        disposition = (part.get_content_disposition() or "").lower()
        cid = _header(part, "Content-ID").strip().strip("<>")[:200] or None
        name = _filename(part)
        # A bare text part that is not the body (a second text/plain, say) is a file only when named or attached.
        if ctype.startswith("text/") and disposition != "attachment" and not name:
            continue
        if len(out.attachments) >= MAX_ATTACHMENTS:
            out.skipped += 1
            continue
        content = _bytes_of(part)
        out.attachments.append(Part(
            filename=name, content_type=ctype[:120], content=content, content_id=cid,
            inline=bool(cid) and disposition != "attachment" and ctype.startswith("image/"),
        ))
    return out


def address_line(name: Optional[str], address: Optional[str]) -> str:
    """"Name <address>", or the address alone."""
    if not address:
        return name or ""
    if not name:
        return address
    try:
        return str(Address(display_name=name, addr_spec=address))
    except Exception:  # noqa: BLE001
        return address
