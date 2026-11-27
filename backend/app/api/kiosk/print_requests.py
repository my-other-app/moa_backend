"""Print stations: remote ID-card printing.

A kiosk tablet, the club dashboard or the door console queues a guest's
card on the server (`card_print_requests`); a paired *printer* device (a
dashboard page on a laptop with a wired printer) claims the oldest request
on a 90 second lease, fetches the card PDF, prints it and confirms.

There is no background loop: a lease that ran out is put back in the queue
(or failed, after three claims) by the next claim, on the same call that
hands out work. At most one open (queued or printing) request exists per
guest, by a partial unique index, so a double tap queues one card.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.api.checkins import service as checkins
from app.api.checkins.gate_auth import GateActor
from app.api.events.models import EventRegistrationsLink, Events
from app.api.idcards.models import PrintSource, ReprintPolicy
from app.api.idcards.service import badge_style, may_print_again
from app.api.kiosk.auth import kiosk_actor
from app.api.kiosk.models import CardPrintRequests, KioskDevices
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

LEASE = timedelta(seconds=90)
MAX_ATTEMPTS = 3
#: A printer is "online" when it claimed (its heartbeat) this recently.
ONLINE_WITHIN = timedelta(seconds=30)
#: A card for a door whose station is offline waits this long before any
#: online station may print it.
DOOR_FALLBACK_AFTER = timedelta(seconds=45)
OPEN = ("queued", "printing")
SOURCES = {"kiosk": PrintSource.kiosk, "dashboard": PrintSource.manual, "door": PrintSource.checkin}
LEASE_LOST = "The printer didn't confirm this card."
GUEST_REFUSAL = "Your card was already printed. Ask at the desk."
NO_PRINTER = "No printer is online right now."


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- printers ---------------------------------------------------------------------------


def _online_printers(event_id: int):
    return select(KioskDevices).where(
        KioskDevices.event_id == event_id, KioskDevices.role == "printer", KioskDevices.revoked_at.is_(None),
        KioskDevices.expires_at > _now(), KioskDevices.last_seen_at > _now() - ONLINE_WITHIN,
    )


async def printers_online(session, event_id: int) -> int:
    return await session.scalar(select(func.count()).select_from(_online_printers(event_id).subquery())) or 0


async def list_printers(session, event: Events) -> list[dict]:
    now = _now()
    rows = await session.scalars(
        select(KioskDevices).options(joinedload(KioskDevices.door)).where(KioskDevices.event_id == event.id, KioskDevices.role == "printer", KioskDevices.revoked_at.is_(None), KioskDevices.expires_at > now).order_by(KioskDevices.name)
    )
    return [
        {"id": d.id, "name": d.name, "door_label": d.door_label, "door": _door_ref(d.door), "online": bool(d.last_seen_at and d.last_seen_at > now - ONLINE_WITHIN), "last_seen_at": d.last_seen_at, "expires_at": d.expires_at}
        for d in rows
    ]


# --- the request as the clients read it ---------------------------------------------------


def _door_ref(door) -> Optional[dict]:
    return {"id": door.id, "name": door.name} if door is not None else None


def _load():
    return select(CardPrintRequests).options(
        joinedload(CardPrintRequests.registration), joinedload(CardPrintRequests.printer), joinedload(CardPrintRequests.claimer), joinedload(CardPrintRequests.door)
    ).execution_options(populate_existing=True)


def _out(r: CardPrintRequests, position: Optional[int], station=None) -> dict:
    who = r.claimer or r.printer
    station = who or station
    return {
        "id": r.id, "registration_id": r.registration_id, "name": (r.registration.full_name if r.registration else "") or "",
        "ticket_code": (r.registration.ticket_id if r.registration else "") or "", "source": r.source, "status": r.status, "attempts": r.attempts,
        "printer": {"id": who.id, "name": who.name} if who is not None else None, "error": r.error, "created_at": r.created_at, "printed_at": r.printed_at,
        "position": position, "door": _door_ref(r.door), "station": {"id": station.id, "name": station.name} if station is not None else None,
    }


async def _positions(session, event_id: int) -> dict:
    ids = await session.scalars(select(CardPrintRequests.id).where(CardPrintRequests.event_id == event_id, CardPrintRequests.status == "queued").order_by(CardPrintRequests.created_at, CardPrintRequests.id))
    return {i: n for n, i in enumerate(ids, start=1)}


async def read(session, request_id) -> CardPrintRequests | None:
    return await session.scalar(_load().where(CardPrintRequests.id == request_id))


async def _expected_station(session, r: CardPrintRequests):
    """The station a queued card will most likely come out of: its door's
    online station. None when any free station may take it."""
    if r.status != "queued" or r.door_id is None or r.claimer is not None or r.printer is not None:
        return None
    return await session.scalar(_online_printers(r.event_id).where(KioskDevices.door_id == r.door_id).order_by(KioskDevices.last_seen_at.desc()).limit(1))


async def out_of(session, request_id) -> dict:
    r = await read(session, request_id)
    return _out(r, (await _positions(session, r.event_id)).get(r.id) if r.status == "queued" else None, await _expected_station(session, r))


async def listing(session, event: Events, status: str, limit: int) -> dict:
    q = _load().where(CardPrintRequests.event_id == event.id)
    # "failed" is what needs a person: failed and cancelled both can be retried.
    wanted = {"open": OPEN, "done": ("printed",), "failed": ("failed", "cancelled")}.get(status)
    if wanted:
        q = q.where(CardPrintRequests.status.in_(wanted))
    rows = list((await session.scalars(q.order_by(CardPrintRequests.created_at.desc(), CardPrintRequests.id.desc()).limit(limit))).unique())
    positions = await _positions(session, event.id) if any(r.status == "queued" for r in rows) else {}
    return {"items": [_out(r, positions.get(r.id)) for r in rows], "printers_online": await printers_online(session, event.id)}


# --- asking for a card --------------------------------------------------------------------


async def _open_of(session, event_id: int, registration_id) -> CardPrintRequests | None:
    return await session.scalar(select(CardPrintRequests).where(CardPrintRequests.event_id == event_id, CardPrintRequests.registration_id == registration_id, CardPrintRequests.status.in_(OPEN)))


async def registration_in(session, event: Events, registration_id) -> EventRegistrationsLink:
    reg = await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == registration_id, EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
    )
    if reg is None:
        raise CustomHTTPException(404, message="Registration not found")
    if reg.ticket_revoked_at is not None:
        raise CustomHTTPException(409, message="That ticket was cancelled, so no card prints for it.", error_code="TICKET_REVOKED")
    return reg


async def ask(
    session, event: Events, reg: EventRegistrationsLink, *, source: str, user_id: Optional[int] = None, device: Optional[KioskDevices] = None,
    printer_id=None, door_id=None, override: bool = False, by_organiser: bool = False,
) -> tuple[dict, bool]:
    """Queue this guest's card. Returns (the request, created): the open
    request when there already is one, so a double tap prints once."""
    open_now = await _open_of(session, event.id, reg.id)
    if open_now is not None:
        return await out_of(session, open_now.id), False
    ok, why = may_print_again(event.reprint_policy or ReprintPolicy.anyone, reg.badge_printed_at, by_organiser, override)
    if not ok:
        raise CustomHTTPException(409, message=GUEST_REFUSAL if source == "kiosk" else why, error_code="REPRINT_NOT_ALLOWED")
    online = {p.id: p for p in await session.scalars(_online_printers(event.id))}
    if not online or (printer_id is not None and printer_id not in online):
        raise CustomHTTPException(409, message=NO_PRINTER if printer_id is None else "That printer is not online right now.", error_code="NO_PRINTER")
    row = CardPrintRequests(
        id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, source=source, requested_by_user_id=user_id, requested_by_device_id=device.id if device else None,
        printer_id=printer_id, door_id=door_id, status="queued", attempts=0, override=bool(override and by_organiser),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Another tap got in between the check and the insert.
        await session.rollback()
        again = await _open_of(session, event.id, reg.id)
        if again is None:
            raise
        return await out_of(session, again.id), False
    await session.commit()
    return await out_of(session, row.id), True


async def kiosk_ask(session, device: KioskDevices, *, redemption: Optional[str], session_id: Optional[str]) -> tuple[dict, bool]:
    """A tablet's guest: by the redemption of a ticket this kiosk read, or a
    phone session it serves. Always as a guest, never as an organiser."""
    from app.api.kiosk import self_service, service

    if bool(redemption) == bool(session_id):
        raise CustomHTTPException(422, message="Send either a redemption or a session.", errors={"fields": {"redemption": "One of redemption or session."}})
    event = device.event
    if redemption:
        registration_id = uuid.UUID(service.read_redemption(device, redemption)["a"])
    else:
        s = await self_service._load(session, session_id)
        if s.kiosk_id != device.id or not s.verified or not s.checked_in or not s.registration_id:
            raise CustomHTTPException(404, message="Nothing to print for that session here.")
        registration_id = s.registration_id
    reg = await registration_in(session, event, registration_id)
    if await checkins.check_in_of(session, reg.id) is None:
        raise CustomHTTPException(409, message="Scan your ticket at the kiosk first.", error_code="NOT_ADMITTED")
    return await ask(session, event, reg, source="kiosk", device=device, door_id=device.door_id)


async def of_device(session, device: KioskDevices, request_id) -> dict:
    r = await read(session, request_id)
    if r is None or r.requested_by_device_id != device.id:
        raise CustomHTTPException(404, message="That print request could not be found.")
    return await out_of(session, r.id)


# --- the station ------------------------------------------------------------------------------


async def _expire_leases(session, event_id: int) -> None:
    """Leases that ran out go back in the queue, or fail on the third claim."""
    now = _now()
    t = CardPrintRequests.__table__
    stale = (t.c.event_id == event_id, t.c.status == "printing", t.c.lease_until < now)
    await session.execute(update(t).where(*stale, t.c.attempts >= MAX_ATTEMPTS).values(status="failed", error=LEASE_LOST, lease_until=None, updated_at=now))
    await session.execute(update(t).where(*stale, t.c.attempts < MAX_ATTEMPTS).values(status="queued", lease_until=None, updated_at=now))
    await session.commit()


def _mine(device: KioskDevices):
    """What this station may take. A card picked for this station is its
    own. Otherwise, by the card's door:

    - no door: any station;
    - this station's door: this station;
    - a door with no online station: a station with no door at once, and
      any station once the card has waited DOOR_FALLBACK_AFTER, so nothing
      is stuck behind a station that went away."""
    R = CardPrintRequests
    now = _now()
    staffed = _online_printers(device.event_id).where(KioskDevices.door_id.is_not(None)).with_only_columns(KioskDevices.door_id)
    unstaffed = R.door_id.not_in(staffed)
    by_door = [R.door_id.is_(None), and_(unstaffed, R.created_at < now - DOOR_FALLBACK_AFTER)]
    if device.door_id is not None:
        by_door.append(R.door_id == device.door_id)
    else:
        by_door.append(unstaffed)
    return (
        R.event_id == device.event_id, R.status == "queued",
        or_(R.printer_id == device.id, and_(R.printer_id.is_(None), or_(*by_door))),
    )


async def claim(session, device: KioskDevices) -> dict:
    """The station's heartbeat, and its work: the oldest queued request that
    is for any printer or for this one, locked SKIP LOCKED so two stations
    never take the same card."""
    device.last_seen_at = _now()
    await session.commit()
    await _expire_leases(session, device.event_id)
    row = await session.scalar(select(CardPrintRequests).where(*_mine(device)).order_by(CardPrintRequests.created_at, CardPrintRequests.id).limit(1).with_for_update(skip_locked=True))
    if row is None:
        await session.commit()
        return {"request": None, "queued": 0}
    now = _now()
    row.status, row.claimed_by, row.claimed_at, row.lease_until, row.attempts, row.error = "printing", device.id, now, now + LEASE, (row.attempts or 0) + 1, None
    request_id = row.id
    await session.commit()
    r = await read(session, request_id)
    queued = await session.scalar(select(func.count()).select_from(CardPrintRequests).where(*_mine(device))) or 0
    return {
        "request": {"id": r.id, "registration_id": r.registration_id, "name": r.registration.full_name or "", "ticket_code": r.registration.ticket_id or "", "source": r.source, "attempt": r.attempts, "card_url": f"/kiosk/printer/requests/{r.id}/card.pdf"},
        "queued": queued,
    }


async def _own(session, device: KioskDevices, request_id) -> CardPrintRequests:
    r = await read(session, request_id)
    if r is None or r.claimed_by != device.id or r.event_id != device.event_id:
        raise CustomHTTPException(404, message="That print request is not yours.")
    return r


async def card_for(session, device: KioskDevices, request_id) -> bytes:
    """The card, for the request this station is printing right now: drawn
    and recorded as a print. A retry of a card that never came out (attempt
    two on) is not a reprint. The reprint rule was applied when the card was
    asked for; a staff request carries that authority (and its override) to
    here, a guest's does not, and the rule has its say again if the card
    was printed meanwhile."""
    from app.api.idcards.service import card_pdf

    r = await _own(session, device, request_id)
    if r.status != "printing":
        raise CustomHTTPException(409, message="That request is not being printed.", error_code="NOT_PRINTING")
    actor = GateActor(kind="kiosk", name=device.name, door=device.door, can_check_in=True, full=r.source != "kiosk" or r.attempts > 1)
    try:
        return await card_pdf(session, device.event, r.registration_id, actor=actor, source=SOURCES[r.source], record=True, override=bool(r.override or r.attempts > 1), door=device.door_label)
    except CustomHTTPException as exc:
        if exc.error_code != "REPRINT_REFUSED":
            raise
        await session.rollback()
        row = await session.get(CardPrintRequests, request_id)
        row.status, row.error, row.lease_until = "failed", (exc.message or "")[:400], None
        await session.commit()
        raise CustomHTTPException(409, message=row.error, error_code="REPRINT_NOT_ALLOWED")


async def done(session, device: KioskDevices, request_id, *, ok: bool, error: Optional[str]) -> dict:
    """Printed, or failed with words for staff. Idempotent: a repeat, or a
    word after a cancel, changes nothing."""
    r = await _own(session, device, request_id)
    if r.status in ("printing", "queued"):
        # Queued: the lease ran out while it printed, and the same station says so late.
        if ok:
            r.status, r.printed_at, r.error, r.lease_until = "printed", _now(), None, None
        elif r.status == "printing":
            r.status, r.error, r.lease_until = "failed", (error or "The printer could not print this card.")[:400], None
        await session.commit()
    return await out_of(session, request_id)


async def test_card(session, device: KioskDevices) -> bytes:
    """One page for the station to prove the printer and paper: a sample
    guest, nothing recorded and no guest or print row made."""
    from app.api.idcards.service import _draw_card
    from app.api.subscriptions import gates

    event = device.event
    await gates.require_feature(session, event.club_id, "badges", actor=None, event=event)
    style = await badge_style(session, event, None)
    sample = EventRegistrationsLink(id=uuid.uuid4(), event_id=event.id, full_name="Sample Guest", email="sample@example.com", ticket_id="MOA-TEST-0000", is_deleted=False)
    with session.no_autoflush:
        pdf = await _draw_card(session, event, sample, style, actor=kiosk_actor(device), door=device.door_label, record=False)
    return pdf


# --- staff: retry and cancel ----------------------------------------------------------------------


async def _of_event(session, event: Events, request_id) -> CardPrintRequests:
    r = await read(session, request_id)
    if r is None or r.event_id != event.id:
        raise CustomHTTPException(404, message="That print request could not be found.")
    return r


async def retry(session, event: Events, request_id) -> dict:
    r = await _of_event(session, event, request_id)
    if r.status not in ("failed", "cancelled"):
        raise CustomHTTPException(409, message="Only a failed or cancelled request can be retried.", error_code="NOT_RETRYABLE")
    r.status, r.error, r.lease_until, r.claimed_at = "queued", None, None, None
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        await session.rollback()
        raise CustomHTTPException(409, message="That guest's card is already in the queue.", error_code="ALREADY_QUEUED")
    await session.commit()
    return await out_of(session, request_id)


async def cancel(session, event: Events, request_id) -> dict:
    r = await _of_event(session, event, request_id)
    if r.status == "cancelled":
        return await out_of(session, request_id)
    if r.status not in OPEN:
        raise CustomHTTPException(409, message="That card has already been printed or has failed.", error_code="NOT_CANCELLABLE")
    r.status, r.lease_until = "cancelled", None
    await session.commit()
    return await out_of(session, request_id)
