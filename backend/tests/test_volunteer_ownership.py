"""Covers the volunteer-management IDOR fix: a club must not be able to
add/remove/list volunteers for another club's event."""
from tests.conftest import auth_header


async def test_club_can_add_volunteer_to_its_own_event(client, make_club, make_event, mint_token):
    club_a_user, club_a = await make_club(name="Club A", email="clubA@example.com")
    event = await make_event(club=club_a, created_by=club_a_user)
    token = await mint_token(club_a_user)

    response = await client.post(
        "/api/v1/events/volunteer/add",
        json={"email_id": "volunteer@example.com", "event_id": event.id, "full_name": "Vol Unteer"},
        headers=auth_header(token),
    )
    assert response.status_code == 200


async def test_club_cannot_add_volunteer_to_another_clubs_event(
    client, make_club, make_event, mint_token
):
    club_a_user, club_a = await make_club(name="Club A", email="clubA2@example.com")
    club_b_user, club_b = await make_club(name="Club B", email="clubB2@example.com")
    event = await make_event(club=club_b, created_by=club_b_user)
    token = await mint_token(club_a_user)

    response = await client.post(
        "/api/v1/events/volunteer/add",
        json={"email_id": "intruder@example.com", "event_id": event.id, "full_name": "Intruder"},
        headers=auth_header(token),
    )
    assert response.status_code == 403


async def test_club_cannot_list_volunteers_for_another_clubs_event(
    client, make_club, make_event, mint_token
):
    club_a_user, club_a = await make_club(name="Club A", email="clubA3@example.com")
    club_b_user, club_b = await make_club(name="Club B", email="clubB3@example.com")
    event = await make_event(club=club_b, created_by=club_b_user)
    token = await mint_token(club_a_user)

    response = await client.get(
        f"/api/v1/events/volunteer/list/{event.id}", headers=auth_header(token)
    )
    assert response.status_code == 403
