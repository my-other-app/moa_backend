"""Tests for the organisation admin endpoints.

Two things were missing and one was wrong:

- There was no way to fetch a single organisation. /orgs/list returns every
  row with no filter, so a detail page had to pull all of them and pick one.
- The admin club list could not be filtered by organisation, so an org page
  could only show its clubs through the public /clubs/list, which hides
  unverified ones -- exactly the clubs an admin needs to see.
- /orgs/block toggled and ignored its request body. The dashboard sends
  {"is_blocked": ...} and caches the list for ten minutes, so clicking
  "Block" on a stale row would have unblocked it.
"""

import pytest

from tests.conftest import auth_header


@pytest.fixture
async def org(session):
    from app.api.orgs.models import Organizations, OrgTypes

    row = Organizations(name="Test University", type=OrgTypes.college)
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_detail_returns_one_organisation(client, make_admin, mint_token, org):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(f"/api/v1/orgs/{org.id}", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Test University"


@pytest.mark.asyncio
async def test_list_is_not_shadowed_by_the_detail_route(
    client, make_admin, mint_token, org
):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get("/api/v1/orgs/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_missing_organisation_is_a_404(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get("/api/v1/orgs/999999", headers=auth_header(token))

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_blocking_with_an_explicit_value_is_idempotent(
    client, make_admin, mint_token, org
):
    """Sending the same value twice must not flip it back."""
    admin = await make_admin()
    token = await mint_token(admin)

    first = await client.put(
        f"/api/v1/orgs/block/{org.id}",
        headers=auth_header(token),
        json={"is_blocked": True},
    )
    assert first.status_code == 200, first.text
    assert first.json()["is_blocked"] is True

    second = await client.put(
        f"/api/v1/orgs/block/{org.id}",
        headers=auth_header(token),
        json={"is_blocked": True},
    )
    assert second.status_code == 200, second.text
    assert second.json()["is_blocked"] is True


@pytest.mark.asyncio
async def test_unblocking_with_an_explicit_value(client, make_admin, mint_token, org):
    admin = await make_admin()
    token = await mint_token(admin)

    await client.put(
        f"/api/v1/orgs/block/{org.id}",
        headers=auth_header(token),
        json={"is_blocked": True},
    )
    response = await client.put(
        f"/api/v1/orgs/block/{org.id}",
        headers=auth_header(token),
        json={"is_blocked": False},
    )

    assert response.json()["is_blocked"] is False


@pytest.mark.asyncio
async def test_an_empty_body_still_toggles(client, make_admin, mint_token, org):
    """Older callers sent no body and relied on the toggle."""
    admin = await make_admin()
    token = await mint_token(admin)

    first = await client.put(
        f"/api/v1/orgs/block/{org.id}", headers=auth_header(token)
    )
    assert first.json()["is_blocked"] is True

    second = await client.put(
        f"/api/v1/orgs/block/{org.id}", headers=auth_header(token)
    )
    assert second.json()["is_blocked"] is False


@pytest.mark.asyncio
async def test_admin_club_list_filters_by_organisation(
    client, session, make_admin, make_club, mint_token, org
):
    admin = await make_admin()
    _, mine = await make_club(name="Mine", email="mineclub@example.com")
    await make_club(name="Theirs", email="theirsclub@example.com")
    mine.org_id = org.id
    await session.flush()

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/clubs/admin/list?org_id={org.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    names = [c["name"] for c in response.json()["items"]]
    assert names == ["Mine"]


@pytest.mark.asyncio
async def test_org_club_list_includes_unverified_clubs(
    client, session, make_admin, make_club, mint_token, org
):
    """The public list hides these; they are the ones an admin must see."""
    admin = await make_admin()
    _, pending = await make_club(
        name="Pending", email="pendingorg@example.com", is_verified=False
    )
    pending.org_id = org.id
    await session.flush()

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/clubs/admin/list?org_id={org.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert [c["name"] for c in response.json()["items"]] == ["Pending"]


@pytest.mark.asyncio
async def test_analytics_counts_clubs_events_and_followers(
    client, session, make_admin, make_club, make_user, make_event, mint_token, org
):
    admin = await make_admin()
    owner, club = await make_club(name="Counted", email="counted@example.com")
    club.org_id = org.id
    await session.flush()
    await make_event(club=club, name="Counted Event", created_by=owner)

    follower = await make_user(email="orgfollower@example.com")
    from app.api.clubs.models import ClubUsersLink

    session.add(ClubUsersLink(club_id=club.id, user_id=follower.id, is_following=True))
    await session.flush()

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/orgs/admin/{org.id}/analytics", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_clubs"] == 1
    assert body["total_events"] == 1
    assert body["total_followers"] == 1
