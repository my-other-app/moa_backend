"""Registration and attendance counts on the admin event list.

The v2 Events table is built around one signal: an event that has finished
with registrations and nobody marked present. Certificates and winners are
both blocked until the club marks attendance, so that row is the admin's
whole reason for opening the screen -- and the list endpoint reported neither
number, which made it impossible to see without opening every event.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header


def row_for(response, event_id):
    for item in response.json()["items"]:
        if item["id"] == event_id:
            return item
    raise AssertionError(f"event {event_id} not in the page")


@pytest.mark.asyncio
async def test_counts_registrations_and_attendance(
    client, session, make_admin, make_club, make_event, make_user, make_registration, mint_token
):
    admin = await make_admin()
    owner, club = await make_club(name="Circuit Society", email="circuit@example.com")
    event = await make_event(club=club, name="Hack Night", created_by=owner)

    for i in range(3):
        user = await make_user(email=f"attendee{i}@example.com")
        registration = await make_registration(event=event, user=user)
        # Two of the three turned up.
        registration.is_attended = i < 2
    await session.flush()
    token = await mint_token(admin)

    response = await client.get("/api/v1/events/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    row = row_for(response, event.id)
    assert row["registration_count"] == 3
    assert row["attended_count"] == 2


@pytest.mark.asyncio
async def test_an_event_nobody_attended_reports_zero_not_missing(
    client, session, make_admin, make_club, make_event, make_user, make_registration, mint_token
):
    """
    The row the screen exists for.

    Registrations above zero and attendance at zero is "the club never marked
    anyone present", which blocks certificates and winners. It has to be
    distinguishable from an event with no registrations at all.
    """
    admin = await make_admin()
    owner, club = await make_club(name="Drama Circle", email="drama@example.com")
    unmarked = await make_event(club=club, name="Street Play", created_by=owner)
    empty = await make_event(club=club, name="Nobody Came", created_by=owner)

    for i in range(4):
        user = await make_user(email=f"unmarked{i}@example.com")
        await make_registration(event=unmarked, user=user)
    await session.flush()
    token = await mint_token(admin)

    response = await client.get("/api/v1/events/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert row_for(response, unmarked.id)["registration_count"] == 4
    assert row_for(response, unmarked.id)["attended_count"] == 0
    assert row_for(response, empty.id)["registration_count"] == 0
    assert row_for(response, empty.id)["attended_count"] == 0


@pytest.mark.asyncio
async def test_a_deleted_registration_is_not_counted(
    client, session, make_admin, make_club, make_event, make_user, make_registration, mint_token
):
    admin = await make_admin()
    owner, club = await make_club(name="Quiz Club", email="quiz@example.com")
    event = await make_event(club=club, name="Quiz Bowl", created_by=owner)

    keep_user = await make_user(email="keep@example.com")
    await make_registration(event=event, user=keep_user)
    gone_user = await make_user(email="gone@example.com")
    cancelled = await make_registration(event=event, user=gone_user)
    cancelled.is_deleted = True
    await session.flush()
    token = await mint_token(admin)

    response = await client.get("/api/v1/events/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert row_for(response, event.id)["registration_count"] == 1


@pytest.mark.asyncio
async def test_counts_do_not_leak_between_events(
    client, session, make_admin, make_club, make_event, make_user, make_registration, mint_token
):
    """
    A grouped count keyed on the wrong column, or a join that fans out, gives
    every event on the page the same number -- which looks plausible and is
    wrong everywhere at once.
    """
    admin = await make_admin()
    owner, club = await make_club(name="Robotics", email="robotics@example.com")
    busy = await make_event(club=club, name="Line Follower Cup", created_by=owner)
    quiet = await make_event(club=club, name="Soldering Basics", created_by=owner)

    for i in range(5):
        user = await make_user(email=f"busy{i}@example.com")
        await make_registration(event=busy, user=user)
    single = await make_user(email="quiet0@example.com")
    await make_registration(event=quiet, user=single)
    await session.flush()
    token = await mint_token(admin)

    response = await client.get("/api/v1/events/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert row_for(response, busy.id)["registration_count"] == 5
    assert row_for(response, quiet.id)["registration_count"] == 1


@pytest.mark.asyncio
async def test_the_counts_survive_the_ended_filter(
    client, session, make_admin, make_club, make_event, make_user, make_registration, mint_token
):
    """The filter and the counts are applied in different places, so the one
    that narrows the rows must not drop the one that fills them."""
    admin = await make_admin()
    owner, club = await make_club(name="Film Society", email="film@example.com")
    past = await make_event(
        club=club,
        name="Film Fest",
        created_by=owner,
        event_datetime=datetime.now(timezone.utc) - timedelta(days=7),
    )
    user = await make_user(email="filmgoer@example.com")
    registration = await make_registration(event=past, user=user)
    registration.is_attended = True
    await session.flush()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/events/admin/list?is_ended=true", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    row = row_for(response, past.id)
    assert row["registration_count"] == 1
    assert row["attended_count"] == 1
