"""Trends and the moderation queue.

The dashboard reported counters and a seven-day sign-up chart. Every other
figure answered "how many" and none answered "is this getting better", which
is the question someone running the platform actually has.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_trends_returns_a_point_for_every_day(client, make_admin, mint_token):
    """
    Days with no activity must be present. A chart built only from days that
    have data draws a straight line between distant points and implies
    activity that did not happen.
    """
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/analytics/trends?days=14", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["days"] == 14
    assert len(body["series"]) == 14
    assert all({"date", "users", "events", "registrations", "revenue"} <= set(p)
               for p in body["series"])


@pytest.mark.asyncio
async def test_trend_days_are_consecutive_and_ordered(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/analytics/trends?days=7", headers=auth_header(token)
    )

    dates = [p["date"] for p in response.json()["series"]]
    assert dates == sorted(dates)
    parsed = [datetime.fromisoformat(d).date() for d in dates]
    gaps = {(b - a).days for a, b in zip(parsed, parsed[1:])}
    assert gaps == {1}


@pytest.mark.asyncio
async def test_todays_signup_lands_on_todays_point(
    client, make_admin, make_user, mint_token
):
    admin = await make_admin()
    await make_user(email="trendsuser@example.com")
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/analytics/trends?days=7", headers=auth_header(token)
    )

    body = response.json()
    today = body["series"][-1]
    assert today["users"] >= 1
    assert body["totals"]["users"] >= 1


@pytest.mark.asyncio
async def test_registrations_are_counted_regardless_of_payment(
    client, make_admin, make_club, make_user, make_event, make_registration, mint_token
):
    """
    Registrations count attendance, so a free place is still a registration.

    Revenue deliberately does not come from these rows any more. A
    registration records what was charged; a refund never changes it, so after
    any refund that figure overstates what the platform kept -- and disagreed
    with the payments page showing the same word on the same screen. Revenue
    now reads the payment log, which is covered in
    test_admin_analytics_accuracy.py.
    """
    admin = await make_admin()
    owner, club = await make_club(name="Revenue", email="revenueclub@example.com")
    event = await make_event(club=club, name="Paid Event", created_by=owner)

    payer = await make_user(email="payer@example.com")
    freeloader = await make_user(email="free@example.com")
    await make_registration(event=event, user=payer, is_paid=True, paid_amount=300)
    await make_registration(event=event, user=freeloader, is_paid=False, paid_amount=0)

    token = await mint_token(admin)
    response = await client.get(
        "/api/v1/admin/analytics/trends?days=7", headers=auth_header(token)
    )

    body = response.json()
    assert body["totals"]["registrations"] == 2
    # No payment was captured, so nothing cleared -- even though a registration
    # claims it was paid.
    assert body["totals"]["revenue"] == 0


@pytest.mark.asyncio
async def test_previous_window_is_reported_for_comparison(
    client, make_admin, mint_token
):
    """Without it the UI can only show a number, never a direction."""
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/analytics/trends?days=30", headers=auth_header(token)
    )

    previous = response.json()["previous_totals"]
    assert {"users", "events", "registrations", "revenue"} == set(previous)


@pytest.mark.asyncio
async def test_window_length_is_capped(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/analytics/trends?days=9999", headers=auth_header(token)
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_moderation_queue_surfaces_pending_clubs(
    client, make_admin, make_club, mint_token
):
    admin = await make_admin()
    await make_club(name="Waiting", email="waitingmod@example.com", is_verified=False)
    await make_club(name="Approved", email="approvedmod@example.com", is_verified=True)
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/moderation-queue", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    names = [c["name"] for c in response.json()["pending_clubs"]]
    assert names == ["Waiting"]


@pytest.mark.asyncio
async def test_moderation_queue_flags_a_finished_event_with_no_attendance(
    client, make_admin, make_club, make_user, make_event, make_registration, mint_token
):
    """
    Registrations but nobody checked in, after the event ran: either
    attendance was never taken or it did not happen. Both are invisible from
    every other page.
    """
    admin = await make_admin()
    owner, club = await make_club(name="Ghost", email="ghostclub@example.com")
    past = await make_event(
        club=club,
        name="Nobody Came",
        created_by=owner,
        event_datetime=datetime.now(timezone.utc) - timedelta(days=3),
    )
    attendee = await make_user(email="ghost@example.com")
    await make_registration(event=past, user=attendee, is_attended=False)

    token = await mint_token(admin)
    response = await client.get(
        "/api/v1/admin/moderation-queue", headers=auth_header(token)
    )

    flagged = response.json()["events_without_attendance"]
    assert [e["name"] for e in flagged] == ["Nobody Came"]
    assert flagged[0]["registration_count"] == 1


@pytest.mark.asyncio
async def test_an_event_with_attendance_is_not_flagged(
    client, make_admin, make_club, make_user, make_event, make_registration, mint_token
):
    admin = await make_admin()
    owner, club = await make_club(name="Fine", email="fineclub@example.com")
    past = await make_event(
        club=club,
        name="Went Fine",
        created_by=owner,
        event_datetime=datetime.now(timezone.utc) - timedelta(days=3),
    )
    attendee = await make_user(email="came@example.com")
    await make_registration(event=past, user=attendee, is_attended=True)

    token = await mint_token(admin)
    response = await client.get(
        "/api/v1/admin/moderation-queue", headers=auth_header(token)
    )

    assert response.json()["events_without_attendance"] == []


@pytest.mark.asyncio
async def test_insights_are_admin_only(client, make_user, mint_token):
    nosy = await make_user(email="nosyinsights@example.com")
    token = await mint_token(nosy)

    for path in ("/api/v1/admin/analytics/trends", "/api/v1/admin/moderation-queue"):
        response = await client.get(path, headers=auth_header(token))
        assert response.status_code in (401, 403), f"{path} -> {response.status_code}"
