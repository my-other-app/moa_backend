"""Covers the OptionalUserAuth fix: a guest who registered once carries a
valid access token whose user_type is "guest", not "app_user"/"admin". Before
the fix, OptionalUserAuth's role_checker only allowed ["app_user", "admin"]
even with optional=True, so presenting that guest token on a second
registration (for a different event) was rejected with 403 instead of being
treated as the same guest."""
import uuid

from tests.conftest import auth_header


async def test_guest_can_register_twice_with_the_token_from_the_first_registration(
    client, session, monkeypatch, make_club, make_event
):
    import app.api.events.registration.background_tasks as background_tasks_module

    monkeypatch.setattr(
        background_tasks_module, "send_email_with_attachment", lambda **kwargs: None
    )

    club_user, club = await make_club(
        name="Guest Regn Club", email=f"guestregnclub{uuid.uuid4()}@example.com"
    )
    event_a = await make_event(club=club, created_by=club_user, name="Event A")
    event_b = await make_event(club=club, created_by=club_user, name="Event B")

    guest_email = f"guest{uuid.uuid4()}@example.com"
    payload = {
        "full_name": "Guest Attendee",
        "email": guest_email,
        "phone": "9999999999",
    }

    first = await client.post(
        f"/api/v1/events/registration/{event_a.id}/register", json=payload
    )
    assert first.status_code == 200
    guest_token = first.json()["auth_token"]["access_token"]

    second = await client.post(
        f"/api/v1/events/registration/{event_b.id}/register",
        json=payload,
        headers=auth_header(guest_token),
    )
    assert second.status_code == 200
