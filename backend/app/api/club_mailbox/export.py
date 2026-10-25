"""All of a club's mail as one mbox file, for its own account to keep.

What a lapsed mailbox's owner does once its 30 days are over (or any time):
download everything -- every thread's mail, received and sent, with its
files -- as an mbox (mboxrd: a line starting "From " inside a mail is
escaped), which Thunderbird, Apple Mail and most mail programs import.
Each mail is written again from what was kept (the raw MIME is not kept):
its headers, its text, its HTML and its files.

Streamed a batch of mail at a time, so a large mailbox is never held whole.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from typing import AsyncIterator

from sqlalchemy import select

from app.api.club_mailbox import mail, parse, store
from app.api.club_mailbox.models import ClubMailMessages

BATCH = 100
_FROM_LINE = re.compile(rb"^(>*From )", re.M)


def to_mime(m: ClubMailMessages, files: list[tuple[str, str, bytes]]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = parse.address_line(m.from_name, m.from_address) or "unknown@invalid"
    if m.to_addresses:
        msg["To"] = ", ".join(m.to_addresses)
    if m.cc_addresses:
        msg["Cc"] = ", ".join(m.cc_addresses)
    msg["Subject"] = m.subject or ""
    when = m.sent_at or m.created_at or datetime.now(timezone.utc)
    msg["Date"] = format_datetime(when if when.tzinfo else when.replace(tzinfo=timezone.utc))
    if m.message_id:
        msg["Message-ID"] = m.message_id
    if m.in_reply_to:
        msg["In-Reply-To"] = m.in_reply_to
    if m.references:
        msg["References"] = m.references
    msg["X-MyOtherApp-Direction"] = "received" if m.direction == "in" else "sent"
    msg.set_content(m.text_body or "")
    if m.html_body:
        msg.add_alternative(m.html_body, subtype="html")
    for filename, ctype, content in files:
        main, _, sub = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(content, maintype=main or "application", subtype=sub or "octet-stream", filename=filename)
    return msg


def mbox_entry(m: ClubMailMessages, files: list[tuple[str, str, bytes]]) -> bytes:
    when = m.sent_at or m.created_at or datetime.now(timezone.utc)
    sender = (m.from_address or "MAILER-DAEMON").replace(" ", "")
    head = f"From {sender} {when.astimezone(timezone.utc).strftime('%a %b %d %H:%M:%S %Y')}\n".encode()
    body = to_mime(m, files).as_bytes().replace(b"\r\n", b"\n")
    return head + _FROM_LINE.sub(rb">\1", body) + b"\n"


async def stream(session, club_id: int) -> AsyncIterator[bytes]:
    after = 0
    while True:
        rows = (await session.scalars(
            select(ClubMailMessages).where(ClubMailMessages.club_id == club_id, ClubMailMessages.id > after)
            .order_by(ClubMailMessages.id).limit(BATCH)
        )).all()
        if not rows:
            return
        files_of = await mail.attachments_of(session, club_id, [r.id for r in rows])
        for m in rows:
            files = []
            for a in files_of.get(m.id, []):
                content = await asyncio.to_thread(store.get, a.storage_key)
                if content is not None:
                    files.append((a.filename or "attachment", a.content_type or "application/octet-stream", content))
            yield mbox_entry(m, files)
        after = rows[-1].id
