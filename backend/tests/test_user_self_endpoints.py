"""Regression tests for the two self-service endpoints the mobile app calls.

Both were broken in production and both failed the same way -- a response
schema that could not serialise the rows the service handed it, raising a
ValidationError and returning 500:

  GET /user/following   ClubListResponse requires followers_count,
                        is_following and interests. The service returned bare
                        Clubs rows, which carry none of them, so this endpoint
                        failed on every call regardless of data.

  GET /user/my-events   UserEventList declares `is_online: bool`, but
                        events.is_online is nullable and most rows are NULL.
                        A default does not cover an explicit None, so the
                        endpoint failed for any user registered to such an
                        event.

Neither is reachable from the admin dashboard, so neither would have been
caught by the admin tests alone.
"""

import pytest

from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_following_returns_the_clubs_a_user_follows(
    client, session, make_club, make_user, mint_token
):
    _, club = await make_club(name="Follow Me", email="followme@example.com")
    follower = await make_user(email="selffollower@example.com")

    from app.api.clubs.models import ClubUsersLink

    session.add(ClubUsersLink(club_id=club.id, user_id=follower.id, is_following=True))
    await session.flush()

    token = await mint_token(follower)
    response = await client.get("/api/v1/user/following", headers=auth_header(token))

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [c["name"] for c in items] == ["Follow Me"]
    # The three fields whose absence caused the 500.
    assert items[0]["followers_count"] == 1
    assert items[0]["is_following"] is True
    assert "interests" in items[0]


@pytest.mark.asyncio
async def test_following_is_empty_rather_than_failing_for_a_new_user(
    client, make_user, mint_token
):
    loner = await make_user(email="loner@example.com")
    token = await mint_token(loner)

    response = await client.get("/api/v1/user/following", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_my_events_serialises_an_event_with_no_is_online_value(
    client, make_club, make_user, make_event, make_registration, mint_token
):
    owner, club = await make_club(name="Null Club", email="nullclub@example.com")
    attendee = await make_user(email="nullattendee@example.com")
    # is_online left unset, exactly as every pre-existing row has it.
    event = await make_event(club=club, name="Null Online", created_by=owner)
    await make_registration(event=event, user=attendee)

    token = await mint_token(attendee)
    response = await client.get(
        "/api/v1/user/my-events?is_attended=false", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    # Absent means "not an online event", not null.
    assert items[0]["event"]["is_online"] is False


@pytest.mark.asyncio
async def test_my_events_keeps_an_explicit_is_online(
    client, make_club, make_user, make_event, make_registration, mint_token
):
    owner, club = await make_club(name="Online Club", email="onlineclub@example.com")
    attendee = await make_user(email="onlineattendee@example.com")
    event = await make_event(
        club=club, name="Online Event", created_by=owner, is_online=True
    )
    await make_registration(event=event, user=attendee)

    token = await mint_token(attendee)
    response = await client.get(
        "/api/v1/user/my-events?is_attended=false", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["event"]["is_online"] is True
