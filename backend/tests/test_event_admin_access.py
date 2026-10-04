"""Admin access to a club's events, and the guard that must survive it.

`ClubAuth` is `check_user_type(["club", "admin"])`, so an admin already got
past the router on every one of these endpoints -- and was then rejected by
`event.club.user_id != user_id`, which an admin's id never satisfies. The
result was a 403 on every event an admin tried to inspect, and no admin
visibility into registrations, analytics, ratings, or editing at all.

The rule now lives in one place (app/api/events/access.py) with an explicit
admin exemption. These tests cover both halves: an admin gets through, and a
club still cannot touch another club's event.
"""

import pytest

from tests.conftest import auth_header


@pytest.fixture
async def event_with_registrations(
    make_club, make_event, make_user, make_registration
):
    owner, club = await make_club(name="Host Club", email="hostclub@example.com")
    event = await make_event(club=club, name="Hosted Event", created_by=owner)
    attendee = await make_user(email="eventattendee@example.com")
    await make_registration(
        event=event,
        user=attendee,
        is_attended=True,
        additional_details={"College Name": "CET"},
    )
    return owner, club, event


@pytest.mark.asyncio
async def test_admin_can_read_registrations_for_any_event(
    client, make_admin, mint_token, event_with_registrations
):
    _, _, event = event_with_registrations
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/list", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


@pytest.mark.asyncio
async def test_admin_can_read_analytics_for_any_event(
    client, make_admin, mint_token, event_with_registrations
):
    _, _, event = event_with_registrations
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/analytics", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_registrations"] == 1
    assert body["total_institutions"] == 1


@pytest.mark.asyncio
async def test_admin_can_read_attendees_and_export(
    client, make_admin, mint_token, event_with_registrations
):
    _, _, event = event_with_registrations
    admin = await make_admin()
    token = await mint_token(admin)

    attendees = await client.get(
        f"/api/v1/events/registration/{event.id}/attendees", headers=auth_header(token)
    )
    assert attendees.status_code == 200, attendees.text

    export = await client.get(
        f"/api/v1/events/registration/{event.id}/export", headers=auth_header(token)
    )
    assert export.status_code == 200, export.text


@pytest.mark.asyncio
async def test_admin_can_read_ratings_for_any_event(
    client, make_admin, mint_token, event_with_registrations
):
    _, _, event = event_with_registrations
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/events/ratings/{event.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_admin_can_delete_an_event_of_any_club(
    client, make_admin, make_club, make_event, mint_token
):
    owner, club = await make_club(name="Delete Club", email="deleteclub@example.com")
    event = await make_event(club=club, name="Deletable", created_by=owner)
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.delete(
        f"/api/v1/events/delete/{event.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_an_event_with_registrations_is_protected_from_admins_too(
    client, make_admin, mint_token, event_with_registrations
):
    """
    Deleting an event that people have registered for is blocked by the
    service, not by the ownership check. Letting admins past the ownership
    rule must not let them past this one -- it is what stops someone's ticket
    being deleted out from under them.
    """
    _, _, event = event_with_registrations
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.delete(
        f"/api/v1/events/delete/{event.id}", headers=auth_header(token)
    )

    assert response.status_code == 400, response.text
    assert "registrations" in response.json()["message"]


@pytest.mark.asyncio
async def test_the_owning_club_still_has_access(
    client, mint_token, event_with_registrations
):
    """Widening for admins must not have narrowed anything for clubs."""
    owner, _, event = event_with_registrations
    token = await mint_token(owner)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/analytics", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_another_club_is_still_refused(
    client, make_club, mint_token, event_with_registrations
):
    """The guard this change could most easily have broken."""
    _, _, event = event_with_registrations
    intruder, _ = await make_club(name="Intruder", email="intruder@example.com")
    token = await mint_token(intruder)

    for path in (
        f"/api/v1/events/registration/{event.id}/list",
        f"/api/v1/events/registration/{event.id}/analytics",
        f"/api/v1/events/registration/{event.id}/export",
        f"/api/v1/events/ratings/{event.id}",
    ):
        response = await client.get(path, headers=auth_header(token))
        assert response.status_code == 403, f"{path} -> {response.status_code}"


@pytest.mark.asyncio
async def test_another_club_cannot_delete_the_event(
    client, make_club, mint_token, event_with_registrations
):
    _, _, event = event_with_registrations
    intruder, _ = await make_club(name="Intruder2", email="intruder2@example.com")
    token = await mint_token(intruder)

    response = await client.delete(
        f"/api/v1/events/delete/{event.id}", headers=auth_header(token)
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_an_ordinary_user_is_refused(
    client, make_user, mint_token, event_with_registrations
):
    _, _, event = event_with_registrations
    nosy = await make_user(email="nosyuser@example.com")
    token = await mint_token(nosy)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/analytics", headers=auth_header(token)
    )

    # Rejected by the role check before ownership is even considered.
    assert response.status_code in (401, 403), response.text
