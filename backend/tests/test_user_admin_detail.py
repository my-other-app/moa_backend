"""Tests for the admin user-detail endpoints.

The admin dashboard could list users but not open one: there was no endpoint
that answered "what has this user done". `/user/following` and
`/user/my-events` exist, but both read the *caller's* id, so an admin asking
about someone else got their own data back at best.

These three routes are admin-scoped and take the user id as a path parameter.
"""

import pytest

from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_detail_reports_activity_counts(
    client,
    session,
    make_admin,
    make_club,
    make_user,
    make_event,
    make_registration,
    mint_token,
):
    admin = await make_admin()
    owner, club = await make_club(name="Stats Club", email="statsclub@example.com")
    subject = await make_user(email="subject@example.com")

    attended = await make_event(club=club, name="Attended", created_by=owner)
    won = await make_event(club=club, name="Won", created_by=owner)
    plain = await make_event(club=club, name="Plain", created_by=owner)
    await make_registration(event=attended, user=subject, is_attended=True)
    await make_registration(
        event=won, user=subject, is_attended=True, is_won=True, is_paid=True,
        paid_amount=250,
    )
    await make_registration(event=plain, user=subject)

    from app.api.clubs.models import ClubUsersLink

    session.add(ClubUsersLink(club_id=club.id, user_id=subject.id, is_following=True))
    await session.flush()

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/user/admin/{subject.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    stats = response.json()["stats"]
    assert stats["events_registered"] == 3
    assert stats["events_attended"] == 2
    assert stats["events_won"] == 1
    assert stats["clubs_following"] == 1
    assert stats["total_paid"] == 250


@pytest.mark.asyncio
async def test_detail_lists_the_clubs_a_user_follows(
    client, session, make_admin, make_club, make_user, mint_token
):
    admin = await make_admin()
    _, club = await make_club(name="Followed Club", email="followed@example.com")
    subject = await make_user(email="follower@example.com")

    from app.api.clubs.models import ClubUsersLink

    session.add(ClubUsersLink(club_id=club.id, user_id=subject.id, is_following=True))
    await session.flush()

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/user/admin/{subject.id}/clubs", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Followed Club"


@pytest.mark.asyncio
async def test_event_list_is_unfiltered_by_default(
    client, make_admin, make_club, make_user, make_event, make_registration, mint_token
):
    """
    The self-service /my-events defaults its filters to False, which the
    service treats as "where is_attended = false" -- so it hides attended
    events from a list whose own summary says it returns them. The admin route
    defaults to None instead, meaning no filter.
    """
    admin = await make_admin()
    owner, club = await make_club(name="Filter Club", email="filterclub@example.com")
    subject = await make_user(email="filtersubject@example.com")

    one = await make_event(club=club, name="One", created_by=owner)
    two = await make_event(club=club, name="Two", created_by=owner)
    await make_registration(event=one, user=subject, is_attended=True)
    await make_registration(event=two, user=subject, is_attended=False)

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/user/admin/{subject.id}/events", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2


@pytest.mark.asyncio
async def test_event_list_filters_when_asked(
    client, make_admin, make_club, make_user, make_event, make_registration, mint_token
):
    admin = await make_admin()
    owner, club = await make_club(name="Attend Club", email="attendclub@example.com")
    subject = await make_user(email="attendsubject@example.com")

    one = await make_event(club=club, name="One", created_by=owner)
    two = await make_event(club=club, name="Two", created_by=owner)
    await make_registration(event=one, user=subject, is_attended=True)
    await make_registration(event=two, user=subject, is_attended=False)

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/user/admin/{subject.id}/events?is_attended=true",
        headers=auth_header(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


@pytest.mark.asyncio
async def test_admin_list_still_resolves(client, make_admin, mint_token):
    """`/admin/list` must not be swallowed by `/admin/{user_id}`."""
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get("/api/v1/user/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert "items" in response.json()


@pytest.mark.asyncio
async def test_a_non_admin_cannot_read_another_user(
    client, make_user, mint_token
):
    """The whole point is cross-user visibility, so the guard matters."""
    nosy = await make_user(email="nosy@example.com")
    subject = await make_user(email="private@example.com")
    token = await mint_token(nosy)

    response = await client.get(
        f"/api/v1/user/admin/{subject.id}", headers=auth_header(token)
    )

    assert response.status_code in (401, 403), response.text


@pytest.mark.asyncio
async def test_missing_user_is_a_404(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get("/api/v1/user/admin/999999", headers=auth_header(token))

    assert response.status_code == 404, response.text
