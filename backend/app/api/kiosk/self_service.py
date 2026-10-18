"""Self check-in from a guest's phone. The kiosk shows a QR that turns
over every minute; scanning it opens a page bound to that kiosk for ten
minutes. The guest types their name, proves it as the event asks (the
name alone, or a six-digit code to the email on the guest list), is
checked in at the kiosk's door, and taps once to have their card printed
at the kiosk.
"""
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import set_committed_value

from app.api.checkins import service as checkins
from app.api.checkins.models import CheckInVia
from app.api.events.documents import layout_colors
from app.api.events.models import EventRegistrationsLink, Events
from app.api.idcards.models import ReprintPolicy
from app.api.idcards.service import may_print_again
from app.api.kiosk.auth import kiosk_actor
from app.api.kiosk.models import KioskDevices, SelfCheckInSessions, SelfCheckInVerify
from app.config import settings
from app.core.auth.payloads import sign_payload, verify_payload
from app.core.email import email as email_mod
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

QR_ROTATE_SECONDS = 60
QR_TTL_SECONDS = 120
SESSION_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=10)
CODE_ATTEMPTS = 3
LOOKUP_ATTEMPTS = 5
ROLE_LABEL = {"attendee": "Attendee", "speaker": "Speaker", "staff": "Staff"}


def normalize_name(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def mask_email(e: str) -> str:
    local, _, domain = e.partition("@")
    return f"{local[:1]}{'•' * max(2, len(local) - 2)}{local[-1:] if len(local) > 1 else ''}@{domain}"


def _code_hash(code: str) -> str:
    return hashlib.sha256(f"{settings.SECRET_KEY}:{code}".encode()).hexdigest()


# --- the kiosk's side -----------------------------------------------------------


async def kiosk_qr(session, device: KioskDevices) -> dict:
    """What the kiosk polls: the current QR, and what just happened here."""
    event = device.event
    recent_rows = list(
        await session.scalars(
            select(SelfCheckInSessions).options(joinedload(SelfCheckInSessions.registration))
            .where(SelfCheckInSessions.kiosk_id == device.id, SelfCheckInSessions.checked_in == True)
            .order_by(SelfCheckInSessions.updated_at.desc()).limit(10)
        )
    )
    recent = [
        {"session": s.id, "registration_id": s.registration_id, "name": s.registration.full_name if s.registration else "", "at": s.updated_at, "print_requested": s.print_requested and not s.printed}
        for s in recent_rows
    ]
    if not event.self_check_in_enabled:
        return {"enabled": False, "verify": event.self_check_in_verify.value, "print": bool(event.self_check_in_print), "url": None, "expires_at": None, "recent": recent}
    now = int(datetime.now(timezone.utc).timestamp())
    slot = now - now % QR_ROTATE_SECONDS
    token = sign_payload({"t": "selfqr", "k": str(device.id), "e": device.event_id}, QR_TTL_SECONDS, slot=slot)
    return {
        "enabled": True, "verify": event.self_check_in_verify.value, "print": bool(event.self_check_in_print),
        "url": f"{settings.PUBLIC_WEB_URL.rstrip('/')}/s/{token}", "expires_at": datetime.fromtimestamp(slot + QR_TTL_SECONDS, tz=timezone.utc), "recent": recent,
    }


async def mark_printed(session, device: KioskDevices, session_id: str) -> None:
    """The kiosk has printed the card a phone asked for."""
    s = await session.get(SelfCheckInSessions, session_id)
    if s is not None and s.kiosk_id == device.id:
        s.printed = True
        await session.commit()


# --- the phone's side -----------------------------------------------------------


async def _take(session, s: SelfCheckInSessions, column, cap: int) -> bool:
    """One more try on `column`, only while under `cap`:
    UPDATE ... SET n = n + 1 WHERE id = ? AND n < cap RETURNING n. The
    session's copy is set to what the row now says."""
    t = SelfCheckInSessions.__table__
    col = t.c[column.key]
    row = (await session.execute(update(t).where(t.c.id == s.id, col < cap).values({col: col + 1}).returning(col))).first()
    if row is None:
        set_committed_value(s, column.key, cap)
        return False
    set_committed_value(s, column.key, int(row[0]))
    return True


async def _load(session, session_id: str) -> SelfCheckInSessions:
    s = await session.scalar(select(SelfCheckInSessions).options(joinedload(SelfCheckInSessions.kiosk).options(joinedload(KioskDevices.event).joinedload(Events.club), joinedload(KioskDevices.door))).where(SelfCheckInSessions.id == session_id))
    if s is None or s.expires_at < datetime.now(timezone.utc):
        raise CustomHTTPException(410, message="This page has expired. Scan the QR on the kiosk again.", error_code="SESSION_EXPIRED")
    return s


def _event_block(event: Events, device: KioskDevices) -> dict:
    club = event.club
    from app.api.events.documents import layout_colors as _lc  # noqa: F401

    return {
        "name": event.name, "venue": event.location_name, "starts_at": event.event_datetime, "org_name": club.name if club else "", "brand_primary": "#1db9a0",
        "kiosk": {"name": device.name, "door": device.door_label},
    }


async def start(session, token: str) -> dict:
    v = verify_payload(token or "")
    if not v or v.get("t") != "selfqr":
        raise CustomHTTPException(410, message="That QR code has expired. Scan the one on the kiosk again.", error_code="QR_EXPIRED")
    device = await session.scalar(select(KioskDevices).options(joinedload(KioskDevices.event).joinedload(Events.club), joinedload(KioskDevices.door)).where(KioskDevices.id == v["k"], KioskDevices.event_id == v["e"], KioskDevices.revoked_at.is_(None)))
    if device is None:
        raise CustomHTTPException(404, message="That kiosk is no longer paired.")
    event = device.event
    if not event.self_check_in_enabled:
        raise CustomHTTPException(403, message="Self check-in from a phone is not on for this event. Please see the desk.")
    s = SelfCheckInSessions(id=secrets.token_urlsafe(24), event_id=event.id, kiosk_id=device.id, expires_at=datetime.now(timezone.utc) + SESSION_TTL)
    session.add(s)
    await session.commit()
    return {
        "session": s.id, "expires_at": s.expires_at, "event": _event_block(event, device), "verify": event.self_check_in_verify.value,
        "print": bool(event.self_check_in_print), "door_open": checkins.door_open(event),
    }


def _none(outcome: str) -> dict:
    return {"outcome": outcome, "channel": None, "masked": None, "checked_in_at": None, "door": None}


async def _send_code(session, s: SelfCheckInSessions, reg: EventRegistrationsLink, event: Events) -> Optional[dict]:
    if not reg.email or reg.email.endswith("walkup.myotherapp.in"):
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    body = f"{code} is your check-in code for {event.name}. It works for ten minutes, at the kiosk you scanned."
    try:
        email_mod.send_email(
            recipients=[reg.email], subject=f"{code} — your {event.name} check-in code", text=body,
            html=f'<p style="font-size:28px;letter-spacing:6px;font-family:monospace"><strong>{code}</strong></p><p>{body}</p>',
            sender_name=(event.club.name if event.club else None) or None,
        )
    except Exception as exc:
        logger.warning("check-in code by email failed for %s: %s", reg.id, exc)
        return None
    s.code_hash = _code_hash(code)
    s.code_channel = "EMAIL"
    s.code_attempts = 0
    s.code_sent_at = datetime.now(timezone.utc)
    s.verified = False
    return {"channel": "EMAIL", "masked": mask_email(reg.email)}


async def lookup(session, session_id: str, name: str, contact: Optional[str]) -> dict:
    """The name, matched whole against the guest list. Nothing about other
    guests ever comes back: not a candidate, not a count."""
    s = await _load(session, session_id)
    event = s.kiosk.event
    if not event.self_check_in_enabled:
        return _none("DISABLED")
    if not checkins.door_open(event):
        return _none("EVENT_NOT_OPEN")
    if not await _take(session, s, SelfCheckInSessions.lookups, LOOKUP_ATTEMPTS):
        raise CustomHTTPException(429, message="Too many tries. Please see the desk.")
    s.registration_id, s.verified, s.code_hash, s.checked_in, s.print_requested, s.printed = None, False, None, False, False, False
    wanted = normalize_name(name)
    tokens = wanted.split()
    if len(tokens) < 2:
        await session.commit()
        return _none("NOT_FOUND")
    # Only names holding every word typed come back from the database (a word
    # of the normalised name is a run of the name as written, lower-cased);
    # the exact rules below then run on those few, not on the whole list.
    holding = [EventRegistrationsLink.full_name.ilike(f"%{_escape_like(t)}%", escape="\\") for t in tokens]
    guests = list(await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False, *holding)))
    matches = [g for g in guests if normalize_name(g.full_name or "") == wanted]
    if not matches:
        def in_order(g):
            words = normalize_name(g.full_name or "").split()
            i = 0
            for w in words:
                if i < len(tokens) and w == tokens[i]:
                    i += 1
            return i == len(tokens)

        matches = [g for g in guests if in_order(g)]
    if len(matches) > 1 and contact:
        c = contact.strip().lower()
        digits = re.sub(r"\D", "", c)
        matches = [g for g in matches if (g.email and g.email.lower() == c) or (digits and g.phone and re.sub(r"\D", "", g.phone) == digits)]
    if not matches:
        await session.commit()
        return _none("NOT_FOUND")
    if len(matches) > 1:
        await session.commit()
        return _none("AMBIGUOUS")
    g = matches[0]
    row = await checkins.check_in_of(session, g.id)
    if row is not None:
        await session.commit()
        return {"outcome": "ALREADY_IN", "channel": None, "masked": None, "checked_in_at": row.checked_in_at, "door": row.door_label}
    s.registration_id = g.id
    if event.self_check_in_verify is SelfCheckInVerify.name:
        s.verified = True
        await session.commit()
        return _none("READY")
    sent = await _send_code(session, s, g, event)
    await session.commit()
    if not sent:
        return _none("NO_CONTACT")
    return {"outcome": "CODE_SENT", "channel": sent["channel"], "masked": sent["masked"], "checked_in_at": None, "door": None}


async def resend(session, session_id: str) -> dict:
    """A fresh code, once."""
    s = await _load(session, session_id)
    if not s.registration_id or s.verified or not s.code_hash:
        return _none("NOT_FOUND")
    if s.code_resent:
        raise CustomHTTPException(429, message="A second code was already sent. Please see the desk.")
    reg = await session.get(EventRegistrationsLink, s.registration_id)
    sent = await _send_code(session, s, reg, s.kiosk.event) if reg else None
    s.code_resent = True
    await session.commit()
    if not sent:
        return _none("NO_CONTACT")
    return {"outcome": "CODE_SENT", "channel": sent["channel"], "masked": sent["masked"], "checked_in_at": None, "door": None}


async def verify(session, session_id: str, code: str) -> dict:
    s = await _load(session, session_id)
    if not s.registration_id or not s.code_hash or not s.code_sent_at:
        return {"outcome": "EXPIRED", "attempts_left": None}
    if datetime.now(timezone.utc) - s.code_sent_at > CODE_TTL:
        return {"outcome": "EXPIRED", "attempts_left": None}
    # The try is counted before the code is looked at, in one conditional
    # UPDATE: parallel guesses cannot each read "0 tries so far".
    if not await _take(session, s, SelfCheckInSessions.code_attempts, CODE_ATTEMPTS):
        return {"outcome": "LOCKED", "attempts_left": 0}
    if hmac.compare_digest(_code_hash(code.strip()), s.code_hash):
        s.verified = True
        s.code_hash = None
        await session.commit()
        return {"outcome": "READY", "attempts_left": None}
    await session.commit()
    left = CODE_ATTEMPTS - s.code_attempts
    return {"outcome": "LOCKED" if left == 0 else "WRONG_CODE", "attempts_left": left}


def _guest(reg: EventRegistrationsLink) -> dict:
    name = reg.full_name or ""
    role = getattr(reg.role, "value", reg.role) or "attendee"
    return {"first_name": name.split(" ")[0] if name else "there", "display_name": name, "role_label": ROLE_LABEL.get(str(role), str(role).title()), "ticket_id": reg.ticket_id}


async def check_in(session, session_id: str, background=None) -> dict:
    """Verified: through the door at the kiosk's gate, the same way a scan would go."""
    s = await _load(session, session_id)
    if not s.registration_id or not s.verified:
        return {"outcome": "NOT_VERIFIED", "guest": None, "can_print": False}
    device = s.kiosk
    event = device.event
    if not checkins.door_open(event):
        return {"outcome": "EVENT_NOT_OPEN", "guest": None, "can_print": False}
    reg = await checkins.registration_of(session, event, s.registration_id)
    res = await checkins.scan(session, event, code=str(reg.id), door_label=device.door_label, client_ref=f"self-{s.id}", scanned_at=None, actor=kiosk_actor(device), via=CheckInVia.phone, background=background)
    if res["outcome"] not in ("CHECKED_IN", "ALREADY_CHECKED_IN"):
        return {"outcome": "EVENT_NOT_OPEN" if res["outcome"] == "EVENT_NOT_OPEN" else "REFUSED", "guest": None, "can_print": False, "why": res["outcome"]}
    if res["outcome"] == "CHECKED_IN":
        row = await checkins.check_in_of(session, reg.id)
        if row is not None:
            row.kiosk_device_id = device.id
    s.checked_in = True
    await session.commit()
    await session.refresh(reg)
    may, _ = may_print_again(event.reprint_policy or ReprintPolicy.anyone, reg.badge_printed_at, False)
    return {"outcome": "ALREADY_IN" if res["outcome"] == "ALREADY_CHECKED_IN" else "CHECKED_IN", "guest": _guest(reg), "can_print": bool(event.self_check_in_print) and may}


async def print_now(session, session_id: str) -> dict:
    """One tap at the kiosk: the kiosk prints the card on its next poll."""
    s = await _load(session, session_id)
    if not s.registration_id or not s.checked_in:
        return {"outcome": "NOT_CHECKED_IN"}
    if s.printed or s.print_requested:
        return {"outcome": "ALREADY_PRINTED"}
    event = s.kiosk.event
    reg = await session.get(EventRegistrationsLink, s.registration_id)
    may, _ = may_print_again(event.reprint_policy or ReprintPolicy.anyone, reg.badge_printed_at if reg else None, False)
    if not event.self_check_in_print or not may:
        return {"outcome": "NO_PRINTER"}
    s.print_requested = True
    await session.commit()
    return {"outcome": "QUEUED"}


async def status(session, session_id: str) -> dict:
    s = await _load(session, session_id)
    return {"checked_in": s.checked_in, "print": None if not s.print_requested else {"status": "DONE" if s.printed else "QUEUED"}}
