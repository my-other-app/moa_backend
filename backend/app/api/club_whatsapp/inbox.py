"""A club's WhatsApp inbox: conversations, messages, replies.

A conversation is one person (their WhatsApp id, or their BSUID when Meta
doesn't share the number) on one of the club's business numbers. It is
linked to who the person is to the club when their number matches a
registration (not deleted) for one of its events -- never to anyone else's
account, nor through a follower's profile: a club learns nothing about a
number from MyOtherApp it did not already have.

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

from sqlalchemy import and_, case, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

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
#: Kept, but neither unread nor waiting for an answer: a reaction, a
#: system notice (a changed number), a message WhatsApp can't show.
NOT_COUNTED = frozenset({"reaction", "system", "unsupported"})
#: Not something the person wrote, so it doesn't open the 24-hour window.
#: (Meta: the window starts "when a WhatsApp user messages you"; a reaction
#: or an unsupported message is still theirs.)
NOT_WRITTEN = frozenset({"system"})


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


def display(wa_id: Optional[str]) -> Optional[str]:
    return f"+{wa_id}" if wa_id else None


#: Meta's business-scoped user id: a country code, a period, then up to 128
#: letters and digits ("IN.13491208655302741918"); a parent one has "ENT."
#: after the country.
BSUID = re.compile(r"^[A-Z]{2}\.(?:ENT\.)?[A-Za-z0-9]{1,128}$")


def _digits(raw) -> Optional[str]:
    """A WhatsApp id (the number in digits) as Meta writes it, or None."""
    value = re.sub(r"\D", "", str(raw or ""))[:20]
    return value or None


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
        .where(Events.club_id == club_id, Events.is_deleted == False, R.is_deleted == False,  # noqa: E712
               _digits_tail(R.phone).in_(tails))
        .order_by(R.created_at.desc())
        .limit(50 * len(tails))
    )).all()
    wanted = set(wa_ids)
    return [r for r in rows if wa_of(r[3]) in wanted]


async def link(session, conv_id: int, club_id: int, wa_id: Optional[str]) -> bool:
    """Link the conversation to the club's own registration (and its
    account) whose number this is, latest first. True when linked. Only
    ever through a registration for the club's events -- a number the club
    was given -- never a follower's profile: the club never sees that
    number, and a club can sign its own webhook to ask about any number.
    One UPDATE, and only of a conversation not linked yet."""
    if not wa_id:
        return False
    rows = await _registrations_for(session, club_id, [wa_id])
    if not rows:
        return False
    reg_id, user_id, full_name = rows[0][0], rows[0][1], rows[0][2]
    C = ClubWhatsAppConversations
    await session.execute(
        update(C).where(C.id == conv_id, C.linked_at.is_(None))
        .values(registration_id=reg_id, user_id=user_id, contact_name=(full_name or None) and str(full_name)[:120], linked_at=_now())
        .execution_options(synchronize_session=False)
    )
    return True


async def contact_events(session, club_id: int, convs: list[ClubWhatsAppConversations]) -> dict[int, list[dict]]:
    """The club's events each contact registered for (by their number, or
    the account their conversation is linked to), latest first."""
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.events.models import Events

    from app.api.club_whatsapp import teamwork

    out: dict[int, list[dict]] = {c.id: [] for c in convs}
    if not convs:
        return out
    # And what conversation_out shows of the team's work on each (assignee, opt-out, notes).
    await teamwork.annotate(session, club_id, convs)
    by_phone = await _registrations_for(session, club_id, [c.wa_id for c in convs if c.wa_id])
    users = {c.user_id for c in convs if c.user_id}
    by_user = []
    if users:
        by_user = (await session.execute(
            select(R.id, R.user_id, R.full_name, R.phone, R.created_at, Events.id, Events.name, Events.event_datetime)
            .join(Events, Events.id == R.event_id)
            .where(Events.club_id == club_id, Events.is_deleted == False, R.is_deleted == False, R.user_id.in_(users))  # noqa: E712
            .order_by(R.created_at.desc())
        )).all()
    for c in convs:
        seen = set()
        for row in [*by_phone, *by_user]:
            mine = (c.wa_id is not None and wa_of(row[3]) == c.wa_id) or (c.user_id is not None and row[1] == c.user_id)
            if not mine or row[5] in seen:
                continue
            seen.add(row[5])
            out[c.id].append({"id": row[5], "name": row[6], "event_datetime": row[7], "registration_id": row[0]})
        out[c.id].sort(key=lambda e: _aware(e["event_datetime"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        out[c.id] = out[c.id][:EVENTS_PER_CONTACT]
    return out


# ─── Conversations ───────────────────────────────────────────────────────


def bsuid_of(raw) -> Optional[str]:
    """A business-scoped user id as Meta gives it ("IN.13491208655302741918";
    a parent one has ".ENT."), or None."""
    value = str(raw or "").strip()
    return value if BSUID.match(value) else None


def recipient(conv: ClubWhatsAppConversations) -> dict:
    """Who a send goes to: the phone number when the club has it (Meta's
    `to`, which wins when both are given), else the person's BSUID (Meta's
    `recipient`, for someone with a username whose number isn't shared)."""
    if conv.wa_id:
        return {"to": conv.wa_id}
    return {"recipient": conv.bsuid}


async def conversation(session, club_id: int, business_phone_id: str, wa_id: Optional[str] = None, *,
                       bsuid: Optional[str] = None, account_id: Optional[int], profile_name: Optional[str] = None,
                       username: Optional[str] = None) -> int:
    """The id of the conversation with this person on this number: found by
    their phone number or their BSUID (since April 2026 Meta may give only
    the BSUID of someone with a username), made the first time -- an insert
    that does nothing when another made it first -- and given whichever of
    the two it lacked. Their profile name, username and the connection are
    kept current; it is linked when it is not yet. Its counters and latest
    line are only ever changed by single UPDATEs (`_bump`)."""
    C = ClubWhatsAppConversations
    if not wa_id and not bsuid:
        raise ValueError("a conversation needs a phone number or a BSUID")
    same = [C.club_id == club_id, C.business_phone_id == business_phone_id]
    keys = [C.wa_id == wa_id] if wa_id else []
    keys += [C.bsuid == bsuid] if bsuid else []
    found = None
    for _ in range(2):
        rows = (await session.execute(select(C.id, C.wa_id, C.bsuid, C.linked_at).where(*same, or_(*keys)))).all()
        if rows:
            # Their number's conversation before one known by BSUID alone: the club has known it longer.
            found = sorted(rows, key=lambda r: (not (wa_id and r[1] == wa_id), r[0]))[0]
            break
        now = _now()
        table = C.__table__
        made = (await session.execute(
            pg_insert(table).values(
                club_id=club_id, business_phone_id=business_phone_id, wa_id=wa_id, bsuid=bsuid, account_id=account_id,
                profile_name=(profile_name or None) and profile_name[:120], username=(username or None) and username[:64],
                status=OPEN, unread_count=0, created_at=now, updated_at=now,
            ).on_conflict_do_nothing().returning(table.c.id, table.c.wa_id, table.c.bsuid, table.c.linked_at)
        )).first()
        if made is not None:
            found = made
            break
    if found is None:
        raise RuntimeError("the conversation could not be made or found")
    conv_id, has_wa, has_bsuid, linked_at = found
    values: dict = {
        "profile_name": func.coalesce(literal((profile_name or None) and profile_name[:120], C.profile_name.type), C.profile_name),
        "username": func.coalesce(literal((username or None) and username[:64], C.username.type), C.username),
        "account_id": func.coalesce(literal(account_id, C.account_id.type), C.account_id),
    }
    await _bump(session, conv_id, **values)
    if wa_id and has_wa is None:
        await _claim(session, conv_id, club_id, business_phone_id, "wa_id", wa_id)
    if bsuid and has_bsuid != bsuid:
        await _claim(session, conv_id, club_id, business_phone_id, "bsuid", bsuid)
    if linked_at is None:
        await link(session, conv_id, club_id, wa_id or has_wa)
    return conv_id


async def _claim(session, conv_id: int, club_id: int, business_phone_id: str, field: str, value: str) -> None:
    """Give the conversation this number or BSUID (`field`) -- unless another
    of the club's conversations on that business number already holds it
    (each is unique there)."""
    C, other = ClubWhatsAppConversations, aliased(ClubWhatsAppConversations)
    taken = select(other.id).where(
        other.club_id == club_id, other.business_phone_id == business_phone_id, getattr(other, field) == value, other.id != conv_id,
    ).exists()
    await session.execute(
        update(C).where(C.id == conv_id, ~taken).values({field: value}).execution_options(synchronize_session=False)
    )


async def changed_identity(session, account: ClubWhatsAppAccounts, business_phone_id: str, message: dict) -> Optional[int]:
    """Meta's system message that someone changed their number (which gives
    them a new BSUID: `user_changed_number`) or only their BSUID
    (`user_changed_user_id`): their existing conversation -- found by the
    old BSUID (`previous_user_id`) or the old number (`from`) -- takes the
    new ones, so the thread carries on. None when nothing is known of them
    by either."""
    C = ClubWhatsAppConversations
    system = message.get("system") if isinstance(message.get("system"), dict) else {}
    old_wa = _digits(message.get("from"))
    old_bsuid = bsuid_of(system.get("previous_user_id"))
    new_wa = _digits(system.get("wa_id") or system.get("new_wa_id"))
    new_bsuid = bsuid_of(system.get("user_id"))
    keys = ([C.bsuid == old_bsuid] if old_bsuid else []) + ([C.wa_id == old_wa] if old_wa else [])
    same = [C.club_id == account.club_id, C.business_phone_id == business_phone_id]
    conv_id = await session.scalar(select(C.id).where(*same, or_(*keys)).order_by(C.id).limit(1)) if keys else None
    if conv_id is None:
        if not (new_wa or new_bsuid or old_wa or old_bsuid):
            return None
        return await conversation(session, account.club_id, business_phone_id, new_wa or old_wa, bsuid=new_bsuid or old_bsuid,
                                  account_id=account.id)
    if new_wa and new_wa != old_wa:
        await _claim(session, conv_id, account.club_id, business_phone_id, "wa_id", new_wa)
    if new_bsuid:
        await _claim(session, conv_id, account.club_id, business_phone_id, "bsuid", new_bsuid)
    return conv_id


async def _bump(session, conv_id: int, **values) -> None:
    await session.execute(
        update(ClubWhatsAppConversations).where(ClubWhatsAppConversations.id == conv_id)
        .values(updated_at=_now(), **values).execution_options(synchronize_session=False)
    )


def _line(direction: str, kind: str, body: Optional[str], status: str, at: datetime, *,
          filename: Optional[str] = None, template: Optional[str] = None) -> dict:
    """The conversation's line for a message, as an UPDATE's values: only
    when it is at least as new as the line already there (every SET sees
    the row as it was, so they agree)."""
    C = ClubWhatsAppConversations
    newer = or_(C.last_message_at.is_(None), C.last_message_at <= at)

    def unless_older(value, column):
        return case((newer, literal(value, column.type)), else_=column)

    return {
        "last_message_at": unless_older(at, C.last_message_at),
        "last_message_direction": unless_older(direction, C.last_message_direction),
        "last_message_type": unless_older(kind, C.last_message_type),
        "last_message_preview": unless_older(preview(kind, body, filename=filename, template=template), C.last_message_preview),
        "last_message_status": unless_older(status, C.last_message_status),
    }


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


async def list_conversations(session, club_id: int, *, filter: str = "all", q: Optional[str] = None,
                             offset: int = 0, limit: int = 30, extra: tuple = ()) -> tuple[list[ClubWhatsAppConversations], int]:
    """`extra`: the team's filters' conditions (teamwork.filter_clauses)."""
    C = ClubWhatsAppConversations
    if filter not in FILTERS:
        raise _refuse(422, f"filter is one of {', '.join(FILTERS)}", "INVALID_FILTER")
    where = [C.club_id == club_id, *extra]
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
        options = [C.profile_name.ilike(like), C.contact_name.ilike(like), C.username.ilike(like)]
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
        # Null for someone Meta knows the club by BSUID alone (a username, their number not shared).
        "wa_id": conv.wa_id,
        "bsuid": conv.bsuid,
        "username": conv.username,
        "phone": display(conv.wa_id),
        "profile_name": conv.profile_name,
        "contact": {
            "name": conv.contact_name,
            "wa_id": conv.wa_id,
            "bsuid": conv.bsuid,
            "username": conv.username,
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
        # Assignee, block, opt-out and notes (teamwork.py).
        **_team_out(conv),
    }


def _team_out(conv: ClubWhatsAppConversations) -> dict:
    from app.api.club_whatsapp import teamwork

    return teamwork.team_out(conv)


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
                   limit: int = 50) -> tuple[list[ClubWhatsAppMessages], bool]:
    """A page of the thread, in the order things happened -- Meta's time,
    then id -- and whether there is more: the latest `limit` (more: older
    ones), those before a message (scrolling back; more: older still), or
    those kept since a message (catching up, by id, so one that arrives late
    with an earlier time is not missed; more: newer still). One query of
    `limit + 1` rows each."""
    M = ClubWhatsAppMessages
    q = select(M).where(M.conversation_id == conv.id)
    if after is not None:
        rows = list((await session.scalars(q.where(M.id > after).order_by(M.id.asc()).limit(limit + 1))).all())
        more = len(rows) > limit
        return sorted(rows[:limit], key=lambda m: (_aware(m.wa_timestamp), m.id)), more
    if before is not None:
        pivot = await session.scalar(select(M.wa_timestamp).where(M.id == before, M.conversation_id == conv.id))
        if pivot is not None:
            q = q.where(tuple_(M.wa_timestamp, M.id) < tuple_(literal(pivot, M.wa_timestamp.type), literal(before)))
        else:
            q = q.where(M.id < before)
    rows = list((await session.scalars(q.order_by(M.wa_timestamp.desc(), M.id.desc()).limit(limit + 1))).all())
    more = len(rows) > limit
    return list(reversed(rows[:limit])), more


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
                 "location": None, "context_wamid": ((m.get("context") or {}).get("id") or None),
                 "error_code": None, "error_title": None}
    part = m.get(kind) if isinstance(m.get(kind), dict) else {}
    errors = m.get("errors") if isinstance(m.get("errors"), list) else []
    error = errors[0] if errors and isinstance(errors[0], dict) else None
    if error is not None:
        # What Meta could not pass on (an unsupported message, media it could not fetch): its code and words.
        out["error_code"] = (error.get("code") is not None and str(error.get("code"))[:16]) or None
        detail = (error.get("error_data") or {}).get("details") if isinstance(error.get("error_data"), dict) else None
        out["error_title"] = str(detail or error.get("title") or error.get("message") or "")[:200] or None
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
    elif kind == "system":
        out["body"] = part.get("body")
    if out["body"] is not None:
        out["body"] = str(out["body"])[:TEXT_MAX]
    return out


async def record_inbound(session, account: ClubWhatsAppAccounts, business_phone_id: str, message: dict,
                         contact: Optional[dict] = None) -> Optional[int]:
    """One message someone sent the club's number: kept once (Meta may send
    it again: the insert does nothing on a wamid already kept), the window
    opened, counted unread and waiting for a reply -- in one UPDATE of the
    conversation, so two messages at once both count. A reaction, a system
    notice or a message WhatsApp can't show is kept but neither unread nor
    waiting; a system notice (not something the person wrote) doesn't open
    the window either. The person is their number (`from`), or -- since
    April 2026, for someone with a username whose number Meta doesn't share
    -- only their BSUID (`from_user_id`); `contact` is their entry in the
    webhook's `contacts` (name, username, wa_id, user_id). A system notice
    that they changed their number or BSUID moves their conversation to the
    new ones. The conversation's id, or None when nothing was kept."""
    contact = contact or {}
    wa_id = _digits(message.get("from") or contact.get("wa_id"))
    bsuid = bsuid_of(message.get("from_user_id") or contact.get("user_id"))
    wamid = str(message.get("id") or "")[:128]
    system = str(message.get("type") or "") == "system"
    # A system notice names them in its own body (changed_identity).
    if not wamid or not (wa_id or bsuid or system):
        return None
    if await session.scalar(select(ClubWhatsAppMessages.id).where(ClubWhatsAppMessages.wamid == wamid)) is not None:
        return None
    at = _ts(message.get("timestamp")) or _now()
    if system:
        conv_id = await changed_identity(session, account, business_phone_id, message)
        if conv_id is None:
            return None
    else:
        conv_id = await conversation(session, account.club_id, business_phone_id, wa_id, bsuid=bsuid, account_id=account.id,
                                     profile_name=contact.get("name"), username=contact.get("username"))
    fields = dict(conversation_id=conv_id, club_id=account.club_id, direction=IN, status=RECEIVED, wamid=wamid,
                  wa_timestamp=at, created_at=_now(), **parse_inbound(message))
    table = ClubWhatsAppMessages.__table__
    kept = await session.scalar(
        pg_insert(table).values(**fields).on_conflict_do_nothing(index_elements=[table.c.wamid]).returning(table.c.id)
    )
    if kept is None:
        # Another delivery of it, at the same moment, kept it first.
        return None
    C, kind = ClubWhatsAppConversations, fields["type"]
    values = _line(IN, kind, fields["body"], RECEIVED, at, filename=fields["media_filename"])
    if kind not in NOT_WRITTEN:
        values["last_inbound_at"] = func.greatest(C.last_inbound_at, literal(at, C.last_inbound_at.type))
    if kind not in NOT_COUNTED:
        values.update(unread_count=C.unread_count + 1, awaiting_since=func.coalesce(C.awaiting_since, literal(at, C.awaiting_since.type)),
                      status=OPEN)
    await _bump(session, conv_id, **values)
    return conv_id


async def record_status(session, account: ClubWhatsAppAccounts, st: dict) -> Optional[int]:
    """Meta saying what became of something the club's number sent: the
    message's status moves forward only (a late "delivered" never undoes
    "read", and "failed" never undoes "delivered"), by a conditional UPDATE
    that two statuses at once can't both pass the wrong way; failed carries
    Meta's error. Only this club's own messages: a ticket or certificate its
    number sent is a delivery, which hears it too when the status moved
    (hooks.service, matched by that message's id alone and in this
    transaction), and one Meta failed for the club's account, its template
    or the club's number alone goes again from the platform's (refused_later).
    The conversation's id when a message moved."""
    from app.api.hooks import service as hooks
    from app.api.hooks.verdicts import whatsapp_verdict

    M = ClubWhatsAppMessages
    wamid = str(st.get("id") or "")
    status = str(st.get("status") or "").lower()
    if not wamid or status not in (SENT, DELIVERED, READ, FAILED_MESSAGE):
        return None
    at = _ts(st.get("timestamp")) or _now()
    error = (st.get("errors") or [None])[0] if isinstance(st.get("errors"), list) else None
    error = error if isinstance(error, dict) else None
    msg = await session.scalar(select(M).where(M.wamid == wamid, M.club_id == account.club_id))
    if msg is None or msg.direction != OUT:
        return None
    values: dict = {"status": status, "status_at": at}
    if status == FAILED_MESSAGE:
        before = (PENDING, SENT)
        if error is not None:
            values["error_code"] = (error.get("code") is not None and str(error.get("code"))[:16]) or None
            detail = (error.get("error_data") or {}).get("details") if isinstance(error.get("error_data"), dict) else None
            values["error_title"] = str(detail or error.get("title") or error.get("message") or "WhatsApp couldn't deliver it")[:200]
    else:
        before = tuple(s for s, n in STATUS_ORDER.items() if n < STATUS_ORDER[status])
    moved = await session.scalar(
        update(M).where(M.id == msg.id, M.status.in_(before)).values(**values).returning(M.id)
        .execution_options(synchronize_session=False)
    )
    if moved is None:
        return None
    if msg.via == VIA_DELIVERY and msg.delivery_id is not None:
        verdict = whatsapp_verdict(status, error)
        if verdict is not None:
            await hooks.record(session, wamid, "whatsapp", verdict, at, commit=False)
    if status == FAILED_MESSAGE and error is not None:
        await refused_later(session, account, msg, error)
    latest = await session.scalar(
        select(M.id).where(M.conversation_id == msg.conversation_id).order_by(M.wa_timestamp.desc(), M.id.desc()).limit(1)
    )
    if latest == msg.id:
        await _bump(session, msg.conversation_id, last_message_status=status)
    return msg.conversation_id


async def refused_later(session, account: ClubWhatsAppAccounts, msg: ClubWhatsAppMessages, error: dict) -> None:
    """A message from the club's number that Meta failed after taking it,
    read as a send's refusal would be (whatsapp.refusal): about the club's
    account (the number is marked failed, and the club told once), its
    template (unusable until Meta says otherwise), or only the club's number
    (its test list). Meta says it was not delivered, so a ticket,
    certificate or reminder among them goes again, from the platform's
    number -- the club's own delivery, sent from its number, only."""
    from app.api.deliveries import service as deliveries

    refusal = whatsapp.refusal(0, {"error": error})
    route = accounts.Route(sender=None, account_id=account.id, club_id=account.club_id, template=msg.template_name or "")
    if accounts.is_account_problem(refusal):
        await accounts.mark_failed(session, account, refusal.reason, refusal.code, source=accounts.WEBHOOK)
    elif accounts.is_template_problem(refusal) and msg.template_name:
        await accounts.after_failure(session, route, refusal)
    elif not accounts.is_club_only(refusal):
        return
    if msg.via == VIA_DELIVERY and msg.delivery_id is not None and whatsapp.configured():
        await deliveries.requeue_from_platform(
            session, msg.delivery_id, wamid=msg.wamid, club_id=account.club_id,
            reason=f"{refusal.reason}; sending it again from MyOtherApp's number", code=refusal.code,
        )


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
    kept on the row (failed, with Meta's words), shown in the thread. A
    reply answers what had arrived when it was sent: a message that came
    meanwhile still waits, and is still unread."""
    M, C = ClubWhatsAppMessages, ClubWhatsAppConversations
    seen = await session.scalar(select(func.max(M.id)).where(M.conversation_id == conv.id, M.direction == IN)) or 0
    waited_since = await session.scalar(select(C.awaiting_since).where(C.id == conv.id))
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
    row.created_at = row.wa_timestamp = row.status_at = now
    values = _line(OUT, row.type, row.body, row.status, now, template=row.template_name)
    if row.status != FAILED_MESSAGE:
        values["last_outbound_at"] = func.greatest(C.last_outbound_at, literal(now, C.last_outbound_at.type))
        if row.via == VIA_INBOX:
            if waited_since is not None:
                row.response_seconds = max(0, int((now - _aware(waited_since)).total_seconds()))
            newer = and_(M.conversation_id == conv.id, M.direction == IN, M.id > seen, M.type.not_in(NOT_COUNTED))
            values["awaiting_since"] = select(func.min(M.wa_timestamp)).where(newer).scalar_subquery()
            values["unread_count"] = select(func.count()).select_from(M).where(newer).scalar_subquery()
    session.add(row)
    await session.flush()
    await _bump(session, conv.id, **values)
    if row.status != FAILED_MESSAGE:
        await quota.record_send(session, conv.club_id, "inbox_template" if row.type == "template" else "inbox_reply", row.wamid)
    return row


async def send_text(session, club_id: int, conv: ClubWhatsAppConversations, text: str, actor: Actor, actor_user=None,
                    context: Optional[str] = None) -> ClubWhatsAppMessages:
    """`context`: the wamid it replies to (replies.context_for), quoted above it."""
    from app.api.club_whatsapp import optout

    text = (text or "").strip()
    if not text:
        raise _refuse(422, "Write a message.", "EMPTY_MESSAGE")
    if len(text) > TEXT_MAX:
        raise _refuse(422, f"A message is at most {TEXT_MAX} characters.", "MESSAGE_TOO_LONG")
    account, keys = await _sending_account(session, club_id, actor_user)
    await optout.check_send(session, conv, kind="text")
    if not window_open(conv):
        raise _refuse(
            409,
            "It's been more than 24 hours since they last wrote. WhatsApp only lets you send an approved template now.",
            "WINDOW_CLOSED",
            {"window_expires_at": window_expires_at(conv).isoformat() if window_expires_at(conv) else None},
        )
    if conv.business_phone_id != account.phone_number_id:
        raise _refuse(409, "This conversation was on another of your numbers.", "OTHER_NUMBER")
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", **recipient(conv), "type": "text",
            "text": {"preview_url": False, "body": text}}
    if context:
        body["context"] = {"message_id": context}
    row = ClubWhatsAppMessages(
        conversation_id=conv.id, club_id=club_id, direction=OUT, type="text", body=text, status=PENDING, via=VIA_INBOX,
        sent_by_user_id=actor.user_id, sent_by_member_id=actor.member_id, sent_by_name=actor.name, context_wamid=context,
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
    from app.api.club_whatsapp import optout

    # Not to someone who opted out (nor marketing to someone who stopped it), nor someone blocked.
    await optout.check_send(session, conv, kind="template", category=row_t.category)
    definition = templates.shape(row_t.components or [])
    components, kept = templates.send_components(definition, params or {})
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", **recipient(conv), "type": "template",
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
        .where(R.id == registration_id, Events.club_id == club_id, R.is_deleted == False, Events.is_deleted == False)  # noqa: E712
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
    from app.api.club_whatsapp import optout

    if await optout.opted_out(session, club_id, wa_id=wa_id) is not None:
        raise _refuse(409, "They opted out of your WhatsApp messages, so you can't write to them first.", "OPTED_OUT",
                      {"scope": "all"})
    conv_id = await conversation(session, club_id, account.phone_number_id, wa_id, account_id=account.id)
    conv = await session.get(ClubWhatsAppConversations, conv_id)
    # As the upsert left it (nothing of it is pending here: it is only ever changed by UPDATEs).
    await session.refresh(conv)
    await optout.check_send(session, conv, kind="start")
    return conv


async def record_delivery_send(session, route: accounts.Route, to: str, template: whatsapp.Template, wamid: Optional[str],
                               *, delivery_id=None, kind: str) -> None:
    """A ticket, certificate or reminder the deliveries sent from the club's
    number, in that person's conversation: the inbox shows what went. Not
    an answer: what waits for one still waits."""
    account = await session.get(ClubWhatsAppAccounts, route.account_id)
    if account is None:
        return
    wa_id = to.lstrip("+")
    conv_id = await conversation(session, account.club_id, account.phone_number_id, wa_id, account_id=account.id)
    now = _now()
    definition = None
    row_t = await templates.approved(session, account.id, template.name, route.sender.language)
    if row_t is not None:
        definition = templates.shape(row_t.components or [])
    kept = {"body": list(template.body), "buttons": {"0": template.button} if template.button else {}}
    msg = ClubWhatsAppMessages(
        conversation_id=conv_id, club_id=account.club_id, direction=OUT, type="template",
        body=templates.render(definition, kept)[:TEXT_MAX] if definition else None,
        template_name=template.name, template_language=route.sender.language, template_params=kept,
        wamid=wamid or None, status=SENT, status_at=now, via=VIA_DELIVERY, delivery_id=delivery_id,
        sent_by_name="MyOtherApp", created_at=now, wa_timestamp=now,
    )
    session.add(msg)
    await session.flush()
    C = ClubWhatsAppConversations
    await _bump(session, conv_id, last_outbound_at=func.greatest(C.last_outbound_at, literal(now, C.last_outbound_at.type)),
                **_line(OUT, "template", msg.body, SENT, now, template=template.name))
    # Counted (as the club's, so not at all) by the pipeline, by the answer's "Via".


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


#: Types shown in the dashboard itself (an <img>, <audio> or <video>): what
#: WhatsApp carries that a browser cannot run. Never SVG, never HTML.
INLINE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp",
    "audio/ogg", "audio/aac", "audio/mpeg", "audio/amr", "audio/mp4",
    "video/mp4", "video/3gpp",
})
#: Documents saved with their own type (the browser's viewer opens them, in a
#: sandbox); anything else is saved as bytes.
DOCUMENT_TYPES = frozenset({
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
})
#: What the proxy says of every attachment: nothing in it may run on the
#: dashboard's origin, whatever Meta or the sender called it.
MEDIA_CSP = "default-src 'none'; sandbox"
#: How long Meta keeps what someone sent: a media id from the webhook can be
#: downloaded for 7 days (Meta's changelog, from 9 October 2025).
MEDIA_DAYS = 7


def _base_type(mime: Optional[str]) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def _filename_parts(filename: Optional[str], fallback: str) -> tuple[str, str]:
    """An ASCII name for `filename=` and the name itself, percent-encoded,
    for `filename*=` (RFC 6266): a header carries Latin-1 at most, and a
    Malayalam or emoji name must still reach the person saving it."""
    import unicodedata
    from urllib.parse import quote

    name = "".join(ch for ch in (filename or "") if ch.isprintable() and ch not in '"\\/\r\n')
    name = " ".join(name.split())[:120].strip(" .")
    if not name:
        return fallback, quote(fallback, safe="")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = "".join(ch for ch in ascii_name if ch.isalnum() or ch in "._- ").strip(" .")
    stem, dot, ext = name.rpartition(".")
    ascii_ext = "".join(ch for ch in ext if ch.isascii() and ch.isalnum())[:10] if dot else ""
    if not ascii_name or ascii_name == ascii_ext:
        ascii_name = f"{fallback}.{ascii_ext}" if ascii_ext else fallback
    return ascii_name, quote(name, safe="")


def media_headers(mime: Optional[str], filename: Optional[str]) -> tuple[str, dict]:
    """The type and headers an attachment is served with. Inline only for
    the pictures, sound and video in INLINE_TYPES; every other kind is a
    download, with its own type only when it is a document a browser opens
    safely (DOCUMENT_TYPES), else as bytes. Always a CSP that runs nothing,
    and nosniff."""
    base = _base_type(mime)
    inline = base in INLINE_TYPES
    media_type = base if (inline or base in DOCUMENT_TYPES) else "application/octet-stream"
    ascii_name, encoded = _filename_parts(filename, "attachment")
    disposition = "inline" if inline else "attachment"
    if filename or not inline:
        disposition += f'; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'
    return media_type, {
        "Content-Disposition": disposition,
        "Content-Security-Policy": MEDIA_CSP,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }


def _size(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


async def _fetch(keys: graph.Keys, media_id: str, limit: int) -> tuple[bytes, dict]:
    """The attachment and what Meta said of it. Meta's URL lasts five
    minutes: one that is already gone (404) is asked for again, once."""
    for attempt in (1, 2):
        info = await graph.media_info(keys, media_id)
        if _size(info.get("file_size")) > limit:
            raise graph.GraphError("The attachment is larger than the inbox shows", code="TOO_LARGE")
        try:
            return await graph.download(keys, str(info.get("url") or ""), limit), info
        except graph.GraphError as exc:
            if exc.status == 404 and attempt == 1:
                continue
            raise
    raise graph.GraphError("Meta's address for the attachment was gone twice", status=404)


async def media(session, club_id: int, message_id: int) -> tuple[bytes, str, Optional[str]]:
    """An attachment, fetched from Meta with the club's token for this one
    request: never stored, never at an address anyone else could open.

    410 MEDIA_GONE once Meta has let it go (MEDIA_DAYS after it arrived);
    410 MEDIA_OTHER_NUMBER for one that came to a number the club has since
    replaced; 409 with the token's own error code when Meta no longer
    accepts the token (the number is marked failed, as a check would)."""
    from app.config import settings

    msg = await session.get(ClubWhatsAppMessages, message_id)
    if msg is None or msg.club_id != club_id or not msg.media_id:
        raise _refuse(404, "No such attachment.", "MEDIA_NOT_FOUND")
    account = await accounts.live_account(session, club_id)
    if account is None:
        raise _refuse(409, "Your WhatsApp number isn't connected, so its attachments can't be fetched.", "NO_WHATSAPP")
    came_to = await session.scalar(
        select(ClubWhatsAppConversations.business_phone_id).where(ClubWhatsAppConversations.id == msg.conversation_id)
    )
    keys = await accounts.require_keys(session, account)
    limit = int(settings.CLUB_WHATSAPP_MEDIA_MAX_MB or 25) * 1024 * 1024
    try:
        content, info = await _fetch(keys, msg.media_id, limit)
    except graph.GraphError as exc:
        if exc.code == "TOO_LARGE":
            raise _refuse(413, "That attachment is too large to show here.", "MEDIA_TOO_LARGE") from None
        if exc.unreachable:
            raise _refuse(502, "Couldn't reach WhatsApp for the attachment. Try again.", "META_UNREACHABLE") from None
        if exc.code == "MEDIA_HOST":
            raise _refuse(502, "WhatsApp gave an unexpected address for the attachment, so it wasn't fetched.", "MEDIA_UNAVAILABLE") from None
        if came_to and came_to != account.phone_number_id:
            raise _refuse(410, "This attachment came to the number you connected before. Only that number could fetch it.",
                          "MEDIA_OTHER_NUMBER") from None
        if graph.is_token_problem(exc):
            _status, code, words = graph.explain(exc)
            await accounts.mark_failed(session, account, words, exc.code, source=accounts.READ)
            await session.commit()
            raise _refuse(409, f"Meta didn't accept your number's keys, so the attachment can't be fetched. {words}", code) from None
        # What the club sent lasts 30 days at Meta (outbound_media.py); what arrived, MEDIA_DAYS.
        days = 30 if msg.direction == OUT else MEDIA_DAYS
        raise _refuse(410, f"WhatsApp no longer has this attachment (it keeps them for {days} days).", "MEDIA_GONE") from None
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
        C.__table__.delete().where(
            C.club_id == club_id, ~empty,
            # Its last message past the period, or none ever (started, never written in) and made before it.
            or_(C.last_message_at < cutoff, and_(C.last_message_at.is_(None), C.created_at < cutoff)),
        )
    )
    # A conversation whose latest line was deleted keeps no copy of it.
    await session.execute(
        update(C).where(C.club_id == club_id, C.last_message_at < cutoff)
        .values(last_message_preview=None).execution_options(synchronize_session=False)
    )
    return len(ids)
