"""A club's mailbox: its address and settings, its mail, the admins' card,
and the webhook SES's notifications come to (/clubs/mailbox, /admin/club-mailbox,
/hooks/mailbox).

Pro and Enterprise (plan flag `mailbox`): setting up, changing the address
and sending answer 403 PLAN_FEATURE_LOCKED on other plans; reading what is
there is never gated by the plan (but a lapsed mailbox closes after 30 days:
403 MAILBOX_LAPSED). Off (APP_CLUB_MAILBOX false), every route but the
status answers 404 MAILBOX_OFF and the dashboard hides Mail.

Who: the address and settings are the owner's and its admins'; the mail is
the owner's and the members it gave the Mail permission (access.py). A
platform admin acting as the club sees the status, never a mail: 403
MAILBOX_PRIVATE on every route that carries one. Every query is by the
club's own id: another club's thread, mail or file is "no such" (404).
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.club_mailbox import access, alerts, export, handles, inbound, live, mail, outbound, providers, service, store
from app.api.club_mailbox.schemas import (
    AddressIn,
    AdminMailboxOut,
    HandleCheckOut,
    MailboxOut,
    MessageOut,
    SettingsIn,
    SignatureIn,
    SummaryOut,
    ThreadDetail,
    ThreadList,
    ThreadOut,
    ThreadPatch,
)
from app.config import settings
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

club_router = APIRouter(prefix="/clubs/mailbox", tags=["club-mailbox"])
admin_router = APIRouter(prefix="/admin/club-mailbox", tags=["club-mailbox"])
hook_router = APIRouter(prefix="/hooks/mailbox", tags=["hooks"])


async def _status(session, request: Request, club_id: int, user) -> dict:
    return await service.status_out(
        session, club_id, manage=access.can_manage(request), mail=access.can_mail(request), owner=access.is_owner(request),
        impersonated=access.impersonated(), actor=user,
    )


def _actor(request: Request, user) -> outbound.Actor:
    user_id, member_id = access.actor_ids(request, user)
    return outbound.Actor(user_id=user_id, member_id=member_id, name=access.actor_name(request, user))


async def _reading(session, request: Request, user):
    """The mail's people, the feature on, the mailbox readable: (club, mailbox)."""
    access.require_mail(request)
    service.require_on()
    club = await service.club_of(session, user)
    mailbox = await service.mailbox_of(session, club.id)
    await service.require_readable(session, club.id, mailbox)
    return club, mailbox


async def _thread_detail(session, club_id: int, thread) -> dict:
    messages = await mail.messages_of(session, thread)
    files = await mail.attachments_of(session, club_id, [m.id for m in messages])
    return {
        "thread": mail.thread_out(thread),
        "messages": [mail.message_out(m, files.get(m.id, [])) for m in messages],
        "own_addresses": await service.own_addresses(session, club_id),
    }


# ─── The address and settings ────────────────────────────────────────────


@club_router.get("", summary="The club's mailbox: whether it is open here, its address, what the plan allows")
async def get_mailbox(request: Request, session: SessionDep, user: ClubAuth) -> MailboxOut:
    """`available` false: mailboxes aren't on on this server (the dashboard
    hides Mail). `plan_allows` false: the page shows `upgrade`. `state`
    lapsed or expired: the plan dropped it (see service.py). No mail here."""
    access.require_any(request)
    club = await service.club_of(session, user)
    return await _status(session, request, club.id, user)


@club_router.get("/handle-check", summary="Whether an address can be the club's")
@limiter.limit("60/minute")
async def handle_check(request: Request, session: SessionDep, user: ClubAuth,
                       handle: str = Query(..., min_length=1, max_length=64)) -> HandleCheckOut:
    access.require_manage(request)
    service.require_on()
    club = await service.club_of(session, user)
    value = handles.normal(handle)
    out = {"handle": value, "address": service.address_of(value), "available": True, "code": None, "message": None}
    found = handles.problem(value)
    if found is not None:
        out.update(available=False, code=found[0], message=found[1])
    elif not await service.handle_free(session, value, club.id):
        out.update(available=False, code="HANDLE_TAKEN", message="That address is taken. Pick another.")
    return out


@club_router.put("/address", summary="Set the club's mailbox up at an address, or change its address")
@limiter.limit("20/hour")
async def put_address(payload: AddressIn, request: Request, session: SessionDep, user: ClubAuth) -> MailboxOut:
    """422 HANDLE_INVALID / HANDLE_RESERVED, 409 HANDLE_TAKEN, 429
    HANDLE_CHANGED_RECENTLY (once a month; `errors.next_change_at`), 403
    PLAN_FEATURE_LOCKED. The old address forwards here for 90 days. Audited."""
    from app.api.club_access import service as club_access

    access.require_manage(request)
    club = await service.club_of(session, user)
    await service.set_address(session, club, user, payload.handle, member=club_access.current_member(request), request=request)
    return await _status(session, request, club.id, user)


@club_router.patch("/settings", summary="The name the club sends as; how long its mail is kept")
async def patch_settings(payload: SettingsIn, request: Request, session: SessionDep, user: ClubAuth) -> MailboxOut:
    """`retention_months` 1 to 36 (12 by default). Audited."""
    access.require_manage(request)
    club = await service.club_of(session, user)
    await service.update_settings(session, club, user, display_name=payload.display_name,
                                  retention_months=payload.retention_months, request=request)
    return await _status(session, request, club.id, user)


@club_router.put("/signature", summary="The signature under what the team writes")
async def put_signature(payload: SignatureIn, request: Request, session: SessionDep, user: ClubAuth) -> MailboxOut:
    access.require_mail(request)
    club = await service.club_of(session, user)
    await service.set_signature(session, club, payload.signature)
    return await _status(session, request, club.id, user)


# ─── The mail ────────────────────────────────────────────────────────────


@club_router.get("/summary", summary="What waits unread: the nav's badge and the tab's title")
async def summary(request: Request, session: SessionDep, user: ClubAuth) -> SummaryOut:
    """Counts only, never a name or a word."""
    club, _mailbox = await _reading(session, request, user)
    return await mail.counts(session, club.id)


@club_router.get("/threads", summary="A folder's conversations, latest first")
async def list_threads(
    request: Request, session: SessionDep, user: ClubAuth,
    folder: str = Query("inbox", pattern="^(inbox|sent|archive|spam)$"),
    q: Optional[str] = Query(None, max_length=100, description="Words in the subject, the sender or the mail"),
    unread: bool = Query(False), offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
) -> ThreadList:
    club, _mailbox = await _reading(session, request, user)
    rows, total = await mail.list_threads(session, club.id, folder=folder, q=q, unread=unread, offset=offset, limit=limit)
    return {"items": [mail.thread_out(t) for t in rows], "total": total, "counts": await mail.counts(session, club.id)}


@club_router.get("/threads/{thread_id}", summary="A conversation's mail, oldest first, with its files")
async def get_thread(thread_id: int, request: Request, session: SessionDep, user: ClubAuth) -> ThreadDetail:
    club, _mailbox = await _reading(session, request, user)
    thread = await mail.get_thread(session, club.id, thread_id)
    return await _thread_detail(session, club.id, thread)


@club_router.post("/threads/{thread_id}/read", summary="Mark a conversation read")
async def read_thread(thread_id: int, request: Request, session: SessionDep, user: ClubAuth) -> ThreadOut:
    club, _mailbox = await _reading(session, request, user)
    thread = await mail.get_thread(session, club.id, thread_id)
    if await mail.mark_read(session, thread, True):
        await alerts.sync_thread(session, club.id, thread.id)
        await session.commit()
        await live.changed(club.id, thread.id, "read")
    return mail.thread_out(thread)


@club_router.post("/threads/{thread_id}/unread", summary="Mark a conversation unread again")
async def unread_thread(thread_id: int, request: Request, session: SessionDep, user: ClubAuth) -> ThreadOut:
    club, _mailbox = await _reading(session, request, user)
    thread = await mail.get_thread(session, club.id, thread_id)
    if await mail.mark_read(session, thread, False):
        await alerts.sync_thread(session, club.id, thread.id)
        await session.commit()
        await live.changed(club.id, thread.id, "read")
    return mail.thread_out(thread)


@club_router.patch("/threads/{thread_id}", summary="Move a conversation: Inbox, Archive or Spam")
async def move_thread(thread_id: int, payload: ThreadPatch, request: Request, session: SessionDep, user: ClubAuth) -> ThreadOut:
    """To Spam: its notice goes; out of Spam ("Not spam"): back to the Inbox."""
    club, _mailbox = await _reading(session, request, user)
    thread = await mail.get_thread(session, club.id, thread_id)
    await mail.move(session, thread, payload.folder)
    await alerts.sync_thread(session, club.id, thread.id)
    await session.commit()
    await live.changed(club.id, thread.id, "thread")
    return mail.thread_out(thread)


@club_router.delete("/threads/{thread_id}", summary="Delete a conversation and its files, for good")
async def delete_thread(thread_id: int, request: Request, session: SessionDep, user: ClubAuth) -> dict:
    """Audited (that one was deleted, never what it said)."""
    from app.api.audit import service as audit

    club, _mailbox = await _reading(session, request, user)
    thread = await mail.get_thread(session, club.id, thread_id)
    count = thread.message_count
    keys = await mail.delete_threads(session, club.id, [thread.id])
    await alerts.forget_thread(session, club.id, [thread_id])
    await audit.record(
        session, actor=user, action=f"{service.ACTION}.delete", entity_type="club", entity_id=club.id,
        summary="Deleted a conversation from the club's mailbox", details={"thread_id": thread_id, "messages": count},
        request=request,
    )
    await session.commit()
    await asyncio.to_thread(store.delete, keys)
    await live.changed(club.id, thread_id, "thread")
    return {"deleted": thread_id}


async def _uploads(request: Request) -> tuple[dict, list[outbound.Upload]]:
    """The form's fields and its files (`files`), read here so their size is
    checked as they arrive: past 10 MB together, 413 before the rest is read."""
    form = await request.form(max_files=outbound.MAX_FILES + 1, max_fields=50)
    fields: dict = {}
    uploads: list[outbound.Upload] = []
    total = 0
    for key, value in form.multi_items():
        if hasattr(value, "read") and hasattr(value, "filename"):
            if key != "files" or not value.filename:
                continue
            content = await value.read(outbound.MAX_ATTACHMENT_BYTES + 1)
            total += len(content)
            if total > outbound.MAX_ATTACHMENT_BYTES:
                raise CustomHTTPException(status_code=413, message="Files can weigh 10 MB at most together.",
                                          error_code="ATTACHMENTS_TOO_LARGE", errors={"limit_bytes": outbound.MAX_ATTACHMENT_BYTES})
            uploads.append(outbound.Upload(filename=value.filename[:255], content_type=(value.content_type or "application/octet-stream")[:120],
                                           content=content))
        else:
            fields.setdefault(key, []).append(str(value))
    return fields, uploads


def _first(fields: dict, name: str, default: str = "") -> str:
    values = fields.get(name) or []
    return values[0] if values else default


def _ids(fields: dict, name: str) -> list[int]:
    out = []
    for value in fields.get(name) or []:
        for part in str(value).replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    return out[: outbound.MAX_FILES]


@club_router.post(
    "/messages", summary="Write new mail, reply, reply to all or forward (multipart, files in `files`)",
    openapi_extra={"requestBody": {"content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["mode", "text"],
        "properties": {
            "mode": {"type": "string", "enum": list(outbound.MODES)}, "message_id": {"type": "integer"},
            "to": {"type": "string"}, "cc": {"type": "string"}, "bcc": {"type": "string"},
            "subject": {"type": "string"}, "text": {"type": "string"}, "include_signature": {"type": "boolean"},
            "forward_attachments": {"type": "string", "description": "Ids of the original's files to send on, comma-separated"},
            "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
        },
    }}}}},
)
@limiter.limit("30/minute")
async def send(request: Request, session: SessionDep, user: ClubAuth) -> MessageOut:
    """Always from the club's own address. The mail as kept: `sent`, or
    `failed` with the reason (send it again: /messages/{id}/retry). 422
    NO_RECIPIENTS / INVALID_ADDRESS / TOO_MANY_RECIPIENTS /
    RECIPIENT_SUPPRESSED / EMPTY_MAIL, 413 ATTACHMENTS_TOO_LARGE, 429
    MAILBOX_RATE_LIMITED, 402 NO_EMAIL_CREDITS, 403 PLAN_FEATURE_LOCKED,
    409 NO_MAILBOX. One email credit each."""
    access.require_mail(request)
    service.require_on()
    club = await service.club_of(session, user)
    fields, uploads = await _uploads(request)
    raw_id = _first(fields, "message_id").strip()
    draft = outbound.Draft(
        mode=_first(fields, "mode", "new").strip() or "new",
        message_id=int(raw_id) if raw_id.isdigit() else None,
        to=outbound.split_addresses(fields.get("to") and ",".join(fields["to"])),
        cc=outbound.split_addresses(fields.get("cc") and ",".join(fields["cc"])),
        bcc=outbound.split_addresses(fields.get("bcc") and ",".join(fields["bcc"])),
        subject=_first(fields, "subject"), text=_first(fields, "text"),
        include_signature=_first(fields, "include_signature", "true").strip().lower() not in ("false", "0", "no"),
        forward_attachment_ids=_ids(fields, "forward_attachments"), uploads=uploads,
    )
    msg = await outbound.compose(session, club, draft, _actor(request, user), actor_user=user)
    files = (await mail.attachments_of(session, club.id, [msg.id])).get(msg.id, [])
    out = mail.message_out(msg, files)
    await live.changed(club.id, msg.thread_id, "message")
    return out


@club_router.post("/messages/{message_id}/retry", summary="Send a mail that failed again")
@limiter.limit("30/minute")
async def retry(message_id: int, request: Request, session: SessionDep, user: ClubAuth) -> MessageOut:
    access.require_mail(request)
    club = await service.club_of(session, user)
    msg = await outbound.retry(session, club, message_id, actor_user=user)
    files = (await mail.attachments_of(session, club.id, [msg.id])).get(msg.id, [])
    out = mail.message_out(msg, files)
    await live.changed(club.id, msg.thread_id, "message")
    return out


@club_router.get("/attachments/{attachment_id}", summary="A file of a mail, for the signed-in team",
                 response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
async def get_attachment(attachment_id: int, request: Request, session: SessionDep, user: ClubAuth) -> Response:
    """Never at a public address. Shown in place only when it is a picture,
    a sound or a video a browser cannot run (never SVG or HTML); anything
    else is a download as bytes (or a document's own type), and nothing in
    it runs on this origin (a sandboxing CSP, nosniff). 410 ATTACHMENT_GONE
    when the file can't be read."""
    from sqlalchemy import select

    from app.api.club_mailbox.models import ClubMailAttachments

    club, _mailbox = await _reading(session, request, user)
    row = await session.scalar(
        select(ClubMailAttachments).where(ClubMailAttachments.id == attachment_id, ClubMailAttachments.club_id == club.id)
    )
    if row is None:
        raise CustomHTTPException(status_code=404, message="No such file.", error_code="ATTACHMENT_NOT_FOUND")
    content = await asyncio.to_thread(store.get, row.storage_key)
    if content is None:
        raise CustomHTTPException(status_code=410, message="That file can't be read any more.", error_code="ATTACHMENT_GONE")
    media_type, headers = media_headers(row.content_type, row.filename)
    return Response(content=content, media_type=media_type, headers=headers)


@club_router.get("/stream", summary="The mailbox's changes as server-sent events",
                 response_class=Response, responses={200: {"content": {"text/event-stream": {}}}})
async def stream(request: Request, session: SessionDep, user: ClubAuth) -> Response:
    """A `changed` frame names the thread that changed (`conversation_id`),
    never what it says. 404 MAILBOX_STREAM_OFF while APP_LIVE_SSE is off:
    the dashboard polls. 503 LIVE_STREAM_FULL when the server holds all the
    streams it can (Retry-After)."""
    from app.api.live import stream as live_stream
    from app.core.net import client_address

    access.require_mail(request)
    service.require_on()
    if not live.on():
        raise CustomHTTPException(404, message="The mailbox is not pushed here; poll it.", error_code="MAILBOX_STREAM_OFF")
    club = await service.club_of(session, user)
    club_id = club.id
    address = client_address(request) or "unknown"
    try:
        live.admit(address)
    except live_stream.Full as full:
        refusal = CustomHTTPException(503, message=f"Too many live streams here ({full}); poll for now.", error_code="LIVE_STREAM_FULL")
        return JSONResponse(refusal.to_dict(), status_code=503, headers={"Retry-After": "60"})
    try:
        await session.commit()
        return live.MailStream(club_id, address=address)
    except BaseException:
        live.release(address)
        raise


# ─── The whole mailbox: export and deletion (the club's own account) ─────


@club_router.get("/export", summary="All of the club's mail as one mbox file",
                 response_class=Response, responses={200: {"content": {"application/mbox": {}}}})
@limiter.limit("5/hour")
async def export_mail(request: Request, session: SessionDep, user: ClubAuth) -> Response:
    """The club's own account only (403 OWNER_ONLY; an admin acting as the
    club, 403 MAILBOX_PRIVATE). Works whatever the plan, so a lapsed
    mailbox's mail can be kept. Audited."""
    from app.api.audit import service as audit

    access.require_owner(request)
    service.require_on()
    club = await service.club_of(session, user)
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    count = 0
    async for chunk in export.stream(session, club.id):
        spool.write(chunk)
        count += 1
    await audit.record(
        session, actor=user, action=f"{service.ACTION}.export", entity_type="club", entity_id=club.id,
        summary=f"Exported the club's mailbox ({count} mails)", details={"messages": count}, request=request,
    )
    await session.commit()
    spool.seek(0)

    def chunks():
        try:
            while True:
                block = spool.read(256 * 1024)
                if not block:
                    return
                yield block
        finally:
            spool.close()

    return StreamingResponse(
        chunks(), media_type="application/mbox",
        headers={"Content-Disposition": 'attachment; filename="club-mail.mbox"', "Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff"},
    )


@club_router.delete("/mail", summary="Delete all of the club's mail and files, for good")
async def wipe(request: Request, session: SessionDep, user: ClubAuth, confirm: str = Query(..., description='"DELETE"')) -> dict:
    """The club's own account only. `confirm=DELETE`. The address stays.
    Audited (how many, never what)."""
    from sqlalchemy import select

    from app.api.audit import service as audit
    from app.api.club_mailbox.models import ClubMailThreads

    access.require_owner(request)
    service.require_on()
    if confirm != "DELETE":
        raise CustomHTTPException(422, message='Type DELETE to delete all of the mail.', error_code="CONFIRM_DELETE")
    club = await service.club_of(session, user)
    ids = list((await session.scalars(select(ClubMailThreads.id).where(ClubMailThreads.club_id == club.id))).all())
    keys = await mail.delete_threads(session, club.id, ids)
    await alerts.forget_thread(session, club.id, ids)
    await audit.record(
        session, actor=user, action=f"{service.ACTION}.wipe", entity_type="club", entity_id=club.id,
        summary=f"Deleted all of the club's mail ({len(ids)} conversations)", details={"threads": len(ids)}, request=request,
    )
    await session.commit()
    await asyncio.to_thread(store.delete, keys)
    await live.changed(club.id, None, "thread")
    return {"deleted_threads": len(ids)}


# ─── Admin ───────────────────────────────────────────────────────────────


@admin_router.get("/{club_id}", summary="A club's mailbox: its address, whether it is open, counts (admin)")
async def admin_get(club_id: int, session: SessionDep, admin: AdminAuth) -> AdminMailboxOut:
    """No subject, no sender, no word of any mail."""
    return await service.admin_out(session, club_id)


# ─── The attachment proxy's headers ──────────────────────────────────────

#: Shown in the dashboard itself (an <img>, <audio> or <video>): kinds a
#: browser cannot run. Never SVG, never HTML.
INLINE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "audio/ogg", "audio/aac", "audio/mpeg", "audio/mp4", "video/mp4", "video/webm",
})
#: Documents saved with their own type (a browser's viewer opens them, sandboxed).
DOCUMENT_TYPES = frozenset({
    "application/pdf", "text/plain", "text/csv",
    "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
})
MEDIA_CSP = "default-src 'none'; sandbox"


def media_headers(mime: Optional[str], filename: Optional[str]) -> tuple[str, dict]:
    """The type and headers a file is served with: inline only for
    INLINE_TYPES; every other kind an attachment, as its own type only when
    a document a browser opens safely, else `application/octet-stream`.
    Always a CSP that runs nothing, nosniff, and the name in ASCII and in
    UTF-8 (RFC 6266)."""
    from app.api.club_whatsapp.inbox import _filename_parts

    base = (mime or "").split(";", 1)[0].strip().lower()
    inline = base in INLINE_TYPES
    media_type = base if (inline or base in DOCUMENT_TYPES) else "application/octet-stream"
    if base.startswith("text/") and base in DOCUMENT_TYPES:
        media_type = f"{base}; charset=utf-8"
    ascii_name, encoded = _filename_parts(filename, "attachment")
    disposition = "inline" if inline else "attachment"
    disposition += f'; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'
    return media_type, {
        "Content-Disposition": disposition,
        "Content-Security-Policy": MEDIA_CSP,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }


# ─── SES's notifications (through SNS) ───────────────────────────────────

#: SNS's largest message is 256 KB; an envelope past this is refused unread.
HOOK_MAX_BYTES = 300 * 1024


def _answer(status: int, body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=status)


@hook_router.post("/ses", summary="SES's notification that a mail for a club's address arrived (via SNS)")
async def ses_hook(request: Request, session: SessionDep) -> Response:
    """An SNS envelope, signed by Amazon (its certificate fetched only from
    an sns.<region>.amazonaws.com host), from the configured topic alone
    (APP_MAILBOX_SNS_TOPIC_ARN). The first call is the subscription, confirmed
    by fetching its SubscribeURL. Then each notification names a mail SES
    wrote to the inbound bucket: read, kept or dropped (inbound.py).

    Answers: 200 taken (kept, dropped, or seen before); 403 an unsigned
    envelope or another topic's; 413 a body larger than SNS sends; 503 the
    raw mail couldn't be read or kept now (SNS delivers again, by the
    subscription's retry policy; nothing is kept twice). Never a mail, an
    address or the body in the log."""
    from app.api.hooks import sns

    length = (request.headers.get("content-length") or "").strip()
    if length and (not length.isdigit() or int(length) > HOOK_MAX_BYTES):
        return _answer(413, {"ok": False, "reason": "too_large"})
    raw = await request.body()
    if len(raw) > HOOK_MAX_BYTES:
        return _answer(413, {"ok": False, "reason": "too_large"})
    msg = sns.parse(raw)
    if msg is None:
        return _answer(400, {"ok": False, "reason": "not_sns"})
    topic = (settings.MAILBOX_SNS_TOPIC_ARN or "").strip()
    if not topic or msg.get("TopicArn") != topic:
        logger.warning("mailbox hook refused: not the mailbox's topic")
        return _answer(403, {"ok": False, "reason": "topic"})
    if not await asyncio.to_thread(sns.verify, msg):
        logger.warning("mailbox hook refused: signature did not verify")
        return _answer(403, {"ok": False, "reason": "signature"})
    kind = msg.get("Type")
    if kind == "SubscriptionConfirmation":
        ok = await asyncio.to_thread(sns.confirm, msg)
        logger.info("mailbox hook: subscription to the topic %s", "confirmed" if ok else "not confirmed")
        return _answer(200 if ok else 403, {"ok": ok, "reason": "subscribed" if ok else "subscribe_url"})
    if kind != "Notification":
        return _answer(200, {"ok": True, "reason": kind or "ignored"})
    try:
        message = json.loads(msg.get("Message") or "{}")
    except ValueError:
        return _answer(200, {"ok": False, "reason": "not_json"})
    try:
        received = providers.ses_received(message)
    except providers.NotOurs as exc:
        logger.info("mailbox hook: a notification not for the mailbox (%s)", exc)
        return _answer(200, {"ok": True, "reason": "not_ours"})
    try:
        outcome = await inbound.receive(session, received)
    except providers.Unreadable as exc:
        logger.warning("mailbox hook: mail %s couldn't be read from the bucket (%s); SNS will deliver again",
                       received.provider_message_id[:24], exc)
        await session.rollback()
        return _answer(503, {"ok": False, "reason": "unreadable"})
    except Exception as exc:  # noqa: BLE001 - never a mail or an address in the log
        logger.warning("mailbox hook: mail %s failed (%s)", received.provider_message_id[:24], type(exc).__name__)
        await session.rollback()
        return _answer(503, {"ok": False, "reason": "retry"})
    for club_id, thread_id in outcome.kept:
        await live.changed(club_id, thread_id, "message")
    await asyncio.to_thread(providers.discard, received)
    return _answer(200, {"ok": True, "kept": len(outcome.kept), "dropped": outcome.dropped, "duplicate": outcome.duplicate})
