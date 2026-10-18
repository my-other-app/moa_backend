"""The public registration form, signed out.

It used to look the typed address up and, when a guest already had it,
register under that guest and return a full session for it. Anyone who
typed an address got that account: its tickets (whose QR codes admit people
to events), its payments and its profile. Now a signed-out sign-up always
gets a fresh guest of its own, and the session reaches nothing else.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.api.deliveries.models import Deliveries, DeliveryKind
from app.api.events.models import EventRegistrationsLink
from app.api.users import service as user_service
from app.api.users.models import UserTypes, Users
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


def _register(client, event_id, email, *, headers=None, name="Asha Menon"):
    return client.post(
        f"/api/v1/events/registration/{event_id}/register",
        json={"full_name": name, "email": email, "phone": "9876500000"},
        headers=headers or {},
    )


async def _registration(session, registration_id) -> EventRegistrationsLink:
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == registration_id)
    )


async def _accounts_with(session, email) -> list[Users]:
    return list(
        await session.scalars(
            select(Users).where(func.lower(Users.email) == email.lower()).order_by(Users.id)
        )
    )


async def _world(session, make_club, make_event, **event_overrides):
    owner, club = await make_club(name="Open Source Club", email="osc-public@example.com")
    first = await make_event(club=club, created_by=owner, name="Rust Meetup", event_datetime=NOW + timedelta(days=5), **event_overrides)
    second = await make_event(club=club, created_by=owner, name="Go Meetup", event_datetime=NOW + timedelta(days=9), **event_overrides)
    await session.commit()
    return owner, club, first, second


async def test_an_existing_guests_address_gets_a_new_guest_that_reaches_nothing_else(
    client, session, make_club, make_event, make_user, make_registration
):
    _, _, first, second = await _world(session, make_club, make_event)
    old_guest = await make_user(email="Asha@Example.com", user_type=UserTypes.guest)
    old_ticket = await make_registration(event=first, user=old_guest)
    await session.commit()

    r = await _register(client, second.id, "asha@example.com")
    assert r.status_code == 200, r.text
    body = r.json()
    token = body["auth_token"]["access_token"]

    mine = await _registration(session, body["event_registration_id"])
    assert mine.user_id != old_guest.id
    new_guest = await session.get(Users, mine.user_id)
    assert new_guest.user_type == UserTypes.guest and new_guest.email == "asha@example.com"

    # The session opens the ticket it was issued for, and not the old guest's.
    own = await client.get(f"/api/v1/events/tickets/{mine.ticket_id}", headers=auth_header(token))
    assert own.status_code == 200, own.text
    theirs = await client.get(f"/api/v1/events/tickets/{old_ticket.ticket_id}", headers=auth_header(token))
    assert theirs.status_code == 403
    listed = await client.get("/api/v1/events/list?is_registered=true", headers=auth_header(token))
    assert listed.status_code == 200, listed.text
    assert [e["id"] for e in listed.json()["items"]] == [second.id]

    # The old guest is untouched, and still the only holder of its registration.
    await session.refresh(old_ticket)
    assert old_ticket.user_id == old_guest.id and old_ticket.is_deleted is False


async def test_an_address_already_on_the_list_is_told_so_and_its_ticket_goes_out_again(
    client, session, make_club, make_event, make_user, make_registration
):
    _, _, first, _ = await _world(session, make_club, make_event)
    holder = await make_user(email="dup@example.com", user_type=UserTypes.guest)
    existing = await make_registration(event=first, user=holder)
    await session.commit()

    r = await _register(client, first.id, "DUP@Example.com")
    assert r.status_code == 409, r.text
    assert r.json()["error_code"] == "ALREADY_REGISTERED"
    assert "already registered for this event" in r.json()["message"]
    assert not r.json().get("auth_token")
    assert len(await _accounts_with(session, "dup@example.com")) == 1

    queued = await session.scalar(
        select(Deliveries).where(Deliveries.registration_id == existing.id, Deliveries.kind == DeliveryKind.ticket)
    )
    assert queued is not None


async def test_an_account_holding_the_address_counts_even_when_the_registration_has_another(
    client, session, make_club, make_event, make_user, make_registration
):
    _, _, first, _ = await _world(session, make_club, make_event)
    holder = await make_user(email="holder@example.com")
    await make_registration(event=first, user=holder, email="typed-something-else@example.com")
    await session.commit()

    r = await _register(client, first.id, "holder@example.com")
    assert r.status_code == 409, r.text


async def test_an_unpaid_registration_on_a_paid_event_is_told_it_waits_for_payment(
    client, session, make_club, make_event, make_user, make_registration
):
    _, _, first, _ = await _world(session, make_club, make_event, has_fee=True, reg_fee=200)
    guest = await make_user(email="unpaid@example.com", user_type=UserTypes.guest)
    await make_registration(event=first, user=guest, actual_amount=200)
    await session.commit()

    r = await _register(client, first.id, "unpaid@example.com")
    assert r.status_code == 409
    assert r.json()["error_code"] == "REGISTRATION_AWAITING_PAYMENT"


async def _walked_away(session, make_user, make_registration, event, email, *, user_type=UserTypes.guest):
    """An unpaid registration made an hour ago, with a receipt and no order since."""
    holder = await make_user(email=email, user_type=user_type)
    reg = await make_registration(event=event, user=holder, actual_amount=200)
    reg.payment_receipt = f"er_{reg.ticket_id}"
    reg.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    return reg


def _order(reg, *, status, age):
    from app.api.payments.models import PaymentOrders

    order = PaymentOrders(
        receipt=reg.payment_receipt, razorpay_order_id=f"order_{reg.ticket_id}", amount=200,
        status=status, user_id=reg.user_id, source="event_registration", payload={},
    )
    order.created_at = datetime.now(timezone.utc) - age  # ages count from now, not from when the module was collected
    return order


async def test_a_checkout_walked_away_from_lets_the_address_start_again(
    client, session, make_club, make_event, make_user, make_registration
):
    from app.api.payments.models import OrderStatus

    _, _, first, _ = await _world(session, make_club, make_event, has_fee=True, reg_fee=200)
    old = await _walked_away(session, make_user, make_registration, first, "walked@example.com")
    session.add(_order(old, status=OrderStatus.created, age=timedelta(minutes=50)))
    await session.commit()

    r = await _register(client, first.id, "walked@example.com")
    assert r.status_code == 200, r.text
    fresh = await _registration(session, r.json()["event_registration_id"])
    assert fresh.id != old.id and fresh.user_id != old.user_id

    # The abandoned one is retired, out of the organiser's list.
    retired = (await (await session.connection()).execute(
        select(EventRegistrationsLink.__table__.c.is_deleted).where(EventRegistrationsLink.__table__.c.id == old.id)
    )).scalar_one()
    assert retired is True


async def test_a_payment_that_may_still_land_or_has_landed_keeps_the_registration(
    client, session, make_club, make_event, make_user, make_registration
):
    from app.api.payments.models import OrderStatus

    _, _, first, _ = await _world(session, make_club, make_event, has_fee=True, reg_fee=200)
    recent = await _walked_away(session, make_user, make_registration, first, "recent@example.com")
    session.add(_order(recent, status=OrderStatus.attempted, age=timedelta(minutes=10)))
    paid = await _walked_away(session, make_user, make_registration, first, "paid-late@example.com")
    # Paid at Razorpay, but the registration was never marked: money moved, so it stays.
    session.add(_order(paid, status=OrderStatus.paid, age=timedelta(hours=1)))
    await session.commit()

    for email in ("recent@example.com", "paid-late@example.com"):
        r = await _register(client, first.id, email)
        assert r.status_code == 409, (email, r.text)
        assert r.json()["error_code"] == "REGISTRATION_AWAITING_PAYMENT"


async def test_an_app_users_unpaid_registration_is_never_retired_from_the_public_form(
    client, session, make_club, make_event, make_user, make_registration
):
    _, _, first, _ = await _world(session, make_club, make_event, has_fee=True, reg_fee=200)
    reg = await _walked_away(session, make_user, make_registration, first, "app-unpaid@example.com", user_type=UserTypes.app_user)

    r = await _register(client, first.id, "app-unpaid@example.com")
    assert r.status_code == 409, r.text
    await session.refresh(reg)
    assert reg.is_deleted is False


async def test_optional_questions_left_blank_do_not_block_and_a_missing_required_one_is_named(
    client, session, make_club, make_event
):
    optional = [{"key": "tshirt", "label": "T-shirt size", "field_type": "text", "required": False}]
    required = [{"key": "college", "label": "College", "field_type": "text", "required": True}]
    owner, club = await make_club(name="Questions Club", email="questions@example.com")
    loose = await make_event(club=club, created_by=owner, name="Loose", event_datetime=NOW + timedelta(days=3), additional_details=optional)
    strict = await make_event(club=club, created_by=owner, name="Strict", event_datetime=NOW + timedelta(days=3), additional_details=required)
    await session.commit()

    assert (await _register(client, loose.id, "loose@example.com")).status_code == 200

    r = await _register(client, strict.id, "strict@example.com")
    assert r.status_code == 400, r.text
    # A sentence in `message`, the field by key in `errors` (where the public site reads it).
    assert r.json()["message"] == "Check your answers: College." and r.json()["error_code"] == "INVALID_ANSWERS"
    assert r.json()["errors"] == {"college": "This field is required"}


async def test_an_app_users_address_registers_under_a_fresh_guest_and_their_password_still_works(
    client, session, make_club, make_event, make_user
):
    _, _, first, _ = await _world(session, make_club, make_event)
    app_user = await make_user(email="appy@example.com", username="appy")
    await session.commit()

    r = await _register(client, first.id, "appy@example.com")
    assert r.status_code == 200, r.text
    mine = await _registration(session, r.json()["event_registration_id"])
    assert mine.user_id != app_user.id
    assert (await session.get(Users, mine.user_id)).user_type == UserTypes.guest

    # A guest now shares the address; signing in by it must still find the
    # account with the password, not the guest in front of it.
    signed_in = await client.post(
        "/api/v1/auth/token", data={"username": "appy@example.com", "password": "Password123!"}
    )
    assert signed_in.status_code == 200, signed_in.text


async def test_a_clubs_address_is_refused(client, session, make_club, make_event):
    _, _, first, _ = await _world(session, make_club, make_event)
    await make_club(name="Robotics Society", email="robotics@example.com")
    await session.commit()

    r = await _register(client, first.id, "Robotics@Example.com ")
    assert r.status_code == 400
    assert r.json()["error_code"] == "CLUB_EMAIL"
    assert [u.user_type for u in await _accounts_with(session, "robotics@example.com")] == [UserTypes.club]


async def test_a_signed_in_app_user_registers_as_themselves(
    client, session, make_club, make_event, make_user, mint_token
):
    _, _, first, _ = await _world(session, make_club, make_event)
    app_user = await make_user(email="member@example.com", username="member")
    await session.commit()

    r = await _register(client, first.id, "Member@Example.com", headers=auth_header(await mint_token(app_user)))
    assert r.status_code == 200, r.text
    assert r.json()["auth_token"] is None
    mine = await _registration(session, r.json()["event_registration_id"])
    assert mine.user_id == app_user.id and mine.email == "member@example.com"


async def test_a_guest_session_used_for_someone_else_signs_them_up_on_their_own(
    client, session, make_club, make_event
):
    _, _, first, second = await _world(session, make_club, make_event)
    r1 = await _register(client, first.id, "first@example.com")
    first_token = r1.json()["auth_token"]["access_token"]
    first_guest = (await _registration(session, r1.json()["event_registration_id"])).user_id

    # The same browser, a friend's address: the friend's ticket must not land
    # on the first person's account.
    r2 = await _register(client, second.id, "friend@example.com", headers=auth_header(first_token), name="Friend Two")
    assert r2.status_code == 200, r2.text
    assert r2.json()["auth_token"]
    assert (await _registration(session, r2.json()["event_registration_id"])).user_id != first_guest

    # With its own address it is still the same guest, as before.
    r3 = await _register(client, second.id, "first@example.com", headers=auth_header(first_token))
    assert r3.status_code == 200, r3.text
    assert r3.json()["auth_token"] is None
    assert (await _registration(session, r3.json()["event_registration_id"])).user_id == first_guest


async def test_staff_paths_find_an_account_ignoring_case_and_prefer_the_app_account(session, make_user):
    guest = await make_user(email="Door@Example.com", user_type=UserTypes.guest, username="door-guest")
    found = await user_service.get_non_club_user_by_email(session, "  door@example.COM ")
    assert found.id == guest.id

    app_user = await make_user(email="door@example.com", username="door-app")
    found = await user_service.get_non_club_user_by_email(session, "DOOR@example.com")
    assert found.id == app_user.id
