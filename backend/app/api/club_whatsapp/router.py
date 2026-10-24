"""A club's own WhatsApp number: its connection, its inbox, its templates;
what a platform admin may see of it; and the webhook Meta calls for it.

Enterprise (plan flag `whatsapp_own`): connecting, sending and submitting
templates answer 403 PLAN_FEATURE_LOCKED on other plans, and the page says
what to upgrade to. Reading what exists is never gated.

Who: the connection is the owner's and its admins'; the inbox and the
templates are the owner's and the members it gave the WhatsApp permission
(access.py). A platform admin acting as the club sees the connection and its
quality, never a message (403 WHATSAPP_PRIVATE); the admin portal's view has
no message content at all.
"""
from __future__ import annotations

import hmac
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api.club_access import service as club_access
from app.api.club_whatsapp import access, crypto, graph, inbox, live, templates, webhook
from app.api.club_whatsapp import service
from app.api.club_whatsapp.models import DISCONNECTED, ClubWhatsAppConversations, ClubWhatsAppQualityLog
from app.api.club_whatsapp.schemas import (
    AccountKeysIn,
    AccountOut,
    AccountSettingsIn,
    AdminWhatsAppOut,
    ConversationList,
    ConversationOut,
    ConversationPatch,
    MessageList,
    MessageOut,
    OverviewOut,
    SendIn,
    StandardInstallOut,
    StartIn,
    TemplateIn,
    TemplateList,
)
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

club_router = APIRouter(prefix="/clubs/whatsapp", tags=["club-whatsapp"])
admin_router = APIRouter(prefix="/admin/club-whatsapp", tags=["club-whatsapp"])
hook_router = APIRouter(prefix="/hooks/whatsapp/club", tags=["hooks"])


async def _account_out(session, request: Request, club_id: int, user) -> dict:
    return await service.account_out(
        session, club_id, manage=access.can_manage(request), inbox=access.can_inbox(request), actor=user,
        impersonated=access.impersonated(),
    )


def _actor(request: Request, user) -> inbox.Actor:
    member = club_access.current_member(request)
    return inbox.Actor(
        user_id=getattr(user, "id", None) if member is None else None,
        member_id=getattr(member, "id", None),
        name=access.actor_name(request, user),
    )


# ─── The connection ──────────────────────────────────────────────────────


@club_router.get("/account", summary="The club's own WhatsApp number, if any")
async def get_account(request: Request, session: SessionDep, user: ClubAuth) -> AccountOut:
    """`webhook_url` and `verify_token` are for the club to paste into its
    Meta app (WhatsApp → Configuration), subscribing to `webhook_fields`;
    the verify token only to those who manage the connection. The access
    token and app secret are never returned (`token_last4` is all there is).
    `plan_allows` false: the page shows `upgrade`."""
    access.require_any(request)
    club = await service.club_of(session, user)
    return await _account_out(session, request, club.id, user)


@club_router.put("/account", summary="Connect the club's own WhatsApp number, or change its keys")
async def put_account(payload: AccountKeysIn, request: Request, session: SessionDep, user: ClubAuth) -> AccountOut:
    """Checked with Meta first: the number is read and must be in that
    WhatsApp Business Account. 422 INVALID_WHATSAPP_KEYS (`errors.field`),
    WHATSAPP_TOKEN_INVALID, WHATSAPP_PERMISSION_MISSING,
    WHATSAPP_NUMBER_NOT_FOUND, WHATSAPP_NUMBER_NOT_IN_WABA,
    WHATSAPP_APP_SECRET_WRONG, WHATSAPP_ACCOUNT_BLOCKED (Meta's code in
    `errors.meta_code`); 409 WHATSAPP_NUMBER_IN_USE; 502 META_UNREACHABLE;
    503 WHATSAPP_UNAVAILABLE; 403 PLAN_FEATURE_LOCKED."""
    access.require_manage(request)
    club = await service.club_of(session, user)
    keys = service.KeysIn(
        waba_id=payload.waba_id, phone_number_id=payload.phone_number_id, access_token=payload.access_token,
        app_secret=payload.app_secret, app_id=payload.app_id,
    )
    await service.connect(session, club, user, keys, member=club_access.current_member(request), request=request)
    return await _account_out(session, request, club.id, user)


@club_router.post("/account/verify", summary="Check the number with Meta again: keys, quality, limit")
async def verify_account(request: Request, session: SessionDep, user: ClubAuth) -> AccountOut:
    access.require_manage(request)
    club = await service.club_of(session, user)
    await service.verify(session, club, user, request=request)
    return await _account_out(session, request, club.id, user)


@club_router.patch("/account", summary="Routing and retention")
async def patch_account(payload: AccountSettingsIn, request: Request, session: SessionDep, user: ClubAuth) -> AccountOut:
    """`routing`: whether the club's tickets, certificates and reminders go
    from its own number (when it has the templates approved). `retention_months`:
    how long the inbox keeps a message, 1 to 36."""
    access.require_manage(request)
    club = await service.club_of(session, user)
    await service.update_settings(session, club, user, routing=payload.routing, retention_months=payload.retention_months, request=request)
    return await _account_out(session, request, club.id, user)


@club_router.delete("/account", summary="Disconnect the number; its keys are wiped")
async def delete_account(request: Request, session: SessionDep, user: ClubAuth) -> AccountOut:
    access.require_manage(request)
    club = await service.club_of(session, user)
    await service.disconnect(session, club, user, request=request)
    return await _account_out(session, request, club.id, user)


@club_router.get("/overview", summary="The number's quality and limit, and how the inbox is answered")
async def overview(request: Request, session: SessionDep, user: ClubAuth, days: int = Query(7, ge=1, le=90)) -> OverviewOut:
    access.require_any(request)
    club = await service.club_of(session, user)
    account = await _account_out(session, request, club.id, user)
    history = []
    row = await service.live_account(session, club.id) or await service.latest_account(session, club.id)
    if row is not None:
        history = (await session.scalars(
            select(ClubWhatsAppQualityLog).where(ClubWhatsAppQualityLog.account_id == row.id)
            .order_by(ClubWhatsAppQualityLog.created_at.desc()).limit(10)
        )).all()
    return {
        "account": account,
        "stats": await inbox.stats(session, club.id, days=days) if row is not None else None,
        "quality_history": [
            {"quality_rating": h.quality_rating, "messaging_limit_tier": h.messaging_limit_tier, "previous_rating": h.previous_rating,
             "previous_tier": h.previous_tier, "source": h.source, "created_at": h.created_at}
            for h in history
        ],
    }


# ─── The inbox ───────────────────────────────────────────────────────────


@club_router.get("/conversations", summary="The inbox: conversations, latest first")
async def list_conversations(
    request: Request, session: SessionDep, user: ClubAuth,
    filter: str = Query("all", description="all, open, unanswered, unread or closed"),
    q: Optional[str] = Query(None, max_length=100, description="A name or part of a number"),
    offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
) -> ConversationList:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    rows, total = await inbox.list_conversations(session, club.id, filter=filter, q=q, offset=offset, limit=limit)
    events = await inbox.contact_events(session, club.id, rows)
    unread = await session.scalar(
        select(func.coalesce(func.sum(ClubWhatsAppConversations.unread_count), 0)).where(ClubWhatsAppConversations.club_id == club.id)
    )
    return {"items": [inbox.conversation_out(c, events.get(c.id, [])) for c in rows], "total": total, "unread_total": int(unread or 0)}


@club_router.post("/conversations", summary="Start a conversation with someone registered who asked for WhatsApp")
async def start_conversation(payload: StartIn, request: Request, session: SessionDep, user: ClubAuth) -> ConversationOut:
    """Only someone who ticked "Send my ticket on WhatsApp" for one of the
    club's events (409 NO_OPT_IN otherwise). It opens with a template."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    conv = await inbox.start(session, club.id, payload.registration_id)
    await session.commit()
    events = await inbox.contact_events(session, club.id, [conv])
    return inbox.conversation_out(conv, events.get(conv.id, []))


@club_router.get("/conversations/{conversation_id}/messages", summary="A conversation's messages, oldest first")
async def list_messages(
    conversation_id: int, request: Request, session: SessionDep, user: ClubAuth,
    before: Optional[int] = Query(None, description="Older than this message id"),
    after: Optional[int] = Query(None, description="Newer than this message id"),
    limit: int = Query(50, ge=1, le=200),
) -> MessageList:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    conv = await inbox.get_conversation(session, club.id, conversation_id)
    rows, more = await inbox.messages(session, conv, before=before, after=after, limit=limit)
    # Older messages before the first item (catching up with `after` never pages back).
    has_more = more if after is None else False
    events = await inbox.contact_events(session, club.id, [conv])
    return {"conversation": inbox.conversation_out(conv, events.get(conv.id, [])),
            "items": [inbox.message_out(m) for m in rows], "has_more": has_more}


@club_router.post("/conversations/{conversation_id}/messages", summary="Reply: free text in the window, a template any time")
@limiter.limit("60/minute")
async def send_message(conversation_id: int, payload: SendIn, request: Request, session: SessionDep, user: ClubAuth) -> MessageOut:
    """The message as kept: `status` sent, or failed with Meta's reason in
    `error`. 409 WINDOW_CLOSED (text more than 24 hours after they last
    wrote), TEMPLATE_NOT_APPROVED, NO_WHATSAPP, WHATSAPP_FAILED; 422
    INVALID_TEMPLATE for missing values; 403 PLAN_FEATURE_LOCKED."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    conv = await inbox.get_conversation(session, club.id, conversation_id)
    actor = _actor(request, user)
    if payload.type == "text":
        msg = await inbox.send_text(session, club.id, conv, payload.text or "", actor, actor_user=user)
    else:
        if not payload.template:
            raise CustomHTTPException(status_code=422, message="Pick a template.", error_code="INVALID_TEMPLATE", errors={"field": "template"})
        msg = await inbox.send_template(session, club.id, conv, payload.template, payload.language, payload.params, actor, actor_user=user)
    out = inbox.message_out(msg)
    await session.commit()
    await live.changed(club.id, conv.id, "message")
    return out


@club_router.post("/conversations/{conversation_id}/read", summary="Mark a conversation read")
async def read_conversation(conversation_id: int, request: Request, session: SessionDep, user: ClubAuth) -> ConversationOut:
    """Nothing unread; and Meta is told (best effort), so they see blue ticks."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    conv = await inbox.get_conversation(session, club.id, conversation_id)
    had = conv.unread_count
    wamid = await inbox.mark_read(session, conv)
    await session.commit()
    if had and wamid:
        account = await service.live_account(session, club.id)
        if account is not None and account.status == "active" and account.phone_number_id == conv.business_phone_id:
            try:
                await graph.mark_read(service.keys_of(account), wamid)
            except (graph.GraphError, crypto.Unavailable):
                pass
        await live.changed(club.id, conv.id, "read")
    events = await inbox.contact_events(session, club.id, [conv])
    return inbox.conversation_out(conv, events.get(conv.id, []))


@club_router.patch("/conversations/{conversation_id}", summary="Close or reopen a conversation")
async def patch_conversation(conversation_id: int, payload: ConversationPatch, request: Request, session: SessionDep, user: ClubAuth) -> ConversationOut:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    conv = await inbox.get_conversation(session, club.id, conversation_id)
    await inbox.set_status(session, conv, payload.status)
    await session.commit()
    await live.changed(club.id, conv.id, "conversation")
    events = await inbox.contact_events(session, club.id, [conv])
    return inbox.conversation_out(conv, events.get(conv.id, []))


@club_router.get("/media/{message_id}", summary="An attachment, fetched from Meta for this request",
                 response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
async def get_media(message_id: int, request: Request, session: SessionDep, user: ClubAuth) -> Response:
    """Never stored and never at a public address: fetched with the club's
    token each time, for the signed-in team. Shown in place only when it is
    a picture, a sound or a video a browser cannot run (inbox.INLINE_TYPES);
    anything else is a download, and nothing it holds runs on this origin
    (a sandboxing Content-Security-Policy, nosniff). 410 MEDIA_GONE once
    Meta has let it go (7 days after it arrived), MEDIA_OTHER_NUMBER for one
    that came to a number since replaced."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    content, mime, filename = await inbox.media(session, club.id, message_id)
    media_type, headers = inbox.media_headers(mime, filename)
    return Response(content=content, media_type=media_type, headers=headers)


@club_router.get("/stream", summary="The inbox's changes as server-sent events",
                 response_class=Response, responses={200: {"content": {"text/event-stream": {}}}})
async def stream(request: Request, session: SessionDep, user: ClubAuth) -> Response:
    """A `changed` frame names the conversation that changed (never what it
    says); the dashboard reads it again. 404 WHATSAPP_STREAM_OFF while
    APP_LIVE_SSE is off: the dashboard polls."""
    access.require_inbox(request)
    if not live.on():
        raise CustomHTTPException(404, message="The inbox is not pushed here; poll it.", error_code="WHATSAPP_STREAM_OFF")
    club = await service.club_of(session, user)
    club_id = club.id
    # Nothing of the request's session is held while the stream is open.
    await session.commit()
    return live.InboxStream(club_id)


# ─── Templates ───────────────────────────────────────────────────────────


async def _keys(session, club_id: int):
    account = await service.live_account(session, club_id)
    if account is None:
        raise CustomHTTPException(status_code=409, message="Connect your WhatsApp number first.", error_code="NO_WHATSAPP")
    return account, await service.require_keys(session, account)


@club_router.get("/templates", summary="The club's message templates, from Meta")
async def list_templates(request: Request, session: SessionDep, user: ClubAuth,
                         refresh: bool = Query(True, description="Ask Meta (true), or show what was last read (false)")) -> TemplateList:
    """Read live from Meta (and kept); `stale` when Meta couldn't be asked
    and this is what was last read. `standard_missing`: MyOtherApp's ticket,
    reminder and certificate templates the account lacks."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    account, keys = await _keys(session, club.id)
    stale = False
    if not refresh:
        items = await templates.cached(session, account.id)
    else:
        try:
            items = await templates.sync(session, account, keys)
            await session.commit()
        except graph.GraphError:
            items, stale = await templates.cached(session, account.id), True
    have = {t["name"] for t in items}
    return {"items": items, "stale": stale, "standard_missing": [n for n in templates.STANDARD if n not in have]}


@club_router.post("/templates", summary="Submit a template to Meta for approval")
@limiter.limit("20/hour")
async def create_template(payload: TemplateIn, request: Request, session: SessionDep, user: ClubAuth) -> TemplateList:
    """Checked as Meta checks it first (422 INVALID_TEMPLATE, `errors.field`);
    then Meta reviews it: PENDING, then APPROVED or REJECTED with a reason."""
    from app.api.audit import service as audit

    access.require_inbox(request)
    club = await service.club_of(session, user)
    await service.require_plan(session, club.id, actor=user)
    body = templates.build(payload.model_dump())
    account, keys = await _keys(session, club.id)
    try:
        await graph.create_template(keys, body)
    except graph.GraphError as exc:
        raise service.refusal(exc) from None
    await audit.record(
        session, actor=user, action="club_whatsapp.template_create", entity_type="club", entity_id=club.id,
        summary=f"Submitted WhatsApp template {body['name']} ({body['language']})",
        details={"name": body["name"], "language": body["language"], "category": body["category"],
                 "member_id": getattr(club_access.current_member(request), "id", None)},
        request=request,
    )
    await session.commit()
    return await list_templates(request, session, user, refresh=True)


@club_router.delete("/templates/{name}", summary="Delete a template at Meta")
async def delete_template(name: str, request: Request, session: SessionDep, user: ClubAuth,
                          language: Optional[str] = Query(None, max_length=16)) -> TemplateList:
    """With `language`, that language only; without, every language of the name."""
    from app.api.audit import service as audit

    access.require_inbox(request)
    club = await service.club_of(session, user)
    if not templates.NAME.match(name):
        raise CustomHTTPException(status_code=404, message="No such template.", error_code="TEMPLATE_NOT_FOUND")
    account, keys = await _keys(session, club.id)
    hsm_id = None
    if language:
        row = (await session.scalars(
            select(templates.ClubWhatsAppTemplates).where(
                templates.ClubWhatsAppTemplates.account_id == account.id, templates.ClubWhatsAppTemplates.name == name,
                templates.ClubWhatsAppTemplates.language == language,
            )
        )).first()
        hsm_id = row.meta_id if row is not None else None
    try:
        await graph.delete_template(keys, name, hsm_id)
    except graph.GraphError as exc:
        raise service.refusal(exc) from None
    await audit.record(
        session, actor=user, action="club_whatsapp.template_delete", entity_type="club", entity_id=club.id,
        summary=f"Deleted WhatsApp template {name}{f' ({language})' if language else ''}",
        details={"name": name, "language": language}, request=request,
    )
    await session.commit()
    return await list_templates(request, session, user, refresh=True)


@club_router.post("/templates/standard", summary="Submit MyOtherApp's ticket, reminder and certificate templates")
async def install_standard(request: Request, session: SessionDep, user: ClubAuth) -> StandardInstallOut:
    """Once Meta approves them, the club's tickets, certificates and
    reminders go from its own number (while routing is on)."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    await service.require_plan(session, club.id, actor=user)
    account, keys = await _keys(session, club.id)
    try:
        existing = {t["name"] for t in await templates.sync(session, account, keys)}
        await session.commit()
    except graph.GraphError as exc:
        raise service.refusal(exc) from None
    results = await templates.install_standard(keys, existing)
    try:
        await templates.sync(session, account, keys)
        await session.commit()
    except graph.GraphError:
        pass
    return {"results": results}


# ─── Admin ───────────────────────────────────────────────────────────────


@admin_router.get("/{club_id}", summary="Whether a club has its own WhatsApp number, and its quality (admin)")
async def admin_get(club_id: int, session: SessionDep, admin: AdminAuth) -> AdminWhatsAppOut:
    """No message, no key: the connection's status, quality and limit."""
    return await service.admin_out(session, club_id)


# ─── Meta's webhook for a club's number ──────────────────────────────────


#: Meta's webhook payloads are at most 3 MB (its webhooks overview); a body
#: past this, with a little room, is refused before anything is looked up.
WEBHOOK_MAX_BYTES = 3 * 1024 * 1024 + 256 * 1024


def _hook_answer(status: int, body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=status)


async def _bounded_body(request: Request) -> Optional[bytes]:
    """The body, or None when it is longer than Meta ever sends."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > WEBHOOK_MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@hook_router.get("/{token}", summary="Meta subscribes a club's app by asking for its verify token back")
async def club_hook_verify(token: str, request: Request, session: SessionDep) -> Response:
    q = request.query_params
    account = await service.by_webhook_token(session, token)
    expected = None
    if account is not None and account.verify_token_enc:
        try:
            expected = crypto.decrypt(account.verify_token_enc)
        except crypto.Unavailable:
            expected = None
    given = (q.get("hub.verify_token") or "").encode("utf-8")
    if expected and q.get("hub.mode") == "subscribe" and hmac.compare_digest(given, expected.encode("utf-8")):
        return Response(content=q.get("hub.challenge") or "", media_type="text/plain")
    return Response(content="", media_type="text/plain", status_code=403)


@hook_router.post("/{token}", summary="What Meta tells a club's own number: messages, statuses, templates, quality")
async def club_hook(token: str, request: Request, session: SessionDep) -> Response:
    """Signed by Meta with the club's app secret (`X-Hub-Signature-256`).
    Meta delivers again, for 7 days, whatever is not answered 200, so the
    answer says whether to: 200 when it was taken, and for a token that is
    no club's (or a number since disconnected: nothing will ever take it);
    403 for a signature that doesn't match (the club may be fixing its app
    secret); 413 for a body larger than Meta sends; 503 when the keys can't
    be read or something failed here -- what was kept is not kept twice
    when it comes again. Never a message or the body in the log."""
    length = (request.headers.get("content-length") or "").strip()
    if length and (not length.isdigit() or int(length) > WEBHOOK_MAX_BYTES):
        return _hook_answer(413, {"ok": False, "reason": "too_large"})
    raw = await _bounded_body(request)
    if raw is None:
        return _hook_answer(413, {"ok": False, "reason": "too_large"})
    account = await service.by_webhook_token(session, token)
    if account is None or account.status == DISCONNECTED or not account.app_secret_enc:
        return _hook_answer(200, {"ok": False, "reason": "unknown"})
    try:
        secret = crypto.decrypt(account.app_secret_enc)
    except crypto.Unavailable:
        await service.keys_unreadable(session, account)
        await session.commit()
        return _hook_answer(503, {"ok": False, "reason": "keys"})
    if not webhook.signature_ok(raw, request.headers.get("x-hub-signature-256"), secret):
        logger.warning("club whatsapp hook %s refused: signature did not verify", service.token_hint(token))
        await service.bad_signature(session, account)
        await session.commit()
        return _hook_answer(403, {"ok": False, "reason": "signature"})
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        logger.warning("club whatsapp hook %s: a signed body that isn't JSON", service.token_hint(token))
        return _hook_answer(400, {"ok": False, "reason": "not_json"})
    club_id = account.club_id
    try:
        result = await webhook.handle(session, account, payload if isinstance(payload, dict) else {})
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - never the body or a message in the log
        logger.warning("club whatsapp hook %s failed (%s)", service.token_hint(token), type(exc).__name__)
        await session.rollback()
        return _hook_answer(503, {"ok": False, "reason": "retry"})
    for conversation_id, kind in result["conversations"].items():
        await live.changed(club_id, conversation_id, kind)
    out = {"ok": not result["failed"], "messages": result["messages"], "statuses": result["statuses"], "failed": result["failed"]}
    # What was kept stays kept; Meta brings the rest again.
    return _hook_answer(503 if result["failed"] else 200, out)
