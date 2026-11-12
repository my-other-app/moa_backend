"""What the rest of the backend tells webhooks: one call per kind of change,
each at the change's own choke point (the function every surface goes
through), in the caller's transaction, never raising (emit.py).

- event.created: events.service.create_event, events.duplicate.duplicate_event,
  mcp.tools.create_event_draft;
- event.updated: events.service.update_event / restore_event, mcp.tools.update_event_draft,
  closeout.service.update_settings (a status that is not a publish) and
  advance_clock (to `ended`), ticketing.types (the club's ticket types);
- event.published: closeout.service.update_settings (draft -> published),
  events.service.create_event (published straight away);
- event.cancelled: club_ops.cancel.cancel, events.service.delete_event;
- registration.created: events.registration.service.add_registration (every
  registration is made there: the form, the app, the desk, imports, walk-ups,
  waiting-list claims, approvals);
- registration.updated: registration.service.update_registration, the
  answers asked for later (missing_answers), the contact taken at the gate,
  a ticket type or code changed (ticketing.service.requote), a payment applied
  or refunded (payments);
- registration.approved / declined: approvals.service (_confirm_one, decline);
- registration.cancelled: registration.service.take_off_list (the club, the
  self-cancel link), retire_if_abandoned (never paid), approvals.retire_lapsed,
  users.service.delete_user;
- checkin.created / deleted: checkins.service.admit / undo (the gate, the
  scanners offline, kiosks, self check-in, volunteers, by hand, walk-ups);
- certificate.issued: certificates.service.issue;
- payment.completed: payments.service.apply_payment (verify, both Razorpay
  webhooks, the reconcile sweep);
- payment.refunded: payments.service._apply_refund (every full refund),
  club_ops.refunds._apply and admin_ops.refunds._apply_part (partial ones);
- club.updated: clubs.service.update_club, update_club_logo,
  update_club_cover, create_or_update_club_socials.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import select

from app.api.webhooks_out.emit import Ref, before, changed, emit, wanted

logger = logging.getLogger(__name__)


async def club_of_event(session, event_id) -> Optional[int]:
    from app.api.events.models import Events

    if event_id is None:
        return None
    return await session.scalar(select(Events.club_id).where(Events.id == int(event_id)).execution_options(include_deleted=True))


async def _club(session, club_id, event_id) -> Optional[int]:
    return club_id if club_id is not None else await club_of_event(session, event_id)


# ─── Events ──────────────────────────────────────────────────────────────


async def event_created(session, event) -> None:
    await emit(session, event.club_id, "event.created", Ref("event", event.id))


async def event_published(session, event) -> None:
    await emit(session, event.club_id, "event.published", Ref("event", event.id))


async def event_cancelled(session, event, *, was: Optional[str] = None) -> None:
    await emit(session, event.club_id, "event.cancelled", Ref("event", event.id),
               previous={"status": was} if was else None)


async def event_status_changed(session, event, was: str) -> None:
    """The status the partner API shows moved by itself (the clock ending it)."""
    await emit(session, event.club_id, "event.updated", Ref("event", event.id), previous={"status": was})


async def event_before(session, event) -> Optional[dict]:
    """The event before a change, for event_updated (None: nobody wants it)."""
    return await before(session, event.club_id, "event", event.id, "event.updated")


async def event_updated(session, event, was: Optional[dict]) -> None:
    await changed(session, event.club_id, "event.updated", Ref("event", event.id), was)


# ─── Registrations ───────────────────────────────────────────────────────


async def registration_created(session, registration, *, club_id: Optional[int] = None) -> None:
    club_id = await _club(session, club_id, registration.event_id)
    await emit(session, club_id, "registration.created", Ref("registration", registration.id))


async def registration_before(session, registration, *, club_id: Optional[int] = None) -> Optional[dict]:
    club_id = await _club(session, club_id, registration.event_id)
    return await before(session, club_id, "registration", registration.id, "registration.updated")


async def registration_updated(session, registration, was: Optional[dict], *, club_id: Optional[int] = None) -> None:
    if was is None:
        return
    club_id = await _club(session, club_id, registration.event_id)
    await changed(session, club_id, "registration.updated", Ref("registration", registration.id), was)


async def registration_answers_changed(session, event, registration_id, was: dict) -> None:
    """Answers written by a statement (the answers asked for later): `previous` is the old answers."""
    await emit(session, event.club_id, "registration.updated", Ref("registration", registration_id), previous={"answers": was})


async def registrations_updated(session, event, registration_ids: Iterable) -> None:
    """Several of one event's registrations changed at once (a bulk write):
    one registration.updated each, without `previous`."""
    ids = list(registration_ids or [])
    if not ids or not await wanted(session, event.club_id, "registration.updated"):
        return
    for rid in ids:
        await emit(session, event.club_id, "registration.updated", Ref("registration", rid))


async def registration_cancelled(session, registration, *, club_id: Optional[int] = None) -> None:
    club_id = await _club(session, club_id, registration.event_id)
    await emit(session, club_id, "registration.cancelled", Ref("registration", registration.id))


async def registrations_cancelled(session, rows: Iterable) -> None:
    """Several registrations cancelled at once (an account deleted): each to its event's club."""
    for reg in rows or []:
        await registration_cancelled(session, reg)


async def request_decided(session, request, *, approved: bool, club_id: Optional[int] = None) -> None:
    club_id = await _club(session, club_id, request.event_id)
    await emit(session, club_id, "registration.approved" if approved else "registration.declined", Ref("request", request.id))


async def requests_declined(session, event, request_ids: Iterable) -> None:
    ids = list(request_ids or [])
    if not ids or not await wanted(session, event.club_id, "registration.declined"):
        return
    for rid in ids:
        await emit(session, event.club_id, "registration.declined", Ref("request", rid))


# ─── Check-ins and certificates ──────────────────────────────────────────


async def checkin_created(session, checkin, *, club_id: Optional[int] = None) -> None:
    club_id = await _club(session, club_id, checkin.event_id)
    await emit(session, club_id, "checkin.created", Ref("checkin", checkin.id))


async def checkin_deleted(session, checkin, event=None, *, club_id: Optional[int] = None) -> None:
    """Built now: the check-in is gone by the time it is sent."""
    from app.api.webhooks_out import objects

    club_id = await _club(session, club_id if club_id is not None else getattr(event, "club_id", None), checkin.event_id)
    if not await wanted(session, club_id, "checkin.deleted"):
        return
    try:
        snapshot = objects.checkin_object(checkin, deleted=True)
    except Exception:  # noqa: BLE001
        logger.exception("webhooks: could not describe check-in %s being undone", getattr(checkin, "id", None))
        return
    await emit(session, club_id, "checkin.deleted", snapshot)


async def certificates_issued(session, event, registration_ids: Iterable) -> None:
    ids = list(registration_ids or [])
    if not ids or not await wanted(session, event.club_id, "certificate.issued"):
        return
    for rid in ids:
        await emit(session, event.club_id, "certificate.issued", Ref("certificate", rid))


# ─── Payments ────────────────────────────────────────────────────────────


async def payment_completed(session, payment_log_id, *, event_id=None, club_id: Optional[int] = None) -> None:
    club_id = await _club(session, club_id, event_id)
    await emit(session, club_id, "payment.completed", Ref("payment", payment_log_id))


async def registration_paid(session, registration_id, *, event_id=None, club_id: Optional[int] = None) -> None:
    """Paid for: confirmed (registration.updated, its status as it was)."""
    await registration_payment_changed(session, registration_id, {"status": "awaiting_payment"}, event_id=event_id, club_id=club_id)


async def registration_payment_changed(session, registration_id, previous: dict, *, event_id=None,
                                       club_id: Optional[int] = None) -> None:
    """Paid, or refunded in full: registration.updated with what it was."""
    club_id = await _club(session, club_id, event_id)
    await emit(session, club_id, "registration.updated", Ref("registration", registration_id), previous=previous)


async def payment_refunded(session, payment_log_id, *, event_id=None, club_id: Optional[int] = None) -> None:
    """In full or in part: the payment's `amount_refunded` and `status` say which."""
    club_id = await _club(session, club_id, event_id)
    await emit(session, club_id, "payment.refunded", Ref("payment", payment_log_id))


# ─── The club ────────────────────────────────────────────────────────────


async def club_before(session, club_id: Optional[int]) -> Optional[dict]:
    return await before(session, club_id, "club", club_id, "club.updated")


async def club_updated(session, club_id: Optional[int], was: Optional[dict]) -> None:
    await changed(session, club_id, "club.updated", Ref("club", club_id), was)
