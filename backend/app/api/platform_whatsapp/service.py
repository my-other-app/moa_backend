"""What the admin WhatsApp inbox does: keeps what people write to the
platform's own number, reads the platform's automatic sends back from the
delivery log, and sends the admins' replies.

Sending rules (Meta's, and ours):

- Free text only inside the customer-service window: 24 hours from the
  guest's last message.
- Outside it, only an approved template the platform owns (EXPECTED in
  app/api/admin_ops/whatsapp_templates.py) that has no document header
  (MANUAL_TEMPLATES below), with its values.
- Nothing at all to someone who wrote STOP to this number (until START),
  and no template to someone who opted out of the club the thread is about
  (that club's opt-out store, app/api/club_whatsapp/optout.py).

The webhook part never raises: a provider never hears our trouble, and the
statuses of the automatic sends (app/api/hooks/service.py) are kept before
this runs, in their own transaction.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.club_whatsapp import inbox as club_inbox
from app.api.club_whatsapp import optout
from app.api.platform_whatsapp.models import (
    DELIVERED,
    FAILED,
    IN,
    OUT,
    PENDING,
    READ,
    RECEIVED,
    SENT,
    PlatformWaConversations,
    PlatformWaMessages,
)
from app.config import settings
from app.core import whatsapp
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
TEXT_MAX = 4096
PAGE_MAX = 100

#: The templates an admin may send by hand: the platform's own, without a
#: document header (a ticket or a certificate is sent again from the event).
#: Each: the values it takes, in the words the composer shows, and its builder.
MANUAL_TEMPLATES: dict[str, dict] = {
    "event_reminder": {
        "label": "Event reminder",
        "params": ["name", "event", "when", "venue", "ticket_id"],
        "build": whatsapp.event_reminder,
    },
    "payment_pending": {
        "label": "Payment pending",
        "params": ["name", "event", "amount", "slug"],
        "build": whatsapp.payment_pending,
    },
    "event_update": {
        "label": "Event update",
        "params": ["name", "club", "event", "update"],
        "build": whatsapp.event_update,
    },
}

#: How each automatic send is labelled (Deliveries.kind).
KIND_LABELS = {"ticket": "Ticket", "reminder": "Reminder", "certificate": "Certificate", "payment": "Payment",
               "manual": "Manual reply"}
STATUS_ORDER = {PENDING: 0, SENT: 1, DELIVERED: 2, READ: 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(code, message=message, error_code=error_code, errors=errors)


def platform_number_id() -> str:
    return (settings.WHATSAPP_PHONE_NUMBER_ID or "").strip()


# ─── The window ──────────────────────────────────────────────────────────


def window_expires_at(conv: PlatformWaConversations) -> Optional[datetime]:
    last = _aware(conv.last_inbound_at)
    return last + WINDOW if last is not None else None


def window_open(conv: PlatformWaConversations, now: Optional[datetime] = None) -> bool:
    ends = window_expires_at(conv)
    return ends is not None and (now or _now()) < ends


def can_send(conv: PlatformWaConversations, now: Optional[datetime] = None) -> dict:
    """What the composer may send now, in plain words."""
    now = now or _now()
    ends = window_expires_at(conv)
    if conv.opted_out_at is not None:
        return {"text": False, "template": False, "window_expires_at": ends.isoformat() if ends else None,
                "reason": "They wrote STOP to this number. Nothing can be sent until they write START."}
    if window_open(conv, now):
        return {"text": True, "template": True, "window_expires_at": ends.isoformat(),
                "reason": f"They wrote in the last 24 hours: free text is allowed until {ends.isoformat()}."}
    return {"text": False, "template": True, "window_expires_at": ends.isoformat() if ends else None,
            "reason": "More than 24 hours since they last wrote: only an approved template can be sent."}


# ─── Labels: what a message is about ─────────────────────────────────────


def _attempts_to(wa_id: str):
    """The platform number's automatic WhatsApp sends to this number, with
    their event and club (delivery_attempts keep the number each went to)."""
    from app.api.clubs.models import Clubs
    from app.api.deliveries.models import Deliveries, DeliveryAttempts
    from app.api.events.models import Events

    A = DeliveryAttempts
    return (
        select(A.id, A.created_at, A.outcome, A.provider_id, Deliveries.kind, Deliveries.status, Events.id.label("event_id"),
               Events.name.label("event_name"), Clubs.id.label("club_id"), Clubs.name.label("club_name"))
        .join(Deliveries, Deliveries.id == A.delivery_id)
        .join(Events, Events.id == Deliveries.event_id)
        .outerjoin(Clubs, Clubs.id == Events.club_id)
        .where(A.provider == whatsapp.PROVIDER, A.address == wa_id, or_(A.via.is_(None), A.via == whatsapp.VIA))
    )


async def probably_about(session, wa_id: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """The event (and its club) this number was most recently messaged for
    from the platform's number, or (None, None)."""
    if not wa_id:
        return None, None
    q = _attempts_to(wa_id).order_by(_attempts_to(wa_id).selected_columns.created_at.desc()).limit(1)
    row = (await session.execute(q)).first()
    return (row.event_id, row.club_id) if row is not None else (None, None)


# ─── The webhook: what people write ──────────────────────────────────────


async def _conversation(session, wa_id: Optional[str], bsuid: Optional[str], contact: dict) -> int:
    C = PlatformWaConversations
    match = []
    if wa_id:
        match.append(C.wa_id == wa_id)
    if bsuid:
        match.append(C.bsuid == bsuid)
    conv = await session.scalar(select(C).where(or_(*match)).order_by(C.id).limit(1))
    if conv is None:
        conv = C(wa_id=wa_id, bsuid=bsuid, created_at=_now(), last_message_at=_now())
        session.add(conv)
        await session.flush()
    else:
        if wa_id and not conv.wa_id:
            conv.wa_id = wa_id
        if bsuid and not conv.bsuid:
            conv.bsuid = bsuid
    if contact.get("name"):
        conv.profile_name = str(contact["name"])[:120]
    if contact.get("username"):
        conv.username = str(contact["username"])[:120]
    return conv.id


async def record_inbound(session, message: dict, contact: Optional[dict] = None) -> Optional[int]:
    """One message someone sent the platform's number: kept once (by its
    wamid), counted unread, the window opened, labelled with what it is
    probably about, and read for STOP and START. The conversation's id, or
    None when nothing was kept."""
    contact = contact or {}
    wa_id = club_inbox._digits(message.get("from") or contact.get("wa_id"))
    bsuid = club_inbox.bsuid_of(message.get("from_user_id") or contact.get("user_id"))
    wamid = str(message.get("id") or "")[:128]
    kind = str(message.get("type") or "")
    if not wamid or not (wa_id or bsuid) or kind == "system":
        return None
    M, C = PlatformWaMessages, PlatformWaConversations
    if await session.scalar(select(M.id).where(M.wamid == wamid)) is not None:
        return None
    at = club_inbox._ts(message.get("timestamp")) or _now()
    conv_id = await _conversation(session, wa_id, bsuid, contact)
    parsed = club_inbox.parse_inbound(message)
    event_id, club_id = await probably_about(session, wa_id)
    table = M.__table__
    kept = await session.scalar(
        pg_insert(table).values(
            conversation_id=conv_id, direction=IN, type=parsed["type"], body=parsed["body"], media_id=parsed["media_id"],
            media_mime=parsed["media_mime"], media_filename=parsed["media_filename"], wamid=wamid, status=RECEIVED,
            error=parsed["error_title"], event_id=event_id, club_id=club_id, at=at,
        ).on_conflict_do_nothing(index_elements=[table.c.wamid], index_where=table.c.wamid.isnot(None)).returning(table.c.id)
    )
    if kept is None:
        return None
    counted = parsed["type"] not in club_inbox.NOT_COUNTED
    values = dict(
        last_inbound_at=func.greatest(C.last_inbound_at, literal(at, C.last_inbound_at.type)),
        last_message_at=func.greatest(C.last_message_at, literal(at, C.last_message_at.type)),
        last_preview=club_inbox.preview(parsed["type"], parsed["body"], filename=parsed["media_filename"]),
        last_direction=IN,
    )
    if counted:
        values["unread_count"] = C.unread_count + 1
    if event_id is not None:
        values.update(about_event_id=event_id, about_club_id=club_id)
    word = optout.keyword(parsed["body"]) if parsed["type"] == "text" else None
    if word == optout.STOP:
        values["opted_out_at"] = at
    elif word == optout.START:
        values["opted_out_at"] = None
    await session.execute(update(C).where(C.id == conv_id).values(**values).execution_options(synchronize_session=False))
    return conv_id


async def record_status(session, st: dict) -> bool:
    """What became of a manual reply (by its wamid). Only forward."""
    wamid = str(st.get("id") or "")
    status = str(st.get("status") or "").lower()
    if not wamid or status not in (SENT, DELIVERED, READ, FAILED):
        return False
    row = await session.scalar(select(PlatformWaMessages).where(PlatformWaMessages.wamid == wamid, PlatformWaMessages.direction == OUT))
    if row is None:
        return False
    if status == FAILED:
        errors = st.get("errors") if isinstance(st.get("errors"), list) else []
        error = errors[0] if errors and isinstance(errors[0], dict) else {}
        row.status = FAILED
        row.error = str(error.get("title") or error.get("message") or "WhatsApp couldn't deliver it")[:300]
    elif row.status != FAILED and STATUS_ORDER.get(status, 0) > STATUS_ORDER.get(row.status, 0):
        row.status = status
    return True


async def handle(session, payload: dict) -> dict:
    """The platform number's messages in a webhook delivery: each in a
    SAVEPOINT of its own. Only the platform's own number (its phone number
    id): anything for another number is not looked at."""
    number = platform_number_id()
    out = {"messages": 0, "statuses": 0, "failed": 0}
    if not number or not isinstance(payload, dict):
        return out
    for entry in payload.get("entry") or []:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            if not isinstance(change, dict) or change.get("field") not in (None, "messages"):
                continue
            value = change.get("value") if isinstance(change.get("value"), dict) else {}
            if str((value.get("metadata") or {}).get("phone_number_id") or "") != number:
                continue
            contacts: dict = {}
            for c in value.get("contacts") or []:
                if not isinstance(c, dict):
                    continue
                profile = c.get("profile") if isinstance(c.get("profile"), dict) else {}
                seen = {"name": profile.get("name") or None, "username": profile.get("username") or None,
                        "wa_id": c.get("wa_id") or None, "user_id": c.get("user_id") or None}
                for key in (seen["wa_id"], seen["user_id"]):
                    if key:
                        contacts[str(key)] = seen
            for m in value.get("messages") or []:
                if not isinstance(m, dict):
                    continue
                contact = contacts.get(str(m.get("from") or "")) or contacts.get(str(m.get("from_user_id") or ""))
                try:
                    async with session.begin_nested():
                        if await record_inbound(session, m, contact) is not None:
                            out["messages"] += 1
                except Exception as exc:  # noqa: BLE001 - never the message in the log
                    out["failed"] += 1
                    logger.warning("platform whatsapp inbox: a message could not be kept (%s)", type(exc).__name__)
            for st in value.get("statuses") or []:
                if not isinstance(st, dict):
                    continue
                try:
                    async with session.begin_nested():
                        if await record_status(session, st):
                            out["statuses"] += 1
                except Exception as exc:  # noqa: BLE001
                    out["failed"] += 1
                    logger.warning("platform whatsapp inbox: a status could not be kept (%s)", type(exc).__name__)
    return out


async def handle_safely(session, payload: dict) -> dict:
    """`handle`, committed; never raises (the webhook's answer stays as it was)."""
    try:
        result = await handle(session, payload)
        await session.commit()
        return result
    except Exception:  # noqa: BLE001
        logger.warning("platform whatsapp inbox: the delivery could not be kept", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"messages": 0, "statuses": 0, "failed": 1}


# ─── Reading ─────────────────────────────────────────────────────────────


async def _names(session, event_ids: set, club_ids: set) -> tuple[dict, dict]:
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events

    events = dict((await session.execute(select(Events.id, Events.name).where(Events.id.in_(event_ids)))).all()) if event_ids else {}
    clubs = dict((await session.execute(select(Clubs.id, Clubs.name).where(Clubs.id.in_(club_ids)))).all()) if club_ids else {}
    return events, clubs


def _label(event_id, club_id, kind: str, events: dict, clubs: dict) -> dict:
    return {"event_id": event_id, "event_name": events.get(event_id), "club_id": club_id, "club_name": clubs.get(club_id),
            "kind": kind, "kind_label": KIND_LABELS.get(kind, kind.title())}


def conversation_out(conv: PlatformWaConversations, events: dict, clubs: dict, now: Optional[datetime] = None) -> dict:
    return {
        "id": conv.id,
        "wa_id": conv.wa_id,
        "phone": f"+{conv.wa_id}" if conv.wa_id else None,
        "bsuid": conv.bsuid,
        "name": conv.profile_name or conv.username or (f"+{conv.wa_id}" if conv.wa_id else "WhatsApp user"),
        "username": conv.username,
        "last_message_at": _aware(conv.last_message_at).isoformat() if conv.last_message_at else None,
        "last_inbound_at": _aware(conv.last_inbound_at).isoformat() if conv.last_inbound_at else None,
        "last_preview": conv.last_preview,
        "last_direction": conv.last_direction,
        "unread_count": conv.unread_count or 0,
        "about": {"event_id": conv.about_event_id, "event_name": events.get(conv.about_event_id),
                  "club_id": conv.about_club_id, "club_name": clubs.get(conv.about_club_id)},
        "opted_out": conv.opted_out_at is not None,
        "can_send": can_send(conv, now),
    }


async def list_conversations(session, *, club_id: Optional[int] = None, event_id: Optional[int] = None, unread: bool = False,
                             q: Optional[str] = None, page: int = 1, page_size: int = 30) -> dict:
    C = PlatformWaConversations
    query = select(C)
    if club_id is not None:
        query = query.where(C.about_club_id == club_id)
    if event_id is not None:
        query = query.where(C.about_event_id == event_id)
    if unread:
        query = query.where(C.unread_count > 0)
    words = " ".join((q or "").split())[:80]
    if words:
        digits = "".join(ch for ch in words if ch.isdigit())
        like = f"%{words.replace('%', '').replace('_', '')}%"
        terms = [C.profile_name.ilike(like), C.username.ilike(like)]
        if len(digits) >= 3:
            terms.append(C.wa_id.like(f"%{digits}%"))
        query = query.where(or_(*terms))
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (await session.scalars(query.order_by(C.last_message_at.desc(), C.id.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    events, clubs = await _names(session, {r.about_event_id for r in rows if r.about_event_id},
                                 {r.about_club_id for r in rows if r.about_club_id})
    now = _now()
    return {"items": [conversation_out(r, events, clubs, now) for r in rows], "total": total, "page": page, "page_size": page_size,
            **await unread_totals(session)}


async def unread_totals(session) -> dict:
    C = PlatformWaConversations
    row = (await session.execute(
        select(func.count().filter(C.unread_count > 0), func.coalesce(func.sum(C.unread_count), 0))
    )).one()
    return {"unread_conversations": int(row[0] or 0), "unread_messages": int(row[1] or 0)}


async def get_conversation(session, conversation_id: int) -> PlatformWaConversations:
    conv = await session.get(PlatformWaConversations, conversation_id, populate_existing=True)
    if conv is None:
        raise _refuse(404, "No such conversation.", "NOT_FOUND")
    return conv


def _iso(value) -> Optional[str]:
    return _aware(value).isoformat() if value is not None else None


async def thread(session, conv: PlatformWaConversations, *, before: Optional[datetime] = None, limit: int = 50) -> dict:
    """The conversation, newest page first (each page oldest first): what
    they wrote, what the admins answered, and the platform's automatic sends
    to the same number from the delivery log -- each outbound one labelled
    with its event, club and kind. `next_before` pages further back."""
    limit = max(1, min(limit, PAGE_MAX))
    M = PlatformWaMessages
    mq = select(M).where(M.conversation_id == conv.id)
    if before is not None:
        mq = mq.where(M.at < before)
    own = (await session.scalars(mq.order_by(M.at.desc(), M.id.desc()).limit(limit + 1))).all()
    sends = []
    if conv.wa_id:
        aq = _attempts_to(conv.wa_id)
        created = aq.selected_columns.created_at
        if before is not None:
            aq = aq.where(created < before)
        sends = (await session.execute(aq.order_by(created.desc()).limit(limit + 1))).all()
    events, clubs = await _names(session, {m.event_id for m in own if m.event_id} | {s.event_id for s in sends},
                                 {m.club_id for m in own if m.club_id} | {s.club_id for s in sends if s.club_id})
    for s in sends:
        events.setdefault(s.event_id, s.event_name)
        if s.club_id:
            clubs.setdefault(s.club_id, s.club_name)
    items = []
    for m in own:
        items.append({
            "id": f"m{m.id}", "direction": m.direction, "type": m.type, "body": m.body, "at": _iso(m.at),
            "status": m.status, "error": m.error, "template_name": m.template_name,
            "media": ({"mime": m.media_mime, "filename": m.media_filename} if m.media_id else None),
            "automatic": False, "sent_by": m.sent_by_name,
            "label": _label(m.event_id, m.club_id, "manual", events, clubs) if m.direction == OUT else None,
            "probably_about": _label(m.event_id, m.club_id, "", events, clubs) if m.direction == IN and m.event_id else None,
        })
    for s in sends:
        kind = s.kind.value if hasattr(s.kind, "value") else str(s.kind)
        outcome = s.outcome.value if hasattr(s.outcome, "value") and s.outcome is not None else None
        items.append({
            "id": f"d{s.id}", "direction": OUT, "type": "template", "body": None, "at": _iso(s.created_at),
            "status": outcome or SENT, "error": None, "template_name": None, "media": None, "automatic": True, "sent_by": None,
            "label": _label(s.event_id, s.club_id, kind, events, clubs), "probably_about": None,
        })
    items.sort(key=lambda i: i["at"] or "", reverse=True)
    more = len(items) > limit
    page = items[:limit]
    page.reverse()
    return {"items": page, "next_before": page[0]["at"] if more and page else None,
            "conversation": conversation_out(conv, events, clubs)}


async def mark_read(session, conv: PlatformWaConversations) -> None:
    conv.unread_count = 0


# ─── Sending ─────────────────────────────────────────────────────────────


def _recipient(conv: PlatformWaConversations) -> dict:
    return {"to": conv.wa_id} if conv.wa_id else {"recipient": conv.bsuid}


async def _post(body: dict) -> str:
    """One message to Meta from the platform's number; its wamid. Raises
    whatsapp's errors as a template send does."""
    import httpx

    async with whatsapp._client() as client:
        try:
            r = await client.post(f"{whatsapp._base()}/messages", headers=whatsapp._headers(), json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise whatsapp.Deferred(f"WhatsApp could not be reached ({e.__class__.__name__}). Try again.") from e
        except httpx.HTTPError as e:
            raise whatsapp.Unconfirmed(f"WhatsApp did not answer ({e.__class__.__name__}); it may have gone.") from e
    if r.status_code >= 300:
        raise whatsapp.refusal(r.status_code, whatsapp._json(r))
    messages = (whatsapp._json(r) or {}).get("messages") or [{}]
    return str((messages[0] or {}).get("id") or "") if isinstance(messages, list) else ""


async def send(session, conv: PlatformWaConversations, *, mode: str, admin, text: Optional[str] = None,
               template: Optional[str] = None, params: Optional[dict] = None) -> PlatformWaMessages:
    """An admin's reply: `mode` "text" (inside the window) or "template"
    (an approved one, any time). Refused with what is allowed instead.
    The reply is kept with who sent it, whatever Meta answered."""
    allowed = can_send(conv)
    if conv.opted_out_at is not None:
        raise _refuse(409, allowed["reason"], "OPTED_OUT", {"can_send": allowed})
    if mode == "text":
        body_text = (text or "").strip()
        if not body_text:
            raise _refuse(422, "Write a message.", "EMPTY_MESSAGE")
        if len(body_text) > TEXT_MAX:
            raise _refuse(422, f"A message is at most {TEXT_MAX} characters.", "MESSAGE_TOO_LONG")
        if not allowed["text"]:
            raise _refuse(409, allowed["reason"], "WINDOW_CLOSED", {"can_send": allowed})
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **_recipient(conv), "type": "text",
                "text": {"preview_url": False, "body": body_text}}
        row = PlatformWaMessages(conversation_id=conv.id, direction=OUT, type="text", body=body_text, status=PENDING)
    elif mode == "template":
        spec = MANUAL_TEMPLATES.get(template or "")
        if spec is None:
            raise _refuse(422, f"Choose one of these templates: {', '.join(MANUAL_TEMPLATES)}.", "UNKNOWN_TEMPLATE",
                          {"templates": list(MANUAL_TEMPLATES)})
        params = {k: str(v) for k, v in (params or {}).items() if k in spec["params"] and v is not None}
        missing = [p for p in spec["params"] if not params.get(p, "").strip()]
        if missing:
            raise _refuse(422, f"Fill in: {', '.join(missing)}.", "MISSING_PARAMS", {"missing": missing})
        if conv.about_club_id is not None and await optout.blocked(session, conv.about_club_id, wa_id=conv.wa_id, bsuid=conv.bsuid):
            raise _refuse(409, "They opted out of this club's WhatsApp messages: no template can go to them.", "OPTED_OUT",
                          {"can_send": {**allowed, "template": False}})
        filled = spec["build"](**params)
        if not conv.wa_id:
            raise _refuse(409, "This person has no phone number on file, only a WhatsApp username: reply inside the window.",
                          "NO_NUMBER")
        body = whatsapp.payload(conv.wa_id, filled)
        row = PlatformWaMessages(conversation_id=conv.id, direction=OUT, type="template", template_name=filled.name,
                                 template_params=params, body=" · ".join(filled.body), status=PENDING)
    else:
        raise _refuse(422, "Send text or a template.", "INVALID_MODE")
    if not whatsapp.configured():
        raise _refuse(503, "MyOtherApp's WhatsApp number is not set up on this server.", "WHATSAPP_NOT_CONFIGURED")
    row.event_id, row.club_id = conv.about_event_id, conv.about_club_id
    row.sent_by_user_id = getattr(admin, "id", None)
    row.sent_by_name = (getattr(admin, "full_name", None) or getattr(admin, "email", None) or "Admin")[:120]
    now = _now()
    try:
        row.wamid = (await _post(body)) or None
        row.status = SENT
    except whatsapp.Unconfirmed as exc:
        row.error = exc.reason[:300]
    except whatsapp.WhatsAppError as exc:
        row.status, row.error = FAILED, exc.reason[:300]
    except Exception as exc:  # noqa: BLE001 - Deferred is a MailDeferred
        row.status = FAILED
        row.error = (getattr(exc, "reason", None) or str(exc) or "WhatsApp couldn't be reached. Try again.")[:300]
    row.at = now
    session.add(row)
    if row.status != FAILED:
        conv.last_message_at = now
        conv.last_direction = OUT
        conv.last_preview = club_inbox.preview(row.type, row.body, template=row.template_name)
        conv.unread_count = 0
    await session.flush()
    return row


def message_out(m: PlatformWaMessages, events: dict, clubs: dict) -> dict:
    return {
        "id": f"m{m.id}", "direction": m.direction, "type": m.type, "body": m.body, "at": _iso(m.at), "status": m.status,
        "error": m.error, "template_name": m.template_name, "media": None, "automatic": False, "sent_by": m.sent_by_name,
        "label": _label(m.event_id, m.club_id, "manual", events, clubs), "probably_about": None,
    }


def templates_out() -> list[dict]:
    from app.api.admin_ops.whatsapp_templates import EXPECTED

    return [{"name": n, "label": s["label"], "purpose": EXPECTED.get(n), "params": s["params"]}
            for n, s in MANUAL_TEMPLATES.items() if n in EXPECTED]

