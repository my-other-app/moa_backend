"""The admin Overview aggregate.

One endpoint stands in for roughly fifteen. The risks are correspondingly
concentrated: a soft-deleted row counted here disagrees with every list screen,
a fan-out join inflates every per-club figure at once, and an empty platform
must render rather than divide by zero.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.conftest import auth_header


async def overview(client, token, query=""):
    response = await client.get(
        f"/api/v1/admin/overview{query}", headers=auth_header(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_an_empty_platform_renders_rather_than_dividing_by_zero(
    client, make_admin, mint_token
):
    """
    Every average has a denominator that is zero before anyone signs up. This
    is the state a brand-new deployment is in, so it has to be the one state
    that definitely works.
    """
    admin = await make_admin()
    token = await mint_token(admin)

    data = await overview(client, token)

    assert data["totals"]["events"] == 0
    assert data["averages"]["registrations_per_event"] == 0
    assert data["averages"]["view_to_signup"] == 0
    assert data["averages"]["average_rating"] == 0
    assert data["money"]["retained_share"] == 0
    assert data["top_clubs"] == []


@pytest.mark.asyncio
async def test_soft_deleted_rows_are_excluded_everywhere(
    client, session, make_admin, make_club, make_event, mint_token
):
    """
    Every list endpoint filters is_deleted. A total here that does not will
    disagree with the table it links to on the same screen.
    """
    admin = await make_admin()
    _, kept_club = await make_club(name="Kept", email="kept-ov@example.com", is_verified=True)
    owner, gone_club = await make_club(name="Gone", email="gone-ov@example.com", is_verified=True)
    await make_event(club=kept_club, name="Kept event", created_by=owner)
    gone_event = await make_event(club=kept_club, name="Gone event", created_by=owner)

    gone_club.is_deleted = True
    gone_event.is_deleted = True
    await session.flush()
    token = await mint_token(admin)

    data = await overview(client, token)

    assert data["totals"]["clubs"] == 1
    assert data["totals"]["verified_clubs"] == 1
    assert data["totals"]["events"] == 1


@pytest.mark.asyncio
async def test_pending_clubs_are_derived_not_counted_twice(
    client, make_admin, make_club, mint_token
):
    admin = await make_admin()
    await make_club(name="A", email="a-ov@example.com", is_verified=True)
    await make_club(name="B", email="b-ov@example.com", is_verified=False)
    token = await mint_token(admin)

    totals = (await overview(client, token))["totals"]

    assert totals["clubs"] == 2
    assert totals["verified_clubs"] == 1
    assert totals["pending_clubs"] == 1


@pytest.mark.asyncio
async def test_per_club_figures_are_not_inflated_by_a_fan_out(
    client, session, make_admin, make_club, make_event, make_user, make_registration, mint_token
):
    """
    Followers, registrations and revenue are three one-to-many relationships on
    the same club. Joining all three in one statement multiplies the rows and
    every figure comes back as a product of the others -- the exact trap the
    correlated subqueries avoid. With 2 followers and 2 registrations a fan-out
    reports 4 of each.
    """
    from app.api.clubs.models import ClubUsersLink

    admin = await make_admin()
    owner, club = await make_club(name="Measured", email="measured@example.com")
    event = await make_event(club=club, name="E", created_by=owner)

    for i in range(2):
        follower = await make_user(email=f"fan{i}-ov@example.com")
        session.add(ClubUsersLink(club_id=club.id, user_id=follower.id, is_following=True))
        attendee = await make_user(email=f"att{i}-ov@example.com")
        await make_registration(event=event, user=attendee)
    await session.flush()
    token = await mint_token(admin)

    top = (await overview(client, token))["top_clubs"]
    row = next(c for c in top if c["name"] == "Measured")

    assert row["followers"] == 2
    assert row["registrations"] == 2


@pytest.mark.asyncio
async def test_top_clubs_can_be_ordered_by_each_metric(
    client, session, make_admin, make_club, make_user, mint_token
):
    from app.api.clubs.models import ClubUsersLink

    admin = await make_admin()
    _, quiet = await make_club(name="Quiet", email="quiet-ov@example.com")
    _, loud = await make_club(name="Loud", email="loud-ov@example.com")
    for i in range(3):
        follower = await make_user(email=f"loudfan{i}@example.com")
        session.add(ClubUsersLink(club_id=loud.id, user_id=follower.id, is_following=True))
    await session.flush()
    token = await mint_token(admin)

    by_followers = (await overview(client, token, "?top_by=followers"))["top_clubs"]

    assert by_followers[0]["name"] == "Loud"


@pytest.mark.asyncio
async def test_an_unknown_sort_falls_back_instead_of_failing(
    client, make_admin, make_club, mint_token
):
    # A stale bookmark must not 500, and must not reach the query.
    admin = await make_admin()
    await make_club(name="C", email="c-ov@example.com")
    token = await mint_token(admin)

    data = await overview(client, token, "?top_by=not_a_column;DROP TABLE clubs")

    assert len(data["top_clubs"]) == 1


@pytest.mark.asyncio
async def test_the_funnel_only_narrows(
    client, session, make_admin, make_club, make_event, make_user, make_registration, mint_token
):
    """
    Stages come from independent counts, so ratings can exceed attendance. The
    design's funnel narrows at every step, so the reported value is clamped --
    a funnel that widens reads as a bug in the data.
    """
    admin = await make_admin()
    owner, club = await make_club(name="Funnel", email="funnel-ov@example.com")
    event = await make_event(club=club, name="F", created_by=owner)
    event.page_views = 100
    for i in range(3):
        user = await make_user(email=f"fun{i}-ov@example.com")
        await make_registration(event=event, user=user)
    await session.flush()
    token = await mint_token(admin)

    funnel = (await overview(client, token))["funnel"]

    values = [stage["value"] for stage in funnel]
    assert values == sorted(values, reverse=True), funnel


@pytest.mark.asyncio
async def test_the_window_is_capped(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/overview?days=99999", headers=auth_header(token)
    )

    # Rejected by the route's own bounds rather than reaching a year-long scan.
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_city_and_mrr_are_absent_rather_than_zero(
    client, make_admin, mint_token
):
    """
    Users carry no city and subscriptions have no domain. A zero would read as
    "no subscriptions" rather than "not built", so the keys are absent.
    """
    admin = await make_admin()
    token = await mint_token(admin)

    data = await overview(client, token)

    assert "users_by_city" not in data["breakdowns"]
    assert "mrr" not in data["totals"]


@pytest.mark.asyncio
async def test_overview_is_admin_only(client, make_user, mint_token):
    nosy = await make_user(email="nosy-ov@example.com")
    token = await mint_token(nosy)

    response = await client.get("/api/v1/admin/overview", headers=auth_header(token))

    assert response.status_code in (401, 403), response.text
