"""Registration requests: "Confirm each registration" (docs/APPROVALS.md).

- Submitting: the public form on an event that requires approval makes a
  request, never a registration. Taken under the address's lock, as a
  registration is (registration.service.hold_address), and checked as one is
  -- the ticket type, the answers, a discount code -- so a request the club
  confirms can become a registration. One open request per person per event
  (two partial unique indexes, by address and by account): the form sent
  again while one is open answers with it.
- Confirming, in bulk: the rows are taken `FOR UPDATE SKIP LOCKED`, in queue
  order, at most CHUNK to a transaction. Each one registers through the
  registration service (registration.service.add_registration, which takes
  the event's row and checks every cap) inside a SAVEPOINT of its own, so a
  row that cannot be confirmed is skipped with its reason and the rest go on.
  Once the event is full the rest are skipped without more work. A paid
  ticket is not registered: it is given a hold, an offer in the waiting-list
  machinery (app/api/ticketing/waitlist.py) that keeps its place for the
  event's offer hours, and its claim registers with the request's answers.
- Locks, in the one order every path here and in registration takes them:
  request rows, then the addresses (sorted), then discount codes (by id),
  then the event's row, then its ticket types' rows. Two people confirming
  the same rows never register anyone twice (SKIP LOCKED, the status read
  under the row lock, and the registrations' own unique key); a confirmation
  and a public registration never both take the last place (the event's row).
- Telling people: every email and push is a job (app/core/jobs.py) written in
  the transaction that decided, so it runs once that commits and never for
  one that rolled back. A free ticket's email is the event's own ticket
  email, through the deliveries outbox like any other.
"""
from __future__ import annotations

import base64
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import String, and_, cast, func, or_, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError

from app.api.approvals.models import (
    APPROVED,
    CLOSED,
    DECLINED,
    LAPSED,
    OPEN,
    PENDING,
    REOPENABLE,
    STATUSES,
    WITHDRAWN,
    RegistrationRequests,
)
from app.api.closeout.models import EventStatus
from app.api.events.models import EventRegistrationsLink, Events, ends_at_sql
from app.core import jobs, money, public_urls
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

R = RegistrationRequests

#: Rows confirmed in one transaction: the event's row is held while they go.
CHUNK = 100
#: Rows one call may name (or `all_pending` take).
MAX_IDS = 500
#: The job that tells people (emails, pushes).
NOTIFY_JOB = "approvals.notify"
#: The form sent again by someone who cannot show the request is theirs: the
#: "request received" email goes again, at most this often.
RESEND_GAP = timedelta(minutes=10)

# What a job tells.
RECEIVED, APPROVED_FREE, APPROVED_PAY, DECLINED_NOTE, LAPSED_NOTE, CANCELLED = (
    "received", "approved", "approved_pay", "declined", "lapsed", "cancelled",
)

# Why a confirmation skipped a row (the dashboard says each).
NOT_PENDING, EVENT_FULL, TYPE_SOLD_OUT, TYPE_UNAVAILABLE = "not_pending", "event_full", "type_sold_out", "type_unavailable"
ALREADY_REGISTERED, CODE_INVALID, EVENT_CLOSED = "already_registered", "code_invalid", "event_closed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def status_url(token: str) -> str:
    """The guest's own page for their request, on the public site."""
    return public_urls.site_url(f"/requests/{token}")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _uuid(value) -> Optional[uuid.UUID]:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _lifecycle(event) -> str:
    return getattr(event.lifecycle, "value", event.lifecycle) or "published"


def event_over(event, now: Optional[datetime] = None) -> bool:
    now = now or _now()
    return event.event_datetime is not None and event.event_datetime + timedelta(hours=event.duration or 0) <= now


def event_closed(event, now: Optional[datetime] = None) -> bool:
    """No request of it can be confirmed any more: gone, called off or over."""
    return (
        event is None or bool(getattr(event, "is_deleted", False))
        or _lifecycle(event) in ("cancelled", "closed", "archived") or event_over(event, now)
    )


def _closed(reason: str, **errors) -> CustomHTTPException:
    messages = {
        "cancelled": "This event was cancelled",
        "deadline_passed": "Registration deadline has passed",
        "not_started": "Registration has not started yet",
        "ended": "This event is over",
    }
    return CustomHTTPException(
        400, message=messages.get(reason, "Registrations are closed"), error_code="REGISTRATIONS_CLOSED",
        errors={"closed_reason": reason, **errors},
    )


def check_open(event, now: Optional[datetime] = None) -> None:
    """Whether the form may take a request now, with register_event's answers."""
    now = now or _now()
    if event is None or event.is_deleted:
        raise CustomHTTPException(404, message="Event not found")
    if _lifecycle(event) == "cancelled":
        raise _closed("cancelled")
    if event.reg_enddate and event.reg_enddate < now:
        raise _closed("deadline_passed", closed_at=event.reg_enddate.isoformat())
    if event.reg_startdate and event.reg_startdate > now:
        raise _closed("not_started", opens_at=event.reg_startdate.isoformat())
    if event_over(event, now) or _lifecycle(event) in ("closed", "archived"):
        raise _closed("ended")


# ─── Finding requests ────────────────────────────────────────────────────


async def open_request(session, event_id: int, *, email: str, user_id: Optional[int] = None) -> Optional[RegistrationRequests]:
    """This person's open request for the event: by address, else by
    account. Two probes, each on its own unique index."""
    from app.api.users.service import same_email

    q = select(R).where(R.event_id == event_id, R.status.in_(OPEN)).limit(1).execution_options(populate_existing=True)
    found = await session.scalar(q.where(same_email(R.email, email)))
    if found is None and user_id is not None:
        found = await session.scalar(q.where(R.user_id == user_id))
    return found


async def _declined(session, event_id: int, *, email: str, user_id: Optional[int]) -> bool:
    from app.api.users.service import same_email

    who = [same_email(R.email, email)]
    if user_id is not None:
        who.append(R.user_id == user_id)
    return await session.scalar(
        select(R.id).where(R.event_id == event_id, R.status == DECLINED, or_(*who)).limit(1)
    ) is not None


async def by_token(session, token: str, *, lock: bool = False) -> RegistrationRequests:
    token = (token or "").strip()
    req = None
    if token and len(token) <= 64:
        q = select(R).where(R.token == token).execution_options(populate_existing=True)
        if lock:
            q = q.with_for_update()
        req = await session.scalar(q)
    if req is None:
        raise CustomHTTPException(404, message="This link is not valid.", error_code="REQUEST_NOT_FOUND")
    return req


async def for_entry(session, entry_id) -> Optional[RegistrationRequests]:
    """The request a waiting-list offer was the hold of (ix_registration_requests_hold)."""
    return await session.scalar(select(R).where(R.waitlist_entry_id == entry_id).limit(1))


# ─── Submitting ──────────────────────────────────────────────────────────


@dataclass
class Submitted:
    request: RegistrationRequests
    created: bool
    #: The caller showed the request is theirs (made it now, or signed in as
    #: its account): they may have its token.
    owner: bool
    #: The guest made for it, to hand a session for (the public form).
    guest: object = None


async def _tracking_link(session, event, ref: Optional[str]):
    """The club's tracking link the page carried, kept to credit on confirmation."""
    if not ref:
        return None
    from app.api.links import service as links

    link = await links.by_code(session, ref)
    if link is None or link.club_id != event.club_id or (link.event_id is not None and link.event_id != event.id):
        return None
    return link.id


async def submit(
    session, event, *, full_name: str, email: str, phone: Optional[str], user, answers: Optional[dict],
    ticket_type_id: Optional[int] = None, discount_code: Optional[str] = None, whatsapp_opt_in: bool = False,
    club_updates_whatsapp: bool = False, tracking_ref: Optional[str] = None,
) -> Submitted:
    """The form's request: the open one this person already has, or a new one.

    `user` is the signed-in account (or the guest whose session it is, with
    this address), or None for the public form, which gets a guest of its own
    -- as a registration does -- only when a request is made. Commits."""
    from app.api.events.registration import answers as answers_mod
    from app.api.events.registration import service as registration
    from app.api.ticketing import codes as codes_mod
    from app.api.ticketing import quote as quote_mod
    from app.api.users import service as user_service

    now = _now()
    email = (email or "").strip()
    phone = (phone or "").strip() or None
    full_name = (full_name or "").strip()
    check_open(event, now)
    if len(email) > 100:
        raise CustomHTTPException(422, message="That email address is too long.", errors={"email": "At most 100 characters."})
    if phone and len(phone) > 15:
        raise CustomHTTPException(422, message="Enter a valid phone number.", errors={"phone": "At most 15 characters."})
    kept = answers_mod.check(event.additional_details, answers)
    if whatsapp_opt_in:
        from app.core import whatsapp

        number, why = whatsapp.normalise(phone)
        if number is None:
            raise CustomHTTPException(
                422, message="To get your ticket on WhatsApp, enter a mobile number WhatsApp can reach, or untick the box.",
                error_code="WHATSAPP_NUMBER", errors={"phone": f"WhatsApp can't reach this number: {why}."},
            )
    user_id = user.id if user is not None else None
    # One address at a time, as a registration takes it: the form sent twice
    # is one request.
    await registration.hold_address(session, event.id, email)
    existing = await open_request(session, event.id, email=email, user_id=user_id)
    if existing is not None:
        owner = user_id is not None and existing.user_id == user_id
        if not owner:
            await _resend_received(session, existing, now)
        await session.commit()
        return Submitted(existing, created=False, owner=owner)
    if await _declined(session, event.id, email=email, user_id=user_id):
        raise CustomHTTPException(
            409, message="The organisers already decided on your request for this event.", error_code="REQUEST_DECLINED",
        )
    # The ticket type, its sales window, the per-person limit and the code,
    # as a registration checks them. A request holds no place, so the places
    # left are not asked: confirming is where they count.
    priced = await quote_mod.quote(
        session, event, ticket_type_id=ticket_type_id, discount_code=discount_code, email=email, phone=phone,
        user_id=user_id, lock=False, early_capacity=False, now=now,
    )
    guest = None
    if user is None:
        guest = await user_service.new_guest(session, full_name=full_name, email=email, phone=phone)
        user_id = guest.id
    req = R(
        event_id=event.id, user_id=user_id, full_name=full_name[:100], email=email, phone=phone,
        ticket_type_id=priced.ticket_type.id,
        discount_code=codes_mod.normalise(priced.code.code) if priced.code is not None else None,
        answers=kept or None, whatsapp_opt_in=bool(whatsapp_opt_in), club_updates_whatsapp=bool(club_updates_whatsapp),
        tracking_link_id=await _tracking_link(session, event, tracking_ref), status=PENDING, token=new_token(),
        submitted_at=now, created_at=now, updated_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(req)
            await session.flush()
    except IntegrityError:
        # The same account from another address at the same moment: its request.
        existing = await open_request(session, event.id, email=email, user_id=user_id)
        if existing is None:
            raise
        await session.commit()
        return Submitted(existing, created=False, owner=existing.user_id == user_id)
    await enqueue(session, [req], RECEIVED)
    await session.commit()
    return Submitted(req, created=True, owner=True, guest=guest)


async def _resend_received(session, req: RegistrationRequests, now: datetime) -> None:
    """The status link again, to the request's own address -- never to the
    caller -- at most once every RESEND_GAP."""
    if req.status != PENDING:
        return
    if req.resent_at is not None and now - req.resent_at < RESEND_GAP:
        return
    req.resent_at = now
    await enqueue(session, [req], RECEIVED, stamp=f"again-{int(now.timestamp())}")


# ─── Telling people ──────────────────────────────────────────────────────


def _stamp(req: RegistrationRequests) -> str:
    when = req.decided_at or req.submitted_at or _now()
    return str(int(when.timestamp() * 1000))


async def enqueue(session, reqs: Iterable[RegistrationRequests], kind: str, *, stamp: Optional[str] = None, extra: Optional[dict] = None) -> int:
    """A job for each request, in the caller's transaction: it runs once that
    commits, and never when it rolls back."""
    items = []
    for req in reqs:
        s = stamp or _stamp(req)
        items.append(({"request_id": str(req.id), "kind": kind, "stamp": s, **(extra or {})}, f"approvals:{kind}:{req.id}:{s}"))
    if not items:
        return 0
    return await jobs.enqueue_many(session, NOTIFY_JOB, items)


async def _enqueue_ids(session, ids: list, kind: str, *, stamp: str, extra: Optional[dict] = None) -> int:
    items = [
        ({"request_id": str(i), "kind": kind, "stamp": stamp, **(extra or {})}, f"approvals:{kind}:{i}:{stamp}")
        for i in ids
    ]
    return await jobs.enqueue_many(session, NOTIFY_JOB, items) if items else 0


# ─── What the guest sees ─────────────────────────────────────────────────


def _poster(poster) -> Optional[str]:
    if isinstance(poster, dict):
        return poster.get("large") or poster.get("medium") or poster.get("original") or poster.get("thumbnail")
    return poster if isinstance(poster, str) else None


def _standing(entry, now: datetime) -> bool:
    return entry is not None and entry.status == "offered" and (entry.offer_expires_at is None or entry.offer_expires_at > now)


def _settled(reg) -> bool:
    """A registration that holds its place: live, and paid or owing nothing."""
    return reg is not None and not reg.is_deleted and (bool(reg.is_paid) or money.rupees(reg.actual_amount) == 0)


async def _context(session, reqs: list) -> dict:
    """What the views of these requests read, a query a kind."""
    from sqlalchemy.orm import joinedload

    from app.api.ticketing import types as types_mod
    from app.api.ticketing.models import WaitlistEntries

    event_ids = sorted({r.event_id for r in reqs})
    events = {}
    if event_ids:
        events = {e.id: e for e in (await session.scalars(
            select(Events).options(joinedload(Events.club)).where(Events.id.in_(event_ids))
            .execution_options(include_deleted=True)
        )).unique().all()}
    reg_ids = [r.registration_id for r in reqs if r.registration_id is not None]
    regs = {}
    if reg_ids:
        regs = {g.id: g for g in (await session.scalars(
            select(EventRegistrationsLink).where(EventRegistrationsLink.id.in_(reg_ids)).execution_options(include_deleted=True)
        )).all()}
    entry_ids = [r.waitlist_entry_id for r in reqs if r.waitlist_entry_id is not None]
    entries = {}
    if entry_ids:
        entries = {w.id: w for w in (await session.scalars(select(WaitlistEntries).where(WaitlistEntries.id.in_(entry_ids)))).all()}
    names = await types_mod.names(session, [r.ticket_type_id for r in reqs if r.ticket_type_id is not None])
    return {"events": events, "regs": regs, "entries": entries, "names": names}


def _pay(req, ctx: dict, now: datetime) -> Optional[dict]:
    """A confirmed paid ticket still inside its hold: what to pay, by when, and the claim."""
    if req.status != APPROVED or req.waitlist_entry_id is None:
        return None
    entry = ctx["entries"].get(req.waitlist_entry_id)
    if not _standing(entry, now) or _settled(ctx["regs"].get(req.registration_id)):
        return None
    return {
        "amount": float(money.rupees(req.amount or 0)), "pay_by": entry.offer_expires_at, "claim_token": entry.offer_token,
        "claim_url": public_urls.site_url(f"/claim/{entry.offer_token}"),
    }


def public_view(req, ctx: dict, *, with_token: bool = True, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    event = ctx["events"].get(req.event_id)
    club = getattr(event, "club", None) if event is not None else None
    reg = ctx["regs"].get(req.registration_id)
    return {
        "id": req.id,
        "status": req.status,
        "event": {
            "id": req.event_id, "slug": getattr(event, "slug", None), "name": getattr(event, "name", None),
            "event_datetime": getattr(event, "event_datetime", None), "poster": _poster(getattr(event, "poster", None)),
            "club": {"name": club.name, "slug": club.slug} if club is not None else None,
        },
        "ticket_type": (
            {"id": req.ticket_type_id, "name": ctx["names"].get(req.ticket_type_id)} if req.ticket_type_id is not None else None
        ),
        "full_name": req.full_name,
        "submitted_at": req.submitted_at,
        "decided_at": req.decided_at,
        "decline_note": req.decline_note if req.status == DECLINED else None,
        "ticket_id": reg.ticket_id if _settled(reg) else None,
        "pay": _pay(req, ctx, now),
        "token": req.token if with_token else None,
        "status_url": status_url(req.token) if with_token else None,
    }


async def view(session, req, *, with_token: bool = True) -> dict:
    return public_view(req, await _context(session, [req]), with_token=with_token)


async def mine(session, user) -> list[dict]:
    """The signed-in person's requests: the open ones first (pending, then a
    confirmed paid one not paid yet), each part newest first."""
    from app.api.users.service import merges_into, same_email

    who = [R.user_id == user.id]
    if merges_into(user):
        # The guests' requests under this address, before the merge folds them in.
        who.append(same_email(R.email, user.email))
    rows = list((await session.scalars(
        select(R).where(or_(*who)).order_by(R.submitted_at.desc(), R.id.desc()).limit(200)
    )).all())
    ctx = await _context(session, rows)
    now = _now()
    views = [public_view(r, ctx, now=now) for r in rows]
    return sorted(views, key=lambda v: 0 if (v["status"] == PENDING or v["pay"] is not None) else 1)


async def viewer_request(session, event_id: int, user) -> Optional[dict]:
    """The signed-in viewer's open request for the event -- pending, or
    confirmed with its ticket not paid yet -- as the event page shows it:
    {id, status, token, status_url}. None otherwise (a confirmed free one is
    a registration, which the page shows instead)."""
    from app.api.users.service import merges_into, same_email

    who = [R.user_id == user.id]
    if merges_into(user):
        who.append(same_email(R.email, user.email))
    rows = list((await session.scalars(
        select(R).where(R.event_id == event_id, R.status.in_(OPEN), or_(*who)).order_by(R.submitted_at.desc()).limit(2)
    )).all())
    for req in rows:
        if req.status == APPROVED:
            reg = await session.get(EventRegistrationsLink, req.registration_id) if req.registration_id else None
            if req.waitlist_entry_id is None or _settled(reg):
                continue
        return {"id": req.id, "status": req.status, "token": req.token, "status_url": status_url(req.token)}
    return None


# ─── Withdrawing ─────────────────────────────────────────────────────────


async def withdraw(session, token: str) -> RegistrationRequests:
    """By the guest, from their link: a pending request, or a confirmed paid
    one not paid yet (its hold goes back to the line). Commits."""
    from app.api.ticketing import waitlist
    from app.api.ticketing.models import LEFT, WaitlistEntries

    req = await by_token(session, token, lock=True)
    now = _now()
    if req.status == WITHDRAWN:
        await session.commit()
        return req
    released_event = None
    if req.status == PENDING:
        req.status = WITHDRAWN
    elif req.status == APPROVED and req.waitlist_entry_id is not None:
        reg = await session.get(EventRegistrationsLink, req.registration_id) if req.registration_id else None
        if _settled(reg):
            raise await _not_withdrawable(session, req)
        entry = await session.scalar(
            select(WaitlistEntries).where(WaitlistEntries.id == req.waitlist_entry_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry is not None and entry.status == "offered":
            entry.status = LEFT
            entry.closed_at = now
            entry.offer_token = None
            released_event = entry.event_id
        req.status = WITHDRAWN
    else:
        raise await _not_withdrawable(session, req)
    req.updated_at = now
    await session.commit()
    if released_event is not None:
        # The place it held goes to whoever is next (the line, or the club's next confirmation).
        await waitlist.fill_soon(session, released_event)
    return req


async def _not_withdrawable(session, req) -> CustomHTTPException:
    """409 REQUEST_NOT_WITHDRAWABLE, with the request as it stands (`errors.request`)."""
    from fastapi.encoders import jsonable_encoder

    shown = jsonable_encoder(await view(session, req))
    return CustomHTTPException(
        409, message="This request can no longer be withdrawn.", error_code="REQUEST_NOT_WITHDRAWABLE",
        errors={"request": shown},
    )


# ─── The club's list ─────────────────────────────────────────────────────


def encode_cursor(submitted_at: datetime, rid) -> str:
    raw = f"{submitted_at.isoformat()}|{rid}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: Optional[str]) -> Optional[tuple[datetime, uuid.UUID]]:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        when, rid = raw.split("|", 1)
        return datetime.fromisoformat(when), uuid.UUID(rid)
    except (ValueError, UnicodeDecodeError):
        raise CustomHTTPException(422, message="That page link is not valid.", errors={"after": "Not a cursor."}) from None


def _search(q: Optional[str]):
    q = (q or "").strip()[:100]
    if not q:
        return None
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return or_(R.full_name.ilike(pattern, escape="\\"), R.email.ilike(pattern, escape="\\"), R.phone.ilike(pattern, escape="\\"))


def list_query(event_id: int, *, status: Optional[str], q: Optional[str], after=None, limit: int = 50):
    """A page of the event's requests from the cursor: the queue (pending)
    oldest first, walking ix_registration_requests_pending; any other status
    newest first, walking ix_registration_requests_queue backwards.
    (submitted_at, id) is the key; a page reads `limit + 1` rows."""
    stmt = select(R).where(R.event_id == event_id)
    newest_first = status != PENDING
    if status in STATUSES:
        stmt = stmt.where(R.status == status)
    match = _search(q)
    if match is not None:
        stmt = stmt.where(match)
    if after is not None:
        key = tuple_(R.submitted_at, R.id)
        stmt = stmt.where(key < tuple_(*after) if newest_first else key > tuple_(*after))
    order = (R.submitted_at.desc(), R.id.desc()) if newest_first else (R.submitted_at.asc(), R.id.asc())
    return stmt.order_by(*order).limit(limit + 1)


async def counts(session, event_id: int) -> dict:
    """By status, from the index alone."""
    rows = (await session.execute(select(R.status, func.count()).where(R.event_id == event_id).group_by(R.status))).all()
    out = {s: 0 for s in STATUSES}
    out.update({s: int(n) for s, n in rows})
    return out


async def pending_counts(session, event_ids: list) -> dict[int, int]:
    """Pending requests by event, for the dashboard's overview and lists."""
    if not event_ids:
        return {}
    rows = (await session.execute(
        select(R.event_id, func.count()).where(R.event_id.in_(list(event_ids)), R.status == PENDING).group_by(R.event_id)
    )).all()
    return {int(e): int(n) for e, n in rows}


async def places_left(session, event) -> Optional[int]:
    """The places someone could still be confirmed into: the event's, or,
    with no total but a cap on every type, the types' together. None: no cap."""
    from app.api.ticketing import places
    from app.api.ticketing import types as types_mod

    types = await places.fresh(session, event)
    room = places.compute(event, types, await places.lines(session, event.id))
    if room.event_public is not None:
        return max(0, room.event_public)
    if types and all(types_mod.capacity_of(event, t, types) is not None for t in types):
        return sum(max(0, room.public.get(t.id) or 0) for t in types)
    return None


async def club_list(session, event, *, status: Optional[str], q: Optional[str], after: Optional[str], limit: int) -> dict:
    cursor = decode_cursor(after)
    rows = list((await session.scalars(list_query(event.id, status=status, q=q, after=cursor, limit=limit))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    ctx = await _context(session, rows)
    now = _now()
    items = []
    for r in rows:
        pay = _pay(r, ctx, now)
        items.append({
            "id": r.id, "status": r.status, "full_name": r.full_name, "email": r.email, "phone": r.phone,
            "ticket_type": {"id": r.ticket_type_id, "name": ctx["names"].get(r.ticket_type_id)} if r.ticket_type_id is not None else None,
            "answers": r.answers or {},
            "submitted_at": r.submitted_at, "decided_at": r.decided_at,
            "decided_by": {"id": r.decided_by_id, "name": r.decided_by_name} if r.decided_at is not None else None,
            "decline_note": r.decline_note, "registration_id": r.registration_id,
            "pay_by": pay["pay_by"] if pay else None,
            "amount": float(money.rupees(r.amount)) if r.amount is not None else None,
            "discount_code": r.discount_code,
            "whatsapp_opt_in": bool(r.whatsapp_opt_in),
        })
    return {
        "items": items,
        "next": encode_cursor(rows[-1].submitted_at, rows[-1].id) if more and rows else None,
        "counts": await counts(session, event.id),
        "places_left": await places_left(session, event),
    }


# ─── Confirming ──────────────────────────────────────────────────────────


@dataclass
class _Snap:
    """A row's values as read under its lock: what a confirmation works from
    (a SAVEPOINT rolled back expires the row's own attributes)."""

    id: uuid.UUID
    user_id: Optional[int]
    full_name: str
    email: str
    phone: Optional[str]
    ticket_type_id: Optional[int]
    discount_code: Optional[str]
    answers: Optional[dict]
    whatsapp_opt_in: bool
    tracking_link_id: Optional[uuid.UUID]
    registration_id: Optional[uuid.UUID]

    @classmethod
    def of(cls, r: RegistrationRequests) -> "_Snap":
        return cls(
            id=r.id, user_id=r.user_id, full_name=r.full_name, email=r.email, phone=r.phone,
            ticket_type_id=r.ticket_type_id, discount_code=r.discount_code, answers=r.answers,
            whatsapp_opt_in=bool(r.whatsapp_opt_in), tracking_link_id=r.tracking_link_id, registration_id=r.registration_id,
        )


class _Skip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Run:
    """One call's state across its chunks."""

    actor_id: Optional[int]
    actor_name: Optional[str]
    approved: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    full: bool = False
    sold_out: set = field(default_factory=set)
    closed: bool = False


def _reason(exc: CustomHTTPException) -> str:
    """What a refusal from registration means for a confirmation."""
    code = exc.error_code or ""
    if code == "EVENT_FULL" or code.startswith("PLAN_"):
        return EVENT_FULL
    if code == "TICKET_TYPE_SOLD_OUT":
        return TYPE_SOLD_OUT
    if code in ("TICKET_TYPE_NOT_FOUND", "TICKET_TYPE_REQUIRED", "TICKET_TYPE_NOT_ON_SALE"):
        return TYPE_UNAVAILABLE
    if code.startswith("DISCOUNT_CODE"):
        return CODE_INVALID
    if code in ("ALREADY_REGISTERED", "TICKET_TYPE_LIMIT"):
        return ALREADY_REGISTERED
    if code in ("REGISTRATIONS_CLOSED", "EVENT_CANCELLED") or exc.status_code == 404:
        return EVENT_CLOSED
    raise exc


def _ids(values) -> list[uuid.UUID]:
    """The ids named, each once, in the order named."""
    seen, out = set(), []
    for v in values or []:
        u = _uuid(v)
        if u is not None and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def approve(session, event, *, ids=None, all_pending: bool = False, limit: int = MAX_IDS, actor=None) -> dict:
    """Confirm these requests (or the oldest `limit` pending), in queue order,
    CHUNK to a transaction. Commits each chunk."""
    from app.api.ticketing import types as types_mod

    event_id = event.id
    if all_pending:
        wanted = list((await session.scalars(
            select(R.id).where(R.event_id == event_id, R.status == PENDING)
            .order_by(R.submitted_at, R.id).limit(max(1, min(int(limit or MAX_IDS), MAX_IDS)))
        )).all())
    else:
        wanted = _ids(ids)[:MAX_IDS]
    run = _Run(actor_id=getattr(getattr(actor, "user", actor), "id", None), actor_name=getattr(actor, "name", None))
    if wanted:
        # The event's default type made now, if it has none: never while its row is held (types.ensure).
        await types_mod.ensure(session, event)
        await session.commit()
    for i in range(0, len(wanted), CHUNK):
        await _approve_chunk(session, event_id, wanted[i:i + CHUNK], run)
    fresh = await session.scalar(
        select(Events).where(Events.id == event_id).execution_options(populate_existing=True, include_deleted=True)
    )
    left = await places_left(session, fresh) if fresh is not None and not fresh.is_deleted else 0
    await session.commit()
    return {"approved": run.approved, "skipped": run.skipped, "places_left": left}


async def _lock_codes(session, event, texts: set) -> None:
    """The discount codes these rows carry, held in id order before the
    event's row: as a registration takes a code's row (codes.resolve) before
    the event's."""
    from app.api.ticketing import codes as codes_mod
    from app.api.ticketing.models import DiscountCodes

    ids = set()
    for t in texts:
        code = await codes_mod.find(session, event, t)
        if code is not None:
            ids.add(code.id)
    if ids:
        await session.execute(
            select(DiscountCodes.id).where(DiscountCodes.id.in_(sorted(ids))).order_by(DiscountCodes.id).with_for_update()
        )


async def _approve_chunk(session, event_id: int, chunk: list, run: _Run) -> None:
    from app.api.deliveries import service as deliveries
    from app.api.deliveries.models import DeliveryKind
    from app.api.events.registration import service as registration

    now = _now()
    rows = list((await session.scalars(
        select(R).where(R.id.in_(chunk), R.event_id == event_id, R.status == PENDING)
        .order_by(R.submitted_at, R.id).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )).all())
    got = {r.id for r in rows}
    # Another's confirmation holds it, it was decided already, or it is not this event's.
    run.skipped.extend({"id": i, "reason": NOT_PENDING} for i in chunk if i not in got)
    if not rows:
        await session.commit()
        return
    event = await session.scalar(
        select(Events).where(Events.id == event_id).execution_options(populate_existing=True, include_deleted=True)
    )
    if run.closed or event_closed(event, now):
        run.closed = True
        run.skipped.extend({"id": r.id, "reason": EVENT_CLOSED} for r in rows)
        await session.commit()
        return
    if run.full:
        run.skipped.extend({"id": r.id, "reason": EVENT_FULL} for r in rows)
        await session.commit()
        return
    snaps = [_Snap.of(r) for r in rows]
    by_id = {r.id: r for r in rows}
    # The addresses, sorted, then the codes, before the event's row (the one lock order).
    for email in sorted({s.email.strip().lower() for s in snaps}):
        await registration.hold_address(session, event_id, email)
    await _lock_codes(session, event, {s.discount_code for s in snaps if s.discount_code})

    free_regs: list = []
    told_free: list = []
    told_pay: list = []
    for snap in snaps:
        if run.full:
            run.skipped.append({"id": snap.id, "reason": EVENT_FULL})
            continue
        if snap.ticket_type_id in run.sold_out:
            run.skipped.append({"id": snap.id, "reason": TYPE_SOLD_OUT})
            continue
        try:
            async with session.begin_nested():
                done = await _confirm_one(session, event, by_id[snap.id], snap, run, now)
        except _Skip as skip:
            run.skipped.append({"id": snap.id, "reason": skip.reason})
            if skip.reason == EVENT_FULL:
                run.full = True
            elif skip.reason == TYPE_SOLD_OUT and snap.ticket_type_id is not None:
                run.sold_out.add(snap.ticket_type_id)
            continue
        run.approved.append({"id": snap.id, "registration_id": done["registration_id"], "pay_by": done["pay_by"]})
        if done["pay_by"] is None:
            free_regs.append(done["registration_id"])
            told_free.append(snap.id)
        else:
            told_pay.append(snap.id)
    stamp = str(int(now.timestamp() * 1000))
    await _enqueue_ids(session, told_free, APPROVED_FREE, stamp=stamp)
    await _enqueue_ids(session, told_pay, APPROVED_PAY, stamp=stamp)
    if free_regs:
        # Their tickets: the event's own ticket email, through the deliveries
        # outbox (rows and the job that sends them), committed with the rest.
        await deliveries.queue(session, event, DeliveryKind.ticket, registration_ids=free_regs, only="unsent", send=True)
    else:
        await session.commit()


async def _confirm_one(session, event, req: RegistrationRequests, snap: _Snap, run: _Run, now: datetime) -> dict:
    """One row, inside its SAVEPOINT: registered (it owes nothing), or held
    (a paid ticket: an offer that keeps the place while they pay). Raises
    _Skip with the reason, and the SAVEPOINT takes back whatever it did."""
    from app.api.events.registration import service as registration
    from app.api.ticketing import quote as quote_mod
    from app.api.users import service as user_service
    from app.api.users.models import Users

    if snap.ticket_type_id is None:
        raise _Skip(TYPE_UNAVAILABLE)
    user_id = snap.user_id
    if user_id is not None and await session.scalar(select(Users.id).where(Users.id == user_id, Users.is_deleted == False)) is None:  # noqa: E712
        user_id = None
    existing = await registration.existing_registration(session, event.id, user_id if user_id is not None else -1, snap.email)
    # A claim of an earlier hold made a registration that was never paid:
    # it is the one a new hold pays for.
    own_unpaid = (
        existing is not None and existing.id == snap.registration_id and not existing.is_paid
        and money.rupees(existing.actual_amount) > 0
    )
    if existing is not None and not own_unpaid:
        raise _Skip(ALREADY_REGISTERED)
    try:
        priced = await quote_mod.quote(
            session, event, ticket_type_id=snap.ticket_type_id, discount_code=snap.discount_code, email=snap.email,
            phone=snap.phone, user_id=user_id, early_capacity=False, window=False, now=now,
            exclude=existing.id if own_unpaid else None,
        )
        if priced.code is not None and priced.code.max_uses:
            await _check_code_holds(session, priced.code, now)
        if priced.holding:
            if user_id is None:
                user_id = (await user_service.new_guest(session, full_name=snap.full_name, email=snap.email, phone=snap.phone)).id
            reg = await registration.add_registration(
                session, event, priced, full_name=snap.full_name, email=snap.email, phone=snap.phone, user_id=user_id,
                additional_details=snap.answers, whatsapp_opt_in=snap.whatsapp_opt_in,
                tracking_link_id=snap.tracking_link_id, by_organiser=True, now=now,
            )
            req.registration_id = reg.id
            pay_by = None
        else:
            entry = await _hold(session, event, priced, snap, user_id, now, registration_id=existing.id if own_unpaid else None)
            req.waitlist_entry_id = entry.id
            req.amount = priced.amount
            pay_by = entry.offer_expires_at
    except CustomHTTPException as exc:
        raise _Skip(_reason(exc)) from None
    except IntegrityError:
        # A live registration of this account the checks above did not see (made since).
        raise _Skip(ALREADY_REGISTERED) from None
    req.status = APPROVED
    req.decided_at = now
    req.decided_by_id = run.actor_id
    req.decided_by_name = (run.actor_name or None) and run.actor_name[:120]
    req.updated_at = now
    await session.flush()
    return {"registration_id": req.registration_id, "pay_by": pay_by}


async def _hold(session, event, priced, snap: _Snap, user_id: Optional[int], now: datetime, *, registration_id=None):
    """A confirmed paid ticket's place, held for the event's offer hours: an
    offer in the waiting-list machinery, made holding the event's row and its
    types' as `waitlist.fill` makes one, so a hold is never given for a place
    a registration is taking. The people waiting in line come first here as
    for any registration (places.Room.refusal)."""
    from app.api.ticketing import places, waitlist
    from app.api.ticketing import types as types_mod
    from app.api.ticketing.models import DEFAULT_OFFER_HOURS, OFFERED, WAITING, WaitlistEntries

    await types_mod.lock_event(session, event.id)
    await waitlist._locked_types(session, event.id)
    types = await types_mod.live(session, event.id)
    ttype = next((t for t in types if t.id == priced.ticket_type.id), None)
    if ttype is None:
        raise _Skip(TYPE_UNAVAILABLE)
    # Their own place in this type's line, if they have one: it becomes the hold.
    entry = await waitlist.open_entry(session, event.id, ttype.id, email=snap.email)
    standing = entry is not None and entry.status == OFFERED and entry.offer_expires_at is not None and entry.offer_expires_at > now
    if not standing:
        # (A standing offer of theirs holds its place already.)
        lines_ = await places.lines(session, event.id, exclude=entry.id if entry is not None else None, now=now)
        room = places.compute(event, types, lines_)
        why = room.refusal(event, ttype, claiming=False)
        if why == "event":
            raise _Skip(EVENT_FULL)
        if why == "type":
            raise _Skip(TYPE_SOLD_OUT)
    settings_row = await waitlist.settings_for(session, event.id)
    hours = settings_row.offer_hours if settings_row is not None else DEFAULT_OFFER_HOURS
    if entry is None:
        entry = WaitlistEntries(
            event_id=event.id, ticket_type_id=ttype.id, user_id=user_id, full_name=snap.full_name[:100],
            email=snap.email, phone=snap.phone, whatsapp_opt_in=False, status=WAITING, created_at=now, updated_at=now,
        )
        session.add(entry)
    entry.via_request = True
    if not standing:
        waitlist._offer(entry, hours, now)
    if registration_id is not None:
        # Claiming it pays for the registration an earlier claim made.
        entry.registration_id = registration_id
    await session.flush()
    return entry


async def _check_code_holds(session, code, now: datetime) -> None:
    """A code with a limit on its uses: the holds confirmations gave count as
    uses until they are paid or run out, so the club never confirms more
    people at a code's price than the code has uses left."""
    from app.api.ticketing import codes as codes_mod
    from app.api.ticketing.models import WaitlistEntries as W

    used = await codes_mod.uses(session, code.id, reserving_since=now - codes_mod.RESERVE_FOR)
    reg = EventRegistrationsLink
    unpaid_or_none = or_(
        R.registration_id.is_(None),
        select(reg.id).where(
            reg.id == R.registration_id, reg.is_paid == False, reg.created_at <= now - codes_mod.RESERVE_FOR,  # noqa: E712
        ).exists(),
    )
    scope = Events.club_id == code.club_id if code.event_id is None else Events.id == code.event_id
    held = await session.scalar(
        select(func.count()).select_from(R).join(W, W.id == R.waitlist_entry_id).join(Events, Events.id == R.event_id)
        .where(R.status == APPROVED, R.discount_code == code.code, W.status == "offered", W.offer_expires_at > now, scope, unpaid_or_none)
        .execution_options(include_deleted=True)
    )
    if int(used) + int(held or 0) >= int(code.max_uses):
        raise _Skip(CODE_INVALID)


# ─── Declining and reopening ─────────────────────────────────────────────


async def decline(
    session, event, ids=None, *, all_pending: bool = False, limit: int = MAX_IDS, notify: bool = True,
    note: Optional[str] = None, actor=None,
) -> dict:
    """Pending requests declined -- these, or the oldest `limit` -- in one
    statement; told by email (and in the app) when `notify`. Rows another
    call holds right now are skipped. Commits."""
    now = _now()
    note = (note or "").strip()[:300] or None
    done: list = []
    if all_pending:
        # The queue's head, read and locked in one go (rows a confirmation holds are passed over).
        wanted = list((await session.scalars(
            select(R.id).where(R.event_id == event.id, R.status == PENDING).order_by(R.submitted_at, R.id)
            .limit(max(1, min(int(limit or MAX_IDS), MAX_IDS))).with_for_update(skip_locked=True)
        )).all())
    else:
        wanted = _ids(ids)[:MAX_IDS]
    if wanted:
        locked = (
            select(R.id).where(R.id.in_(wanted), R.event_id == event.id, R.status == PENDING)
            .with_for_update(skip_locked=True).scalar_subquery()
        )
        done = list((await session.execute(
            update(R).where(R.id.in_(locked)).values(
                status=DECLINED, decided_at=now, decided_by_id=getattr(getattr(actor, "user", actor), "id", None),
                decided_by_name=(getattr(actor, "name", None) or None), decline_note=note, updated_at=now,
            ).returning(R.id).execution_options(synchronize_session=False)
        )).scalars().all())
        if notify and done:
            await _enqueue_ids(session, done, DECLINED_NOTE, stamp=str(int(now.timestamp() * 1000)))
    await session.commit()
    got = set(done)
    return {"declined": [i for i in wanted if i in got], "skipped": [{"id": i, "reason": NOT_PENDING} for i in wanted if i not in got]}


async def reopen(session, event, ids, *, actor=None) -> dict:
    """Declined or lapsed requests back in the queue, where they were. Not
    one whose person has an open request since. Commits."""
    wanted = _ids(ids)[:MAX_IDS]
    now = _now()
    rows = list((await session.scalars(
        select(R).where(R.id.in_(wanted), R.event_id == event.id, R.status.in_(REOPENABLE))
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)
    )).all()) if wanted else []
    by_id = {r.id: r for r in rows}
    reopened, skipped = [], []
    for rid in wanted:
        r = by_id.get(rid)
        if r is None:
            skipped.append({"id": rid, "reason": "not_reopenable"})
            continue
        try:
            async with session.begin_nested():
                await session.execute(
                    update(R).where(R.id == rid).values(
                        status=PENDING, decided_at=None, decided_by_id=None, decided_by_name=None, decline_note=None,
                        amount=None, waitlist_entry_id=None, updated_at=now,
                    ).execution_options(synchronize_session=False)
                )
        except IntegrityError:
            skipped.append({"id": rid, "reason": "already_open"})
            continue
        reopened.append(rid)
    await session.commit()
    return {"reopened": reopened, "skipped": skipped}


# ─── What the waiting-list machinery tells us ────────────────────────────


async def link_registration(session, entry, registration) -> None:
    """A hold's claim made its registration (paid when the payment lands)."""
    await session.execute(
        update(R).where(R.waitlist_entry_id == entry.id).values(registration_id=registration.id, updated_at=_now())
        .execution_options(synchronize_session=False)
    )


async def on_paid(session, entry, registration) -> None:
    """A hold's payment landed and took the place: the request is confirmed
    and registered -- even one that had lapsed, when a place was still free."""
    await session.execute(
        update(R).where(R.waitlist_entry_id == entry.id, R.status.in_((APPROVED, LAPSED)))
        .values(status=APPROVED, registration_id=registration.id, updated_at=_now())
        .execution_options(synchronize_session=False)
    )


async def lapse(session, entry_ids: list, *, now: Optional[datetime] = None) -> list:
    """Holds that ran out (waitlist.expire): their requests lapse, and are
    told. In the caller's transaction."""
    if not entry_ids:
        return []
    now = now or _now()
    ids = list((await session.execute(
        update(R).where(R.waitlist_entry_id.in_(list(entry_ids)), R.status == APPROVED)
        .values(status=LAPSED, updated_at=now).returning(R.id).execution_options(synchronize_session=False)
    )).scalars().all())
    await _enqueue_ids(session, ids, LAPSED_NOTE, stamp=str(int(now.timestamp() * 1000)))
    return ids


async def close_holds(session, entry_ids: list, *, cancelled: bool, event_id: int, now: Optional[datetime] = None) -> list:
    """Holds closed with their event's line (waitlist.fill, the event over or
    called off): their requests close too; on a cancellation they are told,
    as close_for_event tells the rest. In the caller's transaction."""
    if not entry_ids:
        return []
    now = now or _now()
    ids = list((await session.execute(
        update(R).where(R.waitlist_entry_id.in_(list(entry_ids)), R.status == APPROVED)
        .values(status=CLOSED, updated_at=now).returning(R.id).execution_options(synchronize_session=False)
    )).scalars().all())
    if cancelled:
        await _enqueue_ids(session, ids, CANCELLED, stamp=str(event_id))
    return ids


async def withdrawn_by_entry(session, entry) -> None:
    """The guest gave the hold back from the claim page ("No thanks")."""
    await session.execute(
        update(R).where(R.waitlist_entry_id == entry.id, R.status == APPROVED)
        .values(status=WITHDRAWN, updated_at=_now()).execution_options(synchronize_session=False)
    )


# ─── The event called off, or over ───────────────────────────────────────


def _unsettled_hold():
    from app.api.ticketing.models import WaitlistEntries as W

    return and_(R.status == APPROVED, R.waitlist_entry_id.in_(select(W.id).where(W.status == "offered")))


async def close_for_event(session, event_id: int, *, cancelled: bool, now: Optional[datetime] = None) -> list:
    """Every request of the event not yet settled -- pending, or confirmed
    with a hold not yet paid -- closed, the holds' places given back. On a
    cancellation the people are told. In the caller's transaction."""
    from app.api.ticketing.models import WaitlistEntries as W

    now = now or _now()
    rows = (await session.execute(
        update(R).where(R.event_id == event_id, or_(R.status == PENDING, _unsettled_hold()))
        .values(status=CLOSED, updated_at=now).returning(R.id, R.waitlist_entry_id)
        .execution_options(synchronize_session=False)
    )).all()
    entry_ids = [e for _i, e in rows if e is not None]
    if entry_ids:
        await session.execute(
            update(W).where(W.id.in_(entry_ids), W.status == "offered")
            .values(status="expired", closed_at=now, updated_at=now).execution_options(synchronize_session=False)
        )
    if cancelled and rows:
        await _enqueue_ids(session, [i for i, _e in rows], CANCELLED, stamp=str(event_id))
    return [i for i, _e in rows]


#: The events with requests still pending, a jump per event over
#: ix_registration_requests_pending (a loose index scan): as many steps as
#: such events, however many requests each has.
_PENDING_EVENTS = text(
    """
    WITH RECURSIVE e AS (
      (SELECT event_id FROM registration_requests WHERE status = 'pending' ORDER BY event_id LIMIT 1)
      UNION ALL
      SELECT (SELECT r.event_id FROM registration_requests r
              WHERE r.status = 'pending' AND r.event_id > e.event_id ORDER BY r.event_id LIMIT 1)
      FROM e WHERE e.event_id IS NOT NULL
    )
    SELECT event_id FROM e WHERE event_id IS NOT NULL
    """
)


async def close_finished(session, *, now: Optional[datetime] = None) -> int:
    """The sweep's minute: events over, cancelled or gone that still have
    requests pending have them closed (a cancellation tells its own people,
    from club_ops.cancel). Commits."""
    now = now or _now()
    event_ids = list((await session.execute(_PENDING_EVENTS)).scalars().all())
    if not event_ids:
        return 0
    done = list((await session.scalars(
        select(Events.id).where(
            Events.id.in_(event_ids),
            or_(
                Events.is_deleted == True,  # noqa: E712
                Events.lifecycle.in_((EventStatus.cancelled, EventStatus.closed, EventStatus.archived)),
                ends_at_sql(Events) <= now,
            ),
        ).execution_options(include_deleted=True)
    )).all())
    closed = 0
    for event_id in done:
        closed += len(await close_for_event(session, event_id, cancelled=False, now=now))
    await session.commit()
    return closed


# ─── Retention and the guest merge ───────────────────────────────────────


async def scrub_event(session, event_id: int) -> int:
    """The retention scrub: what the requests gathered goes -- name,
    address, number, answers, the club's note. A request that became a
    registration keeps nothing either; the registration is the record.
    In the caller's transaction."""
    result = await session.execute(
        update(R).where(R.event_id == event_id, R.email.notlike("removed-%@removed.invalid")).values(
            full_name="Removed",
            email=func.concat("removed-", func.replace(cast(R.id, String), "-", ""), "@removed.invalid"),
            phone=None, answers=None, decline_note=None, updated_at=_now(),
        ).execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def count_for_event(session, event_id: int) -> int:
    return int(await session.scalar(select(func.count()).select_from(R).where(R.event_id == event_id)) or 0)


async def move_requests(session, guest_ids: list, app_user_id: int) -> int:
    """The guest merge (users.service.merge_guest_into_user): the guests'
    requests to the account. An open one stays with its guest only if the
    account has an open request for that event already (one per person)."""
    if not guest_ids:
        return 0
    from sqlalchemy.orm import aliased

    other = aliased(R)
    clash = select(other.id).where(
        other.user_id == app_user_id, other.event_id == R.event_id, other.status.in_(OPEN),
    ).exists()
    result = await session.execute(
        update(R).where(R.user_id.in_(list(guest_ids)), or_(R.status.notin_(OPEN), ~clash))
        .values(user_id=app_user_id).execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
