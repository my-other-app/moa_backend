"""Mail arriving for clubs' addresses: which club, kept or dropped, which thread.

For each envelope recipient on the mailbox domain:

- an address that is no club's (never was, or its forwarding has ended),
  or a club whose mailbox is closed (its plan lapsed, or the feature is
  off): **dropped and logged** (the address masked), never bounced -- a
  bounce to a forged sender is backscatter, and gets the domain listed;
- SES found a virus: dropped and logged, never read;
- SES called it spam (or it failed DMARC under a reject or quarantine
  policy): kept, in the club's Spam;
- otherwise kept: the raw MIME read once (providers.read), parsed
  (parse.py), its HTML sanitised, its files put in the private folder,
  and the thread found (threads.py) or started.

One mail addressed to two clubs is kept by each, separately. The same mail
delivered twice (SNS retries a notification it got no 200 for) is kept once
per club (`uq_club_mail_messages_provider`).

Nothing a mail says reaches a log: counts, the provider's id, and a masked
address at most.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.club_mailbox import alerts, handles, parse, providers, sanitise, service, store, threads
from app.api.club_mailbox.models import (
    ARCHIVE,
    IN,
    INBOX,
    RECEIVED,
    SENT,
    SPAM,
    ClubMailAttachments,
    ClubMailboxes,
    ClubMailMessages,
    ClubMailThreads,
)
from app.core.mail.suppression import masked

logger = logging.getLogger(__name__)


@dataclass
class Outcome:
    kept: list[tuple[int, int]] = field(default_factory=list)  # (club id, thread id)
    dropped: dict = field(default_factory=dict)  # reason -> count
    duplicate: int = 0

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _club_for(session, recipient: str) -> Optional[tuple[int, str]]:
    """(club id, the address it came to) for a recipient that is one of a club's addresses."""
    row = await handles.resolve(session, recipient, service.domain())
    if row is None:
        return None
    return row.club_id, service.address_of(row.handle)


async def receive(session, received: providers.Received, reader: Optional[Callable[[providers.Received], bytes]] = None) -> Outcome:
    """Keep (or drop) one received mail for each club it is addressed to.
    The caller commits nothing: each club's copy is committed here, so one
    club's failure doesn't lose another's. Raises providers.Unreadable when
    the raw mail can't be read now (the webhook answers so SNS retries)."""
    reader = reader or providers.read
    out = Outcome()
    if not service.on():
        out.drop("off")
        logger.info("mailbox: mail %s dropped (club mailboxes are off)", received.provider_message_id[:24])
        return out
    targets: dict[int, str] = {}
    for recipient in received.recipients:
        found = await _club_for(session, recipient)
        if found is None:
            if recipient.endswith("@" + service.domain()):
                logger.info("mailbox: mail for %s dropped: no such address", masked(recipient))
                out.drop("unknown")
            continue
        targets.setdefault(found[0], found[1])
    if not targets:
        return out
    if received.virus:
        for club_id in targets:
            logger.warning("mailbox: mail for club %s dropped: SES found a virus (%s)", club_id, received.provider_message_id[:24])
            out.drop("virus")
        return out
    open_targets: dict[int, str] = {}
    for club_id, address in targets.items():
        mailbox = await service.mailbox_of(session, club_id)
        if mailbox is None or not await service.plan_allows(session, club_id):
            logger.info("mailbox: mail for club %s dropped: its mailbox is closed", club_id)
            if mailbox is not None:
                mailbox.dropped_count = (mailbox.dropped_count or 0) + 1
            out.drop("closed")
            continue
        open_targets[club_id] = address
    await session.commit()
    if not open_targets:
        return out
    try:
        raw = await asyncio.to_thread(reader, received)
    except providers.TooLarge:
        for club_id in open_targets:
            logger.warning("mailbox: mail for club %s dropped: larger than APP_MAILBOX_INBOUND_MAX_MB", club_id)
            out.drop("too_large")
        return out
    try:
        parsed = parse.parse(raw)
    except Exception as exc:  # noqa: BLE001 - a mail that can't be read at all
        logger.warning("mailbox: mail %s couldn't be parsed (%s); dropped", received.provider_message_id[:24], type(exc).__name__)
        out.drop("unparseable")
        return out
    for club_id, address in open_targets.items():
        thread_id = await keep(session, club_id, address, received, parsed, size=len(raw))
        if thread_id is None:
            out.duplicate += 1
        else:
            out.kept.append((club_id, thread_id))
    return out


async def keep(session, club_id: int, delivered_to: str, received: providers.Received, parsed: parse.Parsed, *,
               size: int = 0) -> Optional[int]:
    """Keep one club's copy and commit it. Returns its thread's id, or None
    when it was kept before (a delivery again)."""
    M = ClubMailMessages
    seen = await session.scalar(
        select(M.id).where(M.club_id == club_id, M.provider == received.provider, M.provider_message_id == received.provider_message_id)
    )
    if seen is not None:
        return None
    now = _now()
    ours = await service.own_addresses(session, club_id)
    people = threads.outside([parsed.from_address, *parsed.to, *parsed.cc], ours)
    spam = received.spam
    thread = await threads.by_headers(session, club_id, [*parsed.references, *([parsed.in_reply_to] if parsed.in_reply_to else [])], spam=spam)
    if thread is None:
        thread = await threads.by_subject(session, club_id, parsed.subject, parsed.from_address, spam=spam, now=now)
    keys: list[str] = []
    try:
        async with session.begin_nested():
            if thread is None:
                thread = ClubMailThreads(
                    club_id=club_id, folder=SPAM if spam else INBOX, subject=parsed.subject or None,
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
                club_id=club_id, thread_id=thread.id, direction=IN, status=RECEIVED,
                message_id=parsed.message_id, in_reply_to=parsed.in_reply_to,
                references=" ".join(parsed.references) or None,
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
                key = store.key_for(club_id, msg.id, n)
                await asyncio.to_thread(store.put, key, part.content, part.content_type)
                keys.append(key)
                session.add(ClubMailAttachments(
                    club_id=club_id, message_id=msg.id, filename=part.filename, content_type=part.content_type,
                    size_bytes=len(part.content), storage_key=key, content_id=part.content_id, inline=part.inline,
                ))
            await session.flush()
            await threads_recount(session, thread)
            mailbox = await session.scalar(select(ClubMailboxes).where(ClubMailboxes.club_id == club_id))
            if mailbox is not None:
                mailbox.last_received_at = now
            await alerts.sync_thread(session, club_id, thread.id, arrived=True)
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


async def threads_recount(session, thread: ClubMailThreads) -> None:
    from app.api.club_mailbox import mail

    await mail.recount(session, thread)
