"""Reading the platform inbox: threads by mailbox and folder, a thread's
messages, unread counts, moving and deleting threads -- the club mailbox's
reading (club_mailbox/mail.py) for the platform's tables.

Folders as the club's: Inbox (mail that came, not archived), Sent (every
thread the admins wrote in, outside Spam), Archive, Spam. A thread belongs to
the mailbox it started at; `mailbox` None lists every address's.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, exists, func, or_, select, update

from app.api.club_mailbox.mail import MOVE_TO, _like
from app.api.club_mailbox.models import ARCHIVE, IN, INBOX, OUT, SENT, SPAM
from app.api.platform_mail.models import PlatformMailAttachments, PlatformMailMessages, PlatformMailThreads
from app.response import CustomHTTPException


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def get_thread(session, thread_id: int) -> PlatformMailThreads:
    thread = await session.get(PlatformMailThreads, thread_id)
    if thread is None:
        raise CustomHTTPException(status_code=404, message="No such conversation.", error_code="THREAD_NOT_FOUND")
    return thread


async def get_message(session, message_id: int) -> PlatformMailMessages:
    msg = await session.get(PlatformMailMessages, message_id)
    if msg is None:
        raise CustomHTTPException(status_code=404, message="No such mail.", error_code="MESSAGE_NOT_FOUND")
    return msg


async def list_threads(session, *, mailbox: Optional[str] = None, folder: str = "inbox", q: Optional[str] = None,
                       unread: bool = False, offset: int = 0, limit: int = 25) -> tuple[list[PlatformMailThreads], int]:
    T, M = PlatformMailThreads, PlatformMailMessages
    where = []
    if mailbox:
        where.append(T.mailbox == mailbox)
    order = [T.last_message_at.desc().nulls_last(), T.id.desc()]
    if folder == "sent":
        where += [T.last_outbound_at.isnot(None), T.folder != SPAM]
        order = [T.last_outbound_at.desc(), T.id.desc()]
    elif folder in (INBOX, ARCHIVE, SPAM):
        where.append(T.folder == folder)
    if unread:
        where.append(T.unread_count > 0)
    term = " ".join((q or "").split())[:100]
    if term:
        like = _like(term)
        where.append(or_(
            T.subject.ilike(like),
            exists().where(M.thread_id == T.id, or_(
                M.subject.ilike(like), M.from_address.ilike(like), M.from_name.ilike(like), M.text_body.ilike(like),
            )),
        ))
    total = int(await session.scalar(select(func.count()).select_from(T).where(*where)) or 0)
    rows = (await session.scalars(select(T).where(*where).order_by(*order).offset(offset).limit(limit))).all()
    return list(rows), total


async def counts(session, mailbox: Optional[str] = None) -> dict:
    """Unread by folder, for one mailbox or all of them. Counts only."""
    T = PlatformMailThreads
    where = [T.mailbox == mailbox] if mailbox else []
    row = (await session.execute(
        select(
            func.coalesce(func.sum(T.unread_count).filter(T.folder == INBOX), 0),
            func.count().filter(T.folder == INBOX, T.unread_count > 0),
            func.coalesce(func.sum(T.unread_count).filter(T.folder == SPAM), 0),
            func.coalesce(func.sum(T.unread_count).filter(T.folder == ARCHIVE), 0),
        ).where(*where)
    )).one()
    return {"unread_total": int(row[0] or 0), "unread_threads": int(row[1] or 0), "spam_unread": int(row[2] or 0),
            "archive_unread": int(row[3] or 0)}


async def unread_by_mailbox(session) -> dict[str, int]:
    """Unread mail in each mailbox's Inbox."""
    T = PlatformMailThreads
    rows = (await session.execute(
        select(T.mailbox, func.coalesce(func.sum(T.unread_count), 0)).where(T.folder == INBOX).group_by(T.mailbox)
    )).all()
    return {mailbox: int(n or 0) for mailbox, n in rows}


async def messages_of(session, thread: PlatformMailThreads) -> list[PlatformMailMessages]:
    return list((await session.scalars(
        select(PlatformMailMessages).where(PlatformMailMessages.thread_id == thread.id).order_by(PlatformMailMessages.id.asc())
    )).all())


async def attachments_of(session, message_ids: list[int]) -> dict[int, list[PlatformMailAttachments]]:
    if not message_ids:
        return {}
    rows = (await session.scalars(
        select(PlatformMailAttachments).where(PlatformMailAttachments.message_id.in_(message_ids)).order_by(PlatformMailAttachments.id)
    )).all()
    out: dict[int, list[PlatformMailAttachments]] = {}
    for a in rows:
        out.setdefault(a.message_id, []).append(a)
    return out


async def recount(session, thread: PlatformMailThreads) -> None:
    """The thread's counts and its latest mail's line, from its messages."""
    M = PlatformMailMessages
    row = (await session.execute(
        select(
            func.count(),
            func.count().filter(M.unread == True),  # noqa: E712
            func.max(M.created_at),
            func.max(M.created_at).filter(M.direction == IN),
            func.max(M.created_at).filter(M.direction == OUT),
        ).where(M.thread_id == thread.id)
    )).one()
    thread.message_count, thread.unread_count = int(row[0]), int(row[1])
    thread.last_message_at, thread.last_inbound_at, thread.last_outbound_at = row[2], row[3], row[4]
    latest = await session.scalar(select(M).where(M.thread_id == thread.id).order_by(M.id.desc()).limit(1))
    if latest is not None:
        thread.last_from = (latest.from_name or latest.from_address or "")[:200] if latest.direction == IN else (latest.sent_by_name or "MyOtherApp")[:200]
        thread.snippet = (latest.snippet or "")[:200]
    thread.has_attachments = bool(await session.scalar(
        select(exists().where(PlatformMailAttachments.message_id == M.id, M.thread_id == thread.id))
    ))
    thread.updated_at = datetime.now(timezone.utc)


async def mark_read(session, thread: PlatformMailThreads, read: bool = True) -> bool:
    """Every mail in it read (or, `read` false, its latest received one unread again)."""
    M = PlatformMailMessages
    if read:
        result = await session.execute(
            update(M).where(M.thread_id == thread.id, M.unread == True).values(unread=False)  # noqa: E712
            .execution_options(synchronize_session=False)
        )
        changed = bool(result.rowcount)
    else:
        latest = await session.scalar(select(M.id).where(M.thread_id == thread.id, M.direction == IN).order_by(M.id.desc()).limit(1))
        changed = False
        if latest is not None:
            result = await session.execute(update(M).where(M.id == latest, M.unread == False).values(unread=True))  # noqa: E712
            changed = bool(result.rowcount)
    if changed:
        await recount(session, thread)
    return changed


async def move(session, thread: PlatformMailThreads, folder: str) -> None:
    if folder not in MOVE_TO:
        raise CustomHTTPException(status_code=422, message="Move it to the Inbox, the Archive or Spam.", error_code="INVALID_FOLDER")
    if folder == INBOX and thread.last_inbound_at is None:
        folder = SENT
    thread.folder = folder
    thread.updated_at = datetime.now(timezone.utc)


async def delete_threads(session, thread_ids: list[int]) -> list[str]:
    """Delete these threads with their mail. Returns their files' storage keys."""
    if not thread_ids:
        return []
    M, A, T = PlatformMailMessages, PlatformMailAttachments, PlatformMailThreads
    keys = list((await session.scalars(select(A.storage_key).join(M, M.id == A.message_id).where(M.thread_id.in_(thread_ids)))).all())
    await session.execute(delete(T).where(T.id.in_(thread_ids)))
    return keys


# ─── What the admin portal is shown ──────────────────────────────────────


def attachment_path(attachment_id: int) -> str:
    return f"/api/v1/admin/mail/attachments/{attachment_id}"


def thread_out(t: PlatformMailThreads) -> dict:
    return {
        "id": t.id,
        "mailbox": t.mailbox,
        "folder": t.folder,
        "subject": t.subject or "",
        "participants": list(t.participants or []),
        "message_count": t.message_count,
        "unread_count": t.unread_count,
        "last_message_at": _aware(t.last_message_at),
        "last_inbound_at": _aware(t.last_inbound_at),
        "last_outbound_at": _aware(t.last_outbound_at),
        "last_from": t.last_from,
        "snippet": t.snippet or "",
        "has_attachments": bool(t.has_attachments),
    }


def attachment_out(a: PlatformMailAttachments) -> dict:
    return {
        "id": a.id, "filename": a.filename, "content_type": a.content_type, "size_bytes": a.size_bytes,
        "content_id": a.content_id, "inline": bool(a.inline), "url": attachment_path(a.id),
    }


def message_out(m: PlatformMailMessages, attachments: list[PlatformMailAttachments]) -> dict:
    return {
        "id": m.id,
        "thread_id": m.thread_id,
        "mailbox": m.mailbox,
        "direction": m.direction,
        "status": m.status,
        "from_address": m.from_address,
        "from_name": m.from_name,
        "to": list(m.to_addresses or []),
        "cc": list(m.cc_addresses or []),
        "bcc": list(m.bcc_addresses or []) if m.direction == OUT else [],
        "reply_to": m.reply_to,
        "subject": m.subject or "",
        "text": m.text_body or "",
        "html": m.html_body or None,
        "has_remote_images": bool(m.has_remote_images),
        "snippet": m.snippet or "",
        "unread": bool(m.unread),
        "spam": (m.verdicts or {}).get("spam") == "FAIL",
        "verdicts": dict(m.verdicts or {}),
        "delivered_to": m.delivered_to,
        "sent_at": _aware(m.sent_at),
        "created_at": _aware(m.created_at),
        "sent_by": m.sent_by_name,
        "error": m.error,
        "attachments": [attachment_out(a) for a in attachments],
    }
