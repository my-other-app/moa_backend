"""Regression tests for GET /events/info/{event_id}.

This endpoint was returning 500 for every event in production. The cause was
`jsonable_encoder(result)` in the router: `get_event` returns
`event.__dict__`, whose "club" value is a live Clubs ORM instance, and
jsonable_encoder walks every attribute of it. Clubs.org / followers / events /
notes / socials are not eagerly loaded, so walking them triggered a lazy load
outside the async greenlet context and raised
sqlalchemy.exc.MissingGreenlet.

EventClubDetail only declares id, name, slug and logo, so letting FastAPI's
response_model do the conversion reads just those four attributes and never
touches the unloaded relationships.
"""

import pytest

from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_event_detail_returns_200_anonymously(client, make_club, make_event):
    owner, club = await make_club(name="Detail Club", email="detail@example.com")
    event = await make_event(club=club, name="Detail Event", created_by=owner)

    response = await client.get(f"/api/v1/events/info/{event.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == event.id
    assert body["name"] == "Detail Event"


@pytest.mark.asyncio
async def test_event_detail_serialises_the_nested_club(client, make_club, make_event):
    """The club relationship is the one that triggered MissingGreenlet."""
    owner, club = await make_club(name="Nested Club", email="nested@example.com")
    event = await make_event(club=club, name="Has A Club", created_by=owner)

    response = await client.get(f"/api/v1/events/info/{event.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["club"] is not None
    assert body["club"]["name"] == "Nested Club"
    # Only the declared fields should be emitted (the four, and whether the
    # form offers the club's updates on WhatsApp); pulling the whole ORM
    # object back in is what broke this endpoint.
    assert set(body["club"]).issubset({"id", "name", "slug", "logo", "whatsapp_updates"})
    assert body["club"]["whatsapp_updates"] is False


@pytest.mark.asyncio
async def test_event_detail_404s_for_a_missing_event(client):
    response = await client.get("/api/v1/events/info/99999999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_event_detail_works_for_an_authenticated_user(
    client, make_club, make_event, make_user, mint_token
):
    """The regression this file exists for.

    get_event's router first calls increment_event_page_view, which ends in
    `await session.commit()`. With the session factory's default
    expire_on_commit=True, that commit expires every instance in the identity
    map -- including the Users object the auth dependency just loaded. The very
    next line evaluates `user.id` to pass user_id into the service, which
    triggers a synchronous attribute refresh on an async engine and raises
    sqlalchemy.exc.MissingGreenlet.

    Anonymous requests never touch `user`, which is why this endpoint returned
    200 publicly and 500 for every signed-in user -- verified against
    production before the fix.
    """
    owner, club = await make_club(name="Auth Club", email="authclub@example.com")
    event = await make_event(club=club, name="Auth Event", created_by=owner)
    viewer = await make_user(email="viewer@example.com")
    token = await mint_token(viewer)

    response = await client.get(
        f"/api/v1/events/info/{event.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["id"] == event.id


# A "two consecutive authenticated reads" test was tried here and removed.
# It cannot work in this harness: conftest overrides get_session to hand every
# request the *same* session so test setup and requests share one transaction,
# whereas production builds a session per request. The first request's commit
# therefore leaves the shared session inactive and the second fails with
# InvalidRequestError -- an artefact of the fixture, not of the endpoint.
# Verifying repeat reads needs a per-request session fixture, which would be a
# larger change to the suite than this fix warrants.


@pytest.mark.asyncio
async def test_event_detail_treats_a_dead_token_as_a_visitor(client, make_club, make_event):
    """The event site keeps the token a registration handed it. Once that token
    has expired or been revoked, the public event page must still load as it
    would for anyone else, not fail with a 401 it never needed."""
    owner, club = await make_club(name="Visitor Club", email="visitor@example.com")
    event = await make_event(club=club, name="Open Evening", created_by=owner)

    expired = await client.get(f"/api/v1/events/info/{event.id}", headers=auth_header("not-a-real-token"))
    assert expired.status_code == 200, expired.text
    assert expired.json()["name"] == "Open Evening"

    # A route that requires a sign-in still refuses the same token.
    refused = await client.get("/api/v1/clubs/dashboard", headers=auth_header("not-a-real-token"))
    assert refused.status_code == 401
