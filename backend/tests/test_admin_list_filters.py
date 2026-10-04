"""Filtering and ordering on the admin list endpoints.

Every admin list had a hard-coded `order_by` and accepted nothing but
`search`, so the dashboard could not filter or sort any of its tables.

The ordering goes through one helper with a per-endpoint whitelist, so an
unrecognised sort key falls back to the default rather than reaching the
query -- covered here, since that is the part that must not be got wrong.
"""

import pytest

from tests.conftest import auth_header


def names(response, key="full_name"):
    return [item[key] for item in response.json()["items"]]


@pytest.mark.asyncio
async def test_users_filter_by_type(client, make_admin, make_user, make_club, mint_token):
    admin = await make_admin()
    await make_user(email="plain@example.com")
    await make_club(name="A Club", email="filterclub@example.com")
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/list?user_type=admin", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    types = {item["user_type"] for item in response.json()["items"]}
    assert types == {"admin"}


@pytest.mark.asyncio
async def test_users_filter_narrows_the_total_too(
    client, make_admin, make_user, mint_token
):
    """A filtered total that counts unfiltered rows breaks pagination."""
    admin = await make_admin()
    for i in range(3):
        await make_user(email=f"counted{i}@example.com")
    token = await mint_token(admin)

    unfiltered = await client.get(
        "/api/v1/user/admin/list", headers=auth_header(token)
    )
    filtered = await client.get(
        "/api/v1/user/admin/list?user_type=admin", headers=auth_header(token)
    )

    assert filtered.json()["total"] < unfiltered.json()["total"]
    assert filtered.json()["total"] == len(filtered.json()["items"])


@pytest.mark.asyncio
async def test_users_sort_by_name_ascending(
    client, session, make_admin, make_user, mint_token
):
    admin = await make_admin()
    for email, name in [
        ("zed@example.com", "Zed"),
        ("amy@example.com", "Amy"),
        ("mia@example.com", "Mia"),
    ]:
        user = await make_user(email=email)
        user.full_name = name
    await session.flush()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/list?user_type=app_user&sort=name&order=asc",
        headers=auth_header(token),
    )

    assert response.status_code == 200, response.text
    assert names(response) == ["Amy", "Mia", "Zed"]


@pytest.mark.asyncio
async def test_users_sort_order_reverses(
    client, session, make_admin, make_user, mint_token
):
    admin = await make_admin()
    for email, name in [("b@example.com", "Bea"), ("a@example.com", "Ann")]:
        user = await make_user(email=email)
        user.full_name = name
    await session.flush()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/list?user_type=app_user&sort=name&order=desc",
        headers=auth_header(token),
    )

    assert names(response) == ["Bea", "Ann"]


@pytest.mark.asyncio
async def test_an_unknown_sort_key_falls_back_instead_of_failing(
    client, make_admin, make_user, mint_token
):
    """A stale bookmark or a typo must not 422, and must not reach the query."""
    admin = await make_admin()
    await make_user(email="fallback@example.com")
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/list?sort=not_a_column;DROP+TABLE+users",
        headers=auth_header(token),
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) >= 1


@pytest.mark.asyncio
async def test_clubs_filter_by_verification(
    client, make_admin, make_club, mint_token
):
    admin = await make_admin()
    await make_club(name="Approved", email="approved@example.com", is_verified=True)
    await make_club(name="Waiting", email="waiting@example.com", is_verified=False)
    token = await mint_token(admin)

    pending = await client.get(
        "/api/v1/clubs/admin/list?is_verified=false", headers=auth_header(token)
    )

    assert pending.status_code == 200, pending.text
    assert names(pending, "name") == ["Waiting"]


@pytest.mark.asyncio
async def test_clubs_sort_by_name(client, make_admin, make_club, mint_token):
    admin = await make_admin()
    await make_club(name="Beta", email="beta@example.com")
    await make_club(name="Alpha", email="alpha@example.com")
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/clubs/admin/list?sort=name&order=asc", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert names(response, "name") == ["Alpha", "Beta"]


@pytest.mark.asyncio
async def test_events_filter_by_club(
    client, make_admin, make_club, make_event, mint_token
):
    admin = await make_admin()
    owner_a, club_a = await make_club(name="Club A", email="clubaev@example.com")
    owner_b, club_b = await make_club(name="Club B", email="clubbev@example.com")
    await make_event(club=club_a, name="A Event", created_by=owner_a)
    await make_event(club=club_b, name="B Event", created_by=owner_b)
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/events/admin/list?club_id={club_a.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert names(response, "name") == ["A Event"]


@pytest.mark.asyncio
async def test_events_filter_by_past_or_upcoming(
    client, make_admin, make_club, make_event, mint_token
):
    from datetime import datetime, timedelta, timezone

    admin = await make_admin()
    owner, club = await make_club(name="Timed", email="timedclub@example.com")
    await make_event(
        club=club,
        name="Long Over",
        created_by=owner,
        event_datetime=datetime.now(timezone.utc) - timedelta(days=30),
    )
    await make_event(
        club=club,
        name="Coming Up",
        created_by=owner,
        event_datetime=datetime.now(timezone.utc) + timedelta(days=30),
    )
    token = await mint_token(admin)

    past = await client.get(
        "/api/v1/events/admin/list?is_ended=true", headers=auth_header(token)
    )
    upcoming = await client.get(
        "/api/v1/events/admin/list?is_ended=false", headers=auth_header(token)
    )

    assert names(past, "name") == ["Long Over"]
    assert names(upcoming, "name") == ["Coming Up"]


@pytest.mark.asyncio
async def test_events_sort_by_date(
    client, make_admin, make_club, make_event, mint_token
):
    from datetime import datetime, timedelta, timezone

    admin = await make_admin()
    owner, club = await make_club(name="Sorted", email="sortedclub@example.com")
    base = datetime.now(timezone.utc) + timedelta(days=1)
    await make_event(
        club=club, name="Later", created_by=owner, event_datetime=base + timedelta(days=5)
    )
    await make_event(
        club=club, name="Sooner", created_by=owner, event_datetime=base
    )
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/events/admin/list?sort=date&order=asc", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert names(response, "name") == ["Sooner", "Later"]


@pytest.fixture
async def interest(session):
    from app.api.interests.models import InterestCategory, Interests

    category = InterestCategory(name="Academic")
    session.add(category)
    await session.flush()
    row = Interests(name="Codng", category_id=category.id)
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_an_interest_can_be_renamed(client, make_admin, mint_token, interest):
    """
    Interests were create-only, so a typo was permanent -- and they appear on
    every club and user profile.
    """
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.put(
        f"/api/v1/interests/{interest.id}",
        headers=auth_header(token),
        json={"name": "Coding", "category_id": interest.category_id},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Coding"


@pytest.mark.asyncio
async def test_deleting_an_interest_detaches_it_from_clubs(
    client, session, make_admin, make_club, mint_token, interest
):
    """The link rows reference it, so they have to go with it."""
    from app.api.clubs.models import ClubInterestsLink

    admin = await make_admin()
    _, club = await make_club(name="Interested", email="interested@example.com")
    session.add(ClubInterestsLink(club_id=club.id, interest_id=interest.id))
    await session.flush()
    token = await mint_token(admin)

    response = await client.delete(
        f"/api/v1/interests/{interest.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["detached_from"] == 1

    remaining = await client.get("/api/v1/interests/list", headers=auth_header(token))
    names = [i["name"] for c in remaining.json() for i in c["interests"]]
    assert "Codng" not in names


@pytest.mark.asyncio
async def test_interest_management_is_admin_only(client, make_user, mint_token, interest):
    nosy = await make_user(email="nosyinterest@example.com")
    token = await mint_token(nosy)

    response = await client.delete(
        f"/api/v1/interests/{interest.id}", headers=auth_header(token)
    )

    assert response.status_code in (401, 403), response.text
