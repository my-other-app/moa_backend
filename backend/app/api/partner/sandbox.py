"""Test mode: what `moa_test_` keys and partner test tokens work on.

A sandbox per club, in partner_sandbox_objects: test events, registrations,
check-ins and certificates in exactly the shapes the live objects have
(`livemode: false`, ids starting `test_`). Nothing here is in the real
tables, so a test object is never on the public site, in the app, in the
club's dashboard lists or in the plan's monthly counts; it sends no email,
WhatsApp or push, and takes no payment. Live credentials never see it, and
test credentials never see a live object.

The rules a partner's integration has to handle are the live ones, applied
here on the sandbox: the same input checks (shared with live.py), events made
as drafts and published or cancelled, registration windows, capacity
(EVENT_FULL), the same address twice (ALREADY_REGISTERED), events that review
registrations (pending_approval), the money rules (PAYMENT_REQUIRED,
EXTERNAL_PAYMENTS_OFF, payments outside MyOtherApp), the door refusing a
draft, one check-in per person (a second is `duplicate: true`), certificates
issued once. External ids, idempotency, ETags, cursors and `updated_since`
work exactly as live.
"""
from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi.responses import JSONResponse
from sqlalchemy import TIMESTAMP, cast, select, tuple_

from app.api.partner import external_ids as ext_ids
from app.api.partner.emitting import emit
from app.api.partner.common import (
    TIMEZONE, check_if_match, check_limit, decode_cursor, encode_cursor, iso, list_response, money, now, object_response, parse_time,
)
from app.api.partner.errors import PartnerError, not_found
from app.api.partner.models import PartnerCertificateJobs, PartnerSandboxObjects

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_id(kind: str) -> str:
    short = {"event": "evt", "registration": "reg", "checkin": "chk", "certificate": "crt", "ticket_type": "tt"}[kind]
    return f"test_{short}_{uuid.uuid4().hex[:24]}"


def _code(prefix: str, groups: int = 2) -> str:
    return prefix + "-" + "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(groups))


def _strip(data: dict) -> dict:
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _event_status(data: dict) -> str:
    status = data.get("status")
    if status == "published" and data.get("ends_at") and datetime.fromisoformat(data["ends_at"]) < now():
        return "ended"
    return status


async def _get(caller, kind: str, object_id) -> PartnerSandboxObjects:
    caller.require_club()
    if not str(object_id or "").startswith("test_"):
        raise not_found(kind.capitalize())
    row = await caller.session.get(PartnerSandboxObjects, str(object_id), populate_existing=True)
    if row is None or row.club_id != caller.club_id or row.kind != kind:
        raise not_found(kind.capitalize())
    return row


def _save(row: PartnerSandboxObjects, data: dict) -> None:
    stamp = now()
    data["updated_at"] = iso(stamp)
    row.data = dict(data)
    row.updated_at = stamp


async def _new(caller, kind: str, data: dict, *, parent_id: Optional[str] = None) -> PartnerSandboxObjects:
    stamp = now()
    data.setdefault("created_at", iso(stamp))
    data["updated_at"] = iso(stamp)
    row = PartnerSandboxObjects(id=data.get("id") or new_id(kind), club_id=caller.club_id, kind=kind, parent_id=parent_id, data=dict(data),
                                created_at=stamp, updated_at=stamp)
    caller.session.add(row)
    await caller.session.flush()
    return row


async def _external(caller, resource: str, object_id: str) -> Optional[str]:
    return await ext_ids.of_object(caller.session, caller.client_id, resource, object_id)


async def _page(caller, kind: str, *, parent_id=None, filters=(), updated_since=None, external_id=None, resource=None, limit=None, cursor=None):
    S = PartnerSandboxObjects
    caller.require_club()
    limit = check_limit(limit)
    q = select(S).where(S.club_id == caller.club_id, S.kind == kind, *filters)
    if parent_id is not None:
        q = q.where(S.parent_id == parent_id)
    if external_id is not None:
        found = await ext_ids.find(caller.session, caller.client_id, resource, ext_ids.check(external_id))
        q = q.where(S.id == (found or "-"))
    since = parse_time(updated_since, "updated_since") if updated_since else None
    if since is not None:
        q = q.where(S.updated_at >= since)
        order = (S.updated_at, S.id)
    else:
        order = (S.created_at, S.id)
    after = decode_cursor(cursor, ("dt", "str"))
    if after:
        q = q.where(tuple_(*order) > (after[0], after[1]))
    rows = list((await caller.session.scalars(q.order_by(*order).limit(limit + 1))).all())
    page, more = rows[:limit], len(rows) > limit
    nxt = None
    if more and page:
        last = page[-1]
        nxt = encode_cursor(last.updated_at if since is not None else last.created_at, last.id)
    return page, nxt


# ─── Events ──────────────────────────────────────────────────────────────


def _event_present(data: dict, external_id: Optional[str]) -> dict:
    out = _strip(data)
    out["status"] = _event_status(data)
    out["external_id"] = external_id
    return out


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "event"
    return f"{base}-test-{secrets.token_hex(2)}"


def _apply_event(data: dict, fields: dict, sent: dict) -> None:
    from app.core import money as money_mod

    if "name" in fields:
        data["name"] = fields["name"]
    if "about" in fields:
        data["description"] = fields["about"]
    if "event_datetime" in fields:
        data["starts_at"] = iso(fields["event_datetime"])
        data["ends_at"] = iso(fields["event_datetime"] + timedelta(hours=float(fields["duration"])))
    if "location_name" in fields:
        pin = fields.get("pin")
        data["venue"] = None if fields["location_name"] is None else {
            "name": fields["location_name"], "address": None, "city": None,
            "lat": pin[0] if pin else None, "lng": pin[1] if pin else None, "map_url": None,
        }
    if "is_online" in fields:
        data["online"] = {"url": fields["url"]} if fields["is_online"] else None
    if "max_participants" in fields:
        data["capacity"] = fields["max_participants"]
    reg = data.setdefault("registration", {"opens_at": iso(now()), "closes_at": None, "requires_approval": False})
    if "reg_startdate" in fields:
        reg["opens_at"] = iso(fields["reg_startdate"])
    if "reg_enddate" in fields:
        reg["closes_at"] = iso(fields["reg_enddate"])
    if "requires_approval" in fields:
        reg["requires_approval"] = bool(fields["requires_approval"])
    if "has_fee" in fields:
        data["price"] = money(money_mod.to_paise(fields["reg_fee"])) if fields["has_fee"] else None
        for t in data.get("ticket_types") or []:
            t["price"] = data["price"] or money(0)
    if "max_participants" in fields:
        for t in data.get("ticket_types") or []:
            t["capacity"] = fields["max_participants"]
    if "poster_url" in sent:
        data["poster_url"] = sent["poster_url"]


async def list_events(caller, *, status, starts_after, starts_before, updated_since, external_id, limit, cursor):
    S = PartnerSandboxObjects
    filters = []
    ends = cast(S.data["ends_at"].astext, TIMESTAMP(timezone=True))
    starts = cast(S.data["starts_at"].astext, TIMESTAMP(timezone=True))
    if status == "ended":
        filters += [S.data["status"].astext == "published", ends < now()]
    elif status == "published":
        filters += [S.data["status"].astext == "published", ends >= now()]
    elif status in ("draft", "cancelled"):
        filters.append(S.data["status"].astext == status)
    if starts_after:
        filters.append(starts >= parse_time(starts_after, "starts_after"))
    if starts_before:
        filters.append(starts < parse_time(starts_before, "starts_before"))
    page, nxt = await _page(caller, "event", filters=filters, updated_since=updated_since, external_id=external_id, resource="event", limit=limit, cursor=cursor)
    exts = await ext_ids.of_objects(caller.session, caller.client_id, "event", [r.id for r in page])
    return list_response([_event_present(r.data, exts.get(r.id)) for r in page], nxt)


async def event_out(caller, row) -> dict:
    return _event_present(row.data, await _external(caller, "event", row.id))


async def get_event(caller, event_id):
    row = await _get(caller, "event", event_id)
    return object_response(caller.request, await event_out(caller, row))


async def create_event(caller, body):
    from app.api.partner import live

    caller.require_club()
    external = ext_ids.check(body.external_id)
    fields = live.event_fields(body)
    if fields["event_datetime"] <= now():
        raise PartnerError("VALIDATION", "starts_at is in the past; an event is for something still to come.", details={"fields": {"starts_at": "In the future."}})
    await live._category(caller.session, body.category)
    reservation = await ext_ids.reserve(caller.session, caller, "event", external)
    event_id = new_id("event")
    data = {
        "id": event_id, "object": "event", "club_id": str(caller.club_id), "name": fields["name"], "slug": _slug(fields["name"]),
        "description": None, "status": "draft", "timezone": TIMEZONE, "venue": None, "online": None, "poster_url": None, "capacity": None,
        "registration": {"opens_at": iso(fields.get("reg_startdate") or now()), "closes_at": None, "requires_approval": False},
        "price": None, "ticket_types": [{"id": new_id("ticket_type"), "name": "General", "price": money(0), "capacity": None,
                                         "sales_start": None, "sales_end": None, "hidden": False}],
        "external_payments_allowed": False, "registrations_count": 0, "public_url": None, "livemode": False,
    }
    _apply_event(data, fields, body.model_dump(exclude_unset=True))
    try:
        row = await _new(caller, "event", data)
        await caller.session.commit()
    except Exception:
        await ext_ids.release(caller.session, reservation)
        raise
    await ext_ids.bind(caller.session, reservation, row.id)
    await emit(caller, "event.created", _event_present(row.data, None))
    return object_response(caller.request, await event_out(caller, row), status=201)


async def patch_event(caller, event_id, body):
    from app.api.partner import live

    row = await _get(caller, "event", event_id)
    check_if_match(caller.request, await event_out(caller, row))
    data = dict(row.data)
    if data["status"] == "cancelled":
        raise PartnerError("EVENT_CANCELLED", "This event was cancelled, so it can't be changed.", status=409)
    sent = body.model_dump(exclude_unset=True)
    if "category" in sent:
        raise PartnerError("VALIDATION", "An event's type is changed in the dashboard.", details={"fields": {"category": "Read only here."}})
    current = _Shadow(data)
    fields = live.event_fields(body, current=current)
    if not ({"starts_at", "ends_at"} & set(sent)):
        fields.pop("event_datetime", None)
        fields.pop("duration", None)
    if "external_id" in sent:
        await ext_ids.set_for(caller.session, caller, "event", row.id, ext_ids.check(sent["external_id"]))
    _apply_event(data, fields, sent)
    _save(row, data)
    await caller.session.commit()
    await emit(caller, "event.updated", _event_present(row.data, None))
    return object_response(caller.request, await event_out(caller, row))


class _Shadow:
    """A sandbox event read as live.event_fields reads the event's columns."""

    def __init__(self, data: dict):
        self.event_datetime = datetime.fromisoformat(data["starts_at"])
        ends = datetime.fromisoformat(data["ends_at"])
        self.duration = (ends - self.event_datetime).total_seconds() / 3600
        closes = (data.get("registration") or {}).get("closes_at")
        self.reg_enddate = datetime.fromisoformat(closes) if closes else None


async def publish_event(caller, event_id):
    row = await _get(caller, "event", event_id)
    data = dict(row.data)
    status = _event_status(data)
    if status == "published":
        return object_response(caller.request, await event_out(caller, row))
    if status != "draft":
        raise PartnerError("INVALID_STATE", f"Only a draft can be published; this event is {status}.")
    if datetime.fromisoformat(data["ends_at"]) <= now():
        raise PartnerError("INVALID_STATE", "This event is already over, so it can't be published.")
    data["status"] = "published"
    _save(row, data)
    await caller.session.commit()
    await emit(caller, "event.published", _event_present(row.data, None))
    return object_response(caller.request, await event_out(caller, row))


async def cancel_event(caller, event_id, reason: str):
    row = await _get(caller, "event", event_id)
    data = dict(row.data)
    status = _event_status(data)
    if status == "cancelled":
        return object_response(caller.request, await event_out(caller, row))
    if status == "ended":
        raise PartnerError("INVALID_STATE", "This event is over, so it can't be cancelled.")
    data["status"] = "cancelled"
    data["_cancel_reason"] = reason
    _save(row, data)
    await caller.session.commit()
    await emit(caller, "event.cancelled", _event_present(row.data, None))
    return object_response(caller.request, await event_out(caller, row))


# ─── Registrations ───────────────────────────────────────────────────────


def _registration_present(caller, data: dict, external_id: Optional[str]) -> dict:
    out = _strip(data)
    if not caller.contact:
        out.pop("person", None)
        out.pop("answers", None)
    out["external_id"] = external_id
    return out


async def _registrations_of(caller, event_id: str) -> list[PartnerSandboxObjects]:
    S = PartnerSandboxObjects
    return list((await caller.session.scalars(
        select(S).where(S.club_id == caller.club_id, S.kind == "registration", S.parent_id == event_id).with_for_update()
    )).all())


async def _count_reads(caller, n: int, event_id) -> None:
    from app.api.partner.live import _count_attendee_reads

    await _count_attendee_reads(caller, n, event_id=None, what=f"test registrations of {event_id}")


async def list_registrations(caller, event_id, *, status, updated_since, external_id, limit, cursor):
    S = PartnerSandboxObjects
    await _get(caller, "event", event_id)
    filters = [S.data["status"].astext == status] if status else []
    page, nxt = await _page(caller, "registration", parent_id=str(event_id), filters=filters, updated_since=updated_since,
                            external_id=external_id, resource="registration", limit=limit, cursor=cursor)
    exts = await ext_ids.of_objects(caller.session, caller.client_id, "registration", [r.id for r in page])
    data = [_registration_present(caller, r.data, exts.get(r.id)) for r in page]
    await _count_reads(caller, len(data) if caller.contact else 0, event_id)
    return list_response(data, nxt)


async def registration_out(caller, row) -> dict:
    return _registration_present(caller, row.data, await _external(caller, "registration", row.id))


async def get_registration(caller, registration_id):
    row = await _get(caller, "registration", registration_id)
    return object_response(caller.request, await registration_out(caller, row))


async def create_registration(caller, event_id, body):
    session = caller.session
    event_row = await _get(caller, "event", event_id)
    event = dict(event_row.data)
    external = ext_ids.check(body.external_id)
    status = _event_status(event)
    if status in ("cancelled", "ended"):
        raise PartnerError("REGISTRATION_CLOSED", f"This event is {status}, so nobody new can register.", details={"closed_reason": status})
    reg_window = event.get("registration") or {}
    if reg_window.get("opens_at") and datetime.fromisoformat(reg_window["opens_at"]) > now():
        raise PartnerError("REGISTRATION_CLOSED", "Registration has not opened yet.", details={"closed_reason": "not_started", "opens_at": reg_window["opens_at"]})
    if reg_window.get("closes_at") and datetime.fromisoformat(reg_window["closes_at"]) < now():
        raise PartnerError("REGISTRATION_CLOSED", "Registration has closed.", details={"closed_reason": "deadline_passed", "closed_at": reg_window["closes_at"]})
    types = event.get("ticket_types") or []
    ttype = types[0] if types else None
    if body.ticket_type_id not in (None, ""):
        ttype = next((t for t in types if t["id"] == str(body.ticket_type_id)), None)
        if ttype is None:
            raise PartnerError("TICKET_TYPE_NOT_FOUND", "That ticket type is not on sale for this event.", status=422,
                               details={"ticket_type_id": "Not a ticket type of this event."})
    price = int((event.get("price") or {}).get("amount") or 0)
    payment = body.payment
    approval = bool(reg_window.get("requires_approval"))
    if approval and payment is not None:
        raise PartnerError("VALIDATION", "This event reviews every registration, so a payment can't come with one.", details={"fields": {"payment": "Not with a reviewed event."}})
    if price > 0 and not approval:
        if payment is None:
            raise PartnerError("PAYMENT_REQUIRED", "This is a paid event. Test events take no payments: send a payment taken outside MyOtherApp, if the event allows it.",
                               details={"checkout_url": None, "amount": price, "currency": "INR",
                                        "external_payments_allowed": bool(event.get("external_payments_allowed"))})
        if not event.get("external_payments_allowed"):
            raise PartnerError("EXTERNAL_PAYMENTS_OFF", "This event doesn't accept registrations paid outside MyOtherApp.", details={"checkout_url": None})
    elif payment is not None and price == 0:
        raise PartnerError("VALIDATION", "This event is free, so no payment comes with a registration.", details={"fields": {"payment": "The event is free."}})
    email = str(body.person.email).strip()
    regs = await _registrations_of(caller, event_row.id)
    live = [r for r in regs if r.data.get("status") not in ("cancelled", "declined", "withdrawn")]
    same = next((r for r in live if (r.data.get("person") or {}).get("email", "").strip().lower() == email.lower()), None)
    if same is not None:
        raise PartnerError("ALREADY_REGISTERED", "This email is already registered for this event.", details={"id": same.id})
    holding = [r for r in live if r.data.get("status") == "confirmed"]
    if event.get("capacity") and not approval and len(holding) >= int(event["capacity"]):
        raise PartnerError("EVENT_FULL", "This event is full.", details={"max_participants": event["capacity"]})
    reservation = await ext_ids.reserve(session, caller, "registration", external)
    reg_id = new_id("registration")
    data = {
        "id": reg_id, "object": "registration", "event_id": event_row.id,
        "status": "pending_approval" if approval else "confirmed",
        "person": {"name": body.person.name.strip(), "email": email, "phone": (body.person.phone or "").strip() or None},
        "answers": dict(body.answers or {}),
        "ticket": None if approval else {"code": _code("TEST"), "qr_url": None, "status": "valid"},
        "ticket_type": {"id": ttype["id"], "name": ttype["name"]} if ttype else None,
        "approval": {"status": "pending", "decided_at": None, "request_id": reg_id} if approval else None,
        "payment": (
            {"status": "paid_externally", "amount": int(payment.amount), "currency": "INR", "reference": payment.reference}
            if payment is not None else {"status": "not_required", "amount": 0, "currency": "INR"}
        ),
        "checked_in_at": None, "request_id": reg_id if approval else None, "livemode": False,
    }
    try:
        row = await _new(caller, "registration", data, parent_id=event_row.id)
        if not approval:
            event["registrations_count"] = int(event.get("registrations_count") or 0) + 1
            _save(event_row, event)
        await session.commit()
    except Exception:
        await ext_ids.release(session, reservation)
        raise
    await ext_ids.bind(session, reservation, row.id)
    await emit(caller, "registration.created", _strip(row.data))
    return object_response(caller.request, await registration_out(caller, row), status=202 if approval else 201)


async def patch_registration(caller, registration_id, body):
    row = await _get(caller, "registration", registration_id)
    check_if_match(caller.request, await registration_out(caller, row))
    data = dict(row.data)
    if data["status"] in ("cancelled", "declined", "withdrawn"):
        raise PartnerError("NOT_FOUND", "Registration not found.")
    sent = body.model_dump(exclude_unset=True)
    if "name" in sent and sent["name"]:
        data["person"] = {**(data.get("person") or {}), "name": sent["name"].strip()}
    if "answers" in sent and sent["answers"] is not None:
        data["answers"] = {**(data.get("answers") or {}), **sent["answers"]}
    if "external_id" in sent:
        await ext_ids.set_for(caller.session, caller, "registration", row.id, ext_ids.check(sent["external_id"]))
    _save(row, data)
    await caller.session.commit()
    await emit(caller, "registration.updated", _strip(row.data))
    return object_response(caller.request, await registration_out(caller, row))


async def cancel_registration(caller, registration_id):
    row = await _get(caller, "registration", registration_id)
    data = dict(row.data)
    if data["status"] in ("cancelled", "withdrawn"):
        return object_response(caller.request, await registration_out(caller, row))
    if data.get("checked_in_at"):
        raise PartnerError("ALREADY_CHECKED_IN", "This person is already checked in. Undo the check-in first.")
    was = data["status"]
    data["status"] = "withdrawn" if was == "pending_approval" else "cancelled"
    if data.get("ticket"):
        data["ticket"] = {**data["ticket"], "status": "cancelled", "qr_url": None}
    _save(row, data)
    if was == "confirmed":
        event_row = await _get(caller, "event", row.parent_id)
        event = dict(event_row.data)
        event["registrations_count"] = max(0, int(event.get("registrations_count") or 0) - 1)
        _save(event_row, event)
    await caller.session.commit()
    await emit(caller, "registration.cancelled", _strip(row.data))
    return object_response(caller.request, await registration_out(caller, row))


# ─── Check-ins ───────────────────────────────────────────────────────────


async def create_checkin(caller, body):
    S = PartnerSandboxObjects
    session = caller.session
    caller.require_club()
    given = [k for k in ("ticket_code", "registration_id", "external_id") if getattr(body, k)]
    if len(given) != 1:
        raise PartnerError("VALIDATION", "Send exactly one of ticket_code, registration_id or external_id.",
                           details={"fields": {k: "Exactly one." for k in ("ticket_code", "registration_id", "external_id")}})
    if body.ticket_code:
        reg = await session.scalar(select(S).where(
            S.club_id == caller.club_id, S.kind == "registration", S.data["ticket"]["code"].astext == body.ticket_code.strip().upper(),
        ).with_for_update())
    else:
        target = body.registration_id
        if body.external_id:
            target = await ext_ids.find(session, caller.client_id, "registration", ext_ids.check(body.external_id))
        reg = await session.get(S, str(target or ""), with_for_update=True) if target else None
        if reg is not None and (reg.club_id != caller.club_id or reg.kind != "registration"):
            reg = None
    if reg is None or reg.data.get("status") in ("cancelled", "withdrawn", "declined"):
        raise not_found("Registration")
    if reg.data.get("status") == "pending_approval":
        raise PartnerError("INVALID_STATE", "This registration is still waiting for review.")
    event_row = await _get(caller, "event", reg.parent_id)
    existing = await session.scalar(select(S).where(S.club_id == caller.club_id, S.kind == "checkin", S.data["registration_id"].astext == reg.id))
    if existing is not None:
        return JSONResponse({**_strip(existing.data), "duplicate": True}, status_code=200)
    if _event_status(event_row.data) in ("draft", "cancelled"):
        raise PartnerError("EVENT_NOT_OPEN", "The door isn't open for this event (a draft, or the door is closed).", details={"outcome": "EVENT_NOT_OPEN"})
    scanned = parse_time(body.scanned_at, "scanned_at") if body.scanned_at else None
    stamp = now()
    data = {
        "id": new_id("checkin"), "object": "checkin", "registration_id": reg.id, "event_id": reg.parent_id, "checked_in_at": iso(stamp),
        "scanned_at": iso(scanned), "gate": (body.gate or "").strip() or None, "via": "api", "duplicate": False, "livemode": False,
    }
    row = await _new(caller, "checkin", data, parent_id=reg.parent_id)
    reg_data = dict(reg.data)
    reg_data["checked_in_at"] = iso(stamp)
    _save(reg, reg_data)
    await session.commit()
    await emit(caller, "checkin.created", _strip(row.data))
    return JSONResponse(_strip(row.data), status_code=201)


async def delete_checkin(caller, checkin_id):
    row = await _get(caller, "checkin", checkin_id)
    reg = await caller.session.get(PartnerSandboxObjects, row.data.get("registration_id"))
    out = {**_strip(row.data), "deleted": True}
    await caller.session.delete(row)
    if reg is not None:
        data = dict(reg.data)
        data["checked_in_at"] = None
        _save(reg, data)
    await caller.session.commit()
    await emit(caller, "checkin.deleted", out)
    return JSONResponse(out)


async def list_checkins(caller, event_id, *, updated_since, limit, cursor):
    await _get(caller, "event", event_id)
    page, nxt = await _page(caller, "checkin", parent_id=str(event_id), updated_since=updated_since, limit=limit, cursor=cursor)
    return list_response([_strip(r.data) for r in page], nxt)


# ─── Certificates ────────────────────────────────────────────────────────


def _certificate_present(caller, data: dict) -> dict:
    out = _strip(data)
    if not caller.contact:
        out.pop("name", None)
    return out


async def list_certificates(caller, event_id, *, limit, cursor):
    await _get(caller, "event", event_id)
    page, nxt = await _page(caller, "certificate", parent_id=str(event_id), limit=limit, cursor=cursor)
    return list_response([_certificate_present(caller, r.data) for r in page], nxt)


async def issue_certificates(caller, event_id, body):
    S = PartnerSandboxObjects
    session = caller.session
    event_row = await _get(caller, "event", event_id)
    if bool(body.registration_ids) == bool(body.all_checked_in):
        raise PartnerError("VALIDATION", "Send registration_ids, or all_checked_in: true (one of them).",
                           details={"fields": {"registration_ids": "One of the two.", "all_checked_in": "One of the two."}})
    regs = [r for r in await _registrations_of(caller, event_row.id) if r.data.get("status") == "confirmed"]
    if body.registration_ids:
        wanted = set(body.registration_ids)
        known = {r.id for r in regs}
        missing = sorted(wanted - known)
        if missing:
            raise PartnerError("NOT_FOUND", "Some of those registrations are not on this event.", details={"missing": missing[:50]})
        regs = [r for r in regs if r.id in wanted]
    else:
        regs = [r for r in regs if r.data.get("checked_in_at")]
    have = {
        r.data.get("registration_id") for r in await session.scalars(select(S).where(S.club_id == caller.club_id, S.kind == "certificate", S.parent_id == event_row.id))
    }
    issued, created = 0, []
    for reg in regs:
        if reg.id in have:
            continue
        number = _code("TEST-CERT", 2)
        made = await _new(caller, "certificate", {
            "id": new_id("certificate"), "number": number, "object": "certificate", "registration_id": reg.id, "event_id": event_row.id,
            "status": "issued", "issued_at": iso(now()), "revoked_at": None, "name": (reg.data.get("person") or {}).get("name"),
            "verify_url": None, "pdf_url": None, "livemode": False,
        }, parent_id=event_row.id)
        created.append(_strip(made.data))
        issued += 1
    job = PartnerCertificateJobs(
        club_id=caller.club_id, event_ref=event_row.id, client_id=caller.client_id, livemode=False, status="completed",
        request={"registration_ids": body.registration_ids, "all_checked_in": bool(body.all_checked_in), "template_id": body.template_id, "send": bool(body.send)},
        result={"issued": issued, "refreshed": 0, "already_issued": len(regs) - issued, "emails_queued": None}, finished_at=now(),
    )
    session.add(job)
    await session.commit()
    for cert in created:
        await emit(caller, "certificate.issued", cert)
    from app.api.partner.live import job_out

    return JSONResponse(job_out(job), status_code=202)


async def get_certificate_job(caller, job_id):
    from app.api.partner.live import job_out

    caller.require_club()
    try:
        pk = uuid.UUID(str(job_id))
    except ValueError:
        pk = None
    job = await caller.session.get(PartnerCertificateJobs, pk) if pk else None
    if job is None or job.club_id != caller.club_id or job.livemode:
        raise not_found("Certificate job")
    return JSONResponse(job_out(job))


async def get_certificate(caller, number: str):
    S = PartnerSandboxObjects
    caller.require_club()
    row = await caller.session.scalar(select(S).where(S.club_id == caller.club_id, S.kind == "certificate", S.data["number"].astext == number))
    if row is None:
        raise not_found("Certificate")
    data = row.data
    event = await caller.session.get(S, row.parent_id)
    out = {
        "number": data["number"], "object": "certificate_verification", "valid": data.get("status") == "issued",
        "reason": "ok" if data.get("status") == "issued" else "revoked", "registration_id": data["registration_id"],
        "event_id": row.parent_id, "event_name": (event.data.get("name") if event else None), "issued_at": data.get("issued_at"),
        "revoked_at": data.get("revoked_at"), "verify_url": None, "pdf_url": None, "livemode": False,
    }
    if caller.contact:
        out["name"] = data.get("name")
    return JSONResponse(out)


async def create_club(caller, body):
    """clubs:create in test mode: checked as live, nothing made."""
    if caller.app is None:
        raise PartnerError("FORBIDDEN_SCOPE", "Only a partner app can create clubs.")
    ext_ids.check(body.external_id)
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:60]
    return JSONResponse({
        "id": f"test_club_{uuid.uuid4().hex[:20]}", "object": "club", "name": body.name, "slug": f"{slug}-test", "status": "pending_approval",
        "tagline": body.tagline, "about": body.about, "links": (body.links.model_dump() if body.links else {}), "logo_url": None,
        "public_url": None, "external_id": body.external_id, "livemode": False, "updated_at": iso(now()),
        "connection": None,
    }, status_code=201)

