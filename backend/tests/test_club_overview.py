"""The club dashboard's single response.

The thing most worth guarding here is the revenue figure. It is tempting to
compute a club's revenue as `reg_fee x registrations`, which is what the club
*would* have made if everyone paid -- a different and much larger number, and
one that people make decisions on. These pin it to money actually captured.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header


async def register(make_registration, event, user, *, paid=0.0, attended=False, when=None):
    row = await make_registration(event=event, user=user)
    row.is_paid = paid > 0
    row.paid_amount = paid
    row.actual_amount = paid
    row.is_attended = attended
    if when is not None:
        row.created_at = when
    return row


@pytest.fixture
async def club_setup(session, make_club, make_user, mint_token):
    owner, club = await make_club(name="Circuit Society", email="circuit@example.com")
    token = await mint_token(owner)
    return owner, club, token


@pytest.mark.asyncio
async def test_revenue_counts_money_actually_paid(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    owner, club = await make_club(name="Circuit", email="circuit@example.com")
    event = await make_event(club=club, name="Hack Night", created_by=owner, reg_fee=100, has_fee=True)

    # Three registered at ₹100; only two actually paid.
    for i in range(3):
        user = await make_user(email=f"payer{i}@example.com")
        await register(make_registration, event, user, paid=100.0 if i < 2 else 0.0)
    await session.flush()
    token = await mint_token(owner)

    response = await client.get("/api/v1/clubs/overview", headers=auth_header(token))

    assert response.status_code == 200, response.text
    body = response.json()
    # 200, not 300: reg_fee x registrations would be the latter.
    assert body["kpis"]["revenue"]["value"] == 200.0
    assert body["kpis"]["registrations"]["value"] == 3


@pytest.mark.asyncio
async def test_delta_is_null_with_no_previous_period(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    """
    A club's first month has nothing to compare against.

    "+100%" against nothing is a number someone repeats out loud, so the
    endpoint returns null and the UI can say "no comparison yet".
    """
    owner, club = await make_club(name="New Club", email="new@example.com")
    event = await make_event(club=club, name="First", created_by=owner)
    user = await make_user(email="first@example.com")
    await register(make_registration, event, user)
    await session.flush()
    token = await mint_token(owner)

    response = await client.get("/api/v1/clubs/overview", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert response.json()["kpis"]["registrations"]["delta"] is None


@pytest.mark.asyncio
async def test_needs_attention_lists_finished_events_with_nobody_present(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    owner, club = await make_club(name="Drama", email="drama@example.com")
    past = datetime.now(timezone.utc) - timedelta(days=5)

    unmarked = await make_event(
        club=club, name="Street Play", created_by=owner, event_datetime=past
    )
    marked = await make_event(
        club=club, name="Open Mic", created_by=owner, event_datetime=past
    )
    empty = await make_event(
        club=club, name="Nobody Came", created_by=owner, event_datetime=past
    )

    for i in range(3):
        await register(
            make_registration, unmarked, await make_user(email=f"u{i}@example.com")
        )
    await register(
        make_registration, marked, await make_user(email="m0@example.com"), attended=True
    )
    await session.flush()
    token = await mint_token(owner)

    response = await client.get("/api/v1/clubs/overview", headers=auth_header(token))

    assert response.status_code == 200, response.text
    names = [e["name"] for e in response.json()["needs_attention"]]
    assert names == ["Street Play"]
    # An event with attendance marked is done; one nobody registered for had
    # nobody to mark. Neither is the club failing to do its job.
    assert "Open Mic" not in names
    assert "Nobody Came" not in names


@pytest.mark.asyncio
async def test_next_event_is_the_soonest_upcoming_one(
    client, session, make_club, make_event, mint_token
):
    owner, club = await make_club(name="Quiz", email="quiz@example.com")
    now = datetime.now(timezone.utc)
    await make_event(club=club, name="Later", created_by=owner, event_datetime=now + timedelta(days=10))
    await make_event(club=club, name="Sooner", created_by=owner, event_datetime=now + timedelta(days=2))
    await make_event(club=club, name="Past", created_by=owner, event_datetime=now - timedelta(days=2))
    await session.flush()
    token = await mint_token(owner)

    response = await client.get("/api/v1/clubs/overview", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert response.json()["next_event"]["name"] == "Sooner"


@pytest.mark.asyncio
async def test_the_revenue_series_includes_quiet_days(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    """
    A chart that skips empty days compresses time and makes a flat week look
    busy. Every day in the window is present, zero or not.
    """
    owner, club = await make_club(name="Film", email="film@example.com")
    event = await make_event(club=club, name="Fest", created_by=owner)
    user = await make_user(email="one@example.com")
    await register(make_registration, event, user, paid=50.0)
    await session.flush()
    token = await mint_token(owner)

    response = await client.get(
        "/api/v1/clubs/overview?days=7", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    series = response.json()["revenue_series"]["current"]
    assert len(series) == 7
    assert sum(point["revenue"] for point in series) == 50.0


@pytest.mark.asyncio
async def test_reach_reports_conversion_and_turnout(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    owner, club = await make_club(name="Robotics", email="robotics@example.com")
    past = datetime.now(timezone.utc) - timedelta(days=3)
    event = await make_event(
        club=club, name="Line Follower", created_by=owner, event_datetime=past
    )
    event.page_views = 1000
    for i in range(4):
        await register(
            make_registration,
            event,
            await make_user(email=f"r{i}@example.com"),
            attended=i < 3,
        )
    await session.flush()
    token = await mint_token(owner)

    response = await client.get("/api/v1/clubs/overview", headers=auth_header(token))

    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["reach"] if r["name"] == "Line Follower")
    assert row["page_views"] == 1000
    assert row["registrations"] == 4
    # 4 of 1000 viewers signed up; 3 of 4 turned up.
    assert row["view_to_signup"] == 0.4
    assert row["turnout"] == 75.0


@pytest.mark.asyncio
async def test_turnout_is_null_rather_than_zero_with_no_registrations(
    client, session, make_club, make_event, mint_token
):
    """Zero turnout of zero people is not 0% -- it is not a rate at all, and
    showing 0% invites someone to chase a club that did nothing wrong."""
    owner, club = await make_club(name="Empty", email="empty@example.com")
    past = datetime.now(timezone.utc) - timedelta(days=3)
    await make_event(club=club, name="Quiet", created_by=owner, event_datetime=past)
    await session.flush()
    token = await mint_token(owner)

    response = await client.get("/api/v1/clubs/overview", headers=auth_header(token))

    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["reach"] if r["name"] == "Quiet")
    assert row["turnout"] is None
    assert row["view_to_signup"] is None


@pytest.mark.asyncio
async def test_another_clubs_events_are_not_counted(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    """The query scopes on club_id; a missing filter would show every club
    the whole platform's numbers, which would look plausible."""
    owner, club = await make_club(name="Mine", email="mine@example.com")
    other_owner, other = await make_club(name="Theirs", email="theirs@example.com")

    mine = await make_event(club=club, name="Mine", created_by=owner)
    theirs = await make_event(club=other, name="Theirs", created_by=other_owner)
    await register(make_registration, mine, await make_user(email="a@example.com"), paid=10.0)
    for i in range(5):
        await register(
            make_registration, theirs, await make_user(email=f"b{i}@example.com"), paid=100.0
        )
    await session.flush()
    token = await mint_token(owner)

    response = await client.get("/api/v1/clubs/overview", headers=auth_header(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kpis"]["registrations"]["value"] == 1
    assert body["kpis"]["revenue"]["value"] == 10.0


@pytest.mark.asyncio
async def test_a_club_account_is_required(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get("/api/v1/clubs/overview", headers=auth_header(token))

    assert response.status_code in (403, 404), response.text


@pytest.mark.asyncio
async def test_the_series_ends_today_and_the_windows_abut(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    """
    The series was counted forward from `now - days`, so it ended yesterday
    and left today out, while the totals printed beside it used the rolling
    window and included today. Today's takings showed up in the headline
    figure and nowhere on the chart.
    """
    owner, club = await make_club(name="Press", email="press@example.com")
    event = await make_event(club=club, name="Open Mic", created_by=owner)
    user = await make_user(email="today@example.com")
    await register(make_registration, event, user, paid=30.0)
    await session.flush()
    token = await mint_token(owner)

    response = await client.get(
        "/api/v1/clubs/overview?days=7", headers=auth_header(token)
    )

    body = response.json()
    current = body["revenue_series"]["current"]
    previous = body["revenue_series"]["previous"]
    today = datetime.now(timezone.utc).date()

    assert current[-1]["date"] == today.isoformat()
    # A registration made right now lands on the last point, not off the end.
    assert current[-1]["revenue"] == 30.0

    # The two windows abut rather than overlapping or leaving a gap: a
    # period-on-period comparison double-counts a day if they overlap.
    assert previous[-1]["date"] == (today - timedelta(days=7)).isoformat()
    assert len(previous) == 7
    assert previous[-1]["date"] < current[0]["date"]
    assert (
        datetime.fromisoformat(current[0]["date"]).date()
        - datetime.fromisoformat(previous[-1]["date"]).date()
    ) == timedelta(days=1)


@pytest.mark.asyncio
async def test_a_registration_made_late_in_the_utc_day_still_lands_on_it(
    client, session, make_club, make_event, make_user, make_registration, mint_token
):
    """
    Day buckets are computed in UTC on both sides.

    `date_trunc` on a `timestamptz` resolves using the database session's
    TimeZone, while the keys it is looked up by come from
    `datetime.now(timezone.utc)`. On a session set to Asia/Kolkata, anything
    created after 18:30 UTC bucketed to the next day and vanished from the
    series. Production runs Postgres in UTC, which is the only reason this
    was invisible.
    """
    owner, club = await make_club(name="Night", email="night@example.com")
    event = await make_event(club=club, name="Late Set", created_by=owner)
    user = await make_user(email="late@example.com")
    # 23:30 UTC is 05:00 the following day in IST.
    when = datetime.now(timezone.utc).replace(hour=23, minute=30) - timedelta(days=2)
    await register(make_registration, event, user, paid=40.0, when=when)
    await session.flush()
    token = await mint_token(owner)

    response = await client.get(
        "/api/v1/clubs/overview?days=7", headers=auth_header(token)
    )

    series = response.json()["revenue_series"]["current"]
    by_date = {point["date"]: point["revenue"] for point in series}

    assert by_date[when.date().isoformat()] == 40.0
