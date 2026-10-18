"""The personalised feed behind GET /events/for-you.

What it replaces: "For You" called /events/list with no arguments, which is
every event ever created ordered by date ascending. The first card was the
oldest event in the database -- in practice a competition from eighteen
months earlier -- and nothing about it was personalised.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header


def soon(days=2, hours=0):
    return datetime.now(timezone.utc) + timedelta(days=days, hours=hours)


@pytest.fixture
async def make_interest(session):
    async def _factory(name):
        from sqlalchemy import select

        from app.api.interests.models import InterestCategory, Interests

        category = await session.scalar(select(InterestCategory).limit(1))
        if category is None:
            category = InterestCategory(name="General")
            session.add(category)
            await session.flush()
        interest = Interests(name=name, category_id=category.id)
        session.add(interest)
        await session.flush()
        return interest

    return _factory


@pytest.fixture
async def tag_event(session):
    async def _factory(event, interest):
        from app.api.events.models import EventInterestsLink

        link = EventInterestsLink(event_id=event.id, interest_id=interest.id)
        session.add(link)
        await session.flush()
        return link

    return _factory


@pytest.fixture
async def give_user_interest(session):
    async def _factory(user, interest):
        from app.api.users.models import UserInterests

        link = UserInterests(user_id=user.id, interest_id=interest.id)
        session.add(link)
        await session.flush()
        return link

    return _factory


@pytest.fixture
async def link_club(session):
    async def _factory(user, club, **flags):
        from app.api.clubs.models import ClubUsersLink

        link = ClubUsersLink(club_id=club.id, user_id=user.id, **flags)
        session.add(link)
        await session.flush()
        return link

    return _factory


async def feed(client, token, **params):
    qs = "".join(f"&{k}={v}" for k, v in params.items())
    response = await client.get(
        f"/api/v1/events/for-you?limit=20{qs}", headers=auth_header(token)
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def names(items):
    return [i["name"] for i in items]


async def test_an_event_matching_your_interests_outranks_one_that_does_not(
    client, session, make_club, make_event, make_user, mint_token,
    make_interest, tag_event, give_user_interest,
):
    owner, club = await make_club(name="Club", email="f1@example.com")
    robotics = await make_interest("Robotics")
    matching = await make_event(
        club=club, created_by=owner, name="Robot Wars", event_datetime=soon(10)
    )
    other = await make_event(
        club=club, created_by=owner, name="Bake Sale", event_datetime=soon(10)
    )
    await tag_event(matching, robotics)

    user = await make_user(email="u1@example.com")
    await give_user_interest(user, robotics)
    token = await mint_token(user)

    items = names(await feed(client, token))

    # Same club, same date: the only thing separating them is the interest.
    assert items.index("Robot Wars") < items.index("Bake Sale")


async def test_a_pinned_club_outranks_a_followed_one(
    client, session, make_club, make_event, make_user, mint_token, link_club,
):
    owner_a, pinned_club = await make_club(name="Pinned", email="f2a@example.com")
    owner_b, followed_club = await make_club(name="Followed", email="f2b@example.com")
    await make_event(
        club=pinned_club, created_by=owner_a, name="Pinned Event",
        event_datetime=soon(10),
    )
    await make_event(
        club=followed_club, created_by=owner_b, name="Followed Event",
        event_datetime=soon(10),
    )

    user = await make_user(email="u2@example.com")
    await link_club(user, pinned_club, is_pinned=True)
    await link_club(user, followed_club, is_following=True)
    token = await mint_token(user)

    items = names(await feed(client, token))

    assert items.index("Pinned Event") < items.index("Followed Event")


async def test_events_from_a_hidden_club_are_gone(
    client, session, make_club, make_event, make_user, mint_token, link_club,
):
    owner, club = await make_club(name="Muted", email="f3@example.com")
    await make_event(
        club=club, created_by=owner, name="Muted Event", event_datetime=soon(5)
    )

    user = await make_user(email="u3@example.com")
    await link_club(user, club, is_hidden=True)
    token = await mint_token(user)

    assert "Muted Event" not in names(await feed(client, token))


async def test_an_event_you_already_registered_for_is_gone(
    client, session, make_club, make_event, make_user, make_registration,
    mint_token,
):
    owner, club = await make_club(name="Club", email="f4@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Already Mine", event_datetime=soon(5)
    )
    user = await make_user(email="u4@example.com")
    await make_registration(event=event, user=user)
    token = await mint_token(user)

    # Hidden rather than demoted: a feed of things you cannot act on is noise.
    # It is still reachable under Events -> Upcoming.
    assert "Already Mine" not in names(await feed(client, token))


async def test_an_event_whose_registration_closed_is_gone(
    client, session, make_club, make_event, make_user, mint_token,
):
    owner, club = await make_club(name="Club", email="f5@example.com")
    await make_event(
        club=club, created_by=owner, name="Too Late",
        event_datetime=soon(5),
        reg_enddate=datetime.now(timezone.utc) - timedelta(days=1),
    )
    await make_event(
        club=club, created_by=owner, name="Still Open",
        event_datetime=soon(5),
        reg_enddate=datetime.now(timezone.utc) + timedelta(days=2),
    )
    await make_event(
        club=club, created_by=owner, name="No Deadline", event_datetime=soon(5)
    )

    items = names(await feed(client, await mint_token(await make_user(email="u5@example.com"))))

    assert "Too Late" not in items
    assert "Still Open" in items
    # A null deadline means none was set, which is open, not closed.
    assert "No Deadline" in items


async def test_a_finished_event_is_gone(
    client, session, make_club, make_event, make_user, mint_token,
):
    # The actual bug on the home screen: an event from last year sitting at
    # the top of the feed.
    owner, club = await make_club(name="Club", email="f6@example.com")
    await make_event(
        club=club, created_by=owner, name="Last Year",
        event_datetime=datetime.now(timezone.utc) - timedelta(days=400),
        duration=7.0,
    )
    await make_event(
        club=club, created_by=owner, name="Next Week", event_datetime=soon(7)
    )

    items = names(await feed(client, await mint_token(await make_user(email="u6@example.com"))))

    assert "Last Year" not in items
    assert items[0] == "Next Week"


async def test_an_event_still_running_is_kept(
    client, session, make_club, make_event, make_user, mint_token,
):
    # Started an hour ago, runs for seven: you can still walk in.
    owner, club = await make_club(name="Club", email="f7@example.com")
    await make_event(
        club=club, created_by=owner, name="In Progress",
        event_datetime=datetime.now(timezone.utc) - timedelta(hours=1),
        duration=7.0,
    )

    items = names(await feed(client, await mint_token(await make_user(email="u7@example.com"))))

    assert "In Progress" in items


async def test_sooner_wins_when_nothing_else_separates_two_events(
    client, session, make_club, make_event, make_user, mint_token,
):
    owner, club = await make_club(name="Club", email="f8@example.com")
    await make_event(
        club=club, created_by=owner, name="This Week", event_datetime=soon(2)
    )
    await make_event(
        club=club, created_by=owner, name="Next Month", event_datetime=soon(40)
    )

    items = names(await feed(client, await mint_token(await make_user(email="u8@example.com"))))

    assert items.index("This Week") < items.index("Next Month")


async def test_a_brand_new_user_still_gets_a_feed(
    client, session, make_club, make_event, make_user, mint_token,
):
    # Cold start: no interests, no follows, no history. The score collapses
    # to popularity and urgency, which is "trending and soon" rather than an
    # empty screen.
    owner, club = await make_club(name="Club", email="f9@example.com")
    for i in range(3):
        await make_event(
            club=club, created_by=owner, name=f"Event {i}",
            event_datetime=soon(3 + i),
        )

    items = await feed(client, await mint_token(await make_user(email="u9@example.com")))

    assert len(items) == 3
    assert names(items)[0] == "Event 0"


async def test_paging_does_not_repeat_or_skip_an_event(
    client, session, make_club, make_event, make_user, mint_token,
):
    # Every event here scores identically, which is exactly when an unstable
    # sort shows the same row on two pages and drops another entirely.
    owner, club = await make_club(name="Club", email="f10@example.com")
    for i in range(6):
        await make_event(
            club=club, created_by=owner, name=f"Same {i}", event_datetime=soon(9)
        )

    token = await mint_token(await make_user(email="u10@example.com"))

    page1 = await client.get(
        "/api/v1/events/for-you?limit=3&offset=0", headers=auth_header(token)
    )
    page2 = await client.get(
        "/api/v1/events/for-you?limit=3&offset=3", headers=auth_header(token)
    )
    first = [i["name"] for i in page1.json()["items"]]
    second = [i["name"] for i in page2.json()["items"]]

    assert len(set(first) & set(second)) == 0
    assert len(set(first) | set(second)) == 6


async def test_drafts_and_unverified_clubs_never_reach_the_feed(
    client, session, make_club, make_event, make_user, mint_token,
):
    from app.api.closeout.models import EventStatus

    owner, club = await make_club(name="Open Club", email="f-vis@example.com")
    await make_event(club=club, created_by=owner, name="Shown Night", event_datetime=soon(6))
    await make_event(club=club, created_by=owner, name="Draft Night", event_datetime=soon(6), lifecycle=EventStatus.draft)
    shadow_owner, shadow = await make_club(name="Shadow Club", email="f-shadow@example.com", is_verified=False)
    await make_event(club=shadow, created_by=shadow_owner, name="Shadow Night", event_datetime=soon(6))
    await session.commit()

    items = names(await feed(client, await mint_token(await make_user(email="f-vis-user@example.com"))))

    assert "Shown Night" in items
    assert "Draft Night" not in items and "Shadow Night" not in items


async def test_an_interest_under_one_of_your_topics_counts_for_less_than_your_own(
    client, session, make_club, make_event, make_user, mint_token, tag_event, give_user_interest, link_club,
):
    """Someone who picked Cybersecurity: a Cybersecurity event first, then one
    on Web Development (the same topic, Development), then one on Drawing."""
    from app.api.interests.models import InterestCategory, Interests

    dev, arts = InterestCategory(name="Development"), InterestCategory(name="Arts")
    session.add_all([dev, arts])
    await session.flush()
    cyber, web, drawing = (Interests(name=n, category_id=c.id) for n, c in (("Cybersecurity", dev), ("Web Development", dev), ("Drawing", arts)))
    session.add_all([cyber, web, drawing])
    await session.flush()
    owner, club = await make_club(name="Topic Feed Club", email="f-topics@example.com")
    for name, interest in (("Paint Night", drawing), ("Web Night", web), ("Security Night", cyber)):
        await tag_event(await make_event(club=club, created_by=owner, name=name, event_datetime=soon(10)), interest)

    user = await make_user(email="u-topics@example.com")
    await give_user_interest(user, cyber)
    await link_club(user, club, is_following=True)  # all three are candidates, and the club counts the same for each
    token = await mint_token(user)

    assert names(await feed(client, token))[:3] == ["Security Night", "Web Night", "Paint Night"]

    from app.api.events import recommendations

    events, ranked = await recommendations.for_you_events(session, user.id, limit=20)
    scores = {e.name: s for e, s in zip(events, ranked)}
    assert round(scores["Web Night"] - scores["Paint Night"], 3) == 1.0
    assert round(scores["Security Night"] - scores["Web Night"], 3) == 1.5
