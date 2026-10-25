"""Waiting lists: people in line for a sold-out ticket type, offered a place
as one frees up, one at a time, in the order they joined.

- Joining: when a type, or the event, has nothing left and the club has the
  event's waiting list on (and its plan has `waitlist`). Name, an email or a
  phone number, the account when signed in, and the type.
- An offer: when a place frees up -- a ticket cancelled or refunded, a cap
  raised -- the next person in line is offered it (`fill`): a claim link
  (`offer_url`) that holds the place for them for the event's offer hours
  (24 unless the club set otherwise), sent by email, push to the app when
  they joined signed in, and WhatsApp when they ticked it. An offer that runs
  out moves to the next person (`expire`, then `fill`).
- Claiming: the link registers them (registration.service.register_event
  with the offer); a paid type then takes its payment, and the place is
  theirs once it lands (places.capture_room).

`fill` runs after anything that can free a place this module sees (a type
saved, an entry removed or declined, an offer expiring), and the sweep runs
it every minute for every event with people waiting, so a ticket refunded or
cancelled from anywhere reaches the line within the minute. Meanwhile the
place is not anyone else's: the people waiting come before anyone new
(places.compute).

`fill` holds the event's row and its types' rows while it counts and offers,
as a registration does, so an offer is never made for a place a
registration is taking: never more offers than places.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select, update

from app.api.events.models import EventRegistrationsLink, Events
from app.api.ticketing import places
from app.api.ticketing import types as types_mod
from app.api.ticketing.models import (
    CLAIMED,
    DEFAULT_OFFER_HOURS,
    EXPIRED,
    LEFT,
    OFFERED,
    OPEN_STATUSES,
    REMOVED,
    WAITING,
    TicketTypes,
    WaitlistEntries,
    WaitlistSettings,
)
from app.core import jobs, public_urls
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FEATURE = "waitlist"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def offer_url(token: str) -> str:
    """The claim link an offer sends: the public site's claim page."""
    return public_urls.site_url(f"/claim/{token}")


def _token() -> str:
    return secrets.token_urlsafe(24)


# ─── Is it on ────────────────────────────────────────────────────────────


async def settings_for(session, event_id: int) -> Optional[WaitlistSettings]:
    return await session.get(WaitlistSettings, event_id)


async def enabled(session, event) -> bool:
    """The club turned the event's list on, and its plan has the feature."""
    row = await settings_for(session, event.id)
    if row is None or not row.enabled:
        return False
    from app.api.subscriptions import gates

    return await gates.allows(session, event.club_id, FEATURE, event=event)


def registration_open(event, now: Optional[datetime] = None) -> bool:
    """Still taking registrations: past its start, before its deadline, not over."""
    now = now or _now()
    if getattr(event, "is_deleted", False):
        return False
    if event.reg_enddate is not None and event.reg_enddate < now:
        return False
    if event.event_datetime is not None and event.event_datetime + timedelta(hours=event.duration or 0) <= now:
        return False
    lifecycle = getattr(event.lifecycle, "value", event.lifecycle)
    return lifecycle not in ("closed", "archived", "cancelled")


async def joinable(session, event) -> bool:
    """Whether someone turned away now could join the line instead."""
    return registration_open(event) and await enabled(session, event)


async def set_settings(session, event, *, enabled_: Optional[bool], offer_hours: Optional[int], actor=None) -> WaitlistSettings:
    row = await settings_for(session, event.id)
    if enabled_ and not (row is not None and row.enabled):
        from app.api.subscriptions import gates

        await gates.require_feature(session, event.club_id, FEATURE, actor=actor, event=event)
    if row is None:
        row = WaitlistSettings(event_id=event.id, enabled=False, offer_hours=DEFAULT_OFFER_HOURS)
        session.add(row)
    if enabled_ is not None:
        row.enabled = bool(enabled_)
    if offer_hours is not None:
        row.offer_hours = int(offer_hours)
    await session.commit()
    return row


# ─── Joining and leaving ─────────────────────────────────────────────────


def _same_phone(column, phone: str):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    return func.right(func.regexp_replace(column, r"\D", "", "g"), 10) == digits


async def open_entry(session, event_id: int, ticket_type_id: int, *, email=None, phone=None, user_id=None):
    """Their place in this type's line, if they have one."""
    from app.api.users.service import same_email

    W = WaitlistEntries
    who = []
    if email:
        who.append(same_email(W.email, email))
    if phone and sum(ch.isdigit() for ch in phone) >= 10:
        who.append(_same_phone(W.phone, phone))
    if user_id is not None:
        who.append(W.user_id == user_id)
    if not who:
        return None
    return await session.scalar(
        select(W).where(W.event_id == event_id, W.ticket_type_id == ticket_type_id, W.status.in_(OPEN_STATUSES), or_(*who))
        .order_by(W.created_at).limit(1)
    )


async def position(session, entry: WaitlistEntries) -> Optional[int]:
    """Their place in line (1 is next), while they wait."""
    if entry.status != WAITING:
        return None
    W = WaitlistEntries
    ahead = await session.scalar(
        select(func.count()).select_from(W).where(
            W.event_id == entry.event_id, W.ticket_type_id == entry.ticket_type_id, W.status == WAITING,
            or_(W.created_at < entry.created_at, and_(W.created_at == entry.created_at, W.id < entry.id)),
        )
    )
    return int(ahead or 0) + 1


async def join(
    session, event, *, ticket_type_id: Optional[int], full_name: str, email: Optional[str], phone: Optional[str],
    user=None, whatsapp_opt_in: bool = False,
) -> tuple[WaitlistEntries, bool]:
    """Their place in line, and whether it is new (joining twice is the one place)."""
    email = (email or "").strip() or None
    phone = (phone or "").strip() or None
    if user is not None and user.email and not email:
        email = user.email
    if not email and not phone:
        raise CustomHTTPException(
            422, message="Give an email address or a phone number, so the organisers can reach you.",
            error_code="WAITLIST_CONTACT_REQUIRED", errors={"email": "An email address or a phone number."},
        )
    if whatsapp_opt_in:
        from app.core import whatsapp

        number, why = whatsapp.normalise(phone)
        if number is None:
            raise CustomHTTPException(
                422, message="To hear on WhatsApp, enter a mobile number WhatsApp can reach, or untick the box.",
                error_code="WHATSAPP_NUMBER", errors={"phone": f"WhatsApp can't reach this number: {why}."},
            )
    if not await joinable(session, event):
        raise CustomHTTPException(
            409, message="This event has no waiting list.", error_code="WAITLIST_CLOSED",
        )
    types = await types_mod.ensure(session, event)
    if ticket_type_id is None:
        visible = [t for t in types if not t.is_hidden]
        if len(types) == 1:
            ttype = types[0]
        elif len(visible) == 1:
            ttype = visible[0]
        else:
            raise CustomHTTPException(
                422, message="Choose the ticket type to wait for.", error_code="TICKET_TYPE_REQUIRED",
                errors={"ticket_type_id": "Choose a ticket type.", "ticket_types": [t.id for t in visible]},
            )
    else:
        ttype = next((t for t in types if t.id == int(ticket_type_id)), None)
        if ttype is None:
            raise CustomHTTPException(
                422, message="That ticket type is not this event's.", error_code="TICKET_TYPE_NOT_FOUND",
                errors={"ticket_type_id": "Not a ticket type of this event."},
            )
    if email:
        from app.api.events.registration import service as registration

        if await registration.live_registration_for_email(session, event.id, email) is not None:
            raise CustomHTTPException(
                409, message="This email is already registered for this event.", error_code="ALREADY_REGISTERED",
            )
    if user is not None:
        mine = await session.scalar(
            select(EventRegistrationsLink.id).where(
                EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.user_id == user.id,
                EventRegistrationsLink.is_deleted == False,  # noqa: E712
            ).limit(1)
        )
        if mine is not None:
            raise CustomHTTPException(409, message="You're already registered for this event.", error_code="ALREADY_REGISTERED")
    # The same person twice is the one place in line. The address's lock
    # (as a registration takes it) makes two taps one entry.
    from app.api.events.registration import service as registration

    await registration.hold_address(session, event.id, f"waitlist:{ttype.id}:{(email or phone or '').lower()}")
    existing = await open_entry(session, event.id, ttype.id, email=email, phone=phone, user_id=user.id if user else None)
    if existing is not None:
        return existing, False
    room = await places.room_now(session, event, types)
    left = room.for_type(ttype.id)
    if left is None or left > 0:
        raise CustomHTTPException(
            409, message="Tickets are still available: register instead.", error_code="WAITLIST_NOT_NEEDED",
            errors={"ticket_type_id": ttype.id, "available": left},
        )
    entry = WaitlistEntries(
        event_id=event.id, ticket_type_id=ttype.id, user_id=user.id if user is not None else None,
        full_name=(full_name or "").strip()[:100] or (user.full_name if user is not None else "") or "Guest",
        email=email, phone=phone, whatsapp_opt_in=bool(whatsapp_opt_in), status=WAITING,
    )
    session.add(entry)
    await session.commit()
    return entry, True


async def mine(session, event_id: int, user) -> list[WaitlistEntries]:
    W = WaitlistEntries
    from app.api.users.service import same_email

    who = [W.user_id == user.id]
    if user.email:
        who.append(same_email(W.email, user.email))
    return list((await session.scalars(
        select(W).where(W.event_id == event_id, W.status.in_(OPEN_STATUSES), or_(*who)).order_by(W.created_at)
    )).all())


async def close(session, entry: WaitlistEntries, status: str) -> None:
    """Out of line (removed, left, declined): a place they held goes to the next."""
    entry.status = status
    entry.closed_at = _now()
    await session.commit()
    await fill_soon(session, entry.event_id)


def mark_claimed(entry: WaitlistEntries) -> None:
    entry.status = CLAIMED
    entry.closed_at = _now()


# ─── Offering ────────────────────────────────────────────────────────────


def _offer(entry: WaitlistEntries, hours: int, now: datetime) -> None:
    entry.status = OFFERED
    entry.offer_token = _token()
    entry.offered_at = now
    entry.offer_expires_at = now + timedelta(hours=hours)
    entry.offers_made = int(entry.offers_made or 0) + 1
    entry.registration_id = None
    entry.closed_at = None


async def _notify_later(session, entry: WaitlistEntries) -> None:
    await jobs.enqueue(
        session, "waitlist.offer", {"entry_id": str(entry.id), "n": int(entry.offers_made)},
        dedupe_key=f"waitlist:offer:{entry.id}:{entry.offers_made}",
    )


async def _locked_types(session, event_id: int) -> list[TicketTypes]:
    """The event's live types, every row held (in id order): an offer is
    made for a place no registration is taking at the same time."""
    return list((await session.scalars(
        select(TicketTypes)
        .where(TicketTypes.event_id == event_id, TicketTypes.is_deleted == False)  # noqa: E712
        .order_by(TicketTypes.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).all())


async def fill(session, event_id: int, *, now: Optional[datetime] = None) -> list:
    """Offer the places free to the people next in line, oldest first,
    while there are both. Commits. Returns the entries offered now."""
    now = now or _now()
    event = await session.scalar(
        select(Events).where(Events.id == event_id)
        .with_for_update(of=Events).execution_options(populate_existing=True, include_deleted=True)
    )
    if event is None:
        await session.commit()
        return []
    if event.is_deleted or getattr(event.lifecycle, "value", event.lifecycle) == "cancelled" or (
        event.event_datetime is not None and event.event_datetime + timedelta(hours=event.duration or 0) <= now
    ):
        # Over, cancelled or gone: its line is closed, and the sweep stops coming back to it.
        W = WaitlistEntries
        await session.execute(
            update(W).where(W.event_id == event.id, W.status.in_(OPEN_STATUSES)).values(status=EXPIRED, closed_at=now, updated_at=now)
        )
        await session.commit()
        return []
    if not registration_open(event, now):
        await session.commit()
        return []
    await _locked_types(session, event.id)
    types = await types_mod.live(session, event.id)
    lines_ = await places.lines(session, event.id, now=now)
    if not any(line.waiting for line in lines_.values()):
        await session.commit()
        return []
    room = places.compute(event, types, lines_)
    free_event = room.event_free
    free = dict(room.free)
    settings_row = await settings_for(session, event.id)
    hours = settings_row.offer_hours if settings_row is not None else DEFAULT_OFFER_HOURS
    W = WaitlistEntries
    queue = (await session.scalars(
        select(W).where(W.event_id == event.id, W.status == WAITING)
        .order_by(W.created_at, W.id).with_for_update(skip_locked=True)
    )).all()
    offered = []
    for entry in queue:
        if free_event is not None and free_event <= 0:
            break
        if entry.ticket_type_id not in free:
            continue
        left = free[entry.ticket_type_id]
        if left is not None and left <= 0:
            continue
        _offer(entry, hours, now)
        await _notify_later(session, entry)
        offered.append(entry.id)
        if left is not None:
            free[entry.ticket_type_id] = left - 1
        if free_event is not None:
            free_event -= 1
    await session.commit()
    return offered


async def offer_now(session, event, entry: WaitlistEntries) -> WaitlistEntries:
    """The club offering a place to this person, ahead of the line (or again,
    after an offer ran out). Only a place that is free: never over a cap."""
    if entry.status not in (WAITING, EXPIRED, OFFERED):
        raise CustomHTTPException(409, message="This person is no longer waiting.", error_code="WAITLIST_ENTRY_CLOSED")
    now = _now()
    event = await types_mod.lock_event(session, event.id)
    await _locked_types(session, event.id)
    entry = await session.scalar(
        select(WaitlistEntries).where(WaitlistEntries.id == entry.id).with_for_update().execution_options(populate_existing=True)
    )
    types = await types_mod.live(session, event.id)
    if entry.ticket_type_id not in {t.id for t in types}:
        raise CustomHTTPException(409, message="That ticket type no longer exists.", error_code="TICKET_TYPE_NOT_FOUND")
    if entry.status == OFFERED and entry.offer_expires_at and entry.offer_expires_at > now:
        # Offered already: the link is sent again, the place still held.
        await _notify_later(session, entry)
        await session.commit()
        return entry
    lines_ = await places.lines(session, event.id, exclude=entry.id, now=now)
    room = places.compute(event, types, lines_)
    left = room.free.get(entry.ticket_type_id)
    if (left is not None and left <= 0) or (room.event_free is not None and room.event_free <= 0):
        raise CustomHTTPException(
            409, message="No place is free. Raise the capacity first, then offer it.", error_code="WAITLIST_NO_ROOM",
        )
    settings_row = await settings_for(session, event.id)
    _offer(entry, settings_row.offer_hours if settings_row is not None else DEFAULT_OFFER_HOURS, now)
    await _notify_later(session, entry)
    await session.commit()
    return entry


async def expire(session, *, now: Optional[datetime] = None) -> list[int]:
    """Offers that ran out, closed: the events they were on, whose next
    people are then offered (`fill`). The link keeps working only to say so."""
    now = now or _now()
    W = WaitlistEntries
    rows = (await session.execute(
        update(W)
        .where(W.status == OFFERED, W.offer_expires_at <= now)
        .values(status=EXPIRED, closed_at=now, updated_at=now)
        .returning(W.event_id)
    )).scalars().all()
    await session.commit()
    return sorted(set(rows))


async def fill_soon(session, event_id: int) -> None:
    """`fill`, as a job once the caller's change has committed. Commits."""
    try:
        await jobs.enqueue(session, "waitlist.fill", {"event_id": int(event_id)}, dedupe_key=f"waitlist:fill:{event_id}")
        await session.commit()
    except Exception:  # noqa: BLE001 - the sweep fills within the minute anyway
        await session.rollback()
        logger.warning("waitlist fill for event %s could not be queued", event_id, exc_info=True)


@jobs.handler("waitlist.fill", max_attempts=3, timeout=60.0, priority=jobs.HIGH)
async def _fill_job(p: dict) -> None:
    async with jobs.scope() as session:
        await fill(session, int(p["event_id"]))


async def events_waiting(session) -> list[int]:
    W = WaitlistEntries
    return sorted(set((await session.scalars(select(W.event_id).where(W.status == WAITING).distinct())).all()))


# ─── The claim link ──────────────────────────────────────────────────────


async def by_token(session, token: str) -> WaitlistEntries:
    entry = await session.scalar(
        select(WaitlistEntries).where(WaitlistEntries.offer_token == (token or "").strip()).limit(1)
    ) if token else None
    if entry is None:
        raise CustomHTTPException(404, message="This link is not valid.", error_code="OFFER_NOT_FOUND")
    return entry


def check_claimable(entry: WaitlistEntries, now: Optional[datetime] = None) -> None:
    now = now or _now()
    if entry.status == CLAIMED:
        raise CustomHTTPException(409, message="This place has been claimed already.", error_code="OFFER_CLAIMED")
    if entry.status != OFFERED or (entry.offer_expires_at is not None and entry.offer_expires_at <= now):
        raise CustomHTTPException(
            410, message="This offer has run out, and the place went to the next person in line.",
            error_code="OFFER_EXPIRED",
        )


async def decline(session, entry: WaitlistEntries) -> None:
    check_claimable(entry)
    entry.offer_token = None
    await close(session, entry, LEFT)


# ─── The club's list ─────────────────────────────────────────────────────


async def entries(session, event_id: int, *, status: Optional[str] = None, limit: int = 200, offset: int = 0):
    W = WaitlistEntries
    q = select(W).where(W.event_id == event_id)
    if status:
        q = q.where(W.status == status)
    total = int(await session.scalar(select(func.count()).select_from(q.subquery())) or 0)
    rows = list((await session.scalars(
        q.order_by(
            # Those still in line first, in the line's order; then the rest, newest first.
            (W.status.in_(OPEN_STATUSES)).desc(), W.created_at.asc(), W.id,
        ).limit(limit).offset(offset)
    )).all())
    return rows, total


async def counts(session, event_id: int) -> dict:
    W = WaitlistEntries
    rows = (await session.execute(
        select(W.status, func.count()).where(W.event_id == event_id).group_by(W.status)
    )).all()
    out = {s: 0 for s in (WAITING, OFFERED, CLAIMED, EXPIRED, REMOVED, LEFT)}
    out.update({s: int(n) for s, n in rows})
    return out


async def export_csv(session, event) -> bytes:
    rows, _ = await entries(session, event.id, limit=100_000)
    names = await types_mod.names(session, [r.ticket_type_id for r in rows])
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["Position", "Name", "Email", "Phone", "Ticket type", "Status", "Joined", "Offered", "Offer expires", "WhatsApp"])
    line_pos: dict[int, int] = {}
    for r in rows:
        pos = ""
        if r.status == WAITING:
            line_pos[r.ticket_type_id] = line_pos.get(r.ticket_type_id, 0) + 1
            pos = line_pos[r.ticket_type_id]
        out.writerow([
            pos, r.full_name, r.email or "", r.phone or "", names.get(r.ticket_type_id, ""), r.status,
            r.created_at.isoformat() if r.created_at else "", r.offered_at.isoformat() if r.offered_at else "",
            r.offer_expires_at.isoformat() if r.offer_expires_at else "", "yes" if r.whatsapp_opt_in else "no",
        ])
    return buf.getvalue().encode("utf-8")


# ─── Telling them ────────────────────────────────────────────────────────


def _when(dt: datetime) -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return dt.astimezone(ist).strftime("%d %b, %I:%M %p IST").replace(" 0", " ")


def offer_template(*, name: str, event: str, ticket: str, until: str, token: str):
    """The WhatsApp template `waitlist_offer` (UTILITY, `en`), as it must be
    submitted to Meta: body "Hi {{1}}, a place opened up at {{2}}. Claim your
    {{3}} ticket before {{4}}; after that it goes to the next person in
    line." URL button https://myotherapp.in/claim/{{1}} -- the claim token."""
    from app.core import whatsapp

    return whatsapp.Template(
        "waitlist_offer",
        (
            whatsapp.first_name(name), whatsapp._value(event, "the event"), whatsapp._value(ticket, "your"),
            whatsapp._value(until, "the link runs out"),
        ),
        button=whatsapp._suffix(token),
    )


#: What an offer counts as against the club's messages (app/api/message_quota):
#: a message about a ticket the person asked for -- the plan's allowance,
#: then the club's credits, and, like a ticket, never held back when both
#: are used up (a place nobody hears about would go to waste).
MESSAGE_KIND = "ticket"


@jobs.handler("waitlist.offer", max_attempts=4, timeout=120.0, priority=jobs.HIGH)
async def _offer_job(p: dict) -> None:
    """One offer, told: email (retried until it goes), then a push and a
    WhatsApp message, each once and best effort. The email and the WhatsApp
    message are counted as the club's messages (MESSAGE_KIND), so its plan's
    allowance and its prepaid credits pay for them as for a ticket."""
    import uuid

    async with jobs.scope() as session:
        entry = await session.get(WaitlistEntries, uuid.UUID(str(p["entry_id"])))
        if entry is None or entry.status != OFFERED or int(entry.offers_made or 0) != int(p.get("n") or 0):
            return
        event = await session.get(Events, entry.event_id)
        ttype = await session.get(TicketTypes, entry.ticket_type_id)
        if event is None or ttype is None:
            return
        link = offer_url(entry.offer_token)
        until = _when(entry.offer_expires_at)
        subject = f"A place opened up at {event.name}"
        ref = f"waitlist:{entry.id}:{entry.offers_made}"
        if entry.email:
            from app.core.email import email as email_mod
            from app.core.email import plain

            paragraphs = [
                f"Hi {entry.full_name.split(' ')[0] if entry.full_name else 'there'},",
                f"A place opened up at {event.name}, and you're next in line for a {ttype.name} ticket.",
                f"It's held for you until {until}. After that it goes to the next person waiting.",
            ]
            button = ("Claim your ticket", link)
            footer = "You're getting this because you joined the waiting list for this event."
            await asyncio.to_thread(
                email_mod.send_email,
                recipients=[entry.email], subject=subject,
                text=plain.text(paragraphs, button=button, footer=footer),
                html=plain.render(paragraphs, button=button, footer=footer),
                idempotency_key=ref,
            )
            try:
                from app.api.message_quota import service as quota

                await quota.count(session, club_id=event.club_id, channel=quota.EMAIL, kind=MESSAGE_KIND, ref=f"{ref}:email")
                await session.commit()
            except Exception:  # noqa: BLE001
                await session.rollback()
        if entry.user_id is not None:
            try:
                from app.core.notifications.service import send_notification_to_user

                await send_notification_to_user(
                    session, entry.user_id, subject, f"Your {ttype.name} ticket is held until {until}. Tap to claim it.",
                    data={"type": "waitlist_offer", "event_id": str(event.id), "url": link, "token": entry.offer_token},
                )
            except Exception:  # noqa: BLE001
                logger.warning("waitlist offer %s: the push could not be sent", entry.id, exc_info=True)
        if entry.whatsapp_opt_in and entry.phone:
            from app.core import whatsapp

            if whatsapp.configured():
                try:
                    await whatsapp.send(
                        entry.phone,
                        offer_template(name=entry.full_name, event=event.name, ticket=ttype.name, until=until, token=entry.offer_token),
                        meter={"club_id": event.club_id, "kind": MESSAGE_KIND, "ref": f"{ref}:whatsapp"},
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("waitlist offer %s: WhatsApp could not be sent", entry.id, exc_info=True)
