"""The admin backfills (/admin/discover/*) beyond what their first tests
cover: a club placed by its events' middle, the map-point backfill leaving a
pin alone and bettering a geocoded point, and the counters a restore with
triggers off can leave wrong -- an event's paid places and a live question's
votes. Each is the admin's only, and a second run finds nothing.

(The geocode backfill: tests/test_geocoding.py. The geo backfill's link
following and the first counter and place cases: test_discover.py,
test_discover_bounds.py, test_discover_scale.py, test_discover_integrity.py.)
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.api.clubs.models import Clubs
from app.api.discover import counters, geo, places
from app.api.events.models import EventRegistrationsLink, Events
from app.api.live.models import LiveQuestions, LiveQuestionVotes
from app.api.closeout.models import EventStatus
from tests.conftest import auth_header

ADMIN = "/api/v1/admin/discover"


async def _place(session, club_id):
    row = (await session.execute(text("SELECT place_lat, place_lng FROM clubs WHERE id = :id"), {"id": club_id})).one()
    return (row[0], row[1])


async def test_a_club_placed_by_its_events_middle_is_put_right_by_an_admin_only(client, session, make_club, make_event, make_admin, mint_token):
    owner, club = await make_club(name="Middle Club", email="middle-club@example.com")
    soon = datetime.now(timezone.utc) + timedelta(days=4)
    await make_event(club=club, created_by=owner, name="North", event_datetime=soon, lat=13.0, lng=77.0)
    await make_event(club=club, created_by=owner, name="South", event_datetime=soon, lat=12.0, lng=78.0)
    # Neither a draft, an online event, nor a deleted one moves it.
    await make_event(club=club, created_by=owner, name="Draft", event_datetime=soon, lat=40.0, lng=10.0, lifecycle=EventStatus.draft)
    await make_event(club=club, created_by=owner, name="Online", event_datetime=soon, lat=41.0, lng=11.0, is_online=True)
    await make_event(club=club, created_by=owner, name="Gone", event_datetime=soon, lat=42.0, lng=12.0, is_deleted=True)
    await session.commit()
    assert await _place(session, club.id) == pytest.approx((12.5, 77.5))

    # A restore with triggers off leaves it wrong.
    await session.execute(text("UPDATE clubs SET place_lat = NULL, place_lng = NULL WHERE id = :id"), {"id": club.id})
    await session.commit()
    assert await places.drift(session) == [club.id]

    url = f"{ADMIN}/recompute-club-places"
    assert (await client.post(url)).status_code == 401
    assert (await client.post(url, headers=auth_header(await mint_token(owner)))).status_code == 403
    assert await _place(session, club.id) == (None, None)

    h = auth_header(await mint_token(await make_admin(email="middle-admin@example.com")))
    r = await client.post(url, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"clubs": 1}
    assert await _place(session, club.id) == pytest.approx((12.5, 77.5))
    assert (await client.post(url, headers=h)).json() == {"clubs": 0}


async def test_the_map_point_backfill_leaves_a_pin_alone_and_betters_a_geocoded_point(client, session, make_club, make_event, make_admin, mint_token):
    owner, club = await make_club(name="Pins Club", email="pins-club@example.com")
    soon = datetime.now(timezone.utc) + timedelta(days=6)
    link = "https://maps.google.com/?q=12.97,77.59"
    pinned = await make_event(club=club, created_by=owner, name="Pinned", event_datetime=soon, location_link=link, lat=11.0, lng=76.0, geo_source=geo.MANUAL)
    guessed = await make_event(club=club, created_by=owner, name="Guessed", event_datetime=soon, location_link=link, lat=11.5, lng=76.5, geo_source=geo.GEOCODED)
    words = await make_event(club=club, created_by=owner, name="Words", event_datetime=soon, location_link="Block C, second floor")
    await session.commit()

    url = f"{ADMIN}/geo-backfill"
    assert (await client.post(url, headers=auth_header(await mint_token(owner)))).status_code == 403
    h = auth_header(await mint_token(await make_admin(email="pins-admin@example.com")))
    r = await client.post(url, headers=h)
    assert r.status_code == 200, r.text
    # The pin is not even looked at; the geocoded guess and the words are; only the guess has a point to give.
    assert r.json()["events"] == {"checked": 2, "resolved": 1}

    rows = {e.id: e for e in (await session.scalars(
        select(Events).where(Events.id.in_([pinned.id, guessed.id, words.id])).execution_options(populate_existing=True)
    )).all()}
    assert (rows[pinned.id].lat, rows[pinned.id].lng, rows[pinned.id].geo_source) == (11.0, 76.0, geo.MANUAL)
    assert (rows[guessed.id].lat, rows[guessed.id].lng, rows[guessed.id].geo_source) == (12.97, 77.59, geo.LINK)
    assert rows[words.id].lat is None

    # Run again: the bettered one is a link's point now, and is not looked at.
    assert (await client.post(url, headers=h)).json()["events"] == {"checked": 1, "resolved": 0}


async def test_an_events_paid_places_put_wrong_are_recounted(client, session, make_club, make_event, make_user, make_admin, mint_token):
    owner, club = await make_club(name="Paid Count Club", email="paidcount-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Paid Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=2), has_fee=True, reg_fee=100)
    for i, paid in enumerate((True, True, False)):
        u = await make_user(email=f"paidcount-{i}@example.com")
        session.add(EventRegistrationsLink(event_id=event.id, user_id=u.id, full_name="P", email=u.email, ticket_id=f"T-{uuid.uuid4().hex[:10]}", is_paid=paid))
    await session.flush()
    counts = lambda: session.execute(text("SELECT going_count, paid_count FROM events WHERE id = :id"), {"id": event.id})  # noqa: E731
    assert tuple((await counts()).one()) == (3, 2)
    await session.execute(text("UPDATE events SET paid_count = 9 WHERE id = :id"), {"id": event.id})
    assert await counters.drift(session) == {"events": [event.id], "clubs": []}

    h = auth_header(await mint_token(await make_admin(email="paidcount-admin@example.com")))
    r = await client.post(f"{ADMIN}/reconcile-counters", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["events"] == 1
    assert tuple((await counts()).one()) == (3, 2)


async def test_a_live_questions_votes_put_wrong_are_recounted(client, session, make_club, make_event, make_admin, mint_token):
    owner, club = await make_club(name="Votes Club", email="votes-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Ask Me", event_datetime=datetime.now(timezone.utc))
    q = LiveQuestions(event_id=event.id, text="Will the slides be shared?", author_key="k-author")
    session.add(q)
    await session.flush()
    session.add_all([LiveQuestionVotes(question_id=q.id, voter_key=f"k-{i}") for i in range(3)])
    await session.flush()
    await session.execute(text("UPDATE live_questions SET votes = 40 WHERE id = :id"), {"id": q.id})
    assert (await counters.drift_more(session))["questions"] == [q.id]

    url = f"{ADMIN}/reconcile-counters"
    assert (await client.post(url, headers=auth_header(await mint_token(owner)))).status_code == 403
    h = auth_header(await mint_token(await make_admin(email="votes-admin@example.com")))
    r = await client.post(url, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"events": 0, "clubs": 0, "posts": 0, "poll_options": 0, "questions": 1}
    assert await session.scalar(select(LiveQuestions.votes).where(LiveQuestions.id == q.id).execution_options(populate_existing=True)) == 3
    assert (await client.post(url, headers=h)).json()["questions"] == 0


async def test_a_club_with_its_own_point_keeps_it_whatever_its_events_say(session, make_club, make_event):
    """The rule recompute shares with the triggers: the club's own point first."""
    owner, club = await make_club(name="Own Point", email="own-point@example.com")
    await make_event(club=club, created_by=owner, name="Far", event_datetime=datetime.now(timezone.utc) + timedelta(days=1), lat=30.0, lng=70.0)
    await session.execute(text("UPDATE clubs SET lat = 12.0, lng = 77.0 WHERE id = :id"), {"id": club.id})
    await session.execute(text("UPDATE clubs SET place_lat = 30.0, place_lng = 70.0 WHERE id = :id"), {"id": club.id})
    await session.commit()
    assert await places.recompute(session) == 1
    assert await _place(session, club.id) == (12.0, 77.0)
    assert (await session.get(Clubs, club.id)) is not None
