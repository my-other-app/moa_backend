"""The platform inbox in the admin portal: /admin/mail.

Admins with the owner or support role (MODERATE) read and answer it; a
viewer is refused (403 INSUFFICIENT_ADMIN_ROLE); deleting a conversation is
the owner's (DESTROY). Opening a conversation does not mark it read (POST
.../read does), as in the clubs' Mail. Every route carries mail: marked
sensitive (app/core/redact.py), its search term filtered from the access log.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Query, Request, Response

from app.api.club_mailbox import outbound as club_outbound
from app.api.club_mailbox import store
from app.api.club_mailbox.router import _first, _ids, _uploads, media_headers
from app.api.platform_mail import mail, outbound, service
from app.api.platform_mail.schemas import InboxOut, MessageOut, ThreadDetail, ThreadList, ThreadOut, ThreadPatch
from app.core.auth.permissions import DESTROY, DestroyAuth, ModerateAuth, can
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/admin/mail", tags=["platform-mail"])


def _mailbox_filter(mailbox: Optional[str]) -> Optional[str]:
    if not mailbox:
        return None
    value = mailbox.strip().lower().split("@", 1)[0]
    if value not in service.addresses():
        raise CustomHTTPException(status_code=422, message="No such address in the platform inbox.", error_code="INVALID_MAILBOX",
                                  errors={"field": "mailbox", "allowed": list(service.addresses())})
    return value


async def _detail(session, thread) -> dict:
    messages = await mail.messages_of(session, thread)
    files = await mail.attachments_of(session, [m.id for m in messages])
    return {
        "thread": mail.thread_out(thread),
        "messages": [mail.message_out(m, files.get(m.id, [])) for m in messages],
        "own_addresses": service.own_addresses(),
    }


@router.get("", summary="The platform inbox's addresses and what waits unread in each (admin: owner, support)")
async def inbox(session: SessionDep, admin: ModerateAuth) -> InboxOut:
    """Counts only. `addresses` empty: APP_PLATFORM_INBOX_ADDRESSES is."""
    unread = await mail.unread_by_mailbox(session)
    rows = [{"address": address, "mailbox": mailbox, "unread": unread.get(mailbox, 0)} for mailbox, address in service.addresses().items()]
    return {"addresses": rows, "unread_total": sum(r["unread"] for r in rows), "can_delete": can(admin, DESTROY)}


@router.get("/threads", summary="A folder's conversations, latest first, for one address or all")
async def list_threads(
    session: SessionDep, admin: ModerateAuth,
    mailbox: Optional[str] = Query(None, max_length=320, description="info, admin, ... (or the whole address); none: every address"),
    folder: str = Query("inbox", pattern="^(inbox|sent|archive|spam)$"),
    q: Optional[str] = Query(None, max_length=100, description="Words in the subject, the sender or the mail"),
    unread: bool = Query(False), page: int = Query(1, ge=1, le=10_000), page_size: int = Query(25, ge=1, le=100),
) -> ThreadList:
    box = _mailbox_filter(mailbox)
    rows, total = await mail.list_threads(session, mailbox=box, folder=folder, q=q, unread=unread,
                                          offset=(page - 1) * page_size, limit=page_size)
    return {"items": [mail.thread_out(t) for t in rows], "total": total, "page": page, "page_size": page_size,
            "counts": await mail.counts(session, box)}


@router.get("/threads/{thread_id}", summary="A conversation's mail, oldest first, with its files")
async def get_thread(thread_id: int, session: SessionDep, admin: ModerateAuth) -> ThreadDetail:
    """Opening it does not mark it read."""
    return await _detail(session, await mail.get_thread(session, thread_id))


@router.post("/threads/{thread_id}/read", summary="Mark a conversation read")
async def read_thread(thread_id: int, session: SessionDep, admin: ModerateAuth) -> ThreadOut:
    thread = await mail.get_thread(session, thread_id)
    if await mail.mark_read(session, thread, True):
        await session.commit()
    return mail.thread_out(thread)


@router.post("/threads/{thread_id}/unread", summary="Mark a conversation unread again")
async def unread_thread(thread_id: int, session: SessionDep, admin: ModerateAuth) -> ThreadOut:
    thread = await mail.get_thread(session, thread_id)
    if await mail.mark_read(session, thread, False):
        await session.commit()
    return mail.thread_out(thread)


@router.patch("/threads/{thread_id}", summary="Move a conversation: Inbox, Archive or Spam")
async def move_thread(thread_id: int, payload: ThreadPatch, session: SessionDep, admin: ModerateAuth) -> ThreadOut:
    thread = await mail.get_thread(session, thread_id)
    await mail.move(session, thread, payload.folder)
    await session.commit()
    return mail.thread_out(thread)


@router.delete("/threads/{thread_id}", summary="Delete a conversation and its files, for good (admin: owner)")
async def delete_thread(thread_id: int, request: Request, session: SessionDep, admin: DestroyAuth) -> dict:
    """Audited (that one was deleted, never what it said)."""
    from app.api.audit import service as audit

    thread = await mail.get_thread(session, thread_id)
    count, box = thread.message_count, thread.mailbox
    keys = await mail.delete_threads(session, [thread.id])
    await audit.record(
        session, actor=admin, action=f"{service.ACTION}.delete", entity_type="platform_mail", entity_id=thread_id,
        summary=f"Deleted a conversation from the platform inbox ({box})", details={"thread_id": thread_id, "messages": count,
                                                                                    "mailbox": box},
        request=request,
    )
    await session.commit()
    await asyncio.to_thread(store.delete, keys)
    return {"deleted": thread_id}


@router.post(
    "/messages", summary="Write new mail, reply, reply to all or forward from a platform address (multipart, files in `files`)",
    openapi_extra={"requestBody": {"content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["mode", "text"],
        "properties": {
            "mode": {"type": "string", "enum": list(club_outbound.MODES)}, "message_id": {"type": "integer"},
            "from_mailbox": {"type": "string", "description": "info, admin, ... (a reply defaults to the address it came to)"},
            "to": {"type": "string"}, "cc": {"type": "string"}, "bcc": {"type": "string"},
            "subject": {"type": "string"}, "text": {"type": "string"},
            "forward_attachments": {"type": "string", "description": "Ids of the original's files to send on, comma-separated"},
            "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
        },
    }}}}},
)
@limiter.limit("30/minute")
async def send(request: Request, session: SessionDep, admin: ModerateAuth) -> MessageOut:
    """From `"MyOtherApp" <from_mailbox@...>`. The mail as kept: `sent`, or
    `failed` with the reason (send it again: /messages/{id}/retry). 422
    INVALID_MAILBOX / NO_RECIPIENTS / INVALID_ADDRESS / TOO_MANY_RECIPIENTS /
    RECIPIENT_SUPPRESSED / EMPTY_MAIL, 413 ATTACHMENTS_TOO_LARGE, 429
    PLATFORM_MAIL_RATE_LIMITED. Audited (who, from where, to how many)."""
    fields, uploads = await _uploads(request)
    raw_id = _first(fields, "message_id").strip()
    draft = club_outbound.Draft(
        mode=_first(fields, "mode", "new").strip() or "new",
        message_id=int(raw_id) if raw_id.isdigit() else None,
        to=club_outbound.split_addresses(fields.get("to") and ",".join(fields["to"])),
        cc=club_outbound.split_addresses(fields.get("cc") and ",".join(fields["cc"])),
        bcc=club_outbound.split_addresses(fields.get("bcc") and ",".join(fields["bcc"])),
        subject=_first(fields, "subject"), text=_first(fields, "text"), include_signature=False,
        forward_attachment_ids=_ids(fields, "forward_attachments"), uploads=uploads,
    )
    msg = await outbound.compose(session, draft, from_mailbox=_first(fields, "from_mailbox").strip() or None, admin=admin,
                                 request=request)
    files = (await mail.attachments_of(session, [msg.id])).get(msg.id, [])
    return mail.message_out(msg, files)


@router.post("/messages/{message_id}/retry", summary="Send a mail that failed again")
@limiter.limit("30/minute")
async def retry(message_id: int, request: Request, session: SessionDep, admin: ModerateAuth) -> MessageOut:
    msg = await outbound.retry(session, message_id, admin=admin, request=request)
    files = (await mail.attachments_of(session, [msg.id])).get(msg.id, [])
    return mail.message_out(msg, files)


@router.get("/attachments/{attachment_id}", summary="A file of a mail in the platform inbox",
            response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
async def get_attachment(attachment_id: int, session: SessionDep, admin: ModerateAuth) -> Response:
    """Served as the clubs' mail files are: in place only for a picture,
    sound or video a browser cannot run; anything else a download; a CSP
    that runs nothing, nosniff, never cached. 410 ATTACHMENT_GONE when the
    file can't be read."""
    from app.api.platform_mail.models import PlatformMailAttachments

    row = await session.get(PlatformMailAttachments, attachment_id)
    if row is None:
        raise CustomHTTPException(status_code=404, message="No such file.", error_code="ATTACHMENT_NOT_FOUND")
    content = await asyncio.to_thread(store.get, row.storage_key)
    if content is None:
        raise CustomHTTPException(status_code=410, message="That file can't be read any more.", error_code="ATTACHMENT_GONE")
    media_type, headers = media_headers(row.content_type, row.filename)
    return Response(content=content, media_type=media_type, headers=headers)
