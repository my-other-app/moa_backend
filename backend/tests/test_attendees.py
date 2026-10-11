"""The organiser's own changes to the guest list.

Ported from Muster's attendees module: filters a desk actually uses, a
guest in full, corrections, removal with the slug typed, adding by hand
without an email going out, and an import whose history stays on record.
"""
import io
from datetime import datetime, timezone

import pytest

from app.api.events.registration import background_tasks as reg_background
from tests.conftest import auth_header


@pytest.fixture
def no_email(monkeypatch):
    sent = []
    monkeypatch.setattr(reg_background, "send_email_with_attachment", lambda **kw: sent.append(kw))
    return sent


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="attendees@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0,
    )
    a = await make_user(email="ananya-a@example.com")
    s = await make_user(email="sita-a@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", phone="9876543210", is_attended=True)
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", role="speaker")
    await session.commit()
    return {"event": event, "a": r_a, "s": r_s, "club": auth_header(await mint_token(owner))}


def _base(world):
    return f"/api/v1/events/registration/{world['event'].id}"


async def test_the_list_filters_by_role_check_in_and_delivery_and_searches_phones(client, world):
    e = _base(world)
    names = lambda r: [row["full_name"] for row in r.json()["items"]]  # noqa: E731

    assert names(await client.get(f"{e}/list", params={"role": "speaker"}, headers=world["club"])) == ["Sita Raman"]
    assert names(await client.get(f"{e}/list", params={"is_attended": "false"}, headers=world["club"])) == ["Sita Raman"]
    assert names(await client.get(f"{e}/list", params={"search": "98765"}, headers=world["club"])) == ["Ananya Iyer"]
    # Nobody has been sent anything, so both are "not delivered"; sorted by name.
    assert names(await client.get(f"{e}/list", params={"delivery": "not_delivered"}, headers=world["club"])) == ["Ananya Iyer", "Sita Raman"]


async def test_a_guest_in_full_carries_what_was_sent_to_them(client, world):
    r = await client.get(f"{_base(world)}/info/{world['a'].id}", headers=world["club"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["full_name"] == "Ananya Iyer" and body["phone"] == "9876543210"
    assert body["is_attended"] is True and body["attended_on"] is None
    assert body["ticket_deliveries"] == [] and body["certificate_deliveries"] == []
    assert body["ticket_revoked_at"] is None


async def test_a_correction_changes_the_registration_not_the_account(client, session, world):
    r = await client.patch(
        f"{_base(world)}/info/{world['s'].id}",
        json={"full_name": "Sita R.", "phone": "9000000000", "role": "staff", "additional_details": {"T-shirt": "M"}},
        headers=world["club"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "Sita R." and r.json()["role"] == "staff"
    assert r.json()["additional_details"] == {"T-shirt": "M"}
    await session.refresh(world["s"].user)
    assert world["s"].user.full_name == "Test User"

    taken = await client.patch(f"{_base(world)}/info/{world['s'].id}", json={"email": "ananya-a@example.com"}, headers=world["club"])
    assert taken.status_code == 409


async def test_removal_needs_the_slug_typed_and_then_takes_them_off_the_list(client, world):
    e = _base(world)
    wrong = await client.delete(f"{e}/info/{world['s'].id}", params={"confirm_slug": "nope"}, headers=world["club"])
    assert wrong.status_code == 409

    ok = await client.delete(f"{e}/info/{world['s'].id}", params={"confirm_slug": world["event"].slug}, headers=world["club"])
    assert ok.status_code == 200, ok.text
    names = [row["full_name"] for row in (await client.get(f"{e}/list", headers=world["club"])).json()["items"]]
    assert names == ["Ananya Iyer"]


async def test_adding_by_hand_registers_them_and_emails_nobody(client, world, no_email):
    r = await client.post(
        f"{_base(world)}/attendees",
        json={"full_name": "Bala Krishnan", "email": "bala-a@example.com", "role": "speaker"},
        headers=world["club"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "Bala Krishnan" and r.json()["role"] == "speaker"
    assert r.json()["ticket_id"].startswith("MOA-")
    assert no_email == []

    again = await client.post(f"{_base(world)}/attendees", json={"full_name": "Bala Krishnan", "email": "bala-a@example.com"}, headers=world["club"])
    assert again.status_code == 400


async def test_an_import_is_recorded_against_the_event_and_emails_nobody(client, world, no_email):
    e = _base(world)
    csv = "full_name,email,phone,role\nChitra Nair,chitra-a@example.com,,speaker\nBroken Row,,,\n"
    r = await client.post(f"{e}/import", files={"file": ("guests.csv", csv.encode(), "text/csv")}, headers=world["club"])
    assert r.status_code == 200, r.text

    history = (await client.get(f"{e}/imports", headers=world["club"])).json()
    assert len(history) == 1
    assert history[0]["task_name"].startswith("Import guests.csv")
    assert history[0]["imported"] == 1 and history[0]["failed"] == 1
    assert no_email == []
    names = [row["full_name"] for row in (await client.get(f"{e}/list", headers=world["club"])).json()["items"]]
    assert "Chitra Nair" in names

    bad = await client.post(f"{e}/import", files={"file": ("x.csv", b"name,mail\nA,b@c.d\n", "text/csv")}, headers=world["club"])
    assert bad.status_code == 422
