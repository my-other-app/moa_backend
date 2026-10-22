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


async def test_a_volunteer_with_an_uploaded_photo_is_listed_with_a_link(
    client, session, make_club, make_event, make_user, mint_token
):
    """The photo column reads back as links by size; the list's
    `profile_pic` is one link. A dict there failed the response model, and
    the whole volunteer list was a 500."""
    from app.api.events.volunteer.models import Volunteer
    from app.api.users.models import UserProfiles

    club_user, club = await make_club(name="Photo Club", email="photoclub@example.com")
    event = await make_event(club=club, created_by=club_user)
    person = await make_user(email="snap@example.com")
    photo = UserProfiles.__table__.c.profile_pic.type
    session.add(UserProfiles(
        user_id=person.id, full_name="Snap Shot",
        profile_pic=f"{photo.base_path.rstrip('/')}/v-{'a' * 32}.png",
    ))
    session.add(Volunteer(email="snap@example.com", user_id=person.id, event_id=event.id, is_approved=True))
    await session.commit()
    token = await mint_token(club_user)
    event_id = event.id
    session.expunge_all()  # read back from the database, as in production

    response = await client.get(f"/api/v1/events/volunteer/list/{event_id}", headers=auth_header(token))

    assert response.status_code == 200, response.text
    [row] = [v for v in response.json() if v["email"] == "snap@example.com"]
    assert isinstance(row["profile_pic"], str) and row["profile_pic"].startswith("http")
    assert row["full_name"] == "Snap Shot"
