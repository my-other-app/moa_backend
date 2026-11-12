"""Mail the admins write from the platform inbox: new mail, replies,
reply-all and forwards, from one of the platform's addresses.

From `"MyOtherApp" <info@myotherapp.in>` (settings.MAIL_FROM_NAME and the
address of the mailbox picked; a reply defaults to the address the mail came
to): the server sets it, never the portal. Written as the club mailbox's mail
is (club_mailbox/outbound.py: its address checks, quoting, threading headers
and text-to-HTML), and sent through the platform's provider (app/core/email/
transport) -- with none of a club's credits, plan or limits. Instead:

- at most MAILBOX_MAX_RECIPIENTS (50) people, none on the suppression list
  (422 RECIPIENT_SUPPRESSED);
- files at most 10 MB together (413 ATTACHMENTS_TOO_LARGE);
- PLATFORM_MAIL_SEND_PER_HOUR (200) mails an hour across every admin
  (429 PLATFORM_MAIL_RATE_LIMITED).

A refusal from the provider keeps the mail `failed`, and it can go again
(`retry`). Audited with who sent it, never to whom or what.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from app.api.club_mailbox import outbound as club_outbound
from app.api.club_mailbox import parse, sanitise, store, threads
from app.api.club_mailbox.models import ARCHIVE, BOUNCED, COMPLAINED, FAILED, OUT, SENDING, SENT, SENT_STATUS
from app.api.club_mailbox.outbound import MAX_ATTACHMENT_BYTES, MAX_FILES, MAX_SUBJECT, MAX_TEXT, MODES, Draft
from app.api.platform_mail import mail, service
from app.api.platform_mail.models import PlatformMailAttachments, PlatformMailMessages, PlatformMailThreads
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


async def check_rate(session, now: Optional[datetime] = None) -> None:
    """The platform's own limit: mail sent (or tried) in the last hour, every admin together."""
    now = now or _now()
    M = PlatformMailMessages
    sent = int(await session.scalar(
        select(func.count()).select_from(M).where(M.direction == OUT, M.created_at >= now - timedelta(hours=1))
    ) or 0)
    limit = int(settings.PLATFORM_MAIL_SEND_PER_HOUR or 200)
    if sent >= limit:
        raise _refuse(429, f"The platform inbox sends at most {limit} mails an hour. Try again later.", "PLATFORM_MAIL_RATE_LIMITED",
                      {"per_hour": limit})


async def _files(session, draft: Draft) -> list[tuple[str, str, bytes]]:
    """(filename, type, bytes): forwarded ones, then uploaded. 413 past 10 MB."""
    out: list[tuple[str, str, bytes]] = []
    if draft.forward_attachment_ids:
        rows = (await session.scalars(
            select(PlatformMailAttachments).where(PlatformMailAttachments.id.in_(draft.forward_attachment_ids[:MAX_FILES]))
            .order_by(PlatformMailAttachments.id)
        )).all()
        for a in rows:
            content = await asyncio.to_thread(store.get, a.storage_key)
            if content is None:
                raise _refuse(410, f"{a.filename or 'A file'} can't be read any more; send it without it.", "ATTACHMENT_GONE")
            out.append((a.filename or "attachment", a.content_type or "application/octet-stream", content))
    for u in draft.uploads:
        out.append((u.filename or "attachment", u.content_type or "application/octet-stream", u.content))
    if len(out) > MAX_FILES:
        raise _refuse(422, f"At most {MAX_FILES} files a mail.", "TOO_MANY_FILES")
    if sum(len(c) for _, _, c in out) > MAX_ATTACHMENT_BYTES:
        raise _refuse(413, "Files can weigh 10 MB at most together.", "ATTACHMENTS_TOO_LARGE", {"limit_bytes": MAX_ATTACHMENT_BYTES})
    return out


def admin_name(admin) -> str:
    return (getattr(admin, "full_name", None) or getattr(admin, "email", None) or "An admin")[:120]


async def compose(session, draft: Draft, *, from_mailbox: Optional[str], admin, request=None) -> PlatformMailMessages:
    """Write the mail, keep it, send it. Returns the message as kept: `sent`,
    or `failed` with the provider's reason. Commits."""
    service.require_on()
    if draft.mode not in MODES:
        raise _refuse(422, "Write new mail, reply, reply to all, or forward.", "INVALID_MODE")
    original = thread = None
    if draft.mode != "new":
        if draft.message_id is None:
            raise _refuse(422, "Which mail is this an answer to?", "NO_ORIGINAL", {"field": "message_id"})
        original = await mail.get_message(session, draft.message_id)
        if draft.mode != "forward":
            thread = await mail.get_thread(session, original.thread_id)
    # A reply goes from the address the mail came to, unless the admin picked another.
    if from_mailbox or original is None:
        mailbox = service.require_mailbox(from_mailbox)
    else:
        mailbox = original.mailbox if original.mailbox in service.addresses() else service.require_mailbox(None)
    ours = service.own_addresses()
    to = club_outbound.split_addresses(draft.to)
    cc = club_outbound.split_addresses(draft.cc)
    bcc = club_outbound.split_addresses(draft.bcc)
    if original is not None and draft.mode in ("reply", "reply_all") and not to and not cc:
        to, cc = club_outbound.default_recipients(original, draft.mode, ours)
    await club_outbound.check_recipients(session, to, cc, bcc)
    text = (draft.text or "").strip()
    if len(text) > MAX_TEXT:
        raise _refuse(422, "That's too long for one mail.", "TEXT_TOO_LONG", {"field": "text"})
    if not text and draft.mode != "forward":
        raise _refuse(422, "Write something first.", "EMPTY_MAIL", {"field": "text"})
    subject = " ".join((draft.subject or "").split())[:MAX_SUBJECT]
    if original is not None and not subject:
        subject = club_outbound.reply_subject(original.subject, draft.mode)[:MAX_SUBJECT]
    files = await _files(session, draft)
    await check_rate(session)

    now = _now()
    in_reply_to = references = None
    if original is not None:
        anchor = original.message_id or original.alt_message_id
        chain = parse.message_ids(original.references) + ([anchor] if anchor else [])
        if draft.mode in ("reply", "reply_all") and anchor:
            in_reply_to = anchor
        references = " ".join(list(dict.fromkeys(chain))[-20:]) or None
    full_text = club_outbound.body_text(draft, None, original)
    name = admin_name(admin)
    if thread is None:
        thread = PlatformMailThreads(
            mailbox=mailbox, folder=SENT, subject=subject or None, subject_key=threads.subject_key(subject) or None,
            participants=threads.outside([*to, *cc], ours),
        )
        session.add(thread)
        await session.flush()
    else:
        merged = list(thread.participants or [])
        merged += [p for p in threads.outside([*to, *cc], ours) if p not in merged]
        thread.participants = merged[:100]
        if thread.folder == ARCHIVE:
            thread.folder = "inbox" if thread.last_inbound_at else SENT
    msg = PlatformMailMessages(
        thread_id=thread.id, mailbox=mailbox, direction=OUT, status=SENDING, message_id=club_outbound.new_message_id(),
        in_reply_to=in_reply_to, references=references, from_address=service.address_of(mailbox), from_name=service.sender_name(),
        to_addresses=to, cc_addresses=cc, bcc_addresses=bcc, subject=subject or None, text_body=full_text, html_body=None,
        snippet=sanitise.snippet(text) or None, unread=False, sent_at=now, created_at=now,
        sent_by_user_id=getattr(admin, "id", None), sent_by_name=name,
        size_bytes=len(full_text.encode("utf-8")) + sum(len(c) for _, _, c in files),
    )
    session.add(msg)
    await session.flush()
    keys: list[str] = []
    try:
        for n, (filename, ctype, content) in enumerate(files):
            key = store.platform_key_for(msg.id, n)
            await asyncio.to_thread(store.put, key, content, ctype)
            keys.append(key)
            session.add(PlatformMailAttachments(
                message_id=msg.id, filename=filename[:255], content_type=ctype[:120], size_bytes=len(content), storage_key=key,
            ))
        await session.flush()
        await mail.recount(session, thread)
        await session.commit()
    except Exception:
        await session.rollback()
        store.delete(keys)
        raise
    await deliver(session, msg.id, files)
    await _audit(session, msg, admin, len(files), draft.mode, request=request)
    await session.commit()
    return await mail.get_message(session, msg.id)


async def _audit(session, msg: PlatformMailMessages, admin, files: int, mode: str, request=None) -> None:
    """Who sent it, from which address, to how many, with how many files:
    never the addresses, the subject or a word of it."""
    from app.api.audit import service as audit

    people = len({*(msg.to_addresses or []), *(msg.cc_addresses or []), *(msg.bcc_addresses or [])})
    await audit.record(
        session, actor=admin, action=f"{service.ACTION}.send", entity_type="platform_mail", entity_id=msg.id,
        summary=f"Sent mail from {msg.from_address} to {people} {'person' if people == 1 else 'people'}",
        details={"message_id": msg.id, "thread_id": msg.thread_id, "mailbox": msg.mailbox, "mode": mode, "recipients": people,
                 "files": files, "status": msg.status, "sent_by": msg.sent_by_name},
        request=request,
    )


async def deliver(session, message_id: int, files: list[tuple[str, str, bytes]]) -> PlatformMailMessages:
    """Hand the kept message to the provider, and keep what it said. Commits."""
    from app.core.email import transport

    msg = await mail.get_message(session, message_id)
    headers = {"Message-ID": msg.message_id}
    if msg.in_reply_to:
        headers["In-Reply-To"] = msg.in_reply_to
    if msg.references:
        headers["References"] = msg.references
    outgoing = transport.Mail(
        to=list(msg.to_addresses or []), cc=list(msg.cc_addresses or []), bcc=list(msg.bcc_addresses or []),
        subject=msg.subject or "(no subject)", text=msg.text_body or "", html=sanitise.from_text(msg.text_body or ""),
        from_name=msg.from_name, from_address=msg.from_address,
        attachments=[transport.Attachment(filename=f, content=c) for f, _, c in files],
        idempotency_key=f"platform-mail:{msg.id}:{msg.message_id}", check_suppression=False, headers=headers,
    )
    if not outgoing.to:
        outgoing.to, outgoing.cc = outgoing.cc[:1], outgoing.cc[1:]
    try:
        answer = await asyncio.to_thread(transport.deliver, outgoing)
    except transport.MailDeferred:
        msg.status, msg.error = FAILED, "The mail service didn't take it just now. Send it again in a minute."
    except Exception as exc:  # noqa: BLE001 - the provider's last word on this mail, kept for the admins
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str((response.get("Error") or {}).get("Code") or "")
        msg.status = FAILED
        msg.error = (f"The mail service refused it ({code or type(exc).__name__}).")[:400]
        logger.warning("platform mail: mail %s was refused (%s)", msg.id, code or type(exc).__name__)
    else:
        msg.status, msg.error = SENT_STATUS, None
        msg.provider = (answer or {}).get("Provider") or None
        msg.provider_message_id = (answer or {}).get("MessageId") or None
        if msg.provider == "ses":
            msg.alt_message_id = club_outbound.ses_message_id(msg.provider_message_id)
    await session.commit()
    return msg


async def retry(session, message_id: int, *, admin, request=None) -> PlatformMailMessages:
    """Send a failed mail again, as it was. 409 NOT_FAILED for one that went."""
    service.require_on()
    msg = await mail.get_message(session, message_id)
    if msg.direction != OUT or msg.status != FAILED:
        raise _refuse(409, "Only mail that wasn't sent can be sent again.", "NOT_FAILED")
    address = service.address_of(msg.mailbox)
    if address is None:
        raise _refuse(409, "That address is no longer one of the platform's.", "INVALID_MAILBOX")
    await club_outbound.check_recipients(session, list(msg.to_addresses or []), list(msg.cc_addresses or []), list(msg.bcc_addresses or []))
    await check_rate(session)
    rows = (await mail.attachments_of(session, [msg.id])).get(msg.id, [])
    files = []
    for a in rows:
        content = await asyncio.to_thread(store.get, a.storage_key)
        if content is None:
            raise _refuse(410, f"{a.filename or 'A file'} can't be read any more.", "ATTACHMENT_GONE")
        files.append((a.filename or "attachment", a.content_type or "application/octet-stream", content))
    msg.from_address = address
    msg.status = SENDING
    await session.commit()
    msg = await deliver(session, msg.id, files)
    await _audit(session, msg, admin, len(files), "retry", request=request)
    await session.commit()
    return msg


async def provider_verdict(session, provider_id: str, bounced: bool, complained: bool, reason: str = "") -> int:
    """What the provider later said of a mail the admins sent (hooks/service.py):
    bounced or marked as spam. Returns how many messages were marked."""
    if not provider_id or not (bounced or complained):
        return 0
    M = PlatformMailMessages
    rows = (await session.scalars(select(M).where(M.provider_message_id == provider_id, M.direction == OUT))).all()
    for m in rows:
        if m.status in (SENT_STATUS, SENDING):
            m.status = COMPLAINED if complained else BOUNCED
            m.error = (reason or ("Marked as spam by someone it went to." if complained else "It bounced."))[:400]
    return len(rows)
