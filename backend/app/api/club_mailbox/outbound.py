"""Mail the club's team writes: new mail, replies, reply-all and forwards.

Always from the club's own current address (`"Yukthi" <yukthi@mail...>`):
the From is the server's, never the dashboard's, so a club can send only as
itself. Through the platform's own sending provider (app/core/email/
transport: SES, or Resend first when the platform is set so), with:

- a Message-ID of ours (`<uuid@mail.myotherapp.in>`), and In-Reply-To and
  References for a reply or a forward, so the other side's mail client
  threads it. SES replaces any Message-ID with its own (its "Amazon SES
  header fields" page); that one (`<ses-id@region.amazonses.com>`) is kept
  too (`alt_message_id`), since it is what an answer will quote;
- a plain-text part (what the team wrote, the signature, the quote) and an
  HTML one made from it;
- files: uploaded, or forwarded from the mail being forwarded, at most
  10 MB together.

Before it goes: the plan (403 PLAN_FEATURE_LOCKED), the recipients (at most
MAILBOX_MAX_RECIPIENTS, none on the platform's suppression list: 422
RECIPIENT_SUPPRESSED), the club's limits by the hour and the day (429
MAILBOX_RATE_LIMITED), and its email credits (402 NO_EMAIL_CREDITS, "Buy
more email credits"). Once the provider takes it, it counts one EMAIL
credit (credits.py). The provider refusing it, or not answering, keeps it
`failed` with the reason, and the team may send it again (`retry`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parseaddr
from typing import Optional

from sqlalchemy import func, select

from app.api.club_mailbox import alerts, credits, mail, parse, sanitise, service, store, threads
from app.api.club_mailbox.models import (
    ARCHIVE,
    BOUNCED,
    COMPLAINED,
    FAILED,
    IN,
    OUT,
    SENDING,
    SENT,
    SENT_STATUS,
    ClubMailAttachments,
    ClubMailboxes,
    ClubMailMessages,
    ClubMailThreads,
)
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: What a mail's files may weigh together.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_FILES = 20
MAX_TEXT = 100_000
MAX_SUBJECT = 300
MODES = ("new", "reply", "reply_all", "forward")
_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_REPLY = re.compile(r"^\s*re\s*:", re.I)
_FORWARD = re.compile(r"^\s*(?:fwd?|fw)\s*:", re.I)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Upload:
    filename: str
    content_type: str
    content: bytes


@dataclass
class Draft:
    mode: str = "new"
    #: The message answered or forwarded.
    message_id: Optional[int] = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: Optional[str] = None
    text: str = ""
    include_signature: bool = True
    #: Files of the forwarded (or answered) mail to send on.
    forward_attachment_ids: list[int] = field(default_factory=list)
    uploads: list[Upload] = field(default_factory=list)


@dataclass
class Actor:
    user_id: Optional[int]
    member_id: Optional[int]
    name: str


# ─── Addresses ───────────────────────────────────────────────────────────


def split_addresses(value) -> list[str]:
    """Addresses from a field: a list, or one string separated by commas or
    semicolons ("Ananya <a@x.in>, b@y.in"), each lowered, each once."""
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;\n]", value or "")
    out: list[str] = []
    for item in items:
        if not item or not str(item).strip():
            continue
        address = (parseaddr(str(item))[1] or str(item)).strip().lower()
        if address and address not in out:
            out.append(address)
    return out


def _check_addresses(field_name: str, addresses: list[str]) -> None:
    bad = [a for a in addresses if not _ADDRESS.match(a) or len(a) > 254]
    if bad:
        raise _refuse(422, f"{bad[0]} isn't an email address.", "INVALID_ADDRESS", {"field": field_name, "addresses": bad[:10]})


async def check_recipients(session, to: list[str], cc: list[str], bcc: list[str]) -> None:
    from app.core.mail import suppression

    if not to and not cc:
        raise _refuse(422, "Who is it to? Add an address in To.", "NO_RECIPIENTS", {"field": "to"})
    for name, values in (("to", to), ("cc", cc), ("bcc", bcc)):
        _check_addresses(name, values)
    everyone = list(dict.fromkeys([*to, *cc, *bcc]))
    limit = int(settings.MAILBOX_MAX_RECIPIENTS or 50)
    if len(everyone) > limit:
        raise _refuse(422, f"One mail can go to at most {limit} people.", "TOO_MANY_RECIPIENTS", {"field": "to", "limit": limit})
    found = await suppression.reasons(session, everyone)
    if found:
        listed = sorted(found)
        raise _refuse(
            422, f"{listed[0]} can't be mailed: it bounced or marked our mail as spam before"
            + (f" (and {len(listed) - 1} more)" if len(listed) > 1 else "") + ". Take it out to send.",
            "RECIPIENT_SUPPRESSED", {"field": "to", "addresses": listed[:20]},
        )


# ─── Limits ──────────────────────────────────────────────────────────────


async def check_rate(session, club_id: int, now: Optional[datetime] = None) -> None:
    """The club's own abuse limits: mail sent (or tried) in the last hour and day."""
    now = now or _now()
    M = ClubMailMessages
    row = (await session.execute(
        select(
            func.count().filter(M.created_at >= now - timedelta(hours=1)),
            func.count(),
        ).where(M.club_id == club_id, M.direction == OUT, M.created_at >= now - timedelta(days=1))
    )).one()
    hour, day = int(row[0]), int(row[1])
    per_hour, per_day = int(settings.MAILBOX_SEND_PER_HOUR or 60), int(settings.MAILBOX_SEND_PER_DAY or 300)
    if hour >= per_hour or day >= per_day:
        which = f"{per_hour} an hour" if hour >= per_hour else f"{per_day} a day"
        raise _refuse(429, f"The club's mailbox sends at most {which}. Try again later.", "MAILBOX_RATE_LIMITED",
                      {"per_hour": per_hour, "per_day": per_day})


async def check_credits(session, club_id: int) -> None:
    reason = await credits.held_back(session, club_id)
    if reason:
        raise _refuse(402, credits.BUY_MORE, "NO_EMAIL_CREDITS", {"reason": reason[:300]})


# ─── Writing it ──────────────────────────────────────────────────────────


def new_message_id() -> str:
    return f"<{uuid.uuid4().hex}@{service.domain()}>"


def ses_message_id(provider_id: Optional[str], region: Optional[str] = None) -> Optional[str]:
    """The Message-ID SES gives what it sends: its message id at
    `email.amazonses.com` in us-east-1, at `<region>.amazonses.com` elsewhere."""
    if not provider_id:
        return None
    region = (region or settings.SES_REGION or "ap-south-1").strip()
    host = "email.amazonses.com" if region == "us-east-1" else f"{region}.amazonses.com"
    return f"<{provider_id}@{host}>"


def reply_subject(subject: Optional[str], mode: str) -> str:
    value = " ".join((subject or "").split())
    if mode == "forward":
        return value if _FORWARD.match(value) else f"Fwd: {value}".strip()
    return value if _REPLY.match(value) else f"Re: {value}".strip()


def _quote(original: ClubMailMessages) -> str:
    when = original.sent_at or original.created_at
    who = parse.address_line(original.from_name, original.from_address) or "they"
    lines = (original.text_body or "").splitlines()[:400]
    stamp = format_datetime(when) if when else ""
    return f"On {stamp}, {who} wrote:\n" + "\n".join(f"> {ln}" for ln in lines)


def _forwarded(original: ClubMailMessages) -> str:
    when = original.sent_at or original.created_at
    head = [
        "---------- Forwarded message ---------",
        f"From: {parse.address_line(original.from_name, original.from_address)}",
        f"Date: {format_datetime(when) if when else ''}",
        f"Subject: {original.subject or ''}",
        f"To: {', '.join(original.to_addresses or [])}",
    ]
    if original.cc_addresses:
        head.append(f"Cc: {', '.join(original.cc_addresses)}")
    return "\n".join(head) + "\n\n" + (original.text_body or "")


def body_text(draft: Draft, signature: Optional[str], original: Optional[ClubMailMessages]) -> str:
    text = (draft.text or "").replace("\r\n", "\n").rstrip()
    parts = [text]
    if draft.include_signature and signature:
        parts.append(f"-- \n{signature.strip()}")
    if original is not None and draft.mode in ("reply", "reply_all"):
        parts.append(_quote(original))
    elif original is not None and draft.mode == "forward":
        parts.append(_forwarded(original))
    return "\n\n".join(p for p in parts if p)[: MAX_TEXT * 2] + "\n"


def default_recipients(original: ClubMailMessages, mode: str, ours: list[str]) -> tuple[list[str], list[str]]:
    """Who a reply goes to when the team didn't say: the sender (their
    Reply-To first); reply-all adds everyone else on it, less the club."""
    mine = {a.lower() for a in ours}
    if original.direction == IN:
        first = [(original.reply_to or original.from_address or "").lower()]
    else:
        first = [a.lower() for a in (original.to_addresses or [])]
    to = [a for a in first if a and a not in mine]
    cc: list[str] = []
    if mode == "reply_all":
        for a in [*(original.to_addresses or []), *(original.cc_addresses or [])]:
            a = a.lower()
            if a not in mine and a not in to and a not in cc:
                cc.append(a)
    return to, cc


async def _files(session, club_id: int, draft: Draft) -> list[tuple[str, str, bytes]]:
    """(filename, type, bytes) of every file to send: forwarded, then uploaded. 413 past 10 MB."""
    out: list[tuple[str, str, bytes]] = []
    if draft.forward_attachment_ids:
        rows = (await session.scalars(
            select(ClubMailAttachments).where(
                ClubMailAttachments.club_id == club_id, ClubMailAttachments.id.in_(draft.forward_attachment_ids[:MAX_FILES]),
            ).order_by(ClubMailAttachments.id)
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


async def compose(session, club, draft: Draft, actor: Actor, *, actor_user=None) -> ClubMailMessages:
    """Write the mail, keep it, send it. Returns the message as kept: `sent`,
    or `failed` with the provider's reason. Commits."""
    service.require_on()
    if draft.mode not in MODES:
        raise _refuse(422, "Write new mail, reply, reply to all, or forward.", "INVALID_MODE")
    await service.require_plan(session, club.id, actor=actor_user)
    mailbox = await service.require_mailbox(session, club.id)
    current = await service.current_address(session, club.id)
    if current is None:
        raise _refuse(409, "Pick the club's address first.", "NO_MAILBOX")
    original = None
    thread = None
    if draft.mode != "new":
        if draft.message_id is None:
            raise _refuse(422, "Which mail is this an answer to?", "NO_ORIGINAL", {"field": "message_id"})
        original = await mail.get_message(session, club.id, draft.message_id)
        if draft.mode != "forward":
            thread = await mail.get_thread(session, club.id, original.thread_id)
    ours = await service.own_addresses(session, club.id)
    to, cc, bcc = split_addresses(draft.to), split_addresses(draft.cc), split_addresses(draft.bcc)
    if original is not None and draft.mode in ("reply", "reply_all") and not to and not cc:
        to, cc = default_recipients(original, draft.mode, ours)
    await check_recipients(session, to, cc, bcc)
    text = (draft.text or "").strip()
    if len(text) > MAX_TEXT:
        raise _refuse(422, "That's too long for one mail.", "TEXT_TOO_LONG", {"field": "text"})
    if not text and draft.mode != "forward":
        raise _refuse(422, "Write something first.", "EMPTY_MAIL", {"field": "text"})
    subject = " ".join((draft.subject or "").split())[:MAX_SUBJECT]
    if original is not None and not subject:
        subject = reply_subject(original.subject, draft.mode)[:MAX_SUBJECT]
    files = await _files(session, club.id, draft)
    await check_rate(session, club.id)
    await check_credits(session, club.id)

    now = _now()
    message_id = new_message_id()
    in_reply_to = references = None
    if original is not None:
        anchor = original.message_id or original.alt_message_id
        chain = parse.message_ids(original.references) + ([anchor] if anchor else [])
        if draft.mode in ("reply", "reply_all") and anchor:
            in_reply_to = anchor
        references = " ".join(list(dict.fromkeys(chain))[-20:]) or None
    full_text = body_text(draft, mailbox.signature, original)
    address = service.address_of(current.handle)
    if thread is None:
        thread = ClubMailThreads(
            club_id=club.id, folder=SENT, subject=subject or None, subject_key=threads.subject_key(subject) or None,
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
    msg = ClubMailMessages(
        club_id=club.id, thread_id=thread.id, direction=OUT, status=SENDING, message_id=message_id,
        in_reply_to=in_reply_to, references=references, from_address=address, from_name=service.sender_name(mailbox, club),
        to_addresses=to, cc_addresses=cc, bcc_addresses=bcc, subject=subject or None, text_body=full_text,
        html_body=None, snippet=sanitise.snippet(text) or None, unread=False, sent_at=now, created_at=now,
        sent_by_user_id=actor.user_id, sent_by_member_id=actor.member_id, sent_by_name=actor.name[:120],
        size_bytes=len(full_text.encode("utf-8")) + sum(len(c) for _, _, c in files),
    )
    session.add(msg)
    await session.flush()
    keys: list[str] = []
    try:
        for n, (filename, ctype, content) in enumerate(files):
            key = store.key_for(club.id, msg.id, n)
            await asyncio.to_thread(store.put, key, content, ctype)
            keys.append(key)
            session.add(ClubMailAttachments(
                club_id=club.id, message_id=msg.id, filename=filename[:255], content_type=ctype[:120], size_bytes=len(content),
                storage_key=key,
            ))
        await session.flush()
        await mail.recount(session, thread)
        await alerts.sync_thread(session, club.id, thread.id)
        await session.commit()
    except Exception:
        await session.rollback()
        store.delete(keys)
        raise
    await deliver(session, club.id, msg.id, files)
    await _audit(session, club.id, msg, actor_user, len(files), draft.mode)
    await session.commit()
    return await mail.get_message(session, club.id, msg.id)


async def _audit(session, club_id: int, msg: ClubMailMessages, actor_user, files: int, mode: str) -> None:
    """Who sent a mail, how many it went to, with how many files: never the
    addresses, the subject or a word of it."""
    from app.api.audit import service as audit

    people = len({*(msg.to_addresses or []), *(msg.cc_addresses or []), *(msg.bcc_addresses or [])})
    await audit.record(
        session, actor=actor_user, action=f"{service.ACTION}.send", entity_type="club", entity_id=club_id,
        summary=f"Sent mail from the club's mailbox to {people} {'person' if people == 1 else 'people'}",
        details={"message_id": msg.id, "thread_id": msg.thread_id, "mode": mode, "recipients": people, "files": files,
                 "status": msg.status, "member_id": msg.sent_by_member_id},
    )


async def deliver(session, club_id: int, message_id: int, files: list[tuple[str, str, bytes]]) -> ClubMailMessages:
    """Hand the kept message to the provider, and keep what it said. Commits."""
    from app.core.email import transport

    msg = await mail.get_message(session, club_id, message_id)
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
        idempotency_key=f"mailbox:{msg.id}:{msg.message_id}", check_suppression=False, headers=headers,
    )
    if not outgoing.to:
        outgoing.to, outgoing.cc = outgoing.cc[:1], outgoing.cc[1:]
    try:
        answer = await asyncio.to_thread(transport.deliver, outgoing)
    except transport.MailDeferred:
        msg.status, msg.error = FAILED, "The mail service didn't take it just now. Send it again in a minute."
    except Exception as exc:  # noqa: BLE001 - the provider's last word on this mail, kept for the team
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str((response.get("Error") or {}).get("Code") or "")
        msg.status = FAILED
        msg.error = (f"The mail service refused it ({code or type(exc).__name__}).")[:400]
        logger.warning("mailbox: club %s's mail %s was refused (%s)", club_id, msg.id, code or type(exc).__name__)
    else:
        msg.status, msg.error = SENT_STATUS, None
        msg.provider = (answer or {}).get("Provider") or None
        msg.provider_message_id = (answer or {}).get("MessageId") or None
        if msg.provider == "ses":
            msg.alt_message_id = ses_message_id(msg.provider_message_id)
        mailbox = await session.scalar(select(ClubMailboxes).where(ClubMailboxes.club_id == club_id))
        if mailbox is not None:
            mailbox.last_sent_at = _now()
        await credits.record(session, club_id, msg.id)
    await session.commit()
    return msg


async def retry(session, club, message_id: int, actor_user=None) -> ClubMailMessages:
    """Send a failed mail again, as it was. 409 NOT_FAILED for one that went."""
    service.require_on()
    await service.require_plan(session, club.id, actor=actor_user)
    msg = await mail.get_message(session, club.id, message_id)
    if msg.direction != OUT or msg.status != FAILED:
        raise _refuse(409, "Only mail that wasn't sent can be sent again.", "NOT_FAILED")
    current = await service.current_address(session, club.id)
    if current is None:
        raise _refuse(409, "Pick the club's address first.", "NO_MAILBOX")
    await check_recipients(session, list(msg.to_addresses or []), list(msg.cc_addresses or []), list(msg.bcc_addresses or []))
    await check_rate(session, club.id)
    await check_credits(session, club.id)
    rows = (await mail.attachments_of(session, club.id, [msg.id])).get(msg.id, [])
    files = []
    for a in rows:
        content = await asyncio.to_thread(store.get, a.storage_key)
        if content is None:
            raise _refuse(410, f"{a.filename or 'A file'} can't be read any more.", "ATTACHMENT_GONE")
        files.append((a.filename or "attachment", a.content_type or "application/octet-stream", content))
    # Sent from the address the club has now.
    msg.from_address = service.address_of(current.handle)
    msg.status = SENDING
    await session.commit()
    return await deliver(session, club.id, msg.id, files)


async def provider_verdict(session, provider_id: str, bounced: bool, complained: bool, reason: str = "") -> int:
    """What the provider later said of a mail a club sent (hooks/service.py):
    bounced or marked as spam. Returns how many messages were marked."""
    if not provider_id or not (bounced or complained):
        return 0
    M = ClubMailMessages
    rows = (await session.scalars(select(M).where(M.provider_message_id == provider_id, M.direction == OUT))).all()
    for m in rows:
        if m.status in (SENT_STATUS, SENDING):
            m.status = COMPLAINED if complained else BOUNCED
            m.error = (reason or ("Marked as spam by someone it went to." if complained else "It bounced."))[:400]
    return len(rows)

