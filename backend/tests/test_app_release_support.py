"""What the app's next release builds on: whether the viewer's ticket is
paid (`is_paid` on the event page), finishing a payment for an unpaid
registration, error codes in the standard envelope where there were 500s or
FastAPI's bare `{"detail": ...}`, and the wallet debug endpoint gone."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.auth import service as auth_service
from app.api.events.models import EventRegistrationsLink
from app.api.payments import service as payments
from app.api.store.models import EventStoreItems
from app.api.users.models import UserTypes
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)
API = "/api/v1"
ENVELOPE = {"message", "errors", "error_code", "track_id"}


def _envelope(r, status: int, code: str | None) -> dict:
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == ENVELOPE, body
    assert isinstance(body["message"], str) and body["message"], body
    assert body["error_code"] == code, body
    return body


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, mint_token, monkeypatch):
    owner, club = await make_club(name="Release Club", email="release-club@example.com")
    paid = await make_event(club=club, created_by=owner, name="Paid Night", event_datetime=NOW + timedelta(days=7), has_fee=True, reg_fee=250)
    free = await make_event(club=club, created_by=owner, name="Free Lab", event_datetime=NOW + timedelta(days=8), has_fee=False, reg_fee=0)
    fan = await make_user(email="release-fan@example.com", username="release-fan")
    await session.commit()
    counter = {"n": 0}

    def fake_order_create(data):
        counter["n"] += 1
        return {"id": f"order_rel_{counter['n']}", "amount": data["amount"], "currency": "INR", "status": "created"}

    monkeypatch.setattr(payments.razorpay_client.order, "create", fake_order_create)
    # The receipt is a PDF, and wkhtmltopdf is not where the suite runs.
    monkeypatch.setattr(payments, "send_payment_confirmation_email", lambda **kwargs: None)
    return dict(owner=owner, club=club, paid=paid, free=free, fan=fan, fan_h=auth_header(await mint_token(fan)), owner_h=auth_header(await mint_token(owner)))


def _register(client, event_id, *, email, headers=None, **extra):
    return client.post(
        f"{API}/events/registration/{event_id}/register",
        json={"full_name": "Asha Menon", "email": email, "phone": "9876500000", **extra},
        headers=headers or {},
    )


def _order(client, event_id, registration_id, headers, checkout=2):
    url = f"{API}/payments/orders" + (f"?checkout={checkout}" if checkout else "")
    return client.post(url, json={"source": "event_registration", "payload": {"event_id": event_id, "event_registration_id": registration_id}}, headers=headers)


# ─── is_paid on the event page ──────────────────────────────────────────


async def test_is_paid_is_null_until_registered_then_says_whether_the_door_will_admit(client, session, world, make_registration):
    paid, free, fan, h = world["paid"], world["free"], world["fan"], world["fan_h"]

    # Not registered (and signed out): null, with no registration to pay for.
    for headers in ({}, h):
        info = (await client.get(f"{API}/events/info/{paid.id}", headers=headers)).json()
        assert info["is_registered"] is False and info["is_paid"] is None and info["registration_id"] is None

    # A free event's registration is paid as far as the door is concerned.
    free_reg = await make_registration(event=free, user=fan)
    await session.commit()
    info = (await client.get(f"{API}/events/info/{free.id}", headers=h)).json()
    assert info["is_registered"] is True and info["is_paid"] is True and info["registration_id"] == str(free_reg.id)

    # A paid event's registration waiting for payment: registered, not paid.
    reg = await make_registration(event=paid, user=fan, actual_amount=250, paid_amount=0)
    await session.commit()
    info = (await client.get(f"{API}/events/info/{paid.id}", headers=h)).json()
    assert info["is_registered"] is True and info["is_paid"] is False and info["registration_id"] == str(reg.id)
    # By slug too.
    assert (await client.get(f"{API}/events/info/{paid.slug}", headers=h)).json()["is_paid"] is False

    reg.is_paid = True
    reg.paid_amount = 250
    await session.commit()
    info = (await client.get(f"{API}/events/info/{paid.id}", headers=h)).json()
    assert info["is_registered"] is True and info["is_paid"] is True


async def test_an_unpaid_registration_is_finished_from_the_event_page(client, session, world, monkeypatch):
    """The app's Finish payment: the event page names the registration, an
    order is made for it, checkout opens with the order's key, and verify
    marks it paid -- without sending the registration form again."""
    paid, h = world["paid"], world["fan_h"]
    first = await _register(client, paid.id, email=world["fan"].email, headers=h)
    assert first.status_code == 200 and first.json()["pay_amount"] == 250

    info = (await client.get(f"{API}/events/info/{paid.id}", headers=h)).json()
    assert info["is_paid"] is False and info["registration_id"] == first.json()["event_registration_id"]

    order = await _order(client, paid.id, info["registration_id"], h)
    assert order.status_code == 200, order.text
    o = order.json()
    assert o["amount"] == 250 and o["key_id"] and o["gateway"] == "platform" and o["razorpay_order_id"].startswith("order_rel_")
    # Asking again before paying hands back the same order.
    assert (await _order(client, paid.id, info["registration_id"], h)).json()["razorpay_order_id"] == o["razorpay_order_id"]

    def fake_fetch(payment_id, *args, **kwargs):
        return {"id": payment_id, "order_id": o["razorpay_order_id"], "status": "captured", "method": "upi", "amount": 25000, "upi": {}}

    monkeypatch.setattr(payments.razorpay_client.payment, "fetch", fake_fetch)
    verified = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": o["razorpay_order_id"], "razorpay_payment_id": "pay_finish_1"})
    assert verified.status_code == 200 and verified.json()["payment_status"] == "captured", verified.text
    assert (await client.get(f"{API}/events/info/{paid.id}", headers=h)).json()["is_paid"] is True

    # Paid now: another order is refused plainly, not as a 500.
    _envelope(await _order(client, paid.id, info["registration_id"], h), 409, "ALREADY_PAID")
    # Verifying a payment for the paid order again is idempotent. A second
    # payment on it bought nothing: it is recorded and refunded in full, and
    # the ticket stands on the first.
    again = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": o["razorpay_order_id"], "razorpay_payment_id": "pay_finish_1"})
    assert again.status_code == 200
    refunds = []
    monkeypatch.setattr(payments.razorpay_client.payment, "refund", lambda payment_id, *a, **kw: refunds.append(payment_id) or {"id": "rfnd_1"})
    second = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": o["razorpay_order_id"], "razorpay_payment_id": "pay_finish_2"})
    assert second.status_code == 200 and second.json()["payment_status"] == "refunded", second.text
    assert refunds == ["pay_finish_2"]
    assert (await client.get(f"{API}/events/info/{paid.id}", headers=h)).json()["is_paid"] is True


async def test_an_unpaid_registration_is_paid_after_the_deadline_until_the_event_is_over(client, session, world, make_event, make_registration, make_user):
    """The registration deadline stops new sign-ups, not someone who signed
    up in time finishing their payment. The event being over, or full, does."""
    owner, club, fan, h = world["owner"], world["club"], world["fan"], world["fan_h"]
    closed = await make_event(club=club, created_by=owner, name="Deadline passed", has_fee=True, reg_fee=300, event_datetime=NOW + timedelta(days=2), reg_enddate=NOW - timedelta(hours=3))
    reg = await make_registration(event=closed, user=fan, actual_amount=300, paid_amount=0)
    await session.commit()

    # A new sign-up is refused...
    _envelope(await _register(client, closed.id, email="too-late@example.com"), 400, "REGISTRATIONS_CLOSED")
    # ...while the registration made in time is paid for straight from the event page.
    info = (await client.get(f"{API}/events/info/{closed.id}", headers=h)).json()
    assert info["is_paid"] is False and info["registration_id"] == str(reg.id)
    order = await _order(client, closed.id, info["registration_id"], h)
    assert order.status_code == 200 and order.json()["amount"] == 300, order.text

    # Once the event is over, it can no longer be paid for.
    over = await make_event(club=club, created_by=owner, name="Over", has_fee=True, reg_fee=300, event_datetime=NOW - timedelta(hours=5), duration=2.0)
    late = await make_registration(event=over, user=fan, actual_amount=300, paid_amount=0)
    await session.commit()
    _envelope(await _order(client, over.id, str(late.id), h), 409, "EVENT_ENDED")
    # Nor once the organiser has closed it out, whatever the clock says.
    from app.api.closeout.models import EventStatus

    wrapped = await make_event(club=club, created_by=owner, name="Wrapped", has_fee=True, reg_fee=300, event_datetime=NOW + timedelta(hours=1), lifecycle=EventStatus.closed)
    wrapped_reg = await make_registration(event=wrapped, user=fan, actual_amount=300, paid_amount=0)
    await session.commit()
    _envelope(await _order(client, wrapped.id, str(wrapped_reg.id), h), 409, "EVENT_ENDED")

    # A full event takes no more paid tickets: its paid registrations fill max_participants.
    full = await make_event(club=club, created_by=owner, name="Full house", has_fee=True, reg_fee=300, event_datetime=NOW + timedelta(days=3), max_participants=1)
    other = await make_user(email="seat-holder@example.com", username="seat-holder")
    await make_registration(event=full, user=other, actual_amount=300, paid_amount=300, is_paid=True)
    waiting = await make_registration(event=full, user=fan, actual_amount=300, paid_amount=0)
    await session.commit()
    body = _envelope(await _order(client, full.id, str(waiting.id), h), 400, "EVENT_FULL")
    assert body["errors"] == {"max_participants": 1}


# ─── Payment orders and verify: codes, not 500s ─────────────────────────


async def test_payment_orders_refuse_with_codes(client, session, world, make_registration):
    free, h = world["free"], world["fan_h"]
    reg = await make_registration(event=free, user=world["fan"])
    await session.commit()
    body = _envelope(await _order(client, free.id, str(reg.id), h), 422, "NOTHING_TO_PAY")
    assert body["errors"] == {"event_registration_id": "nothing to pay"}
    _envelope(await _order(client, free.id, str(uuid.uuid4()), h), 404, "REGISTRATION_NOT_FOUND")
    _envelope(await _order(client, 987654, str(reg.id), h), 404, "EVENT_NOT_FOUND")
    # A payload missing its fields is the client's mistake, said as one.
    r = await client.post(f"{API}/payments/orders", json={"source": "event_registration", "payload": {}}, headers=h)
    assert r.status_code == 400 and set(r.json()["errors"]) == {"event_id", "event_registration_id"}


async def test_verify_answers_in_the_envelope(client, session, world, make_registration, monkeypatch):
    paid, h = world["paid"], world["fan_h"]
    reg = await make_registration(event=paid, user=world["fan"], actual_amount=250, paid_amount=0)
    await session.commit()
    o = (await _order(client, paid.id, str(reg.id), h)).json()

    _envelope(await client.post(f"{API}/payments/verify", json={"razorpay_order_id": "order_nobody", "razorpay_payment_id": "pay_x"}), 404, "ORDER_NOT_FOUND")

    def other_order(payment_id, *args, **kwargs):
        return {"id": payment_id, "order_id": "order_someone_else", "status": "captured", "method": "upi", "amount": 25000}

    monkeypatch.setattr(payments.razorpay_client.payment, "fetch", other_order)
    _envelope(await client.post(f"{API}/payments/verify", json={"razorpay_order_id": o["razorpay_order_id"], "razorpay_payment_id": "pay_y"}), 400, "PAYMENT_ORDER_MISMATCH")

    # Whatever it was paid with, a captured payment is applied (EMI used to be refused, after Razorpay had the money).
    def emi(payment_id, *args, **kwargs):
        return {"id": payment_id, "order_id": o["razorpay_order_id"], "status": "captured", "method": "emi", "amount": 25000}

    monkeypatch.setattr(payments.razorpay_client.payment, "fetch", emi)
    applied = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": o["razorpay_order_id"], "razorpay_payment_id": "pay_z"})
    assert applied.status_code == 200 and applied.json()["payment_method"] == "emi", applied.text

    from razorpay.errors import BadRequestError

    def unknown(payment_id, *args, **kwargs):
        raise BadRequestError("The id provided does not exist")

    monkeypatch.setattr(payments.razorpay_client.payment, "fetch", unknown)
    _envelope(await client.post(f"{API}/payments/verify", json={"razorpay_order_id": o["razorpay_order_id"], "razorpay_payment_id": "pay_none"}), 404, "PAYMENT_NOT_FOUND")

    # And the payment the order was paid with, missing from our records after all: the route's own 404.
    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(payments, "verify_razorpay_payment", nothing)
    _envelope(await client.post(f"{API}/payments/verify", json={"razorpay_order_id": o["razorpay_order_id"], "razorpay_payment_id": "pay_q"}), 404, "PAYMENT_NOT_FOUND")


# ─── Google sign-in: codes, not {"detail": ...} ─────────────────────────


async def test_google_sign_in_refuses_in_the_envelope(client, session, make_user, monkeypatch):
    _envelope(await client.post(f"{API}/auth/google", json={"id_token": "t", "platform": "windows"}), 400, "INVALID_PLATFORM")

    def bad_token(id_token_str, request, client_id):
        raise ValueError("Token used too late")

    monkeypatch.setattr(auth_service.id_token, "verify_oauth2_token", bad_token)
    _envelope(await client.post(f"{API}/auth/google", json={"id_token": "t", "platform": "android"}), 401, "INVALID_GOOGLE_TOKEN")

    def unverified(id_token_str, request, client_id):
        return {"email": "unverified-g@example.com", "email_verified": False, "name": "U"}

    monkeypatch.setattr(auth_service.id_token, "verify_oauth2_token", unverified)
    _envelope(await client.post(f"{API}/auth/google", json={"id_token": "t", "platform": "android"}), 401, "EMAIL_NOT_VERIFIED")

    # A suspended account is told so at the door, not handed tokens first.
    suspended = await make_user(email="suspended-g@example.com", username="suspended-g")
    suspended.is_active = False
    await session.commit()

    def theirs(id_token_str, request, client_id):
        return {"email": "suspended-g@example.com", "email_verified": True, "name": "S"}

    monkeypatch.setattr(auth_service.id_token, "verify_oauth2_token", theirs)
    _envelope(await client.post(f"{API}/auth/google", json={"id_token": "t", "platform": "ios"}), 403, "ACCOUNT_SUSPENDED")


# ─── Registration refusals ──────────────────────────────────────────────


async def test_registration_refusals_carry_codes(client, session, world, make_event, make_user, make_registration):
    owner, club, paid, h = world["owner"], world["club"], world["paid"], world["fan_h"]

    closed = await make_event(club=club, created_by=owner, name="Closed", event_datetime=NOW + timedelta(days=3), reg_enddate=NOW - timedelta(hours=1))
    later = await make_event(club=club, created_by=owner, name="Later", event_datetime=NOW + timedelta(days=30), reg_startdate=NOW + timedelta(days=2))
    full = await make_event(club=club, created_by=owner, name="Full", event_datetime=NOW + timedelta(days=3), max_participants=1)
    someone = await make_user(email="first-in@example.com", username="first-in")
    await make_registration(event=full, user=someone)
    await session.commit()

    body = _envelope(await _register(client, closed.id, email="late@example.com"), 400, "REGISTRATIONS_CLOSED")
    assert body["errors"]["closed_reason"] == "deadline_passed" and body["errors"]["closed_at"]
    body = _envelope(await _register(client, later.id, email="early@example.com"), 400, "REGISTRATIONS_CLOSED")
    assert body["errors"]["closed_reason"] == "not_started" and body["errors"]["opens_at"]
    body = _envelope(await _register(client, full.id, email="no-room@example.com"), 400, "EVENT_FULL")
    assert body["message"] == "Event is full" and body["errors"] == {"max_participants": 1}

    # Signed in: once registered, again is ALREADY_REGISTERED (free) ...
    assert (await _register(client, world["free"].id, email=world["fan"].email, headers=h)).status_code == 200
    _envelope(await _register(client, world["free"].id, email=world["fan"].email, headers=h), 400, "ALREADY_REGISTERED")
    # ... and another address than the account's is refused with its own code.
    _envelope(await _register(client, world["free"].id, email="not-mine@example.com", headers=h), 400, "EMAIL_MISMATCH")

    # Signed out, from the public form.
    assert (await _register(client, world["free"].id, email="public@example.com")).status_code == 200
    _envelope(await _register(client, world["free"].id, email="public@example.com"), 409, "ALREADY_REGISTERED")
    assert (await _register(client, paid.id, email="unpaid@example.com")).status_code == 200
    _envelope(await _register(client, paid.id, email="unpaid@example.com"), 409, "REGISTRATION_AWAITING_PAYMENT")
    body = _envelope(await _register(client, paid.id, email=world["owner"].email), 400, "CLUB_EMAIL")


async def test_a_sold_out_pick_is_coded_and_names_the_item(client, session, world):
    paid, owner_h = world["paid"], world["owner_h"]
    assert (await client.patch(f"{API}/events/{paid.id}/store/settings", json={"merch_enabled": True}, headers=owner_h)).status_code == 200
    kit = (await client.post(f"{API}/events/{paid.id}/store", json={"name": "Starter kit", "price": 890, "stock": 1}, headers=owner_h)).json()
    tee = (await client.post(f"{API}/events/{paid.id}/store", json={"name": "Tee", "price": 450, "variants": [{"label": "S", "stock": 0}]}, headers=owner_h)).json()

    body = _envelope(await _register(client, paid.id, email="greedy@example.com", store=[{"item_id": kit["id"], "qty": 2}]), 422, "SOLD_OUT")
    assert body["message"] == "Starter kit: only 1 left"
    assert body["errors"] == {"item_id": kit["id"], "variant_id": None, "available": 1}
    small = tee["variants"][0]["id"]
    body = _envelope(await _register(client, paid.id, email="small@example.com", store=[{"item_id": tee["id"], "variant_id": small, "qty": 1}]), 422, "SOLD_OUT")
    assert body["message"] == "Tee S is sold out" and body["errors"] == {"item_id": tee["id"], "variant_id": small, "available": 0}
    _envelope(await _register(client, paid.id, email="nosize@example.com", store=[{"item_id": tee["id"], "qty": 1}]), 422, "SIZE_REQUIRED")
    # The pick that fits goes through.
    ok = await _register(client, paid.id, email="modest@example.com", store=[{"item_id": kit["id"], "qty": 1}])
    assert ok.status_code == 200 and ok.json()["store_total"] == 890.0
    item = await session.scalar(select(EventStoreItems).where(EventStoreItems.id == uuid.UUID(kit["id"])))
    assert item.sold_count == 0  # sold when paid for, not before


async def test_answers_are_refused_with_a_string_message_and_the_fields_where_the_site_reads_them(client, session, world, make_event):
    """moa_eventwebsite/src/utils/register.js describeRegisterError reads
    `message` (a string, shown as said), `error_code`, and the field errors
    from `errors` by question key ("q:<key>" on the form) -- and only fell
    back to a dict `message` because that is what this used to send."""
    owner, club = world["owner"], world["club"]
    questions = [
        {"key": "college", "label": "College", "field_type": "text", "required": True},
        {"key": "track", "label": "Track", "field_type": "select", "required": True, "options": ["Web", "ML"]},
        {"key": "tshirt", "label": "T-shirt size", "field_type": "text", "required": False},
    ]
    event = await make_event(club=club, created_by=owner, name="Questions", event_datetime=NOW + timedelta(days=4), additional_details=questions)
    await session.commit()

    body = _envelope(await _register(client, event.id, email="answers@example.com", additional_details={"track": "Design"}), 400, "INVALID_ANSWERS")
    assert body["message"] == "Check your answers: College, Track."
    assert body["errors"] == {"college": "This field is required", "track": "Invalid option selected"}

    ok = await _register(client, event.id, email="answers@example.com", additional_details={"college": "CET", "track": "ML"})
    assert ok.status_code == 200, ok.text


# ─── The wallet debug endpoint ──────────────────────────────────────────


async def test_the_wallet_debug_endpoint_is_gone(client, make_admin, mint_token):
    for headers in ({}, auth_header(await mint_token(await make_admin(email="wallet-admin@example.com")))):
        r = await client.get(f"{API}/events/wallet/debug", headers=headers)
        assert r.status_code in (404, 405), r.text
        assert "certs_dir" not in r.text and "team_id" not in r.text


async def test_a_guest_session_finishes_its_own_payment_too(client, session, world):
    """The website's Finish payment: the guest token the form left signs the
    same registration back in, and the event page knows it is unpaid."""
    paid = world["paid"]
    first = await _register(client, paid.id, email="guest-payer@example.com")
    assert first.status_code == 200
    token = first.json()["auth_token"]["access_token"]
    info = (await client.get(f"{API}/events/info/{paid.id}", headers=auth_header(token))).json()
    assert info["is_paid"] is False and info["registration_id"] == first.json()["event_registration_id"]
    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.id == uuid.UUID(info["registration_id"])))
    holder = reg.user_id
    from app.api.users.models import Users

    assert (await session.get(Users, holder)).user_type == UserTypes.guest
    o = await _order(client, paid.id, info["registration_id"], auth_header(token))
    assert o.status_code == 200 and o.json()["amount"] == 250
