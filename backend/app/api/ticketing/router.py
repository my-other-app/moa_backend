"""The ticketing routes: the club's (ticket types, discount codes, waiting
lists, per-type sales) and the public ones (an event's tickets, checking a
code, joining a waiting list, claiming an offer). docs/TICKETING.md is the
contract."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.events.models import Events
from app.api.ticketing import codes as codes_mod
from app.api.ticketing import types as types_mod
from app.api.ticketing import views, waitlist
from app.api.ticketing.models import OFFERED, REMOVED, WaitlistEntries
from app.api.ticketing.schemas import (
    CheckCodeIn,
    CheckCodeOut,
    ClaimIn,
    DiscountCodeIn,
    DiscountCodeOut,
    DiscountCodePatch,
    OfferOut,
    PublicTicketing,
    SalesOut,
    TicketTypeIn,
    TicketTypeOut,
    TicketTypePatch,
    TicketTypesOrder,
    TicketTypesOut,
    TicketTypesSave,
    WaitlistClubOut,
    WaitlistEntryClub,
    WaitlistEntryPublic,
    WaitlistJoinIn,
    WaitlistJoinOut,
    WaitlistSettingsIn,
    WaitlistSettingsOut,
)
from app.core.auth.dependencies import ClubAuth, OptionalUserAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(tags=["ticketing"])
public_router = APIRouter(tags=["ticketing"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fields(model) -> dict:
    """A body's fields as their values (model_dump would turn the datetimes into text)."""
    return {name: getattr(model, name) for name in type(model).model_fields}


async def _club_for(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


# ─── Ticket types (the club's) ──────────────────────────────────────────


@router.get("/events/{event_id}/ticketing/types", summary="The event's ticket types, with what sold")
async def list_types(session: SessionDep, user: ClubAuth, event_id: int) -> TicketTypesOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return await views.club_types(session, event)


@router.put("/events/{event_id}/ticketing/types", summary="Save the event's ticket types as a list")
async def save_types(session: SessionDep, user: ClubAuth, event_id: int, body: TicketTypesSave) -> TicketTypesOut:
    """The event form's editor: a type with an `id` is changed, one without
    is made, one left out is removed (409 TICKET_TYPE_HAS_REGISTRATIONS when
    people hold it: hide it instead). `event_capacity`, when sent, is the
    event's total across its types."""
    event = await deliveries.event_for_club(session, user, event_id)
    items = [_fields(i) for i in body.types]
    capacity = body.event_capacity if "event_capacity" in body.model_fields_set else "unchanged"
    await types_mod.save_all(session, event, items, event_capacity=capacity, actor=user)
    event = await deliveries.event_for_club(session, user, event_id)
    return await views.club_types(session, event)


@router.post("/events/{event_id}/ticketing/types", summary="Add a ticket type", status_code=201)
async def create_type(session: SessionDep, user: ClubAuth, event_id: int, body: TicketTypeIn) -> TicketTypeOut:
    event = await deliveries.event_for_club(session, user, event_id)
    row = await types_mod.create(session, event, _fields(body), actor=user)
    return await _type_out(session, user, event_id, row.id)


@router.patch("/events/{event_id}/ticketing/types/{type_id}", summary="Change a ticket type")
async def update_type(session: SessionDep, user: ClubAuth, event_id: int, type_id: int, body: TicketTypePatch) -> TicketTypeOut:
    event = await deliveries.event_for_club(session, user, event_id)
    ttype = await types_mod.get_type(session, event.id, type_id)
    data = {k: getattr(body, k) for k in body.model_fields_set}
    await types_mod.update_type(session, event, ttype, data, actor=user)
    return await _type_out(session, user, event_id, type_id)


@router.delete("/events/{event_id}/ticketing/types/{type_id}", summary="Remove a ticket type nobody holds")
async def delete_type(session: SessionDep, user: ClubAuth, event_id: int, type_id: int) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    ttype = await types_mod.get_type(session, event.id, type_id)
    await types_mod.delete_type(session, event, ttype)
    return {"deleted": True}


@router.put("/events/{event_id}/ticketing/types/order", summary="Put the ticket types in order")
async def order_types(session: SessionDep, user: ClubAuth, event_id: int, body: TicketTypesOrder) -> TicketTypesOut:
    event = await deliveries.event_for_club(session, user, event_id)
    await types_mod.reorder(session, event, body.ids)
    event = await deliveries.event_for_club(session, user, event_id)
    return await views.club_types(session, event)


async def _type_out(session, user, event_id: int, type_id: int) -> TicketTypeOut:
    event = await deliveries.event_for_club(session, user, event_id)
    listing = await views.club_types(session, event)
    found = next((t for t in listing["types"] if t["id"] == type_id), None)
    if found is None:
        raise CustomHTTPException(404, message="Ticket type not found", error_code="TICKET_TYPE_NOT_FOUND")
    return found


@router.get("/events/{event_id}/ticketing/sales", summary="Sales by ticket type, and the codes used")
async def sales(session: SessionDep, user: ClubAuth, event_id: int) -> SalesOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return await views.sales(session, event)


# ─── Discount codes (the club's) ────────────────────────────────────────


def _code_status(row, uses: int, now: datetime) -> str:
    if not row.is_active:
        return "inactive"
    if row.ends_at is not None and now >= row.ends_at:
        return "expired"
    if row.max_uses is not None and uses >= row.max_uses:
        return "used_up"
    if row.starts_at is not None and now < row.starts_at:
        return "scheduled"
    return "active"


async def _codes_out(session, rows) -> list[dict]:
    now = _now()
    usage = await codes_mod.usage(session, [r.id for r in rows])
    event_ids = sorted({r.event_id for r in rows if r.event_id is not None})
    names = {}
    if event_ids:
        names = dict((await session.execute(select(Events.id, Events.name).where(Events.id.in_(event_ids)))).all())
    out = []
    for r in rows:
        u = usage.get(r.id, {"uses": 0, "discount_total": 0.0})
        out.append({
            "id": r.id, "code": r.code, "kind": r.kind, "value": float(r.value), "event_id": r.event_id,
            "event_name": names.get(r.event_id), "ticket_type_ids": r.ticket_type_ids, "starts_at": r.starts_at,
            "ends_at": r.ends_at, "max_uses": r.max_uses, "per_person_limit": r.per_person_limit,
            "is_active": bool(r.is_active), "uses": u["uses"], "discount_total": u["discount_total"],
            "status": _code_status(r, u["uses"], now), "created_at": r.created_at,
        })
    return out


@router.get("/clubs/discount-codes", summary="The club's discount codes, with their uses")
async def list_codes(
    session: SessionDep, user: ClubAuth,
    event_id: int | None = Query(None, description="Only those this event takes: its own and the club-wide ones"),
    scope: str = Query("all", pattern="^(all|club)$", description="club: the club-wide codes only"),
) -> list[DiscountCodeOut]:
    club = await _club_for(session, user)
    if event_id is not None:
        await deliveries.event_for_club(session, user, event_id)
    return await _codes_out(session, await codes_mod.for_club(session, club.id, event_id=event_id, scope=scope))


@router.get("/events/{event_id}/ticketing/codes", summary="The codes this event takes, with their uses")
async def event_codes(session: SessionDep, user: ClubAuth, event_id: int) -> list[DiscountCodeOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return await _codes_out(session, await codes_mod.for_club(session, event.club_id, event_id=event.id))


@router.post("/clubs/discount-codes", summary="Make a discount code", status_code=201)
async def create_code(session: SessionDep, user: ClubAuth, body: DiscountCodeIn) -> DiscountCodeOut:
    club = await _club_for(session, user)
    row = await codes_mod.create(session, club, _fields(body), actor=user)
    return (await _codes_out(session, [row]))[0]


@router.patch("/clubs/discount-codes/{code_id}", summary="Change a discount code (its text never changes)")
async def update_code(session: SessionDep, user: ClubAuth, code_id: int, body: DiscountCodePatch) -> DiscountCodeOut:
    club = await _club_for(session, user)
    row = await codes_mod.get(session, club.id, code_id)
    data = {k: getattr(body, k) for k in body.model_fields_set}
    row = await codes_mod.update_code(session, club, row, data, actor=user)
    return (await _codes_out(session, [row]))[0]


@router.delete("/clubs/discount-codes/{code_id}", summary="Delete a discount code")
async def delete_code(session: SessionDep, user: ClubAuth, code_id: int) -> dict:
    club = await _club_for(session, user)
    await codes_mod.delete_code(session, await codes_mod.get(session, club.id, code_id))
    return {"deleted": True}


# ─── Waiting lists (the club's) ─────────────────────────────────────────


async def _entry_club(session, entry, names: dict, positions: dict) -> dict:
    return {
        "id": entry.id, "ticket_type_id": entry.ticket_type_id, "ticket_type_name": names.get(entry.ticket_type_id),
        "full_name": entry.full_name, "email": entry.email, "phone": entry.phone, "user_id": entry.user_id,
        "whatsapp_opt_in": bool(entry.whatsapp_opt_in), "status": entry.status, "position": positions.get(entry.id),
        "offered_at": entry.offered_at, "offer_expires_at": entry.offer_expires_at, "offers_made": entry.offers_made,
        "registration_id": entry.registration_id, "created_at": entry.created_at, "closed_at": entry.closed_at,
        "offer_url": waitlist.offer_url(entry.offer_token) if entry.status == OFFERED and entry.offer_token else None,
    }


async def _settings_out(session, event) -> dict:
    from app.api.subscriptions import gates

    row = await waitlist.settings_for(session, event.id)
    return {
        "enabled": bool(row.enabled) if row is not None else False,
        "offer_hours": row.offer_hours if row is not None else 24,
        "allowed": await gates.allows(session, event.club_id, waitlist.FEATURE, event=event),
    }


@router.get("/events/{event_id}/ticketing/waitlist", summary="The event's waiting list")
async def club_waitlist(
    session: SessionDep, user: ClubAuth, event_id: int,
    status: str | None = Query(None, pattern="^(waiting|offered|claimed|expired|removed|left)$"),
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
) -> WaitlistClubOut:
    event = await deliveries.event_for_club(session, user, event_id)
    rows, total = await waitlist.entries(session, event.id, status=status, limit=limit, offset=offset)
    names = await types_mod.names(session, [r.ticket_type_id for r in rows])
    positions = {r.id: await waitlist.position(session, r) for r in rows if r.status == "waiting"}
    return {
        "settings": await _settings_out(session, event),
        "counts": await waitlist.counts(session, event.id),
        "entries": [await _entry_club(session, r, names, positions) for r in rows],
        "total": total,
    }


@router.put("/events/{event_id}/ticketing/waitlist/settings", summary="Turn the waiting list on or off; how long an offer holds")
async def waitlist_settings(session: SessionDep, user: ClubAuth, event_id: int, body: WaitlistSettingsIn) -> WaitlistSettingsOut:
    event = await deliveries.event_for_club(session, user, event_id)
    await waitlist.set_settings(session, event, enabled_=body.enabled, offer_hours=body.offer_hours, actor=user)
    await waitlist.fill_soon(session, event.id)
    return await _settings_out(session, event)


async def _club_entry(session, event, entry_id) -> WaitlistEntries:
    entry = await session.scalar(
        select(WaitlistEntries).where(
            WaitlistEntries.id == entry_id, WaitlistEntries.event_id == event.id,
            # A confirmed request's hold is the request's (app/api/approvals), not the line's.
            WaitlistEntries.via_request == False,  # noqa: E712
        )
    )
    if entry is None:
        raise CustomHTTPException(404, message="Not on this event's waiting list", error_code="WAITLIST_ENTRY_NOT_FOUND")
    return entry


@router.post("/events/{event_id}/ticketing/waitlist/{entry_id}/offer", summary="Offer this person a place now")
async def offer_entry(session: SessionDep, user: ClubAuth, event_id: int, entry_id: str) -> WaitlistEntryClub:
    """Ahead of the line, or again after an offer ran out. Only a free place:
    409 WAITLIST_NO_ROOM when there is none (raise a capacity first)."""
    event = await deliveries.event_for_club(session, user, event_id)
    entry = await waitlist.offer_now(session, event, await _club_entry(session, event, _uuid(entry_id)))
    names = await types_mod.names(session, [entry.ticket_type_id])
    return await _entry_club(session, entry, names, {})


@router.delete("/events/{event_id}/ticketing/waitlist/{entry_id}", summary="Take someone off the waiting list")
async def remove_entry(session: SessionDep, user: ClubAuth, event_id: int, entry_id: str) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    entry = await _club_entry(session, event, _uuid(entry_id))
    if entry.status in ("waiting", "offered"):
        await waitlist.close(session, entry, REMOVED)
    return {"removed": True}


@router.get("/events/{event_id}/ticketing/waitlist/export", summary="The waiting list as a CSV file")
async def export_waitlist(session: SessionDep, user: ClubAuth, event_id: int) -> Response:
    event = await deliveries.event_for_club(session, user, event_id)
    body = await waitlist.export_csv(session, event)
    return Response(
        content=body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="event_{event.id}_waitlist.csv"'},
    )


def _uuid(value: str):
    import uuid

    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise CustomHTTPException(404, message="Not on this event's waiting list", error_code="WAITLIST_ENTRY_NOT_FOUND") from None


# ─── The event page (public) ────────────────────────────────────────────


async def _public_event(session, event_id: str) -> Events:
    from app.api.events.registration.service import resolve_event

    return await resolve_event(session, event_id)


@public_router.get("/events/{event_id}/ticket-types", summary="An event's tickets: types on sale, what is left, the waiting list")
async def public_types(
    session: SessionDep, event_id: str, user: OptionalUserAuth,
    ticket_type: int | None = Query(None, description="A hidden type a link names, shown too"),
) -> PublicTicketing:
    event = await _public_event(session, event_id)
    out = await views.public(session, event, include_hidden=ticket_type, viewer=user)
    await session.commit()
    return out


@public_router.post("/events/{event_id}/discount-codes/check", summary="What a discount code does to the price")
@limiter.limit("10/minute")
async def check_code(request: Request, session: SessionDep, event_id: str, body: CheckCodeIn) -> CheckCodeOut:
    """Only the price: never the code's limits, uses or other events. 422
    DISCOUNT_CODE_INVALID for any code this event does not take."""
    from app.api.ticketing import quote as quote_mod

    event = await _public_event(session, event_id)
    q = await quote_mod.quote(
        session, event, ticket_type_id=body.ticket_type_id, discount_code=body.code, email=body.email, phone=body.phone,
        lock=False, early_capacity=False,
    )
    await session.commit()
    if q.code is None:
        raise codes_mod.invalid()
    return {
        "code": q.code.code, "ticket_type_id": q.ticket_type.id, "price": float(q.price),
        "discount": float(q.discount), "amount": float(q.amount), "free": q.amount == 0,
    }


@public_router.post("/events/{event_id}/waitlist", summary="Join the waiting list for a sold-out ticket type")
@limiter.limit("10/minute")
async def join_waitlist(request: Request, session: SessionDep, event_id: str, body: WaitlistJoinIn, user: OptionalUserAuth) -> WaitlistJoinOut:
    event = await _public_event(session, event_id)
    entry, created = await waitlist.join(
        session, event, ticket_type_id=body.ticket_type_id, full_name=body.full_name, email=body.email,
        phone=body.phone, user=user, whatsapp_opt_in=body.whatsapp_opt_in,
    )
    return {**await views.entry_public(session, entry), "created": created}


@public_router.get("/events/{event_id}/waitlist/me", summary="The signed-in person's places in line")
async def my_waitlist(session: SessionDep, event_id: str, user: OptionalUserAuth) -> list[WaitlistEntryPublic]:
    if user is None:
        return []
    event = await _public_event(session, event_id)
    return [await views.entry_public(session, e) for e in await waitlist.mine(session, event.id, user)]


@public_router.delete("/events/{event_id}/waitlist/me", summary="Leave the waiting list")
async def leave_waitlist(session: SessionDep, event_id: str, user: OptionalUserAuth) -> dict:
    if user is None:
        raise CustomHTTPException(401, message="Sign in to leave the waiting list.")
    event = await _public_event(session, event_id)
    left = 0
    for entry in await waitlist.mine(session, event.id, user):
        await waitlist.close(session, entry, "left")
        left += 1
    return {"left": left}


# ─── The claim link (public, the token is the credential) ───────────────


def _mask_email(email):
    if not email or "@" not in email:
        return None
    name, _, domain = email.partition("@")
    return (name[:2] + "•••" if len(name) > 2 else name[:1] + "•••") + "@" + domain


def _mask_phone(phone):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"•••••{digits[-4:]}" if len(digits) >= 4 else None


@public_router.get("/waitlist/offers/{token}", summary="A waiting-list offer, as its claim page shows it")
async def get_offer(session: SessionDep, token: str) -> OfferOut:
    from sqlalchemy.orm import joinedload

    entry = await waitlist.by_token(session, token)
    event = await session.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == entry.event_id))
    ttype = await types_mod.get_type(session, event.id, entry.ticket_type_id) if event is not None else None
    if event is None or ttype is None:
        raise CustomHTTPException(404, message="This link is not valid.", error_code="OFFER_NOT_FOUND")
    types = await types_mod.live(session, event.id)
    price = types_mod.price_of(event, ttype, types)
    status = entry.status
    if status == OFFERED and entry.offer_expires_at is not None and entry.offer_expires_at <= _now():
        status = "expired"
    # A club's confirmation of a registration request (app/api/approvals):
    # "{club} confirmed your place", at the price it was confirmed at.
    request = None
    if entry.via_request:
        from app.api.approvals import service as approvals

        request = await approvals.for_entry(session, entry.id)
    return {
        "id": entry.id, "status": status,
        "event": {
            "id": event.id, "slug": event.slug, "name": event.name, "event_datetime": event.event_datetime,
            "location_name": event.location_name, "poster": event.poster if isinstance(event.poster, dict) else None,
            "club": {"name": event.club.name, "slug": event.club.slug} if event.club is not None else None,
        },
        "ticket_type": {"id": ttype.id, "name": ttype.name, "price": float(price), "currency": "INR"},
        "full_name": entry.full_name, "email": _mask_email(entry.email), "phone": _mask_phone(entry.phone),
        "expires_at": entry.offer_expires_at, "needs_email": not entry.email,
        "needs_payment": (request.amount > 0) if request is not None and request.amount is not None else price > 0,
        "registration_id": entry.registration_id,
        "reason": "approved" if entry.via_request else "waitlist",
        "amount": float(request.amount) if request is not None and request.amount is not None else None,
    }


@public_router.post("/waitlist/offers/{token}/decline", summary="Give the place to the next person")
async def decline_offer(session: SessionDep, token: str) -> dict:
    entry = await waitlist.by_token(session, token)
    await waitlist.decline(session, entry)
    return {"declined": True}


@public_router.post("/waitlist/offers/{token}/claim", summary="Claim an offered place: register (then pay, for a paid type)")
@limiter.limit("10/minute")
async def claim_offer(
    request: Request, background_tasks: BackgroundTasks, session: SessionDep, token: str, body: ClaimIn, user: OptionalUserAuth,
):
    """Registers the person the place was offered to, as the public form
    does (a guest of their own, with a session for it, unless signed in with
    the offer's address), on the offer's ticket type. The answer is the
    registration's (as POST /events/registration/{event_id}/register's): with
    `pay_amount` above 0 the payment follows (POST /payments/orders), and the
    place is theirs when it lands, if the offer has not run out by then."""
    from app.api.auth import service as auth_service
    from app.api.events.registration import service as registration_service
    from app.api.users import service as user_service
    from app.api.users.models import UserTypes

    entry = await waitlist.by_token(session, token)
    waitlist.check_claimable(entry)
    event = await session.get(Events, entry.event_id)
    if event is None or event.is_deleted:
        raise CustomHTTPException(404, message="Event not found")
    # A club's confirmation of a registration request (app/api/approvals):
    # the claim registers what the request said -- its name, number,
    # answers, code and WhatsApp tick -- whatever the page sends.
    request = None
    if entry.via_request:
        from app.api.approvals import service as approvals

        request = await approvals.for_entry(session, entry.id)
    email = (entry.email or (body.email or "")).strip()
    if not email:
        raise CustomHTTPException(
            422, message="Enter your email address for your ticket.", error_code="EMAIL_REQUIRED",
            errors={"email": "Your ticket is sent here."},
        )
    full_name = ((request.full_name if request is not None else None) or body.full_name or entry.full_name or "").strip()
    if len(full_name) < 3:
        raise CustomHTTPException(422, message="Enter your full name.", errors={"full_name": "At least 3 letters."})
    phone = (request.phone if request is not None else None) or body.phone or entry.phone
    if user is not None and (user.user_type == UserTypes.guest or not user_service.emails_match(user.email, email)):
        # Signed in as someone else: the place is the offer's address's.
        user = None
    if user is None and await user_service.email_has_club_account(session, email):
        raise CustomHTTPException(
            400, "This email belongs to a club's account. Register with your own email.", error_code="CLUB_EMAIL",
        )
    await registration_service.hold_address(session, event.id, email)
    # Looked at again under the address's lock: a second tap finds the first's claim.
    entry = await session.scalar(
        select(WaitlistEntries).where(WaitlistEntries.id == entry.id).execution_options(populate_existing=True)
    )
    waitlist.check_claimable(entry)
    existing = await registration_service.live_registration_for_email(session, event.id, email)
    if existing is not None:
        if existing.id != entry.registration_id:
            raise CustomHTTPException(409, message=registration_service.ALREADY_REGISTERED, error_code="ALREADY_REGISTERED")
        # Their own claim, waiting for its payment: the same registration to pay.
        claimant_id = existing.user_id
    elif user is not None:
        claimant_id = user.id
    elif request is not None and await _request_guest(session, request) is not None:
        # The guest the request made: the claim link is its holder's.
        claimant_id = request.user_id
    else:
        guest = await user_service.new_guest(session, full_name=full_name, email=email, phone=phone)
        claimant_id = guest.id
    if request is not None:
        additional = dict(request.answers or {})
        discount_code, whatsapp_opt_in = request.discount_code, request.whatsapp_opt_in or None
    else:
        additional = dict(body.additional_details or {})
        if body.organization:
            additional["organization"] = body.organization
        discount_code, whatsapp_opt_in = body.discount_code, body.whatsapp_opt_in or None
    data = await registration_service.register_event(
        session=session, background_tasks=background_tasks, full_name=full_name, email=email, phone=phone,
        user_id=claimant_id, event_id=event.id, additional_details=additional or None,
        discount_code=discount_code, offer=entry, whatsapp_opt_in=whatsapp_opt_in,
        # The request's answers were checked when it was made: the form may have changed since.
        relaxed_fields=request is not None,
    )
    if user is None:
        # The claim link reached them, so the guest the claim made is theirs:
        # a session for it, to pay with. Never for an app account.
        from app.api.users.models import Users

        claimant = await session.get(Users, claimant_id)
        if claimant is not None and claimant.user_type == UserTypes.guest:
            data["auth_token"] = await auth_service.create_access_refresh_tokens(claimant)
    from app.api.events.registration.schemas import EventRegistrationResponse

    return EventRegistrationResponse(**data)


async def _request_guest(session, request):
    """The guest a registration request was made by, when it is one (and still live)."""
    from app.api.users.models import UserTypes, Users

    if request.user_id is None:
        return None
    return await session.scalar(
        select(Users).where(Users.id == request.user_id, Users.user_type == UserTypes.guest, Users.is_deleted == False)  # noqa: E712
    )
