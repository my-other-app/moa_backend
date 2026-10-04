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


@pytest.mark.asyncio
async def test_a_user_can_be_suspended_and_reinstated(
    client, make_admin, make_user, mint_token
):
    """
    Suspension did not exist: the only lever for an abusive account was
    delete, which is permanent in practice and takes their data with it.
    """
    admin = await make_admin()
    target = await make_user(email="suspendme@example.com")
    token = await mint_token(admin)

    suspended = await client.put(
        f"/api/v1/user/admin/{target.id}/active",
        headers=auth_header(token),
        json={"is_active": False, "reason": "spam"},
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["is_active"] is False
    assert suspended.json()["suspended_reason"] == "spam"

    reinstated = await client.put(
        f"/api/v1/user/admin/{target.id}/active",
        headers=auth_header(token),
        json={"is_active": True},
    )
    assert reinstated.json()["is_active"] is True
    # The reason is cleared, or a reinstated account still reads as guilty.
    assert reinstated.json()["suspended_reason"] is None


@pytest.mark.asyncio
async def test_suspension_takes_effect_on_the_next_request(
    client, make_admin, make_user, mint_token
):
    """
    An already-issued token must stop working immediately. If suspension only
    applied at sign-in, the user would carry on until their session expired.
    """
    admin = await make_admin()
    target = await make_user(email="stillin@example.com")
    victim_token = await mint_token(target)
    admin_token = await mint_token(admin)

    before = await client.get("/api/v1/auth/me", headers=auth_header(victim_token))
    assert before.status_code == 200

    await client.put(
        f"/api/v1/user/admin/{target.id}/active",
        headers=auth_header(admin_token),
        json={"is_active": False, "reason": "abuse"},
    )

    after = await client.get("/api/v1/auth/me", headers=auth_header(victim_token))
    assert after.status_code in (401, 403), after.text


@pytest.mark.asyncio
async def test_an_admin_cannot_suspend_themselves(client, make_admin, mint_token):
    """Reinstating needs an admin, so this is a one-way lockout."""
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.put(
        f"/api/v1/user/admin/{admin.id}/active",
        headers=auth_header(token),
        json={"is_active": False},
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_suspension_is_recorded_in_the_audit_log(
    client, make_admin, make_user, mint_token
):
    admin = await make_admin()
    target = await make_user(email="logged@example.com")
    token = await mint_token(admin)

    await client.put(
        f"/api/v1/user/admin/{target.id}/active",
        headers=auth_header(token),
        json={"is_active": False, "reason": "repeated reports"},
    )

    log = await client.get(
        "/api/v1/admin/audit-log?action=user.suspend", headers=auth_header(token)
    )
    rows = log.json()["items"]
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "repeated reports"


@pytest.mark.asyncio
async def test_only_an_admin_can_suspend(client, make_user, mint_token):
    nosy = await make_user(email="nosysuspend@example.com")
    victim = await make_user(email="victimsuspend@example.com")
    token = await mint_token(nosy)

    response = await client.put(
        f"/api/v1/user/admin/{victim.id}/active",
        headers=auth_header(token),
        json={"is_active": False},
    )

    assert response.status_code in (401, 403), response.text
