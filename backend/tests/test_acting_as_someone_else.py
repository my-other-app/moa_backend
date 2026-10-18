"""Two more ways a caller could act as someone else, found while closing the
public registration form's."""
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select

from app.api.events.volunteer.models import Volunteer
from app.api.users.models import SigninProviders, UserTypes, Users
from app.config import settings
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


async def test_a_club_wide_volunteer_place_admits_only_its_volunteer(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    owner, club = await make_club(name="Gatekeepers", email="gatekeepers@example.com")
    event = await make_event(club=club, created_by=owner, name="Demo Day", event_datetime=NOW + timedelta(days=2))
    volunteer = await make_user(email="vol@example.com", username="vol")
    session.add(Volunteer(email=volunteer.email, club_id=club.id, event_id=None, user_id=volunteer.id, is_approved=True))
    attendee = await make_registration(event=event, user=await make_user(email="att@example.com", username="att"))
    await session.commit()

    # A session anyone can have: a fresh guest from the public form.
    r = await client.post(
        f"/api/v1/events/registration/{event.id}/register",
        json={"full_name": "Passer By", "email": f"passer-{uuid.uuid4().hex[:6]}@example.com"},
    )
    stranger = auth_header(r.json()["auth_token"]["access_token"])
    for path in ("validate", "checkin"):
        refused = await client.post(f"/api/v1/events/volunteer/{path}/{event.id}", json={"ticket_id": attendee.ticket_id}, headers=stranger)
        assert refused.status_code == 401, (path, refused.text)

    allowed = await client.post(
        f"/api/v1/events/volunteer/validate/{event.id}",
        json={"ticket_id": attendee.ticket_id},
        headers=auth_header(await mint_token(volunteer)),
    )
    assert allowed.status_code == 200, allowed.text


async def test_apple_sign_in_trusts_only_the_address_apple_signed(client, session, make_user, monkeypatch):
    victim = await make_user(email="victim@example.com", username="victim")
    victim.provider = SigninProviders.google
    await session.commit()
    real_decode = jwt.decode

    class Keys:
        def __init__(self, url, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token):
            return type("Key", (), {"key": "k"})()

    # A genuine Apple token with no address in it, and the victim's address
    # typed beside it.
    monkeypatch.setattr(jwt, "PyJWKClient", Keys)
    monkeypatch.setattr(jwt, "get_unverified_header", lambda token: {})
    monkeypatch.setattr(jwt, "decode", lambda *a, **kw: {"sub": "001122.attacker", "iss": "https://appleid.apple.com"})
    r = await client.post(
        "/api/v1/auth/apple",
        json={"identity_token": "t", "authorization_code": "c", "user_email": "victim@example.com"},
    )
    assert r.status_code == 200, r.text

    claims = real_decode(r.json()["access_token"], settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["user_id"] != victim.id
    account = await session.scalar(select(Users).where(Users.id == claims["user_id"]))
    assert account.email == "001122.attacker@privaterelay.appleid.com"
    assert account.user_type == UserTypes.app_user
