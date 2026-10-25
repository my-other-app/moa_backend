"""The inbox worked as a team: its alerts and settings, opt-outs and blocks,
attachments, assignment, notes and saved replies (/clubs/whatsapp/...).

Every route is the inbox's (access.require_inbox): the club's owner and the
members it gave the WhatsApp permission; an admin acting as the club gets
403 WHATSAPP_PRIVATE on each, as on every inbox route. None of them logs,
audits or mails what a message, a note or a saved reply says.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.api.club_whatsapp import (
    access,
    alerts_inbox,
    blocking,
    inbox,
    live,
    optout,
    outbound_media,
    service,
    teamwork,
)
from app.api.club_whatsapp.inbox_models import ClubWhatsAppInboxSettings
from app.api.club_whatsapp.inbox_schemas import (
    AssignIn,
    BlockIn,
    InboxSettingsIn,
    InboxSettingsOut,
    MyNotifications,
    MyNotificationsIn,
    NoteIn,
    NoteList,
    NoteOut,
    OptOutIn,
    SavedReplyIn,
    SavedReplyList,
    SavedReplyOut,
    SummaryOut,
    TeamOut,
)
from app.api.club_whatsapp.models import ClubWhatsAppConversations
from app.api.club_whatsapp.schemas import ConversationOut, MessageOut
from app.core.auth.dependencies import ClubAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep

router = APIRouter(prefix="/clubs/whatsapp", tags=["club-whatsapp"])


async def _conversation(session, request: Request, user, conversation_id: int):
    access.require_inbox(request)
    club = await service.club_of(session, user)
    return club, await inbox.get_conversation(session, club.id, conversation_id)


async def _out(session, club_id: int, conv) -> dict:
    events = await inbox.contact_events(session, club_id, [conv])
    return inbox.conversation_out(conv, events.get(conv.id, []))


def _actor(request: Request, user) -> inbox.Actor:
    me = teamwork.viewer(request, user)
    return inbox.Actor(user_id=me.user_id, member_id=me.member_id, name=me.name)


# ─── What waits ──────────────────────────────────────────────────────────


@router.get("/summary", summary="What waits in the inbox: the nav's badge and the tab's title")
async def summary(request: Request, session: SessionDep, user: ClubAuth) -> SummaryOut:
    """Counts only, never a name or a word; blocked conversations aside."""
    access.require_inbox(request)
    club = await service.club_of(session, user)
    me = teamwork.viewer(request, user)
    C = ClubWhatsAppConversations
    mine = C.assigned_owner == True if me.owner else C.assigned_member_id == me.member_id  # noqa: E712
    row = (await session.execute(
        select(
            func.coalesce(func.sum(C.unread_count), 0),
            func.count().filter(C.unread_count > 0),
            func.count().filter(C.awaiting_since.isnot(None)),
            func.count().filter(mine, C.status == "open"),
        ).where(C.club_id == club.id, C.blocked_at.is_(None))
    )).one()
    return {"unread_total": int(row[0] or 0), "unread_conversations": int(row[1] or 0), "waiting": int(row[2] or 0),
            "mine": int(row[3] or 0)}


@router.get("/team", summary="Who can answer the inbox: the owner and the members with the WhatsApp permission")
async def team(request: Request, session: SessionDep, user: ClubAuth) -> TeamOut:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    me = teamwork.viewer(request, user)
    return {"items": await teamwork.team(session, club.id, me), "me": me.key}


# ─── Settings ────────────────────────────────────────────────────────────


async def _settings_out(session, club, me) -> dict:
    row = await session.get(ClubWhatsAppInboxSettings, club.id)
    pref = (await alerts_inbox.prefs_of(session, club.id)).get(me.member_id)
    return {
        "alert_after_hours": row.alert_after_hours if row else alerts_inbox.DEFAULT_ALERT_HOURS,
        "stop_reply": row.stop_reply if row else None,
        "start_reply": row.start_reply if row else None,
        "stop_reply_default": optout.reply_text(optout.STOP, None, club.name),
        "start_reply_default": optout.reply_text(optout.START, None, club.name),
        "me": {"email_messages": pref.email_messages if pref else True, "email_assigned": pref.email_assigned if pref else True},
    }


@router.get("/inbox-settings", summary="The inbox's alerts and automatic answers, and my notifications")
async def get_settings(request: Request, session: SessionDep, user: ClubAuth) -> InboxSettingsOut:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    return await _settings_out(session, club, teamwork.viewer(request, user))


@router.patch("/inbox-settings", summary="How long a message waits before the team is told; the answers to STOP and START")
async def patch_settings(payload: InboxSettingsIn, request: Request, session: SessionDep, user: ClubAuth) -> InboxSettingsOut:
    """`alert_after_hours` 1 to 48. `stop_reply`/`start_reply`: the club's
    own words ("{club}" is its name); "" puts the default back. Audited
    (which changed, not the words)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.api.audit import service as audit

    access.require_inbox(request)
    club = await service.club_of(session, user)
    S = ClubWhatsAppInboxSettings
    await session.execute(pg_insert(S.__table__).values(club_id=club.id).on_conflict_do_nothing())
    row = await session.get(S, club.id, populate_existing=True)
    changed = []
    if payload.alert_after_hours is not None and payload.alert_after_hours != row.alert_after_hours:
        row.alert_after_hours = payload.alert_after_hours
        changed.append("alert_after_hours")
    for field in ("stop_reply", "start_reply"):
        value = getattr(payload, field)
        if value is None:
            continue
        value = value.strip() or None
        if value != getattr(row, field):
            setattr(row, field, value)
            changed.append(field)
    if changed:
        row.updated_at = datetime.now(timezone.utc)
        await audit.record(
            session, actor=user, action="club_whatsapp.inbox_settings", entity_type="club", entity_id=club.id,
            summary="Changed the WhatsApp inbox's settings",
            details={"changed": changed, "alert_after_hours": row.alert_after_hours,
                     "member_id": teamwork.viewer(request, user).member_id}, request=request,
        )
    await session.commit()
    return await _settings_out(session, club, teamwork.viewer(request, user))


@router.patch("/inbox-settings/me", summary="My mails about the inbox")
async def patch_my_notifications(payload: MyNotificationsIn, request: Request, session: SessionDep, user: ClubAuth) -> MyNotifications:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    me = teamwork.viewer(request, user)
    row = await alerts_inbox.pref_row(session, club.id, me.member_id)
    if payload.email_messages is not None:
        row.email_messages = payload.email_messages
    if payload.email_assigned is not None:
        row.email_assigned = payload.email_assigned
    await session.commit()
    return {"email_messages": row.email_messages, "email_assigned": row.email_assigned}


# ─── Sending a file ──────────────────────────────────────────────────────


@router.post(
    "/conversations/{conversation_id}/media", summary="Send a photo, video, sound or document (multipart: size, then file)",
    openapi_extra={"requestBody": {"content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["size", "file"],
        "properties": {"size": {"type": "integer"}, "file": {"type": "string", "format": "binary"},
                       "caption": {"type": "string"}, "reply_to": {"type": "integer"}},
    }}}}},
)
@limiter.limit("30/minute")
async def send_media(conversation_id: int, request: Request, session: SessionDep, user: ClubAuth) -> MessageOut:
    """Streamed to Meta as it arrives, never kept here. 415 MEDIA_TYPE, 413
    MEDIA_TOO_LARGE (Meta's sizes, outbound_media.KINDS), 422
    MEDIA_SIZE_MISSING / MEDIA_SIZE_MISMATCH / CAPTION_TOO_LONG /
    CAPTION_NOT_ALLOWED, 409 WINDOW_CLOSED / CONTACT_BLOCKED, 502
    META_UNREACHABLE, 403 PLAN_FEATURE_LOCKED."""
    from app.api.club_whatsapp import replies

    club, conv = await _conversation(session, request, user, conversation_id)
    msg = await outbound_media.send(session, request, club.id, conv, _actor(request, user), actor_user=user)
    out = {**inbox.message_out(msg), "quoted": (await replies.quotes(session, conv.id, [msg])).get(msg.context_wamid)}
    await alerts_inbox.sync_conversation(session, club.id, conv.id)
    await session.commit()
    await live.changed(club.id, conv.id, "message")
    return out


# ─── Assigning, blocking, opting out ─────────────────────────────────────


@router.put("/conversations/{conversation_id}/assignee", summary="Give a conversation to the owner, a member, or nobody")
async def assign(conversation_id: int, payload: AssignIn, request: Request, session: SessionDep, user: ClubAuth) -> ConversationOut:
    """Only to someone who can read the inbox (422 INVALID_ASSIGNEE). They
    are mailed (no content) unless they turned it off. Audited."""
    club, conv = await _conversation(session, request, user, conversation_id)
    if await teamwork.assign(session, club.id, conv, owner=payload.owner, member_id=payload.member_id,
                             me=teamwork.viewer(request, user), user=user, request=request):
        await session.commit()
        await live.changed(club.id, conv.id, "conversation")
    return await _out(session, club.id, conv)


@router.post("/conversations/{conversation_id}/block", summary="Block them: at Meta (within 24 hours of their message), or here only")
async def block(conversation_id: int, payload: BlockIn, request: Request, session: SessionDep, user: ClubAuth) -> ConversationOut:
    """At Meta: 409 BLOCK_WINDOW_CLOSED after 24 hours (errors.local_available:
    block here instead), BLOCKLIST_FULL, BLOCK_REFUSED; 503 BLOCK_TRY_AGAIN.
    Here only (`local_only`): no alerts, no sends, the Blocked filter. Audited."""
    club, conv = await _conversation(session, request, user, conversation_id)
    await blocking.block(session, club.id, conv, local_only=payload.local_only, me=teamwork.viewer(request, user), user=user,
                         request=request, actor_user=user)
    await session.commit()
    await live.changed(club.id, conv.id, "conversation")
    return await _out(session, club.id, conv)


@router.delete("/conversations/{conversation_id}/block", summary="Unblock them (at Meta too, unless local_only)")
async def unblock(conversation_id: int, request: Request, session: SessionDep, user: ClubAuth,
                  local_only: bool = Query(False, description="Forget the block here only (lifted in WhatsApp Manager)")) -> ConversationOut:
    club, conv = await _conversation(session, request, user, conversation_id)
    await blocking.unblock(session, club.id, conv, local_only=local_only, me=teamwork.viewer(request, user), user=user,
                           request=request, actor_user=user)
    await session.commit()
    await live.changed(club.id, conv.id, "conversation")
    return await _out(session, club.id, conv)


@router.post("/conversations/{conversation_id}/optout", summary="Opt them out of the club's WhatsApp, on their word")
async def opt_out(conversation_id: int, payload: OptOutIn, request: Request, session: SessionDep, user: ClubAuth) -> ConversationOut:
    """`all`: no template from the club, and their tickets skip WhatsApp;
    `marketing`: no marketing template. Audited (no content)."""
    club, conv = await _conversation(session, request, user, conversation_id)
    if await optout.team_change(session, club.id, conv, opt_out=True, scope=payload.scope, actor=_actor(request, user),
                                user=user, request=request):
        await session.commit()
        await live.changed(club.id, conv.id, "conversation")
    return await _out(session, club.id, conv)


@router.delete("/conversations/{conversation_id}/optout", summary="Opt them back in, on their word")
async def opt_in(conversation_id: int, request: Request, session: SessionDep, user: ClubAuth,
                 scope: str = Query("all", pattern="^(all|marketing)$")) -> ConversationOut:
    """409 OPTOUT_IS_THEIRS for a marketing stop they set in WhatsApp itself."""
    club, conv = await _conversation(session, request, user, conversation_id)
    if await optout.team_change(session, club.id, conv, opt_out=False, scope=scope, actor=_actor(request, user),
                                user=user, request=request):
        await session.commit()
        await live.changed(club.id, conv.id, "conversation")
    return await _out(session, club.id, conv)


# ─── Notes ───────────────────────────────────────────────────────────────


@router.get("/conversations/{conversation_id}/notes", summary="The team's notes on a conversation, oldest first")
async def list_notes(conversation_id: int, request: Request, session: SessionDep, user: ClubAuth) -> NoteList:
    club, conv = await _conversation(session, request, user, conversation_id)
    me = teamwork.viewer(request, user)
    return {"items": [teamwork.note_out(n, me) for n in await teamwork.notes(session, conv)]}


@router.post("/conversations/{conversation_id}/notes", summary="Leave a note for the team (never sent)")
@limiter.limit("60/minute")
async def add_note(conversation_id: int, payload: NoteIn, request: Request, session: SessionDep, user: ClubAuth) -> NoteOut:
    club, conv = await _conversation(session, request, user, conversation_id)
    me = teamwork.viewer(request, user)
    note = await teamwork.add_note(session, club.id, conv, payload.body, me, user, request)
    out = teamwork.note_out(note, me)
    await session.commit()
    await live.changed(club.id, conv.id, "conversation")
    return out


@router.delete("/conversations/{conversation_id}/notes/{note_id}", summary="Delete a note: its author, or the owner")
async def delete_note(conversation_id: int, note_id: int, request: Request, session: SessionDep, user: ClubAuth) -> NoteList:
    club, conv = await _conversation(session, request, user, conversation_id)
    me = teamwork.viewer(request, user)
    await teamwork.delete_note(session, club.id, conv, note_id, me, user, request)
    await session.commit()
    await live.changed(club.id, conv.id, "conversation")
    return {"items": [teamwork.note_out(n, me) for n in await teamwork.notes(session, conv)]}


# ─── Saved replies ───────────────────────────────────────────────────────


@router.get("/saved-replies", summary="The club's saved replies")
async def list_replies(request: Request, session: SessionDep, user: ClubAuth) -> SavedReplyList:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    return {"items": [teamwork.reply_out(r) for r in await teamwork.saved_replies(session, club.id)]}


@router.post("/saved-replies", summary="Save a reply")
async def create_reply(payload: SavedReplyIn, request: Request, session: SessionDep, user: ClubAuth) -> SavedReplyOut:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    row = await teamwork.save_reply(session, club.id, title=payload.title, body=payload.body, shortcut=payload.shortcut,
                                    me=teamwork.viewer(request, user))
    out = teamwork.reply_out(row)
    await session.commit()
    return out


@router.put("/saved-replies/{reply_id}", summary="Change a saved reply")
async def update_reply(reply_id: int, payload: SavedReplyIn, request: Request, session: SessionDep, user: ClubAuth) -> SavedReplyOut:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    row = await teamwork.save_reply(session, club.id, title=payload.title, body=payload.body, shortcut=payload.shortcut,
                                    me=teamwork.viewer(request, user), reply_id=reply_id)
    out = teamwork.reply_out(row)
    await session.commit()
    return out


@router.delete("/saved-replies/{reply_id}", summary="Delete a saved reply")
async def delete_reply(reply_id: int, request: Request, session: SessionDep, user: ClubAuth) -> SavedReplyList:
    access.require_inbox(request)
    club = await service.club_of(session, user)
    await teamwork.delete_reply(session, club.id, reply_id)
    await session.commit()
    return {"items": [teamwork.reply_out(r) for r in await teamwork.saved_replies(session, club.id)]}
