"""Mail arriving for the platform's own addresses (admin@, developer@,
developers@, info@): kept in the admin portal's Mail.

Both mailbox hooks ask here first, before the team's desk and the clubs
(club_mailbox/router.py). For each envelope recipient that is a platform
address (plus-addressing reaches its base):

- a virus (SES's verdict): dropped unread, logged;
- spam (SES's verdict) or a DMARC failure (Cloudflare's, or SES's under a
  reject or quarantine policy): kept, in Spam;
- otherwise kept: parsed once (club_mailbox/parse.py), its HTML sanitised,
  its files in the private folder `platform-mail/`, and the conversation
  found by its headers or its subject (as the club mailbox does,
  club_mailbox/threads.py), within the mailbox it came to.

A mail to two platform addresses is kept in each; the same mail delivered
again is kept once per mailbox (`uq_platform_mail_messages_provider`).
Nothing a mail says reaches a log: counts, the provider's id at most.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.api.club_mailbox import parse, providers, sanitise, store, threads
from app.api.club_mailbox.models import ARCHIVE, IN, INBOX, RECEIVED, SENT, SPAM
from app.api.platform_mail import mail, service
from app.api.platform_mail.models import PlatformMailAttachments, PlatformMailMessages, PlatformMailThreads

logger = logging.getLogger(__name__)


@dataclass
class Outcome:
    #: (mailbox, thread id) of each copy kept.
    kept: list[tuple[str, int]] = field(default_factory=list)
    dropped: dict = field(default_factory=dict)
    duplicate: int = 0
    #: Whether any recipient was a platform address at all.
    addressed: bool = False

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def targets_of(recipients: list[str]) -> dict[str, str]:
    """{mailbox: the address it came to} for the platform's recipients."""
    out: dict[str, str] = {}
    for recipient in recipients:
        mailbox = service.mailbox_for(recipient)
        if mailbox is not None:
            out.setdefault(mailbox, (recipient or "").strip().lower()[:320])
    return out


async def receive(session, received: providers.Received, reader: Callable[[providers.Received], bytes]) -> Outcome:
    """Keep the mail in each platform mailbox it is for. Each copy is
    committed by itself. Raises providers.Unreadable when the raw mail
    can't be read now (the hook answers so it comes again)."""
    out = Outcome()
    targets = targets_of(received.recipients)
    if not targets:
        return out
    out.addressed = True
    if received.virus:
        logger.warning("platform mail: mail %s dropped: SES found a virus", received.provider_message_id[:24])
        out.drop("virus")
        return out
    try:
        raw = await asyncio.to_thread(reader, received)
    except providers.TooLarge:
        logger.warning("platform mail: mail %s dropped: larger than APP_MAILBOX_INBOUND_MAX_MB", received.provider_message_id[:24])
        out.drop("too_large")
        return out
    try:
        parsed = parse.parse(raw)
    except Exception as exc:  # noqa: BLE001 - a mail that can't be read at all
        logger.warning("platform mail: mail %s couldn't be parsed (%s); dropped", received.provider_message_id[:24], type(exc).__name__)
        out.drop("unparseable")
        return out
    for mailbox, delivered_to in targets.items():
        thread_id = await keep(session, mailbox, delivered_to, received, parsed, size=len(raw))
        if thread_id is None:
            out.duplicate += 1
        else:
            out.kept.append((mailbox, thread_id))
    return out


async def by_headers(session, mailbox: str, ids: list[str], *, spam: bool) -> Optional[PlatformMailThreads]:
    """The thread of this mailbox's message with one of these Message-IDs
    (the nearest reference first), in the same kind of folder."""
    if not ids:
        return None
    M, T = PlatformMailMessages, PlatformMailThreads
    wanted = list(dict.fromkeys(ids))[-50:]
    rows = (await session.execute(
        select(M.thread_id, M.message_id, M.alt_message_id)
        .join(T, T.id == M.thread_id)
        .where(M.mailbox == mailbox, or_(M.message_id.in_(wanted), M.alt_message_id.in_(wanted)),
               (T.folder == SPAM) if spam else (T.folder != SPAM))
    )).all()
    if not rows:
        return None
    rank = {mid: i for i, mid in enumerate(wanted)}
    best = max(rows, key=lambda r: max(rank.get(r.message_id, -1), rank.get(r.alt_message_id, -1)))
    return await session.get(T, best.thread_id)


async def by_subject(session, mailbox: str, subject: Optional[str], sender: Optional[str], *, spam: bool,
                     now: Optional[datetime] = None) -> Optional[PlatformMailThreads]:
    """The mailbox's latest thread with this subject, its sender among its
    people, written in within the club mailbox's SUBJECT_WINDOW."""
    key = threads.subject_key(subject)
    sender = (sender or "").strip().lower()
    if not key or not sender:
        return None
    now = now or _now()
    T = PlatformMailThreads
    rows = (await session.scalars(
        select(T).where(
            T.mailbox == mailbox, T.subject_key == key, T.last_message_at >= now - threads.SUBJECT_WINDOW,
            (T.folder == SPAM) if spam else (T.folder != SPAM),
        ).order_by(T.last_message_at.desc(), T.id.desc()).limit(20)
    )).all()
    for thread in rows:
        if sender in (thread.participants or []):
            return thread
    return None


async def keep(session, mailbox: str, delivered_to: str, received: providers.Received, parsed: parse.Parsed, *,
               size: int = 0) -> Optional[int]:
    """Keep one mailbox's copy and commit it. Returns its thread's id, or
    None when it was kept before (a delivery again)."""
    M = PlatformMailMessages
    seen = await session.scalar(
        select(M.id).where(M.mailbox == mailbox, M.provider == received.provider, M.provider_message_id == received.provider_message_id)
    )
    if seen is not None:
        return None
    now = _now()
    people = threads.outside([parsed.from_address, *parsed.to, *parsed.cc], service.own_addresses())
    spam = received.spam
    thread = await by_headers(session, mailbox, [*parsed.references, *([parsed.in_reply_to] if parsed.in_reply_to else [])], spam=spam)
    if thread is None:
        thread = await by_subject(session, mailbox, parsed.subject, parsed.from_address, spam=spam, now=now)
    keys: list[str] = []
    try:
        async with session.begin_nested():
            if thread is None:
                thread = PlatformMailThreads(
                    mailbox=mailbox, folder=SPAM if spam else INBOX, subject=parsed.subject or None,
                    subject_key=threads.subject_key(parsed.subject) or None, participants=people,
                )
                session.add(thread)
                await session.flush()
            else:
                merged = list(thread.participants or [])
                merged += [p for p in people if p not in merged]
                thread.participants = merged[:100]
                if thread.folder in (SENT, ARCHIVE):
                    thread.folder = INBOX
            text = parsed.text or ""
            msg = M(
                thread_id=thread.id, mailbox=mailbox, direction=IN, status=RECEIVED,
                message_id=parsed.message_id, in_reply_to=parsed.in_reply_to, references=" ".join(parsed.references) or None,
                provider=received.provider, provider_message_id=received.provider_message_id, delivered_to=delivered_to,
                from_address=parsed.from_address, from_name=parsed.from_name, to_addresses=parsed.to, cc_addresses=parsed.cc,
                reply_to=parsed.reply_to, subject=parsed.subject or None, text_body=text or None,
                html_body=parsed.html or None, has_remote_images=sanitise.has_remote_images(parsed.html),
                snippet=sanitise.snippet(text) or None, verdicts=received.verdicts or None, size_bytes=size,
                unread=True, sent_at=parsed.date or now, created_at=now,
            )
            session.add(msg)
            await session.flush()
            for n, part in enumerate(parsed.attachments):
                key = store.platform_key_for(msg.id, n)
                await asyncio.to_thread(store.put, key, part.content, part.content_type)
                keys.append(key)
                session.add(PlatformMailAttachments(
                    message_id=msg.id, filename=part.filename, content_type=part.content_type, size_bytes=len(part.content),
                    storage_key=key, content_id=part.content_id, inline=part.inline,
                ))
            await session.flush()
            await mail.recount(session, thread)
        await session.commit()
    except IntegrityError:
        # Kept by a delivery running beside this one.
        await session.rollback()
        store.delete(keys)
        return None
    except Exception:
        await session.rollback()
        store.delete(keys)
        raise
    return thread.id
