"""Admission: one guest, one row, with where, how and by whom.

Every way in -- a scan at a door, the volunteer app, by hand from the
attendees page, a walk-up, the offline queue -- goes through `scan` or
`admit`, so the counters, the log and the registration's own
`is_attended` mirror agree whatever the door.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.api.checkins import doors as doors_mod
from app.api.checkins.gate_auth import GateActor
from app.api.checkins.models import CheckIns, CheckInVia, DoorKind, DoorMode, Doors
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.volunteer.models import Volunteer
from app.api.templates_audience import RegistrationRole
from app.api.users.models import Users
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

WALKUP_DOMAIN = "walkup.myotherapp.in"


# --- the door ---------------------------------------------------------------------


def door_open(event: Events, now: Optional[datetime] = None) -> bool:
    mode = event.door_mode or DoorMode.open
    if mode is DoorMode.open:
        return True
    if mode is DoorMode.closed:
        return False
    now = now or datetime.now(timezone.utc)
    start = event.event_datetime
    end = start + timedelta(hours=event.duration or 0)
    return start - timedelta(hours=24) <= now <= end + timedelta(minutes=event.close_after_minutes or 240)


def _code_filter(code: str):
    code = code.strip()
    try:
        return EventRegistrationsLink.id == uuid.UUID(code)
    except ValueError:
        return func.upper(EventRegistrationsLink.ticket_id) == code.upper()


def _loaded():
    return (joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user))


async def lookup(session, code: str) -> Optional[EventRegistrationsLink]:
    """The registration a scanned code names, whatever event it is for."""
    if not code or not code.strip():
        return None
    return await session.scalar(select(EventRegistrationsLink).options(*_loaded()).where(_code_filter(code), EventRegistrationsLink.is_deleted == False))


async def registration_of(session, event: Events, registration_id) -> EventRegistrationsLink:
    reg = await session.scalar(
        select(EventRegistrationsLink).options(*_loaded()).where(EventRegistrationsLink.id == registration_id, EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)
    )
    if reg is None:
        raise CustomHTTPException(404, message="Registration not found")
    return reg


async def check_in_of(session, registration_id) -> Optional[CheckIns]:
    return await session.scalar(select(CheckIns).options(joinedload(CheckIns.by), joinedload(CheckIns.door)).where(CheckIns.registration_id == registration_id))


def summary(reg: EventRegistrationsLink, checked_in_at=None) -> dict:
    role = getattr(reg.role, "value", reg.role) or "attendee"
    return {
        "id": reg.id, "name": reg.full_name or "", "ticket_id": reg.ticket_id, "role": str(role), "email": reg.email or None,
        "phone": reg.phone or None, "has_contact": bool(reg.email or reg.phone), "is_paid": bool(reg.is_paid) if reg.event.has_fee else True,
        "checked_in_at": checked_in_at, "badge_printed_at": getattr(reg, "badge_printed_at", None),
    }


NO_BADGE = {"auto": False, "may_print": False, "printed_at": None, "policy": "anyone"}


def _not_admitted(outcome: str, **more) -> dict:
    return {"outcome": outcome, "attendee": None, "checked_in_at": None, "checked_in_by": None, "other_event": None, "revoked_at": None, "capture": {"ask": False, "required": False, "reason": "NOT_ADMITTED"}, "replayed": False, "badge": NO_BADGE, **more}


def _capture(event: Events, reg: EventRegistrationsLink) -> dict:
    if not event.capture_contact_at_gate:
        return {"ask": False, "required": False, "reason": "DISABLED"}
    if reg.phone:
        return {"ask": False, "required": False, "reason": "HAS_CONTACT"}
    return {"ask": True, "required": bool(event.require_contact_at_gate), "reason": "NO_CONTACT"}


def _by_name(ci: CheckIns) -> str:
    if ci.by is not None:
        return ci.by.full_name or ci.by.email
    if ci.via in (CheckInVia.kiosk, CheckInVia.phone):
        return "Self check-in"
    return ci.door_label or "Someone"


async def _after_admit(session, event: Events, reg: EventRegistrationsLink) -> None:
    """The app's own hooks: a push to the guest, and the badges a check-in earns."""
    try:
        from app.core.notifications.triggers import notify_user_check_in

        await notify_user_check_in(session=session, user_id=reg.user_id, event_id=event.id, event_name=event.name)
    except Exception as exc:
        logger.error("check-in notification failed: %s", exc)
    try:
        from app.api.badges.service import check_and_award_club_badges, check_and_award_user_badges

        await check_and_award_user_badges(session, user_id=reg.user_id)
        if event.club_id:
            await check_and_award_club_badges(session, club_id=event.club_id)
    except Exception as exc:
        logger.error("badge check failed after check-in: %s", exc)


def _door_kind_for(via: CheckInVia) -> DoorKind:
    if via is CheckInVia.kiosk:
        return DoorKind.kiosk
    if via is CheckInVia.desk:
        return DoorKind.desk
    return DoorKind.door


async def admit(session, event: Events, reg: EventRegistrationsLink, *, actor: GateActor, via: CheckInVia, door_label: Optional[str] = None, client_ref: Optional[str] = None, checked_in_at: Optional[datetime] = None, scanned_at: Optional[datetime] = None) -> CheckIns:
    """The row, and the registration's mirror of it."""
    door = actor.door if actor.door is not None and not door_label else await doors_mod.resolve(session, event.id, door_label, _door_kind_for(via))
    now = datetime.now(timezone.utc)
    row = CheckIns(
        id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, door_id=door.id if door else None, door_label=door.name if door else None,
        checked_in_by=actor.user_id, via=via, checked_in_at=checked_in_at or now, scanned_at=scanned_at, client_ref=client_ref,
    )
    session.add(row)
    reg.is_attended = True
    reg.attended_on = row.checked_in_at
    if actor.volunteer is not None:
        reg.volunteer_id = actor.volunteer.id
    await session.commit()
    await _after_admit(session, event, reg)
    return row


async def scan(session, event: Events, *, code: str, door_label: Optional[str], client_ref: Optional[str], scanned_at: Optional[datetime], actor: GateActor, via: CheckInVia = CheckInVia.gate, _retry: bool = True) -> dict:
    """The hot path. Idempotent on `client_ref`. Seven outcomes, all answered, none raised."""
    reg = await lookup(session, code)
    if reg is None:
        return _not_admitted("INVALID")
    if reg.event_id != event.id:
        return _not_admitted("WRONG_EVENT", other_event={"name": reg.event.name, "starts_at": reg.event.event_datetime})
    if reg.ticket_revoked_at is not None:
        return _not_admitted("REVOKED", attendee=summary(reg), revoked_at=reg.ticket_revoked_at)
    existing = await check_in_of(session, reg.id)
    if existing is None and reg.is_attended:
        # Marked present before admissions were recorded: the mirror is the
        # record, so give it a row rather than admit them twice.
        existing = CheckIns(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, via=CheckInVia.desk, checked_in_at=reg.attended_on or datetime.now(timezone.utc))
        session.add(existing)
        await session.commit()
        existing = await check_in_of(session, reg.id)
    if existing is not None:
        replay = bool(client_ref) and existing.client_ref == client_ref
        from app.api.idcards.service import badge_for_scan

        return {
            "outcome": "CHECKED_IN" if replay else "ALREADY_CHECKED_IN", "attendee": summary(reg, existing.checked_in_at), "checked_in_at": existing.checked_in_at,
            "checked_in_by": {"name": _by_name(existing), "door": existing.door_label or "—"}, "other_event": None, "revoked_at": None, "capture": _capture(event, reg), "replayed": replay,
            # A second scan is a resweep: the card prints only on a first admission.
            "badge": {**badge_for_scan(event, reg, actor, admitted=False), "auto": False},
        }
    if not door_open(event):
        return _not_admitted("EVENT_NOT_OPEN", attendee=summary(reg))
    if event.has_fee and not reg.is_paid:
        return _not_admitted("UNPAID", attendee=summary(reg))
    now = datetime.now(timezone.utc)
    when = now
    if via is CheckInVia.offline_sync and scanned_at is not None and scanned_at <= now and now - scanned_at < timedelta(hours=24):
        when = scanned_at
    try:
        row = await admit(session, event, reg, actor=actor, via=via, door_label=door_label, client_ref=client_ref, checked_in_at=when, scanned_at=scanned_at)
    except IntegrityError:
        # Lost a race, or this client_ref already committed: re-read and report the truth.
        await session.rollback()
        if not _retry:
            raise
        return await scan(session, event, code=code, door_label=door_label, client_ref=client_ref, scanned_at=scanned_at, actor=actor, via=via, _retry=False)
    from app.api.idcards.service import badge_for_scan

    return {
        "outcome": "CHECKED_IN", "attendee": summary(reg, row.checked_in_at), "checked_in_at": row.checked_in_at, "checked_in_by": None, "other_event": None,
        "revoked_at": None, "capture": _capture(event, reg), "replayed": False, "badge": badge_for_scan(event, reg, actor, admitted=True),
    }


async def scan_batch(session, event: Events, scans: list[dict], actor: GateActor) -> list[dict]:
    """The offline queue drained: each scan independently idempotent."""
    out = []
    for s in scans:
        try:
            res = await scan(session, event, code=s["code"], door_label=s.get("door"), client_ref=s["client_ref"], scanned_at=s.get("scanned_at"), actor=actor, via=CheckInVia.offline_sync)
        except Exception as exc:
            logger.exception("offline scan %s failed", s.get("client_ref"))
            res = _not_admitted("INVALID", error=str(exc)[:200])
        out.append({"client_ref": s["client_ref"], "response": res})
    return out


async def check_in_by_hand(session, event: Events, registration_id, door_label: Optional[str], actor: GateActor) -> dict:
    """Someone already on the list, with nothing to scan: the same path as a
    scan, recorded as by hand at the desk."""
    reg = await registration_of(session, event, registration_id)
    return await scan(session, event, code=reg.ticket_id, door_label=door_label or "Desk", client_ref=f"hand-{uuid.uuid4().hex}", scanned_at=None, actor=actor, via=CheckInVia.desk)


async def undo(session, event: Events, registration_id, actor: GateActor) -> dict:
    reg = await registration_of(session, event, registration_id)
    row = await check_in_of(session, reg.id)
    if row is None:
        raise CustomHTTPException(404, message="They are not checked in, so there is nothing to undo.")
    await session.delete(row)
    reg.is_attended = False
    reg.attended_on = None
    await session.commit()
    logger.info("check-in undone for %s by %s", reg.ticket_id, actor.name)
    return {"ok": True}


# --- walk-ups ------------------------------------------------------------------------


def _walkup_result(reg: EventRegistrationsLink, *, checked_in: bool, ticket_sent: bool, checked_in_at=None) -> dict:
    return {"outcome": "REGISTERED", "attendee": summary(reg, checked_in_at), "duplicate_of": None, "duplicate_by": None, "checked_in": checked_in, "ticket_sent": ticket_sent}


def _duplicate(reg: EventRegistrationsLink, by: str) -> dict:
    return {"outcome": "DUPLICATE", "attendee": None, "duplicate_of": {"id": reg.id, "name": reg.full_name or ""}, "duplicate_by": by, "checked_in": False, "ticket_sent": False}


async def walkup(session, event: Events, body, actor: GateActor, background_tasks=None) -> dict:
    """Register someone standing at the door and let them in. One contact is
    enough to tell people apart; with none, a namesake on the list is asked
    about first."""
    if not door_open(event):
        return {"outcome": "EVENT_NOT_OPEN", "attendee": None, "duplicate_of": None, "duplicate_by": None, "checked_in": False, "ticket_sent": False}
    role = RegistrationRole(body.role)
    if role is not RegistrationRole.attendee and not actor.full:
        raise CustomHTTPException(422, message="Only the organiser can register a speaker or staff at the door.", errors={"fields": {"role": "Pick attendee."}})
    name = " ".join(body.name.split())
    email = (str(body.email).strip().lower() if body.email else None) or None
    phone = (body.phone or "").strip() or None

    replay = await session.scalar(select(CheckIns).options(joinedload(CheckIns.registration).options(*_loaded())).where(CheckIns.client_ref == body.client_ref))
    if replay is not None:
        return _walkup_result(replay.registration, checked_in=True, ticket_sent=bool(replay.registration.email and not replay.registration.email.endswith(WALKUP_DOMAIN)), checked_in_at=replay.checked_in_at)

    active = select(EventRegistrationsLink).options(*_loaded()).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)
    if email:
        dup = await session.scalar(active.where(func.lower(EventRegistrationsLink.email) == email))
        if dup is not None:
            return _duplicate(dup, "CONTACT")
    if phone:
        dup = await session.scalar(active.where(EventRegistrationsLink.phone == phone))
        if dup is not None:
            return _duplicate(dup, "CONTACT")
    if not email and not phone and not body.confirm_new:
        same = await session.scalar(active.where(func.lower(EventRegistrationsLink.full_name) == name.lower()).order_by(EventRegistrationsLink.created_at))
        if same is not None:
            return _duplicate(same, "NAME")

    from app.api.events.registration.service import register_event
    from app.api.users import service as user_service
    from app.api.users.models import UserTypes

    # A guest with no email still needs an account to hang the ticket on.
    account_email = email or f"walkup-{uuid.uuid4().hex[:12]}@{WALKUP_DOMAIN}"
    user = await user_service.get_non_club_user_by_email(session, account_email) if email else None
    if user is None:
        user = await user_service.create_user(session=session, full_name=name, email=account_email, phone=phone, provider="email", user_type=UserTypes.guest)
    if user is None:
        raise CustomHTTPException(400, message="The guest account could not be created")
    data = await register_event(
        session=session, full_name=name, email=account_email, phone=phone, user_id=user.id, event_id=event.id,
        additional_details=body.custom_fields or None, role=role, notify=False, relaxed_fields=True,
    )
    reg = await registration_of(session, event, data["event_registration_id"])
    if event.has_fee and not reg.is_paid:
        # Paid at the door; the desk takes the money.
        reg.is_paid = True
        reg.paid_amount = reg.actual_amount
        await session.commit()

    via = CheckInVia.desk if actor.full and actor.door is None else CheckInVia.walkup
    checks_in = True if via is CheckInVia.walkup else bool(event.spot_registration_checks_in)
    row = None
    if checks_in:
        row = await admit(session, event, reg, actor=actor, via=via, door_label=body.door, client_ref=body.client_ref)
    sent = False
    if email:
        from app.api.deliveries import service as deliveries

        try:
            await deliveries.send_ticket(session, event, reg, background_tasks)
            sent = True
        except Exception:
            logger.exception("walk-up ticket email could not be queued")
    return _walkup_result(reg, checked_in=row is not None, ticket_sent=sent, checked_in_at=row.checked_in_at if row else None)


# --- around the gate ----------------------------------------------------------------


async def search(session, event: Events, q: str) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    pat = f"%{q}%"
    regs = list(
        await session.scalars(
            select(EventRegistrationsLink).options(*_loaded())
            .where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False, or_(EventRegistrationsLink.full_name.ilike(pat), EventRegistrationsLink.email.ilike(pat), EventRegistrationsLink.ticket_id.ilike(pat)))
            .order_by(EventRegistrationsLink.full_name).limit(8)
        )
    )
    return [summary(r, r.attended_on if r.is_attended else None) for r in regs]


async def capture_contact(session, event: Events, registration_id, phone: str) -> dict:
    reg = await registration_of(session, event, registration_id)
    phone = " ".join(phone.split())
    reg.phone = phone
    await session.commit()
    shared = list(
        await session.scalars(
            select(EventRegistrationsLink.full_name).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.phone == phone, EventRegistrationsLink.id != reg.id, EventRegistrationsLink.is_deleted == False)
        )
    )
    return {"phone": phone, "shared_with": [s or "" for s in shared]}


async def counters(session, event_id: int) -> dict:
    registered = await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False)) or 0
    checked_in = await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.event_id == event_id)) or 0
    return {"registered": registered, "checked_in": checked_in}


def gate_settings(event: Events) -> dict:
    return {
        "door_mode": (event.door_mode or DoorMode.open).value, "close_after_minutes": event.close_after_minutes or 240,
        "spot_registration_checks_in": bool(event.spot_registration_checks_in), "capture_contact_at_gate": bool(event.capture_contact_at_gate),
        "require_contact_at_gate": bool(event.require_contact_at_gate),
    }


async def update_gate_settings(session, event: Events, body) -> dict:
    changes = body.model_dump(exclude_unset=True)
    if "door_mode" in changes and changes["door_mode"] is not None:
        event.door_mode = DoorMode(changes["door_mode"])
    for key in ("close_after_minutes", "spot_registration_checks_in", "capture_contact_at_gate", "require_contact_at_gate"):
        if changes.get(key) is not None:
            setattr(event, key, changes[key])
    await session.commit()
    return gate_settings(event)


async def context(session, event: Events, actor: GateActor) -> dict:
    doors = [d for d in await doors_mod.options(session, event.id) if d.kind is not DoorKind.kiosk]
    expected = actor.door.id if actor.door is not None else (actor.volunteer.door_id if actor.volunteer is not None else None)
    club = event.club
    return {
        "event": {
            "id": event.id, "name": event.name, "code": f"E{event.id}", "org_name": club.name if club else "", "door_open": door_open(event), **gate_settings(event),
            "has_fee": bool(event.has_fee), "starts_at": event.event_datetime, "ticket_example": "MOA-XXXXX-XXXXX",
            "auto_print_badges": bool(event.auto_print_badges), "reprint_policy": (event.reprint_policy.value if event.reprint_policy else "anyone"),
        },
        "permissions": actor.permissions,
        "counters": await counters(session, event.id),
        "walkup_roles": [r.value for r in RegistrationRole] if actor.full else ["attendee"],
        "doors": [{"id": d.id, "name": d.name, "kind": d.kind.value} for d in doors],
        "expected_door_id": expected,
        "guest_fields": [f for f in (event.additional_details or []) if isinstance(f, dict)],
        "me": {"name": actor.name, "kind": actor.kind},
        # Whether there is anything to hand out: the console shows the merch tab only then.
        "has_merch": await _has_merch(session, event.id),
    }


async def _has_merch(session, event_id: int) -> bool:
    from app.api.merch.models import MerchItems

    return bool(await session.scalar(select(func.count()).select_from(MerchItems).where(MerchItems.event_id == event_id)))


# --- the team ----------------------------------------------------------------------


async def team(session, event: Events) -> dict:
    rows = (
        await session.execute(
            select(Volunteer, Users.full_name)
            .outerjoin(Users, Users.id == Volunteer.user_id)
            .where(Volunteer.is_deleted == False, or_(Volunteer.event_id == event.id, Volunteer.club_id == event.club_id))
            .order_by(Volunteer.created_at)
        )
    ).all()
    counts = dict((await session.execute(select(CheckIns.checked_in_by, func.count()).where(CheckIns.event_id == event.id, CheckIns.checked_in_by.isnot(None)).group_by(CheckIns.checked_in_by))).all())
    owner = event.club.user if event.club is not None and hasattr(event.club, "user") else None
    return {
        "owner": {"name": (owner.full_name if owner else None) or (event.club.name if event.club else ""), "email": owner.email if owner else None},
        "volunteers": [
            {
                "id": v.id, "email": v.email, "name": name, "user_id": v.user_id, "is_approved": v.is_approved, "scope": "event" if v.event_id == event.id else "club",
                "can_check_in": v.can_check_in, "can_walkup": v.can_walkup, "can_merch": v.can_merch, "door_id": v.door_id, "check_ins": counts.get(v.user_id, 0) if v.user_id else 0,
            }
            for v, name in rows
        ],
        "doors": await doors_mod.list_doors(session, event),
        "settings": gate_settings(event),
    }


async def update_volunteer(session, event: Events, volunteer_id: int, body) -> Volunteer:
    v = await session.scalar(select(Volunteer).where(Volunteer.id == volunteer_id, Volunteer.is_deleted == False, or_(Volunteer.event_id == event.id, Volunteer.club_id == event.club_id)))
    if v is None:
        raise CustomHTTPException(404, message="That volunteer is not on this event's team.")
    changes = body.model_dump(exclude_unset=True)
    for key in ("can_check_in", "can_walkup", "can_merch"):
        if changes.get(key) is not None:
            setattr(v, key, changes[key])
    if changes.get("clear_door"):
        v.door_id = None
    elif changes.get("door_id") is not None:
        door = await doors_mod.door_of(session, event, changes["door_id"])
        v.door_id = door.id
    await session.commit()
    return v


# --- the log ----------------------------------------------------------------------


async def log(session, event: Events, *, q: Optional[str] = None, door_id=None, via: Optional[str] = None, by: Optional[int] = None, order: str = "newest", page: int = 1, page_size: int = 50) -> dict:
    """Every admission, with filters; the facets always describe the whole
    event so the chips never vanish while a filter is on."""
    base = (
        select(CheckIns, EventRegistrationsLink, Doors, Users)
        .select_from(CheckIns)
        .join(EventRegistrationsLink, EventRegistrationsLink.id == CheckIns.registration_id)
        .outerjoin(Doors, Doors.id == CheckIns.door_id)
        .outerjoin(Users, Users.id == CheckIns.checked_in_by)
        .where(CheckIns.event_id == event.id)
    )
    if q:
        pat = f"%{q.strip()}%"
        base = base.where(or_(EventRegistrationsLink.full_name.ilike(pat), EventRegistrationsLink.email.ilike(pat), EventRegistrationsLink.ticket_id.ilike(pat)))
    if door_id is not None:
        base = base.where(CheckIns.door_id == door_id)
    if via:
        base = base.where(CheckIns.via == CheckInVia(via))
    if by is not None:
        base = base.where(CheckIns.checked_in_by == by)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    direction = CheckIns.checked_in_at.asc() if order == "oldest" else CheckIns.checked_in_at.desc()
    rows = (await session.execute(base.order_by(direction, CheckIns.id).limit(page_size).offset((page - 1) * page_size))).all()
    items = []
    for ci, reg, door, user in rows:
        role = getattr(reg.role, "value", reg.role) or "attendee"
        items.append({
            "id": ci.id, "registration_id": reg.id, "name": reg.full_name or "", "email": reg.email or None, "ticket_id": reg.ticket_id, "role": str(role),
            "door": {"id": door.id if door else None, "name": door.name if door else (ci.door_label or "No door"), "kind": door.kind.value if door else "door"},
            "via": ci.via.value, "checked_in_at": ci.checked_in_at, "recorded_at": ci.created_at,
            "by": {"kind": "PERSON" if user else ("KIOSK" if ci.via is CheckInVia.kiosk else "NOBODY"), "id": user.id if user else None, "name": (user.full_name or user.email) if user else None},
        })
    doors_f = (
        await session.execute(
            select(CheckIns.door_id, Doors.name, Doors.kind, func.count()).select_from(CheckIns).outerjoin(Doors, Doors.id == CheckIns.door_id).where(CheckIns.event_id == event.id).group_by(CheckIns.door_id, Doors.name, Doors.kind, Doors.sort_order).order_by(Doors.sort_order.nulls_last(), func.count().desc())
        )
    ).all()
    vias = (await session.execute(select(CheckIns.via, func.count()).where(CheckIns.event_id == event.id).group_by(CheckIns.via))).all()
    staff = (await session.execute(select(Users.id, Users.full_name, Users.email, func.count()).select_from(CheckIns).join(Users, Users.id == CheckIns.checked_in_by).where(CheckIns.event_id == event.id).group_by(Users.id, Users.full_name, Users.email).order_by(func.count().desc()))).all()
    first_last = (await session.execute(select(func.min(CheckIns.checked_in_at), func.max(CheckIns.checked_in_at), func.count()).where(CheckIns.event_id == event.id))).one()
    return {
        "items": items, "total": total, "page": page, "page_size": page_size,
        "facets": {
            "total": first_last[2] or 0,
            "doors": [{"id": did, "name": name or "No door", "kind": kind.value if kind else "door", "count": n} for did, name, kind, n in doors_f],
            "methods": [{"via": v.value, "count": n} for v, n in vias],
            "staff": [{"id": uid, "name": name or email, "count": n} for uid, name, email, n in staff],
            "first_at": first_last[0], "last_at": first_last[1],
        },
    }
