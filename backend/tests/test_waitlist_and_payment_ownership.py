"""Whose place in line, whose registration to pay for.

- Joining a waiting list with an email or phone number that already has a
  place there hands that place back (its id, status and position) only to
  its own holder: the account or guest session that joined, or an account
  that has shown the address is its own (Google, Apple, or the code typed
  back: users.service.merges_into). Anyone else gets 409
  ALREADY_ON_WAITLIST, which says nothing of the place, and no second place
  is made.
- A payment order is made only for the registration's holder: its account,
  the guest session the form handed out, or an account that has shown the
  address is its own (registration.service.may_reuse). Anyone else gets the
  404 an unknown id gets, before Razorpay is asked or anything is written.

Razorpay is never called: tests.test_money_integrity's FakeRazorpay stands
in for it, as in the other payment tests.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.events.models import EventRegistrationsLink
from app.api.payments.models import PaymentOrders
from app.api.ticketing.models import WaitlistEntries
from app.api.users.models import SigninProviders
from tests.conftest import auth_header
from tests.test_money_integrity import FakeRazorpay, rzp_fake  # noqa: F401
from tests.test_ticketing import set_types

API = "/api/v1"
NOW = datetime.now(timezone.utc)
VICTIM = "victim-line@example.com"
PHONE = "9876512345"


@pytest.fixture
def mail(monkeypatch):
    from app.core.email import email as email_mod

    sent = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    return sent


@pytest.fixture
async def w(client, session, make_club, make_event, mint_token, mail):
    """A club with a free event, a paid one, and a sold-out one whose single
    "Seat" type has its waiting list on."""
    owner, club = await make_club(name="Ownership Club", email="ownership-club@example.com")
    free = await make_event(club=club, created_by=owner, name="Free Talk", event_datetime=NOW + timedelta(days=4))
    line = await make_event(club=club, created_by=owner, name="Sold Out Night", event_datetime=NOW + timedelta(days=6))
    paid = await make_event(
        club=club, created_by=owner, name="Paid Night", event_datetime=NOW + timedelta(days=8), has_fee=True, reg_fee=200,
    )
    await session.commit()
    h = auth_header(await mint_token(owner))
    types = await set_types(client, SimpleNamespace(h=h), line, [{"name": "Seat", "capacity": 1}])
    r = await client.put(f"{API}/events/{line.id}/ticketing/waitlist/settings", json={"enabled": True}, headers=h)
    assert r.status_code == 200, r.text
    first = await _register(client, line.id, "first-in@example.com", ticket_type_id=types["Seat"]["id"])
    assert first.status_code == 200, first.text
    return SimpleNamespace(owner=owner, owner_h=h, free=free, line=line, paid=paid, seat=types["Seat"]["id"], mail=mail)


def _register(client, event_id, email, *, headers=None, **extra):
    body = {"full_name": "Vidya Raman", "email": email, "phone": "9876500011", **extra}
    return client.post(f"{API}/events/registration/{event_id}/register", json=body, headers=headers or {})


def _join(client, w, email=VICTIM, *, headers=None, **extra):
    body = {"full_name": "Vidya Raman", "ticket_type_id": w.seat, **extra}
    if email is not None:
        body["email"] = email
    return client.post(f"{API}/events/{w.line.id}/waitlist", json=body, headers=headers or {})


def _order(client, event_id, registration_id, headers):
    return client.post(
        f"{API}/payments/orders", params={"checkout": 2},
        json={"source": "event_registration", "payload": {"event_id": event_id, "event_registration_id": str(registration_id)}},
        headers=headers,
    )


async def _guest_session_for(client, event, email):
    """What anyone can get: register at a free event with the address."""
    r = await _register(client, event.id, email, name="Someone Else")
    assert r.status_code == 200, r.text
    return auth_header(r.json()["auth_token"]["access_token"])


async def _proven(session, make_user, email, username, vouched="google"):
    """An app account that has shown `email` is its own."""
    user = await make_user(email=email, username=username)
    if vouched == "google":
        user.provider = SigninProviders.google
    else:
        user.email_verified_at = NOW
    await session.commit()
    return user


def _answer(r) -> dict:
    """The answer without its per-request trace id."""
    return {k: v for k, v in r.json().items() if k != "track_id"}


async def _entries(session, event_id) -> list[WaitlistEntries]:
    return list(await session.scalars(
        select(WaitlistEntries).where(WaitlistEntries.event_id == event_id).execution_options(populate_existing=True)
    ))


# ─── 1. A place in line, joined again ───────────────────────────────────────


async def test_someone_elses_address_or_number_gets_a_refusal_that_says_nothing_of_the_place(
    client, session, w, make_user, mint_token
):
    held = await _join(client, w, phone=PHONE)
    assert held.status_code == 200, held.text
    place = held.json()
    guest = await _guest_session_for(client, w.free, VICTIM)
    typed = await make_user(email=VICTIM, username="typed-line")  # the address only typed, never confirmed
    stranger = await _proven(session, make_user, "stranger-line@example.com", "stranger-line")
    mails_before = len(w.mail)

    tries = {
        "no session": await _join(client, w),
        "no session, the address in another case": await _join(client, w, email="VICTIM-line@Example.com "),
        "a guest session with the address": await _join(client, w, headers=guest),
        "an account that never confirmed the address": await _join(client, w, headers=auth_header(await mint_token(typed))),
        # By the number alone, from an account proven for an address of its own.
        "someone else's number": await _join(
            client, w, email="other-address@example.com", phone="98765 12345", headers=auth_header(await mint_token(stranger)),
        ),
    }
    answers = []
    for who, r in tries.items():
        assert r.status_code == 409, (who, r.text)
        body = r.json()
        assert body["error_code"] == "ALREADY_ON_WAITLIST" and not body.get("errors"), (who, body)
        for secret in (place["id"], "position", "joined_at", "offer_expires_at"):
            assert secret not in r.text, (who, secret)
        answers.append(_answer(r))
    # The one answer for everyone: nothing to tell them apart by.
    assert all(a == answers[0] for a in answers)

    # No second place, the holder's untouched, and nothing sent to them.
    [entry] = await _entries(session, w.line.id)
    assert (str(entry.id), entry.status, entry.email, entry.user_id) == (place["id"], "waiting", VICTIM, None)
    assert len(w.mail) == mails_before


async def test_the_holder_still_gets_their_own_place_back(client, session, w, make_user, mint_token):
    # A signed-in account's own place, asked for twice.
    member = await make_user(email="member-line@example.com", username="member-line")
    await session.commit()
    mh = auth_header(await mint_token(member))
    first = await _join(client, w, email=None, headers=mh)
    assert first.status_code == 200 and first.json()["created"] is True, first.text
    again = await _join(client, w, email=None, headers=mh)
    assert again.status_code == 200, again.text
    assert (again.json()["id"], again.json()["created"], again.json()["position"]) == (first.json()["id"], False, 1)

    # A guest session's own place: the session that joined.
    gh = await _guest_session_for(client, w.free, "guest-line@example.com")
    joined = await _join(client, w, email="guest-line@example.com", headers=gh)
    assert joined.status_code == 200 and joined.json()["created"] is True, joined.text
    back = await _join(client, w, email="guest-line@example.com", headers=gh)
    assert back.status_code == 200, back.text
    assert (back.json()["id"], back.json()["created"], back.json()["position"]) == (joined.json()["id"], False, 2)
    # Another guest session with the same address is not that session.
    other_guest = await _guest_session_for(client, w.paid, "guest-line@example.com")
    assert (await _join(client, w, email="guest-line@example.com", headers=other_guest)).status_code == 409
    assert len(await _entries(session, w.line.id)) == 2


@pytest.mark.parametrize("vouched", ["google", "code"])
async def test_an_account_that_proved_the_address_gets_the_place_joined_with_it(
    client, session, w, make_user, mint_token, vouched
):
    signed_out = await _join(client, w)
    assert signed_out.status_code == 200, signed_out.text
    owner = await _proven(session, make_user, VICTIM, f"victim-{vouched}", vouched)
    r = await _join(client, w, email=None, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["created"], r.json()["status"]) == (signed_out.json()["id"], False, "waiting")
    assert len(await _entries(session, w.line.id)) == 1


# ─── 2. A payment order, for whose registration ─────────────────────────────


async def test_an_order_for_someone_elses_registration_is_refused_as_if_there_were_none(
    client, session, w, rzp_fake, make_user, make_registration, mint_token  # noqa: F811
):
    victim = await _register(client, w.paid.id, VICTIM)
    assert victim.status_code == 200 and victim.json()["pay_amount"] == 200.0, victim.text
    reg_id = victim.json()["event_registration_id"]
    guest = await _guest_session_for(client, w.free, VICTIM)
    other = await _proven(session, make_user, "other-payer@example.com", "other-payer")
    typed = await make_user(email=VICTIM, username="typed-payer")  # the address only typed, never confirmed
    await session.commit()
    oh = auth_header(await mint_token(other))
    unknown = await _order(client, w.paid.id, uuid.uuid4(), oh)
    assert unknown.status_code == 404 and unknown.json()["error_code"] == "REGISTRATION_NOT_FOUND", unknown.text

    callers = {
        "another account": oh,
        "a guest session with the address": guest,
        "an account that never confirmed the address": auth_header(await mint_token(typed)),
        "the club's own account": w.owner_h,
    }
    for who, headers in callers.items():
        r = await _order(client, w.paid.id, reg_id, headers)
        assert r.status_code == 404, (who, r.text)
        assert _answer(r) == _answer(unknown), who

    # Paid already: still the unknown id's answer, not ALREADY_PAID.
    holder = await make_user(email="paid-holder@example.com", username="paid-holder")
    paid_reg = await make_registration(event=w.paid, user=holder, actual_amount=200, paid_amount=200, is_paid=True)
    await session.commit()
    r = await _order(client, w.paid.id, paid_reg.id, oh)
    assert r.status_code == 404 and _answer(r) == _answer(unknown), r.text
    mine = await _order(client, w.paid.id, paid_reg.id, auth_header(await mint_token(holder)))
    assert mine.status_code == 409 and mine.json()["error_code"] == "ALREADY_PAID", mine.text

    # Razorpay was never asked, and nothing was written for the registration.
    assert rzp_fake.orders == {} and rzp_fake.calls == []
    assert await session.scalar(select(func.count()).select_from(PaymentOrders)) == 0
    reg = await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == uuid.UUID(reg_id)).execution_options(populate_existing=True)
    )
    assert reg.payment_receipt is None


async def test_the_holder_still_gets_an_order(client, session, w, rzp_fake, make_user, mint_token):  # noqa: F811
    # The public form's guest session, as the website pays right after registering.
    guest = await _register(client, w.paid.id, "form-payer@example.com")
    assert guest.status_code == 200, guest.text
    r = await _order(client, w.paid.id, guest.json()["event_registration_id"], auth_header(guest.json()["auth_token"]["access_token"]))
    assert r.status_code == 200, r.text
    assert rzp_fake.orders[r.json()["razorpay_order_id"]] == 20000

    # A signed-in account's own registration, as the app and "Finish payment" pay.
    member = await make_user(email="member-payer@example.com", username="member-payer")
    await session.commit()
    mh = auth_header(await mint_token(member))
    mine = await _register(client, w.paid.id, "member-payer@example.com", headers=mh)
    assert mine.status_code == 200, mine.text
    r = await _order(client, w.paid.id, mine.json()["event_registration_id"], mh)
    assert r.status_code == 200 and r.json()["razorpay_order_id"] in rzp_fake.orders, r.text

    # An account that has shown the address is its own, for the guest's
    # registration under it: registering again hands it that registration.
    left = await _register(client, w.paid.id, "proven-payer@example.com")
    owner = await _proven(session, make_user, "proven-payer@example.com", "proven-payer")
    oh = auth_header(await mint_token(owner))
    back = await _register(client, w.paid.id, "proven-payer@example.com", headers=oh)
    assert back.json()["event_registration_id"] == left.json()["event_registration_id"], back.text
    r = await _order(client, w.paid.id, left.json()["event_registration_id"], oh)
    assert r.status_code == 200 and r.json()["razorpay_order_id"] in rzp_fake.orders, r.text


async def test_an_event_id_sent_as_text_still_gets_an_order(client, w, rzp_fake):  # noqa: F811
    """The website sends `event_id` as text ("3"). Compared as sent, Postgres
    refused `integer = character varying` and every website checkout was a
    500 (12 Nov 2026). It is read as the number it is; the order keeps it typed."""
    guest = await _register(client, w.paid.id, "text-id-payer@example.com")
    assert guest.status_code == 200, guest.text
    h = auth_header(guest.json()["auth_token"]["access_token"])
    r = await _order(client, str(w.paid.id), guest.json()["event_registration_id"], h)
    assert r.status_code == 200, r.text
    assert rzp_fake.orders[r.json()["razorpay_order_id"]] == 20000
    # The same order comes back for the same registration, whichever way the id is sent.
    again = await _order(client, w.paid.id, guest.json()["event_registration_id"], h)
    assert again.status_code == 200 and again.json()["razorpay_order_id"] == r.json()["razorpay_order_id"], again.text


async def test_ids_that_are_not_ids_get_the_unknown_id_404_not_a_500(client, w, rzp_fake):  # noqa: F811
    guest = await _register(client, w.paid.id, "bad-id-payer@example.com")
    assert guest.status_code == 200, guest.text
    h = auth_header(guest.json()["auth_token"]["access_token"])
    r = await _order(client, "three", guest.json()["event_registration_id"], h)
    assert r.status_code == 404 and r.json()["error_code"] == "EVENT_NOT_FOUND", r.text
    r = await _order(client, w.paid.id, "not-a-registration", h)
    assert r.status_code == 404 and r.json()["error_code"] == "REGISTRATION_NOT_FOUND", r.text
    assert not rzp_fake.orders
