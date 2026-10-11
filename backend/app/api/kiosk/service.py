"""Kiosks: pairing, the attract screen's context, redeeming a ticket, and
"this isn't me"."""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from app.api.checkins import doors as doors_mod
from app.api.checkins import service as checkins
from app.api.checkins.models import CheckIns, CheckInVia, DoorKind
from app.api.events.documents import layout_colors
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.tickets import poster_url
from app.api.idcards.models import PrintJobs, ReprintPolicy
from app.api.idcards.service import may_print_again
from app.api.kiosk.auth import kiosk_actor, new_token
from app.api.kiosk.models import KioskDevices, SelfCheckInVerify
from app.config import settings
from app.core.auth.payloads import sign_payload, verify_payload
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

# How long a guest at a kiosk may say "this isn't me" after their code read.
UNDO_WINDOW = timedelta(minutes=10)
REDEMPTION_TTL = 600
ROLE_LABEL = {"attendee": "Attendee", "speaker": "Speaker", "staff": "Staff"}


# --- devices ------------------------------------------------------------------------


def device_out(d: KioskDevices) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": d.id, "event_id": d.event_id, "name": d.name, "door_id": d.door_id, "door_label": d.door_label, "token_prefix": d.token_prefix,
        "expires_at": d.expires_at, "last_seen_at": d.last_seen_at, "revoked_at": d.revoked_at,
        "online": bool(d.last_seen_at and now - d.last_seen_at < timedelta(seconds=90)), "active": d.revoked_at is None and d.expires_at > now,
        "created_at": d.created_at,
    }


async def list_devices(session, event: Events) -> list[dict]:
    rows = list(await session.scalars(select(KioskDevices).where(KioskDevices.event_id == event.id).order_by(KioskDevices.created_at.desc()).limit(50)))
    return [device_out(d) for d in rows]


def pair_url(token: str) -> str:
    return f"{settings.PUBLIC_WEB_URL.rstrip('/')}/kiosk?token={token}"


async def create_device(session, event: Events, *, name: str, door_label: Optional[str], door_id, shift_hours: int, created_by) -> dict:
    """Bound to one event, rotated per shift, revocable. The token is shown once."""
    if door_id is not None:
        door = await doors_mod.door_of(session, event, door_id)
    else:
        door = await doors_mod.resolve(session, event.id, door_label or name, DoorKind.kiosk)
    token, h, prefix = new_token()
    device = KioskDevices(id=uuid.uuid4(), event_id=event.id, name=" ".join(name.split())[:60], door_id=door.id if door else None, door_label=door.name if door else (door_label or name)[:40], token_hash=h, token_prefix=prefix, created_by=created_by, expires_at=datetime.now(timezone.utc) + timedelta(hours=shift_hours))
    session.add(device)
    await session.commit()
    return {"device": device_out(device), "token": token, "url": pair_url(token)}


async def device_of(session, event: Events, device_id) -> KioskDevices:
    d = await session.get(KioskDevices, device_id)
    if d is None or d.event_id != event.id:
        raise CustomHTTPException(404, message="That kiosk could not be found.")
    return d


async def rotate_device(session, event: Events, device_id, *, shift_hours: int) -> dict:
    d = await device_of(session, event, device_id)
    token, h, prefix = new_token()
    d.token_hash, d.token_prefix, d.revoked_at, d.last_seen_at = h, prefix, None, None
    d.expires_at = datetime.now(timezone.utc) + timedelta(hours=shift_hours)
    await session.commit()
    return {"device": device_out(d), "token": token, "url": pair_url(token)}


async def revoke_device(session, event: Events, device_id) -> dict:
    d = await device_of(session, event, device_id)
    d.revoked_at = datetime.now(timezone.utc)
    await session.commit()
    return device_out(d)


def kiosk_settings(event: Events) -> dict:
    return {
        "self_check_in_enabled": bool(event.self_check_in_enabled),
        "self_check_in_verify": (event.self_check_in_verify or SelfCheckInVerify.email).value,
        "self_check_in_print": bool(event.self_check_in_print),
        "has_pin": bool(event.kiosk_pin),
    }


async def update_kiosk_settings(session, event: Events, body) -> dict:
    changes = body.model_dump(exclude_unset=True)
    if changes.get("self_check_in_verify") is not None:
        verify = SelfCheckInVerify(changes["self_check_in_verify"])
        if verify is SelfCheckInVerify.phone:
            raise CustomHTTPException(422, message="A code by SMS or WhatsApp needs a provider, which is not set up. Use a code by email, or the name alone.", errors={"fields": {"self_check_in_verify": "No SMS or WhatsApp provider."}})
        event.self_check_in_verify = verify
    if changes.get("self_check_in_enabled") is not None:
        event.self_check_in_enabled = bool(changes["self_check_in_enabled"])
    if changes.get("self_check_in_print") is not None:
        event.self_check_in_print = bool(changes["self_check_in_print"])
    if "kiosk_pin" in changes:
        pin = (changes["kiosk_pin"] or "").strip()
        event.kiosk_pin = pin or None
    await session.commit()
    return kiosk_settings(event)


# --- the kiosk screen -------------------------------------------------------------


async def context(session, device: KioskDevices) -> dict:
    event = device.event
    club = event.club
    from app.api.idcards.service import badge_style

    style = await badge_style(session, event, None)
    counters = await checkins.counters(session, event.id)
    here = await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.kiosk_device_id == device.id)) or 0
    return {
        "device": {"id": device.id, "name": device.name, "door": device.door_label, "expires_at": device.expires_at},
        "event": {
            "id": event.id, "name": event.name, "org_name": club.name if club else "", "venue": event.location_name, "starts_at": event.event_datetime,
            "brand_primary": layout_colors(style)["primary"], "poster_url": poster_url(event), "door_open": checkins.door_open(event),
            "reprint_policy": (event.reprint_policy or ReprintPolicy.anyone).value, "auto_print_badges": bool(event.auto_print_badges), "has_fee": bool(event.has_fee),
        },
        "self_check_in": kiosk_settings(event),
        "counters": {**counters, "checked_in_here": here},
    }


def _redemption(reg_id, device: KioskDevices, new: bool) -> str:
    return sign_payload({"t": "redeem", "a": str(reg_id), "d": str(device.id), "e": device.event_id, "n": new}, REDEMPTION_TTL)


def read_redemption(device: KioskDevices, token: str) -> dict:
    v = verify_payload(token or "")
    if not v or v.get("t") != "redeem":
        raise CustomHTTPException(410, message="That check-in has timed out. Scan again.", error_code="REDEMPTION_EXPIRED")
    if v.get("d") != str(device.id) or v.get("e") != device.event_id:
        raise CustomHTTPException(403, message="That check-in belongs to another kiosk.")
    return v


def _guest(reg: EventRegistrationsLink, res: dict, event: Events) -> dict:
    name = reg.full_name or ""
    role = getattr(reg.role, "value", reg.role) or "attendee"
    may, _ = may_print_again(event.reprint_policy or ReprintPolicy.anyone, reg.badge_printed_at, False)
    return {
        "id": reg.id, "first_name": name.split(" ")[0] if name else "there", "display_name": name, "role": str(role), "role_label": ROLE_LABEL.get(str(role), str(role).title()),
        "ticket_id": reg.ticket_id, "already_checked_in": res["outcome"] == "ALREADY_CHECKED_IN", "has_card": reg.badge_printed_at is not None, "may_print": may,
    }


async def redeem(session, device: KioskDevices, *, code: str, client_ref: str) -> dict:
    """A ticket at the kiosk: checked in the instant the code reads, with a
    token the guest can undo it with for a few minutes."""
    event = device.event
    actor = kiosk_actor(device)
    if not checkins.door_open(event):
        return {"outcome": "EVENT_NOT_OPEN", "guest": None, "other_event": None, "redemption_token": None, "print_url": None}
    res = await checkins.scan(session, event, code=code, door_label=device.door_label, client_ref=client_ref, scanned_at=None, actor=actor, via=CheckInVia.kiosk)
    outcome = "MATCHED" if res["outcome"] in ("CHECKED_IN", "ALREADY_CHECKED_IN") else ("NO_MATCH" if res["outcome"] == "INVALID" else res["outcome"])
    if outcome != "MATCHED" or not res["attendee"]:
        return {"outcome": outcome, "guest": None, "other_event": res.get("other_event"), "redemption_token": None, "print_url": None}
    reg = await checkins.registration_of(session, event, res["attendee"]["id"])
    new = res["outcome"] == "CHECKED_IN"
    if new:
        # The kiosk that made it, for "this isn't me".
        row = await checkins.check_in_of(session, reg.id)
        if row is not None:
            row.kiosk_device_id = device.id
            await session.commit()
    token = _redemption(reg.id, device, new)
    guest = _guest(reg, res, event)
    # The kiosk prints on a first admission when the event asks, or when the
    # guest taps print; either way through the card route with this token.
    wants = new and bool(event.auto_print_badges) and guest["may_print"] and reg.badge_printed_at is None
    return {"outcome": "MATCHED", "guest": guest, "other_event": None, "redemption_token": token, "print_url": f"/api/v1/kiosk/card.pdf?redemption={token}", "auto_print": wants}


async def not_me(session, device: KioskDevices, token: str) -> dict:
    """Undo only the check-in this kiosk itself made, moments ago, for the
    guest the token names. Anything else is someone really here."""
    r = read_redemption(device, token)
    if not r.get("n"):
        return {"ok": True}
    row = await session.scalar(select(CheckIns).where(CheckIns.registration_id == uuid.UUID(r["a"])))
    if row is None or row.via is not CheckInVia.kiosk or row.kiosk_device_id != device.id or row.checked_in_at < datetime.now(timezone.utc) - UNDO_WINDOW:
        return {"ok": True}
    await checkins.undo(session, device.event, row.registration_id, kiosk_actor(device))
    return {"ok": True}


async def card_for_redemption(session, device: KioskDevices, token: str) -> bytes:
    from app.api.idcards.models import PrintSource
    from app.api.idcards.service import card_pdf

    r = read_redemption(device, token)
    return await card_pdf(session, device.event, uuid.UUID(r["a"]), actor=kiosk_actor(device), source=PrintSource.kiosk, record=True, door=device.door_label)


async def staff(session, device: KioskDevices, pin: str) -> dict:
    event = device.event
    if not event.kiosk_pin or event.kiosk_pin != (pin or "").strip():
        raise CustomHTTPException(403, message="That PIN does not open staff mode on this kiosk.")
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    printed_today = await session.scalar(select(func.coalesce(func.sum(PrintJobs.cards), 0)).where(PrintJobs.event_id == event.id, PrintJobs.created_at >= since)) or 0
    here = await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.kiosk_device_id == device.id)) or 0
    return {"ok": True, "device": {"name": device.name, "door": device.door_label}, "printed_today": int(printed_today), "checked_in_here": here, "counters": await checkins.counters(session, event.id)}
