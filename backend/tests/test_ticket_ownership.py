"""Covers the ticket-detail IDOR fix: a user must not be able to fetch
another user's ticket, but the event's own club still can."""
from tests.conftest import auth_header


async def test_user_can_fetch_own_ticket(client, make_user, make_club, make_event, make_registration, mint_token):
    club_user, club = await make_club(name="Club A", email="clubA4@example.com")
    event = await make_event(club=club, created_by=club_user)
    user = await make_user(email="owner@example.com")
    registration = await make_registration(event=event, user=user)
    token = await mint_token(user)

    response = await client.get(
        f"/api/v1/events/tickets/{registration.ticket_id}", headers=auth_header(token)
    )
    assert response.status_code == 200


async def test_other_user_cannot_fetch_ticket(
    client, make_user, make_club, make_event, make_registration, mint_token
):
    club_user, club = await make_club(name="Club A", email="clubA5@example.com")
    event = await make_event(club=club, created_by=club_user)
    owner = await make_user(email="owner2@example.com")
    intruder = await make_user(email="intruder@example.com")
    registration = await make_registration(event=event, user=owner)
    token = await mint_token(intruder)

    response = await client.get(
        f"/api/v1/events/tickets/{registration.ticket_id}", headers=auth_header(token)
    )
    assert response.status_code == 403


async def test_owning_club_can_fetch_attendee_ticket(
    client, make_user, make_club, make_event, make_registration, mint_token
):
    club_user, club = await make_club(name="Club A", email="clubA6@example.com")
    event = await make_event(club=club, created_by=club_user)
    attendee = await make_user(email="attendee@example.com")
    registration = await make_registration(event=event, user=attendee)
    token = await mint_token(club_user)

    response = await client.get(
        f"/api/v1/events/tickets/{registration.ticket_id}", headers=auth_header(token)
    )
    assert response.status_code == 200
