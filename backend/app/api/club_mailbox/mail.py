"""Reading a club's mail: its threads by folder, a thread's messages, what
is unread, moving and deleting threads, and forgetting old mail.

Folders: Inbox (mail that came, not archived), Sent (every thread the club
wrote in, outside Spam), Archive, Spam. A thread the club started stays out
of the Inbox until someone answers; an answer to an archived thread brings
it back to the Inbox. Everything is the club's own: every query is by its
club id, and a thread of another club's is "no such thread".
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, delete, exists, func, or_, select, update

from app.api.club_mailbox.models import (
    ARCHIVE,
    IN,
    INBOX,
    OUT,
    SENT,
    SPAM,
    ClubMailAttachments,
    ClubMailMessages,
    ClubMailThreads,
)
from app.response import CustomHTTPException

LIST_FOLDERS = ("inbox", "sent", "archive", "spam")
#: Where a thread may be moved to by hand (Sent is where the club wrote, not a place).
MOVE_TO = (INBOX, ARCHIVE, SPAM)
PREVIEW = 160


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


async def get_thread(session, club_id: int, thread_id: int) -> ClubMailThreads:
    thread = await session.scalar(
        select(ClubMailThreads).where(ClubMailThreads.id == thread_id, ClubMailThreads.club_id == club_id)
    )
    if thread is None:
        raise CustomHTTPException(status_code=404, message="No such conversation.", error_code="THREAD_NOT_FOUND")
    return thread


async def get_message(session, club_id: int, message_id: int) -> ClubMailMessages:
    msg = await session.scalar(
        select(ClubMailMessages).where(ClubMailMessages.id == message_id, ClubMailMessages.club_id == club_id)
    )
    if msg is None:
        raise CustomHTTPException(status_code=404, message="No such mail.", error_code="MESSAGE_NOT_FOUND")
    return msg


async def list_threads(session, club_id: int, *, folder: str = "inbox", q: Optional[str] = None, unread: bool = False,
                       offset: int = 0, limit: int = 30) -> tuple[list[ClubMailThreads], int]:
    T, M = ClubMailThreads, ClubMailMessages
    where = [T.club_id == club_id]
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
            exists().where(M.thread_id == T.id, M.club_id == club_id, or_(
                M.subject.ilike(like), M.from_address.ilike(like), M.from_name.ilike(like), M.text_body.ilike(like),
            )),
        ))
    total = int(await session.scalar(select(func.count()).select_from(T).where(*where)) or 0)
    rows = (await session.scalars(select(T).where(*where).order_by(*order).offset(offset).limit(limit))).all()
    return list(rows), total


async def counts(session, club_id: int) -> dict:
    """Unread by folder, for the nav's badge and the folder list. Counts only."""
    T = ClubMailThreads
    row = (await session.execute(
        select(
            func.coalesce(func.sum(T.unread_count).filter(T.folder == INBOX), 0),
            func.count().filter(T.folder == INBOX, T.unread_count > 0),
            func.coalesce(func.sum(T.unread_count).filter(T.folder == SPAM), 0),
            func.coalesce(func.sum(T.unread_count).filter(T.folder == ARCHIVE), 0),
        ).where(T.club_id == club_id)
    )).one()
    return {"unread_total": int(row[0] or 0), "unread_threads": int(row[1] or 0), "spam_unread": int(row[2] or 0),
            "archive_unread": int(row[3] or 0)}


async def messages_of(session, thread: ClubMailThreads) -> list[ClubMailMessages]:
    return list((await session.scalars(
        select(ClubMailMessages).where(ClubMailMessages.thread_id == thread.id, ClubMailMessages.club_id == thread.club_id)
        .order_by(ClubMailMessages.id.asc())
    )).all())


async def attachments_of(session, club_id: int, message_ids: list[int]) -> dict[int, list[ClubMailAttachments]]:
    if not message_ids:
        return {}
    rows = (await session.scalars(
        select(ClubMailAttachments).where(ClubMailAttachments.club_id == club_id, ClubMailAttachments.message_id.in_(message_ids))
        .order_by(ClubMailAttachments.id)
    )).all()
    out: dict[int, list[ClubMailAttachments]] = {}
    for a in rows:
        out.setdefault(a.message_id, []).append(a)
    return out


async def recount(session, thread: ClubMailThreads) -> None:
    """The thread's counts and its latest mail's line, from its messages."""
    M = ClubMailMessages
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
        thread.last_from = (latest.from_name or latest.from_address or "")[:200] if latest.direction == IN else (latest.sent_by_name or "You")[:200]
        thread.snippet = (latest.snippet or "")[:200]
    thread.has_attachments = bool(await session.scalar(
        select(exists().where(ClubMailAttachments.message_id == M.id, M.thread_id == thread.id))
    ))
    thread.updated_at = datetime.now(timezone.utc)


async def mark_read(session, thread: ClubMailThreads, read: bool = True) -> bool:
    """Every mail in it read (or, `read` false, its latest received one
    unread again). True when something changed."""
    M = ClubMailMessages
    if read:
        result = await session.execute(
            update(M).where(M.thread_id == thread.id, M.unread == True).values(unread=False)  # noqa: E712
            .execution_options(synchronize_session=False)
        )
        changed = bool(result.rowcount)
    else:
        latest = await session.scalar(
            select(M.id).where(M.thread_id == thread.id, M.direction == IN).order_by(M.id.desc()).limit(1)
        )
        changed = False
        if latest is not None:
            result = await session.execute(update(M).where(M.id == latest, M.unread == False).values(unread=True))  # noqa: E712
            changed = bool(result.rowcount)
    if changed:
        await recount(session, thread)
    return changed


async def move(session, thread: ClubMailThreads, folder: str) -> None:
    if folder not in MOVE_TO:
        raise CustomHTTPException(status_code=422, message="Move it to the Inbox, the Archive or Spam.", error_code="INVALID_FOLDER")
    if folder == INBOX and thread.last_inbound_at is None:
        # Nothing came in it: it belongs with what the club sent.
        folder = SENT
    thread.folder = folder
    thread.updated_at = datetime.now(timezone.utc)


async def delete_threads(session, club_id: int, thread_ids: list[int]) -> list[str]:
    """Delete these threads with their mail. Returns their files' storage
    keys, for the caller to delete once the rows are gone."""
    if not thread_ids:
        return []
    M, A, T = ClubMailMessages, ClubMailAttachments, ClubMailThreads
    keys = list((await session.scalars(
        select(A.storage_key).join(M, M.id == A.message_id).where(A.club_id == club_id, M.thread_id.in_(thread_ids))
    )).all())
    await session.execute(delete(T).where(T.club_id == club_id, T.id.in_(thread_ids)))
    return keys


async def forget_before(session, club_id: int, cutoff: datetime, batch: int = 500) -> tuple[int, list[str]]:
    """Delete the club's mail older than `cutoff`, a batch at a time, and
    the threads left empty. Returns (messages deleted, their files' keys)."""
    M, A, T = ClubMailMessages, ClubMailAttachments, ClubMailThreads
    ids = list((await session.scalars(
        select(M.id).where(M.club_id == club_id, M.created_at < cutoff).order_by(M.id).limit(batch)
    )).all())
    keys: list[str] = []
    touched: set[int] = set()
    if ids:
        keys = list((await session.scalars(select(A.storage_key).where(A.message_id.in_(ids)))).all())
        touched = set((await session.scalars(select(M.thread_id).where(M.id.in_(ids)).distinct())).all())
        await session.execute(delete(M).where(M.id.in_(ids)))
    empty = ~select(M.id).where(M.thread_id == T.id).exists()
    gone = list((await session.scalars(select(T.id).where(T.club_id == club_id, empty))).all())
    if gone:
        await session.execute(delete(T).where(T.id.in_(gone)))
    for thread_id in touched - set(gone):
        thread = await session.get(T, thread_id)
        if thread is not None:
            await recount(session, thread)
    return len(ids), keys


# ─── What the dashboard is shown ─────────────────────────────────────────


def attachment_path(attachment_id: int) -> str:
    return f"/api/v1/clubs/mailbox/attachments/{attachment_id}"


def thread_out(t: ClubMailThreads) -> dict:
    return {
        "id": t.id,
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


def attachment_out(a: ClubMailAttachments) -> dict:
    return {
        "id": a.id, "filename": a.filename, "content_type": a.content_type, "size_bytes": a.size_bytes,
        "content_id": a.content_id, "inline": bool(a.inline), "url": attachment_path(a.id),
    }


def message_out(m: ClubMailMessages, attachments: list[ClubMailAttachments]) -> dict:
    return {
        "id": m.id,
        "thread_id": m.thread_id,
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


def unread_clause():
    return and_(ClubMailMessages.unread == True)  # noqa: E712
