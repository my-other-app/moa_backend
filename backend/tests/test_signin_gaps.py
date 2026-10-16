"""Small gaps the website's Google sign-in ran into.

Refusals google_signin makes on purpose came back as 500s; the registered
list could not tell a ticket from a registration still waiting for its
payment; a signed-in duplicate registration had no error code; and a club
whose name ran past 20 characters could not be read back at all.
"""
from datetime import datetime, timedelta, timezone

from app.api.auth import service as auth_service
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


async def test_an_unverified_google_address_is_a_401_not_a_500(client, monkeypatch):
    monkeypatch.setattr(
        auth_service.id_token, "verify_oauth2_token",
        lambda token, request, client_id: {"email": "unverified@example.com", "email_verified": False, "name": "U"},
    )
    r = await client.post("/api/v1/auth/google", json={"id_token": "fake", "platform": "web"})
    assert r.status_code == 401, r.text
    assert "not verified" in r.text


async def test_an_unknown_platform_is_a_400_not_a_500(client):
    r = await client.post("/api/v1/auth/google", json={"id_token": "fake", "platform": "smart-fridge"})
    assert r.status_code in (400, 422), r.text


async def test_the_registered_list_says_whether_each_one_is_paid(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    owner, club = await make_club(name="Paid Club", email="paid-club@example.com")
    paid_event = await make_event(club=club, created_by=owner, name="Paid", event_datetime=NOW + timedelta(days=4), has_fee=True, reg_fee=100)
    free_event = await make_event(club=club, created_by=owner, name="Free", event_datetime=NOW + timedelta(days=5))
    user = await make_user(email="lister@example.com")
    unpaid = await make_registration(event=paid_event, user=user, actual_amount=100)
    unpaid.is_paid = False
    await make_registration(event=free_event, user=user)
    await session.commit()

    r = await client.get("/api/v1/events/list?is_registered=true", headers=auth_header(await mint_token(user)))
    assert r.status_code == 200, r.text
    by_name = {e["name"]: e for e in r.json()["items"]}
    assert by_name["Paid"]["is_paid"] is False
    assert by_name["Free"]["is_paid"] in (True, False)  # a free registration's flag is whatever registration set


async def test_a_signed_in_duplicate_carries_the_same_code_guests_get(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    owner, club = await make_club(name="Dup Club", email="dup-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Twice", event_datetime=NOW + timedelta(days=4))
    user = await make_user(email="twice@example.com")
    await make_registration(event=event, user=user)
    await session.commit()

    r = await client.post(
        f"/api/v1/events/registration/{event.id}/register",
        json={"full_name": "Twice", "email": "twice@example.com", "phone": "9876500001"},
        headers=auth_header(await mint_token(user)),
    )
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == "ALREADY_REGISTERED"


async def test_a_club_with_a_long_name_can_be_read_back(client, session, make_club, make_user, mint_token):
    _, club = await make_club(name="Short", email="long-name@example.com")
    club.name = "The Very Long Named Robotics And Automation Society"
    await session.commit()
    viewer = await make_user(email="viewer-long@example.com")

    r = await client.get(f"/api/v1/clubs/info/{club.id}", headers=auth_header(await mint_token(viewer)))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "The Very Long Named Robotics And Automation Society"
