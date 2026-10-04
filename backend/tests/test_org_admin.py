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


@pytest.mark.asyncio
async def test_club_list_reports_a_total_so_it_can_paginate(
    client, session, make_admin, make_club, mint_token, org
):
    """
    get_all_clubs returned rows and no count, so paginated_response fell back
    to "is there an extra row" and reported total: null. Every club list was
    therefore stuck on its first page -- including an organisation's, which
    showed at most ten of its clubs with no way to reach the rest.
    """
    admin = await make_admin()
    for i in range(3):
        _, club = await make_club(name=f"Club {i}", email=f"orgclub{i}@example.com")
        club.org_id = org.id
    await session.flush()

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/clubs/admin/list?org_id={org.id}&limit=2", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3, "total must count every match, not just this page"
    assert len(body["items"]) <= 3


@pytest.mark.asyncio
async def test_club_total_respects_the_filters(
    client, session, make_admin, make_club, mint_token, org
):
    """A total that ignores the filter makes the pager offer empty pages."""
    admin = await make_admin()
    _, mine = await make_club(name="In Org", email="inorg@example.com")
    mine.org_id = org.id
    await make_club(name="Not In Org", email="notinorg@example.com")
    await session.flush()

    token = await mint_token(admin)
    scoped = await client.get(
        f"/api/v1/clubs/admin/list?org_id={org.id}", headers=auth_header(token)
    )
    everything = await client.get(
        "/api/v1/clubs/admin/list", headers=auth_header(token)
    )

    assert scoped.json()["total"] == 1
    assert everything.json()["total"] > scoped.json()["total"]


@pytest.mark.asyncio
async def test_events_can_be_listed_by_organisation(
    client, session, make_admin, make_club, make_event, mint_token, org
):
    """The org page reported an event count with no way to see the events."""
    admin = await make_admin()
    owner, mine = await make_club(name="Hosting", email="hostingorg@example.com")
    mine.org_id = org.id
    await session.flush()
    await make_event(club=mine, name="Ours", created_by=owner)

    other_owner, other = await make_club(name="Outside", email="outsideorg@example.com")
    await make_event(club=other, name="Theirs", created_by=other_owner)

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/events/admin/list?org_id={org.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert [e["name"] for e in response.json()["items"]] == ["Ours"]
    assert response.json()["total"] == 1


@pytest.mark.asyncio
async def test_event_count_matches_the_analytics_figure(
    client, session, make_admin, make_club, make_event, mint_token, org
):
    """The headline number and the list below it must agree."""
    admin = await make_admin()
    owner, club = await make_club(name="Agreeing", email="agreeing@example.com")
    club.org_id = org.id
    await session.flush()
    for i in range(3):
        await make_event(club=club, name=f"Event {i}", created_by=owner)

    token = await mint_token(admin)
    analytics = await client.get(
        f"/api/v1/orgs/admin/{org.id}/analytics", headers=auth_header(token)
    )
    listing = await client.get(
        f"/api/v1/events/admin/list?org_id={org.id}", headers=auth_header(token)
    )

    assert analytics.json()["total_events"] == listing.json()["total"] == 3


@pytest.mark.asyncio
async def test_club_count_matches_the_analytics_figure(
    client, session, make_admin, make_club, mint_token, org
):
    admin = await make_admin()
    for i in range(2):
        _, club = await make_club(name=f"Counted {i}", email=f"counted{i}@example.com")
        club.org_id = org.id
    await session.flush()

    token = await mint_token(admin)
    analytics = await client.get(
        f"/api/v1/orgs/admin/{org.id}/analytics", headers=auth_header(token)
    )
    listing = await client.get(
        f"/api/v1/clubs/admin/list?org_id={org.id}", headers=auth_header(token)
    )

    assert analytics.json()["total_clubs"] == listing.json()["total"] == 2
