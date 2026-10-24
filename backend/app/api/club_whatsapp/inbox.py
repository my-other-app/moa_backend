"""A club's WhatsApp inbox: conversations, messages, replies.

A conversation is one person (their WhatsApp id) on one of the club's
business numbers. It is linked to who the person is to the club when their
number matches a registration for one of its events (or a follower's
WhatsApp on their profile) -- never to anyone else's account: a club learns
nothing about a number from MyOtherApp it did not already have.

The 24-hour customer-service window: once someone writes, the club may send
them anything for 24 hours from their latest message; after that, only a
template Meta approved. `send_text` refuses outside the window (409
WINDOW_CLOSED) rather than have Meta refuse it.

Stats are about how the club answers: replies sent from the inbox, the
median time from someone's first unanswered message to the reply, and the
conversations waiting for one.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.club_whatsapp import graph, live, quota, templates
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import (
    ACTIVE,
    CLOSED,
    DELIVERED,
    FAILED_MESSAGE,
    IN,
    OPEN,
    OUT,
    PENDING,
    READ,
    RECEIVED,
    SENT,
    STATUS_ORDER,
    VIA_DELIVERY,
    VIA_INBOX,
    ClubWhatsAppAccounts,
    ClubWhatsAppConversations,
    ClubWhatsAppMessages,
)
from app.core import whatsapp
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
TEXT_MAX = 4096
PREVIEW_MAX = 160
FILTERS = ("all", "open", "unanswered", "unread", "closed")
#: Events listed per contact, at most.
EVENTS_PER_CONTACT = 5

KNOWN_TYPES = {"text", "image", "document", "audio", "video", "sticker", "location", "reaction", "button",
               "interactive", "contacts", "template", "unsupported", "order", "system"}
MEDIA_TYPES = ("image", "document", "audio", "video", "sticker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ─── The window ──────────────────────────────────────────────────────────


def window_expires_at(conv: ClubWhatsAppConversations) -> Optional[datetime]:
    last = _aware(conv.last_inbound_at)
    return last + WINDOW if last is not None else None


def window_open(conv: ClubWhatsAppConversations, now: Optional[datetime] = None) -> bool:
    ends = window_expires_at(conv)
    return ends is not None and (now or _now()) < ends


# ─── Who a number is ─────────────────────────────────────────────────────


def wa_of(raw: Optional[str]) -> Optional[str]:
    """A number as WhatsApp ids it: digits with the country code (Indian if none)."""
    number, _why = whatsapp.normalise(raw)
    return number.lstrip("+") if number else None


def display(wa_id: str) -> str:
    return f"+{wa_id}"


def _digits_tail(column, n: int = 10):
    return func.right(func.regexp_replace(func.coalesce(column, ""), r"\D", "", "g"), n)


async def _registrations_for(session, club_id: int, wa_ids: list[str]) -> list:
    """The registrations for the club's events whose number is one of these."""
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.events.models import Events

    tails = sorted({w[-10:] for w in wa_ids if w})
    if not tails:
        return []
    rows = (await session.execute(
        select(R.id, R.user_id, R.full_name, R.phone, R.created_at, Events.id, Events.name, Events.event_datetime)
        .join(Events, Events.id == R.event_id)
        .where(Events.club_id == club_id, Events.is_deleted == False, _digits_tail(R.phone).in_(tails))  # noqa: E712
        .order_by(R.created_at.desc())
        .limit(50 * len(tails))
    )).all()
    wanted = set(wa_ids)
    return [r for r in rows if wa_of(r[3]) in wanted]


async def link(session, conv: ClubWhatsAppConversations) -> bool:
    """Link the conversation to the registration (and its account) its
    number matches, latest first; else to a follower of the club whose
    profile has this WhatsApp number. True when linked."""
    rows = await _registrations_for(session, conv.club_id, [conv.wa_id])
    if rows:
        reg_id, user_id, full_name = rows[0][0], rows[0][1], rows[0][2]
        conv.registration_id, conv.user_id = reg_id, user_id
        conv.contact_name = (full_name or None) and str(full_name)[:120]
        conv.linked_at = _now()
        return True
    from app.api.clubs.models import ClubUsersLink
    from app.api.users.models import UserProfiles

    candidates = (await session.execute(
        select(UserProfiles.user_id, UserProfiles.full_name, UserProfiles.whatsapp)
        .join(ClubUsersLink, and_(ClubUsersLink.user_id == UserProfiles.user_id, ClubUsersLink.club_id == conv.club_id))
        .where(ClubUsersLink.is_following == True, _digits_tail(UserProfiles.whatsapp) == conv.wa_id[-10:])  # noqa: E712
        .limit(5)
    )).all()
    for user_id, full_name, number in candidates:
        if wa_of(number) == conv.wa_id:
            conv.user_id = user_id
            conv.contact_name = (full_name or None) and str(full_name)[:120]
            conv.linked_at = _now()
            return True
    return False


async def contact_events(session, club_id: int, convs: list[ClubWhatsAppConversations]) -> dict[int, list[dict]]:
    """The club's events each contact registered for (by their number, or
    the account their conversation is linked to), latest first."""
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.events.models import Events

    out: dict[int, list[dict]] = {c.id: [] for c in convs}
    if not convs:
        return out
    by_phone = await _registrations_for(session, club_id, [c.wa_id for c in convs])
    users = {c.user_id for c in convs if c.user_id}
    by_user = []
    if users:
        by_user = (await session.execute(
            select(R.id, R.user_id, R.full_name, R.phone, R.created_at, Events.id, Events.name, Events.event_datetime)
            .join(Events, Events.id == R.event_id)
            .where(Events.club_id == club_id, Events.is_deleted == False, R.user_id.in_(users))  # noqa: E712
            .order_by(R.created_at.desc())
        )).all()
    for c in convs:
        seen = set()
        for row in [*by_phone, *by_user]:
            mine = wa_of(row[3]) == c.wa_id or (c.user_id is not None and row[1] == c.user_id)
            if not mine or row[5] in seen:
                continue
            seen.add(row[5])
            out[c.id].append({"id": row[5], "name": row[6], "event_datetime": row[7], "registration_id": row[0]})
        out[c.id].sort(key=lambda e: _aware(e["event_datetime"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        out[c.id] = out[c.id][:EVENTS_PER_CONTACT]
    return out


# ─── Conversations ───────────────────────────────────────────────────────


async def conversation(session, club_id: int, business_phone_id: str, wa_id: str, *, account_id: Optional[int],
                       profile_name: Optional[str] = None) -> ClubWhatsAppConversations:
    """The conversation with this person on this number, made the first time."""
    table = ClubWhatsAppConversations.__table__
    now = _now()
    await session.execute(
        pg_insert(table)
        .values(club_id=club_id, business_phone_id=business_phone_id, wa_id=wa_id, account_id=account_id,
                profile_name=(profile_name or None) and profile_name[:120], status=OPEN, unread_count=0,
                created_at=now, updated_at=now)
        .on_conflict_do_nothing(constraint="uq_club_whatsapp_conversations_contact")
    )
    conv = await session.scalar(
        select(ClubWhatsAppConversations)
        .where(ClubWhatsAppConversations.club_id == club_id, ClubWhatsAppConversations.business_phone_id == business_phone_id,
               ClubWhatsAppConversations.wa_id == wa_id)
        .execution_options(populate_existing=True)
    )
    if profile_name and conv.profile_name != profile_name[:120]:
        conv.profile_name = profile_name[:120]
    if account_id and conv.account_id != account_id:
        conv.account_id = account_id
    if conv.linked_at is None:
        await link(session, conv)
    return conv


async def get_conversation(session, club_id: int, conversation_id: int) -> ClubWhatsAppConversations:
    conv = await session.get(ClubWhatsAppConversations, conversation_id)
    if conv is None or conv.club_id != club_id:
        raise _refuse(404, "No such conversation.", "CONVERSATION_NOT_FOUND")
    return conv


def preview(kind: str, body: Optional[str], *, filename: Optional[str] = None, template: Optional[str] = None) -> str:
    words = {
        "image": "Photo", "video": "Video", "audio": "Voice message", "sticker": "Sticker", "document": "Document",
        "location": "Location", "contacts": "Contact card", "reaction": "Reacted", "unsupported": "A message WhatsApp can't show here",
    }
    text = " ".join((body or "").split())
    if kind == "template":
        text = text or f"Template: {template}"
    elif kind == "document":
        text = f"{words['document']}: {text or filename or 'file'}"
    elif kind == "reaction":
        text = f"{words['reaction']} {text}".strip()
    elif kind in words and kind != "document":
        text = f"{words[kind]}{': ' + text if text else ''}"
    return text[:PREVIEW_MAX]


def _touch(conv: ClubWhatsAppConversations, msg: ClubWhatsAppMessages, at: datetime) -> None:
    last = _aware(conv.last_message_at)
    if last is None or at >= last:
        conv.last_message_at = at
        conv.last_message_direction = msg.direction
        conv.last_message_type = msg.type
        conv.last_message_preview = preview(msg.type, msg.body, filename=msg.media_filename, template=msg.template_name)
        conv.last_message_status = msg.status


async def list_conversations(session, club_id: int, *, filter: str = "all", q: Optional[str] = None,
                             offset: int = 0, limit: int = 30) -> tuple[list[ClubWhatsAppConversations], int]:
    C = ClubWhatsAppConversations
    if filter not in FILTERS:
        raise _refuse(422, f"filter is one of {', '.join(FILTERS)}", "INVALID_FILTER")
    where = [C.club_id == club_id]
    if filter == "open":
        where.append(C.status == OPEN)
    elif filter == "closed":
        where.append(C.status == CLOSED)
    elif filter == "unanswered":
        where.append(C.awaiting_since.isnot(None))
    elif filter == "unread":
        where.append(C.unread_count > 0)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        digits = re.sub(r"\D", "", term)
        options = [C.profile_name.ilike(like), C.contact_name.ilike(like)]
        if len(digits) >= 3:
            options.append(C.wa_id.like(f"%{digits}%"))
        where.append(or_(*options))
    total = await session.scalar(select(func.count()).select_from(C).where(*where))
    rows = (await session.scalars(
        select(C).where(*where).order_by(C.last_message_at.desc().nulls_last(), C.id.desc()).offset(offset).limit(limit)
    )).all()
    return list(rows), int(total or 0)


def conversation_out(conv: ClubWhatsAppConversations, events: list[dict], now: Optional[datetime] = None) -> dict:
    now = now or _now()
    return {
        "id": conv.id,
        "wa_id": conv.wa_id,
        "phone": display(conv.wa_id),
        "profile_name": conv.profile_name,
        "contact": {
            "name": conv.contact_name,
            "user_id": conv.user_id,
            "registration_id": conv.registration_id,
            "events": events,
        },
        "status": conv.status,
        "unread_count": conv.unread_count,
        "awaiting_reply": conv.awaiting_since is not None,
        "awaiting_since": conv.awaiting_since,
        "last_message": {
            "direction": conv.last_message_direction,
            "type": conv.last_message_type,
            "preview": conv.last_message_preview,
            "status": conv.last_message_status,
            "at": conv.last_message_at,
        } if conv.last_message_at else None,
        "last_inbound_at": conv.last_inbound_at,
        "window_open": window_open(conv, now),
        "window_expires_at": window_expires_at(conv),
    }


async def set_status(session, conv: ClubWhatsAppConversations, status: str) -> None:
    if status not in (OPEN, CLOSED):
        raise _refuse(422, "status is open or closed", "INVALID_STATUS")
    conv.status = status


async def mark_read(session, conv: ClubWhatsAppConversations) -> Optional[str]:
    """Nothing unread any more. The latest message from them, whose read
    receipt the caller may send to Meta."""
    conv.unread_count = 0
    return await session.scalar(
        select(ClubWhatsAppMessages.wamid)
        .where(ClubWhatsAppMessages.conversation_id == conv.id, ClubWhatsAppMessages.direction == IN,
               ClubWhatsAppMessages.wamid.isnot(None))
        .order_by(ClubWhatsAppMessages.id.desc()).limit(1)
    )


# ─── Messages ────────────────────────────────────────────────────────────


def media_path(message_id: int) -> str:
    return f"/api/v1/clubs/whatsapp/media/{message_id}"


def message_out(m: ClubWhatsAppMessages) -> dict:
    return {
        "id": m.id,
        "direction": m.direction,
        "type": m.type,
        "body": m.body,
        "media": {"url": media_path(m.id), "mime": m.media_mime, "filename": m.media_filename} if m.media_id else None,
        "location": m.location,
        "template": {"name": m.template_name, "language": m.template_language, "params": m.template_params}
        if m.template_name else None,
        "context_wamid": m.context_wamid,
        "wamid": m.wamid,
        "status": m.status,
        "status_at": m.status_at,
        "error": {"code": m.error_code, "title": m.error_title} if (m.error_code or m.error_title) else None,
        "via": m.via,
        "sent_by": m.sent_by_name,
        "created_at": m.created_at,
        "wa_timestamp": m.wa_timestamp,
    }


async def messages(session, conv: ClubWhatsAppConversations, *, before: Optional[int] = None, after: Optional[int] = None,
                   limit: int = 50) -> list[ClubWhatsAppMessages]:
    """A page of the thread, oldest first: the latest `limit`, or those
    before (scrolling back) or after (catching up) a message id."""
    M = ClubWhatsAppMessages
    q = select(M).where(M.conversation_id == conv.id)
    if after is not None:
        rows = (await session.scalars(q.where(M.id > after).order_by(M.id.asc()).limit(limit))).all()
        return list(rows)
    if before is not None:
        q = q.where(M.id < before)
    rows = (await session.scalars(q.order_by(M.id.desc()).limit(limit))).all()
    return list(reversed(rows))


def _ts(value) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_inbound(m: dict) -> dict:
    """One message object from Meta's webhook, as a row's fields."""
    kind = str(m.get("type") or "unsupported")
    kind = kind if kind in KNOWN_TYPES else "unsupported"
    out: dict = {"type": kind, "body": None, "media_id": None, "media_mime": None, "media_filename": None,
                 "location": None, "context_wamid": ((m.get("context") or {}).get("id") or None)}
    part = m.get(kind) if isinstance(m.get(kind), dict) else {}
    if kind == "text":
        out["body"] = part.get("body")
    elif kind in MEDIA_TYPES:
        out["media_id"] = (part.get("id") or None) and str(part.get("id"))[:128]
        out["media_mime"] = (part.get("mime_type") or None) and str(part.get("mime_type"))[:100]
        out["media_filename"] = (part.get("filename") or None) and str(part.get("filename"))[:255]
        out["body"] = part.get("caption")
    elif kind == "location":
        out["location"] = {k: part.get(k) for k in ("latitude", "longitude", "name", "address", "url") if part.get(k) is not None}
        out["body"] = part.get("name") or part.get("address")
    elif kind == "reaction":
        out["body"] = part.get("emoji") or ""
        out["context_wamid"] = part.get("message_id") or out["context_wamid"]
    elif kind == "button":
        out["body"] = part.get("text") or part.get("payload")
    elif kind == "interactive":
        reply = part.get("button_reply") or part.get("list_reply") or part.get("nfm_reply") or {}
        out["body"] = reply.get("title") or reply.get("body") or reply.get("id")
    elif kind == "contacts":
        names = [((c or {}).get("name") or {}).get("formatted_name") for c in (m.get("contacts") or [])]
        out["body"] = ", ".join(n for n in names if n) or None
    if out["body"] is not None:
        out["body"] = str(out["body"])[:TEXT_MAX]
    return out


async def record_inbound(session, account: ClubWhatsAppAccounts, business_phone_id: str, message: dict,
                         profile_name: Optional[str]) -> Optional[ClubWhatsAppConversations]:
    """One message someone sent the club's number: kept once (Meta may send
    it again), the window opened, counted unread and waiting for a reply."""
    wa_id = re.sub(r"\D", "", str(message.get("from") or ""))[:20]
    wamid = str(message.get("id") or "")[:128]
    if not wa_id or not wamid:
        return None
    if await session.scalar(select(ClubWhatsAppMessages.id).where(ClubWhatsAppMessages.wamid == wamid)) is not None:
        return None
    at = _ts(message.get("timestamp")) or _now()
    conv = await conversation(session, account.club_id, business_phone_id, wa_id, account_id=account.id, profile_name=profile_name)
    fields = parse_inbound(message)
    msg = ClubWhatsAppMessages(
        conversation_id=conv.id, club_id=account.club_id, direction=IN, status=RECEIVED, wamid=wamid,
        wa_timestamp=at, created_at=_now(), **fields,
    )
    session.add(msg)
    await session.flush()
    last_in = _aware(conv.last_inbound_at)
    if last_in is None or at > last_in:
        conv.last_inbound_at = at
    conv.unread_count = (conv.unread_count or 0) + 1
    if conv.awaiting_since is None:
        conv.awaiting_since = at
    conv.status = OPEN
    _touch(conv, msg, at)
    return conv


async def record_status(session, account: ClubWhatsAppAccounts, st: dict) -> Optional[int]:
    """Meta saying what became of something the club's number sent: the
    message's status moves forward only (a late "delivered" never undoes
    "read"); failed carries Meta's error. A ticket or certificate sent from
    the club's number is a delivery, which hears it too (hooks.service).
    The conversation's id when a message moved."""
    from app.api.hooks import service as hooks
    from app.api.hooks.verdicts import whatsapp_verdict

    wamid = str(st.get("id") or "")
    status = str(st.get("status") or "").lower()
    if not wamid or status not in (SENT, DELIVERED, READ, FAILED_MESSAGE):
        return None
    at = _ts(st.get("timestamp")) or _now()
    error = (st.get("errors") or [None])[0] if isinstance(st.get("errors"), list) else None
    verdict = whatsapp_verdict(status, error)
    if verdict is not None:
        await hooks.record(session, wamid, "whatsapp", verdict, at)
    msg = await session.scalar(
        select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.wamid == wamid, ClubWhatsAppMessages.club_id == account.club_id)
    )
    if msg is None or msg.direction != OUT:
        return None
    if status == FAILED_MESSAGE:
        if msg.status == READ:
            return None
        msg.status = FAILED_MESSAGE
        if isinstance(error, dict):
            msg.error_code = (error.get("code") is not None and str(error.get("code"))[:16]) or None
            detail = ((error.get("error_data") or {}).get("details") if isinstance(error.get("error_data"), dict) else None)
            msg.error_title = str(detail or error.get("title") or error.get("message") or "WhatsApp couldn't deliver it")[:200]
    elif msg.status != FAILED_MESSAGE and STATUS_ORDER.get(status, -1) > STATUS_ORDER.get(msg.status, -1):
        msg.status = status
    else:
        return None
    msg.status_at = at
    conv = await session.get(ClubWhatsAppConversations, msg.conversation_id)
    if conv is not None:
        latest = await session.scalar(
            select(func.max(ClubWhatsAppMessages.id)).where(ClubWhatsAppMessages.conversation_id == conv.id)
        )
        if latest == msg.id:
            conv.last_message_status = msg.status
    return msg.conversation_id


# ─── Sending ─────────────────────────────────────────────────────────────


@dataclass
class Actor:
    user_id: Optional[int]
    member_id: Optional[int]
    name: str


async def _sending_account(session, club_id: int, actor_user=None) -> tuple[ClubWhatsAppAccounts, graph.Keys]:
    await accounts.require_plan(session, club_id, actor=actor_user)
    account = await accounts.live_account(session, club_id)
    if account is None:
        raise _refuse(409, "Connect your WhatsApp number first.", "NO_WHATSAPP")
    if account.status != ACTIVE:
        raise _refuse(409, "Your WhatsApp number isn't working right now. Check it in WhatsApp settings.", "WHATSAPP_FAILED")
    keys = await accounts.require_keys(session, account)
    return account, keys


async def _send(session, account: ClubWhatsAppAccounts, keys: graph.Keys, conv: ClubWhatsAppConversations, body: dict,
                row: ClubWhatsAppMessages) -> ClubWhatsAppMessages:
    """Send `body` to Meta and keep `row` with what came of it. A refusal is
    kept on the row (failed, with Meta's words), shown in the thread."""
    now = _now()
    try:
        wamid = await graph.send(keys, body)
        row.wamid = wamid or None
        row.status = SENT
    except whatsapp.Unconfirmed as exc:
        row.status = PENDING
        row.error_title = exc.reason[:200]
    except whatsapp.WhatsAppError as exc:
        row.status = FAILED_MESSAGE
        row.error_code = (exc.code or None) and str(exc.code)[:16]
        row.error_title = exc.reason[:200]
        if accounts.is_account_problem(exc):
            await accounts.mark_failed(session, account, exc.reason, exc.code)
    except Exception as exc:  # noqa: BLE001 - Deferred is a MailDeferred, not a WhatsAppError
        row.status = FAILED_MESSAGE
        row.error_code = getattr(exc, "code", None) and str(exc.code)[:16]
        row.error_title = (getattr(exc, "reason", None) or str(exc) or "WhatsApp couldn't be reached. Try again.")[:200]
    row.created_at = now
    row.status_at = now
    session.add(row)
    await session.flush()
    if row.status != FAILED_MESSAGE:
        conv.last_outbound_at = now
        if row.via == VIA_INBOX:
            if conv.awaiting_since is not None:
                row.response_seconds = max(0, int((now - _aware(conv.awaiting_since)).total_seconds()))
            conv.awaiting_since = None
            conv.unread_count = 0
        await quota.record_send(session, conv.club_id, "inbox_template" if row.type == "template" else "inbox_reply", row.wamid)
    _touch(conv, row, now)
    return row


async def send_text(session, club_id: int, conv: ClubWhatsAppConversations, text: str, actor: Actor, actor_user=None) -> ClubWhatsAppMessages:
    text = (text or "").strip()
    if not text:
        raise _refuse(422, "Write a message.", "EMPTY_MESSAGE")
    if len(text) > TEXT_MAX:
        raise _refuse(422, f"A message is at most {TEXT_MAX} characters.", "MESSAGE_TOO_LONG")
    account, keys = await _sending_account(session, club_id, actor_user)
    if not window_open(conv):
        raise _refuse(
            409,
            "It's been more than 24 hours since they last wrote. WhatsApp only lets you send an approved template now.",
            "WINDOW_CLOSED",
            {"window_expires_at": window_expires_at(conv).isoformat() if window_expires_at(conv) else None},
        )
    if conv.business_phone_id != account.phone_number_id:
        raise _refuse(409, "This conversation was on another of your numbers.", "OTHER_NUMBER")
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": conv.wa_id, "type": "text",
            "text": {"preview_url": False, "body": text}}
    row = ClubWhatsAppMessages(
        conversation_id=conv.id, club_id=club_id, direction=OUT, type="text", body=text, status=PENDING, via=VIA_INBOX,
        sent_by_user_id=actor.user_id, sent_by_member_id=actor.member_id, sent_by_name=actor.name,
    )
    return await _send(session, account, keys, conv, body, row)


async def send_template(session, club_id: int, conv: ClubWhatsAppConversations, name: str, language: Optional[str],
                        params: dict, actor: Actor, actor_user=None) -> ClubWhatsAppMessages:
    """An approved template, inside the window or out of it."""
    account, keys = await _sending_account(session, club_id, actor_user)
    if conv.business_phone_id != account.phone_number_id:
        raise _refuse(409, "This conversation was on another of your numbers.", "OTHER_NUMBER")
    row_t = await templates.approved(session, account.id, name, language)
    if row_t is None:
        raise _refuse(409, "That template isn't approved on your WhatsApp account. Refresh the templates and pick an approved one.",
                      "TEMPLATE_NOT_APPROVED")
    definition = templates.shape(row_t.components or [])
    components, kept = templates.send_components(definition, params or {})
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": conv.wa_id, "type": "template",
            "template": {"name": row_t.name, "language": {"code": row_t.language}, "components": components}}
    row = ClubWhatsAppMessages(
        conversation_id=conv.id, club_id=club_id, direction=OUT, type="template", body=templates.render(definition, kept)[:TEXT_MAX],
        template_name=row_t.name, template_language=row_t.language, template_params=kept, status=PENDING, via=VIA_INBOX,
        sent_by_user_id=actor.user_id, sent_by_member_id=actor.member_id, sent_by_name=actor.name,
    )
    return await _send(session, account, keys, conv, body, row)


async def start(session, club_id: int, registration_id) -> ClubWhatsAppConversations:
    """A conversation with someone registered for one of the club's events
    who ticked "Send my ticket on WhatsApp" -- the consent WhatsApp asks a
    business to have before writing first. It opens with a template."""
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.events.models import Events

    row = (await session.execute(
        select(R.id, R.phone, R.whatsapp_opt_in).join(Events, Events.id == R.event_id)
        .where(R.id == registration_id, Events.club_id == club_id)
    )).first()
    if row is None:
        raise _refuse(404, "No such registration for your events.", "REGISTRATION_NOT_FOUND")
    if not row[2]:
        raise _refuse(409, "They didn't ask for WhatsApp messages when they registered, so you can't write to them first.", "NO_OPT_IN")
    wa_id = wa_of(row[1])
    if wa_id is None:
        raise _refuse(409, "Their number isn't one WhatsApp can reach.", "NO_WHATSAPP_NUMBER")
    account = await accounts.live_account(session, club_id)
    if account is None:
        raise _refuse(409, "Connect your WhatsApp number first.", "NO_WHATSAPP")
    await accounts.require_plan(session, club_id)
    return await conversation(session, club_id, account.phone_number_id, wa_id, account_id=account.id)


async def record_delivery_send(session, route: accounts.Route, to: str, template: whatsapp.Template, wamid: Optional[str],
                               *, delivery_id=None, kind: str) -> None:
    """A ticket, certificate or reminder the deliveries sent from the club's
    number, in that person's conversation: the inbox shows what went."""
    account = await session.get(ClubWhatsAppAccounts, route.account_id)
    if account is None:
        return
    wa_id = to.lstrip("+")
    conv = await conversation(session, account.club_id, account.phone_number_id, wa_id, account_id=account.id)
    now = _now()
    definition = None
    row_t = await templates.approved(session, account.id, template.name, route.sender.language)
    if row_t is not None:
        definition = templates.shape(row_t.components or [])
    kept = {"body": list(template.body), "buttons": {"0": template.button} if template.button else {}}
    msg = ClubWhatsAppMessages(
        conversation_id=conv.id, club_id=account.club_id, direction=OUT, type="template",
        body=templates.render(definition, kept)[:TEXT_MAX] if definition else None,
        template_name=template.name, template_language=route.sender.language, template_params=kept,
        wamid=wamid or None, status=SENT, status_at=now, via=VIA_DELIVERY, delivery_id=delivery_id,
        sent_by_name="MyOtherApp", created_at=now,
    )
    session.add(msg)
    await session.flush()
    conv.last_outbound_at = now
    _touch(conv, msg, now)
    await quota.record_send(session, account.club_id, kind, wamid)


# ─── Stats ───────────────────────────────────────────────────────────────


async def stats(session, club_id: int, *, days: int = 7, now: Optional[datetime] = None) -> dict:
    C, M = ClubWhatsAppConversations, ClubWhatsAppMessages
    now = now or _now()
    since = now - timedelta(days=days)
    counts = (await session.execute(
        select(
            func.count(),
            func.count().filter(C.status == OPEN),
            func.count().filter(C.awaiting_since.isnot(None)),
            func.coalesce(func.sum(C.unread_count), 0),
            func.count().filter(C.last_inbound_at >= now - WINDOW),
        ).where(C.club_id == club_id)
    )).one()
    traffic = (await session.execute(
        select(
            func.count().filter(M.direction == IN),
            func.count().filter(and_(M.direction == OUT, M.via == VIA_INBOX, M.status != FAILED_MESSAGE)),
            func.count().filter(and_(M.direction == OUT, M.via == VIA_DELIVERY)),
            # Both pass over the nulls: only replies that answered someone have a time.
            func.percentile_cont(0.5).within_group(M.response_seconds),
            func.avg(M.response_seconds),
        ).where(M.club_id == club_id, M.created_at >= since)
    )).one()
    oldest_waiting = await session.scalar(select(func.min(C.awaiting_since)).where(C.club_id == club_id))
    return {
        "days": days,
        "conversations": int(counts[0] or 0),
        "open": int(counts[1] or 0),
        "unanswered": int(counts[2] or 0),
        "unread": int(counts[3] or 0),
        "window_open": int(counts[4] or 0),
        "received": int(traffic[0] or 0),
        "replies": int(traffic[1] or 0),
        "sent_by_deliveries": int(traffic[2] or 0),
        "median_response_seconds": None if traffic[3] is None else int(traffic[3]),
        "average_response_seconds": None if traffic[4] is None else int(traffic[4]),
        "oldest_unanswered_at": oldest_waiting,
    }


# ─── Media ───────────────────────────────────────────────────────────────


async def media(session, club_id: int, message_id: int) -> tuple[bytes, str, Optional[str]]:
    """An attachment, fetched from Meta with the club's token for this one
    request: never stored, never at an address anyone else could open."""
    from app.config import settings

    msg = await session.get(ClubWhatsAppMessages, message_id)
    if msg is None or msg.club_id != club_id or not msg.media_id:
        raise _refuse(404, "No such attachment.", "MEDIA_NOT_FOUND")
    account = await accounts.live_account(session, club_id)
    if account is None:
        raise _refuse(409, "Your WhatsApp number isn't connected, so its attachments can't be fetched.", "NO_WHATSAPP")
    keys = await accounts.require_keys(session, account)
    limit = int(settings.CLUB_WHATSAPP_MEDIA_MAX_MB or 25) * 1024 * 1024
    try:
        info = await graph.media_info(keys, msg.media_id)
        if int(info.get("file_size") or 0) > limit:
            raise _refuse(413, "That attachment is too large to show here.", "MEDIA_TOO_LARGE")
        content = await graph.download(keys, str(info.get("url") or ""), limit)
    except graph.GraphError as exc:
        if exc.code == "TOO_LARGE":
            raise _refuse(413, "That attachment is too large to show here.", "MEDIA_TOO_LARGE") from None
        if exc.unreachable:
            raise _refuse(502, "Couldn't reach WhatsApp for the attachment. Try again.", "META_UNREACHABLE") from None
        # Meta keeps a received attachment for 30 days.
        raise _refuse(410, "WhatsApp no longer has this attachment (it keeps them for 30 days).", "MEDIA_GONE") from None
    mime = str(info.get("mime_type") or msg.media_mime or "application/octet-stream")
    return content, mime, msg.media_filename


# ─── Retention ───────────────────────────────────────────────────────────


async def forget_before(session, club_id: int, cutoff: datetime, batch: int = 1000) -> int:
    """Delete the club's messages older than `cutoff`, a batch at a time, and
    the conversations left with none older than it. Returns messages deleted."""
    M, C = ClubWhatsAppMessages, ClubWhatsAppConversations
    ids = (await session.scalars(
        select(M.id).where(M.club_id == club_id, M.created_at < cutoff).order_by(M.id).limit(batch)
    )).all()
    if ids:
        await session.execute(M.__table__.delete().where(M.id.in_(ids)))
    empty = select(M.id).where(M.conversation_id == C.id).exists()
    await session.execute(
        C.__table__.delete().where(C.club_id == club_id, C.last_message_at < cutoff, ~empty)
    )
    # A conversation whose latest line was deleted keeps no copy of it.
    await session.execute(
        update(C).where(C.club_id == club_id, C.last_message_at < cutoff)
        .values(last_message_preview=None).execution_options(synchronize_session=False)
    )
    return len(ids)
