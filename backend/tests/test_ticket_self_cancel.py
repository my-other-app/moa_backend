"""“Can't come? Give your ticket back” (app/api/ticket_cancel,
feature-register/ticket-self-cancel-contract.md): the signed link, each
refusal, the seat freed as the club's own removal frees it, and the email
token."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.api.club_alerts.models import ClubAlerts
from app.api.events.models import Events, EventRegistrationsLink
from app.api.ticket_cancel import service as cancelling
from app.api.ticket_cancel.models import TicketCancellations
from app.core import jobs
from tests.conftest import auth_header

API = "/api/v1"


def _soon(days: float = 5) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


@pytest.fixture
def mail(monkeypatch):
    from app.core.email import email as email_mod

    sent = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    return sent


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Gate Club", email="tsc-club@example.com")
    club.contact_email = "hello@gate.example"
    event = await make_event(club=club, name="Free Night", created_by=owner, event_datetime=_soon(), max_participants=2)
    guest = await make_user(email="tsc-guest@example.com")
    other = await make_user(email="tsc-other@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Asha Guest")
    reg2 = await make_registration(event=event, user=other, full_name="Ravi Other")
    owner2, _club2 = await make_club(name="Other Club", email="tsc-club2@example.com")
    await session.commit()
    return SimpleNamespace(
        club=club, event=event, reg=reg, reg2=reg2, guest=guest, h=auth_header(await mint_token(owner)),
        other_h=auth_header(await mint_token(owner2)), token=cancelling.token_for(reg.id),
    )


async def _places_left(client, event_id: int):
    """What the ticket picker says is left (ticketing.places, as the public form counts it)."""
    r = await client.get(f"{API}/events/{event_id}/ticket-types")
    assert r.status_code == 200, r.text
    return r.json()["available"]


async def _reg(session, reg_id):
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg_id)
        .execution_options(include_deleted=True, populate_existing=True)
    )


# ─── The link ────────────────────────────────────────────────────────────


async def test_the_token_names_its_registration_and_nothing_else(session):
    from app.core.mail import links

    rid = uuid.uuid4()
    token = cancelling.token_for(rid)
    assert cancelling.registration_id_of(token) == rid
    assert cancelling.url_for(rid).endswith(f"/tickets/cancel?t={token}")
    rid_hex, _, sig = token.partition(".")
    other = uuid.uuid4().hex
    for bad in (None, "", "x", f"{other}.{sig}", f"{rid_hex}.{'0' * len(sig)}", f"{rid_hex}.", rid_hex, f"{rid_hex}.{sig}é"):
        assert cancelling.registration_id_of(bad) is None, bad
    # Purpose-scoped: a ticket file's signature over the same id is not a cancel link.
    assert cancelling.registration_id_of(f"{rid_hex}.{links.sign(f'file:ticket:{rid_hex}')}") is None
    assert cancelling.mask_code("MOA-1K2F9-QX7RT") == "MOA-" + "•" * 7 + "X7RT"


async def test_a_bad_link_says_invalid(client, world):
    r = await client.get(f"{API}/tickets/cancel/not-a-token")
    assert r.status_code == 200 and r.json()["cancellable"] is False and r.json()["reason"] == "invalid"
    assert r.json()["event"] is None
    tampered = world.token[:-1] + ("e" if world.token[-1] != "e" else "f")
    r = await client.post(f"{API}/tickets/cancel/{tampered}", json={})
    assert r.status_code == 409 and r.json()["error_code"] == "invalid"


# ─── Giving it back ──────────────────────────────────────────────────────


async def test_a_free_ticket_is_given_back_and_its_seat_freed_as_the_club_would(client, session, world, mail):
    assert await _places_left(client, world.event.id) == 0
    page = (await client.get(f"{API}/tickets/cancel/{world.token}")).json()
    assert page["cancellable"] is True and page["reason"] is None
    assert page["event"]["id"] == world.event.id and page["event"]["slug"] == world.event.slug and page["event"]["starts_at"]
    assert page["attendee_name"] == "Asha Guest" and page["organiser_email"] == "hello@gate.example"
    assert page["ticket_code_masked"] != world.reg.ticket_id and page["ticket_code_masked"][-4:] == world.reg.ticket_id[-4:]

    r = await client.post(f"{API}/tickets/cancel/{world.token}", json={"reason": "  Exams that week  "})
    assert r.status_code == 200 and r.json() == {"cancelled": True}, r.text
    reg = await _reg(session, world.reg.id)
    assert reg.is_deleted and reg.deleted_at is not None
    # The place is free.
    assert await _places_left(client, world.event.id) == 1
    row = await session.scalar(select(TicketCancellations))
    assert (row.registration_id, row.reason, row.email) == (world.reg.id, "Exams that week", world.reg.email)
    alert = await session.scalar(select(ClubAlerts).where(ClubAlerts.club_id == world.club.id))
    assert alert.kind == "ticket_cancelled" and "Asha Guest" in alert.title and "Exams that week" in alert.body
    assert len(mail) == 1 and mail[0]["recipients"] == [world.reg.email] and "cancelled" in mail[0]["subject"]
    # The club sees it beside its list; another club does not.
    given = (await client.get(f"{API}/events/registration/{world.event.id}/cancellations", headers=world.h)).json()["items"]
    assert [(g["full_name"], g["reason"], g["cancelled_by"]) for g in given] == [("Asha Guest", "Exams that week", "attendee")]
    assert (await client.get(f"{API}/events/registration/{world.event.id}/cancellations", headers=world.other_h)).status_code == 403
    # Single use, in effect.
    again = await client.post(f"{API}/tickets/cancel/{world.token}", json={})
    assert again.status_code == 409 and again.json()["error_code"] == "already_cancelled"
    assert (await client.get(f"{API}/tickets/cancel/{world.token}")).json()["reason"] == "already_cancelled"
    assert len(mail) == 1


async def test_no_reason_is_needed(client, session, world, mail):
    r = await client.post(f"{API}/tickets/cancel/{world.token}")
    assert r.status_code == 200, r.text
    assert (await session.scalar(select(TicketCancellations))).reason is None


async def test_both_ways_off_the_list_free_the_seat_and_offer_it_to_the_waiting_list(client, session, world, monkeypatch, mail):
    queued = []
    real = jobs.enqueue

    async def spy(session, name, payload, **kw):
        queued.append(name)
        return await real(session, name, payload, **kw)

    monkeypatch.setattr(jobs, "enqueue", spy)
    r = await client.delete(
        f"{API}/events/registration/{world.event.id}/info/{world.reg2.id}", params={"confirm_slug": world.event.slug}, headers=world.h,
    )
    assert r.status_code == 200, r.text
    assert queued.count("waitlist.fill") == 1 and await _places_left(client, world.event.id) == 1
    assert (await client.post(f"{API}/tickets/cancel/{world.token}", json={})).status_code == 200
    assert queued.count("waitlist.fill") == 2 and await _places_left(client, world.event.id) == 2


# ─── Each refusal ────────────────────────────────────────────────────────


async def _refused(client, session, world, reason: str):
    page = (await client.get(f"{API}/tickets/cancel/{world.token}")).json()
    assert page["cancellable"] is False and page["reason"] == reason, page
    r = await client.post(f"{API}/tickets/cancel/{world.token}", json={"reason": "x"})
    assert r.status_code == 409 and r.json()["error_code"] == reason, r.text
    assert not (await _reg(session, world.reg.id)).is_deleted
    assert await session.scalar(select(func.count()).select_from(TicketCancellations)) == 0
    return page, r.json()


@pytest.mark.parametrize("money", ["is_paid", "paid_amount", "actual_amount", "payment_receipt"])
async def test_a_paid_ticket_is_never_cancelled_here(client, session, world, money):
    setattr(world.reg, money, {"is_paid": True, "paid_amount": 499, "actual_amount": 499, "payment_receipt": "rcpt_1"}[money])
    await session.commit()
    page, refused = await _refused(client, session, world, "paid")
    # The page names the organiser to write to for a refund; no money moves (Razorpay is never reached: conftest).
    assert page["organiser_email"] == "hello@gate.example" and refused["errors"]["organiser_email"] == "hello@gate.example"


async def test_a_ticket_with_a_store_order_is_paid_too(client, session, world):
    from app.api.store.models import EventStoreOrders

    session.add(EventStoreOrders(registration_id=world.reg.id, event_id=world.event.id, status="pending"))
    await session.commit()
    await _refused(client, session, world, "paid")


async def test_after_the_start_it_is_too_late(client, session, world):
    world.event.event_datetime = datetime.now(timezone.utc) - timedelta(minutes=5)
    await session.commit()
    await _refused(client, session, world, "started")


async def test_a_ticket_used_at_the_door_stays(client, session, world):
    world.reg.is_attended = True
    await session.commit()
    await _refused(client, session, world, "checked_in")


async def test_a_ticket_the_club_revoked_counts_as_cancelled(client, session, world):
    world.reg.ticket_revoked_at = datetime.now(timezone.utc)
    await session.commit()
    await _refused(client, session, world, "already_cancelled")


async def test_the_club_can_turn_it_off_for_an_event(client, session, world):
    form = {
        "name": world.event.name, "event_datetime": world.event.event_datetime.isoformat(), "duration": "2",
        "category_id": str(world.event.category_id), "allow_self_cancel": "false",
    }
    r = await client.put(f"{API}/events/update/{world.event.id}", data=form, headers=world.h)
    assert r.status_code == 200 and r.json()["allow_self_cancel"] is False, r.text
    info = (await client.get(f"{API}/events/info/{world.event.id}", headers=world.h)).json()
    assert info["allow_self_cancel"] is False
    await _refused(client, session, world, "not_allowed")
    # Left out of a later edit, it stays off; sent again, it is on.
    form.pop("allow_self_cancel")
    assert (await client.put(f"{API}/events/update/{world.event.id}", data=form, headers=world.h)).status_code == 200
    assert (await client.get(f"{API}/tickets/cancel/{world.token}")).json()["reason"] == "not_allowed"
    form["allow_self_cancel"] = "true"
    assert (await client.put(f"{API}/events/update/{world.event.id}", data=form, headers=world.h)).status_code == 200
    assert (await client.get(f"{API}/tickets/cancel/{world.token}")).json()["cancellable"] is True


async def test_a_new_event_allows_it_unless_the_form_says_no(client, session, world):
    base = {"event_datetime": _soon(9).isoformat(), "duration": "2", "has_fee": "false", "category_id": str(world.event.category_id)}
    on = await client.post(f"{API}/events/create", data={**base, "name": "Default Night"}, headers=world.h)
    off = await client.post(f"{API}/events/create", data={**base, "name": "Strict Night", "allow_self_cancel": "false"}, headers=world.h)
    assert on.status_code in (200, 201) and off.status_code in (200, 201), (on.text, off.text)
    assert (on.json()["allow_self_cancel"], off.json()["allow_self_cancel"]) == (True, False)


async def test_a_cancelled_event_takes_nothing_back(client, session, world):
    from app.api.closeout.models import EventStatus

    world.event.lifecycle = EventStatus.cancelled
    await session.commit()
    await _refused(client, session, world, "not_allowed")


async def test_the_link_is_rate_limited(client, world):
    codes = [(await client.post(f"{API}/tickets/cancel/nope", json={})).status_code for _ in range(11)]
    assert codes[:10] == [409] * 10 and codes[10] == 429


# ─── The email token ─────────────────────────────────────────────────────


async def test_the_ticket_emails_carry_the_cancel_link(session, world):
    from app.api.emails import service as emails
    from app.core.mail.schema import EMAIL_ONLY_TOKENS, EMAIL_TOKEN_KEYS
    from app.core.mail.starters import request_starter

    event = await session.scalar(select(Events).where(Events.id == world.event.id).options(joinedload(Events.club)))
    reg = await emails.loaded_registration(session, event.id, world.reg.id)
    url = emails.guest_values(event, reg)["cancel_ticket_url"]
    assert url == cancelling.url_for(world.reg.id) and "/tickets/cancel?t=" in url
    assert cancelling.registration_id_of(url.split("?t=", 1)[1]) == world.reg.id
    assert "cancel_ticket_url" in EMAIL_TOKEN_KEYS and any(t["key"] == "cancel_ticket_url" for t in EMAIL_ONLY_TOKENS)
    assert emails.sample_values(event)["cancel_ticket_url"]
    # The confirmed-ticket starter has the quiet line; the rest of it is as it was.
    approved = request_starter("REQUEST_APPROVED").model_dump_json()
    assert "Can't come?" in approved and "{{cancel_ticket_url}}" in approved and "{{ticket_url}}" in approved
