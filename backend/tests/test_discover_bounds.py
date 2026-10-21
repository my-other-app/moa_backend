"""The read paths made to stay small at a lakh of clubs (docs/SCALING.md,
section d: fixes 1–4, 12–14):

- what is on is bounded from below by the longest an event may run, and an
  event saved before that limit, still running, widens the bound;
- the length limit itself, on create, edit and copy;
- list totals count at most COUNT_CAP + 1 rows and say when they stopped;
- For You scores a bounded set of candidates, keeps each ranking a few
  minutes, and forgets it when the user's clubs change;
- each club's place is kept by triggers, and "clubs near" reads it;
- the paged sitemap lists every page;
- the front page's "cities" is kept in a table.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update

from app.api.closeout.models import EventStatus
from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.discover import cache, geo, places, service, stats, upcoming
from app.api.events import recommendations
from app.api.events.models import MAX_EVENT_HOURS, EventCategories, EventInterestsLink, Events
from app.api.events.service import check_duration
from app.api.interests.models import InterestCategory, Interests
from app.api.threads.models import ClubThreads
from app.api.users.models import UserInterests
from app.db.listeners import add_loader_criteria
from app.response import CustomHTTPException
from tests.conftest import auth_header

API = "/api/v1/discover"
BLR = (12.9716, 77.5946)
KORAMANGALA = (12.9352, 77.6245)  # about 5 km from BLR
MYSURU = (12.2958, 76.6394)       # about 128 km
DELHI = (28.6139, 77.2090)


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


@pytest.fixture(autouse=True)
def background(monkeypatch, session):
    """Work after the response runs on the test's own session."""
    lock = asyncio.Lock()

    @asynccontextmanager
    async def scope():
        async with lock:
            yield session

    monkeypatch.setattr(geo, "session_scope", scope)


@pytest.fixture
def kept():
    """The shared and personal caches switched on, as in production."""
    cache.reset(enabled=True)
    stats.reset()
    yield
    cache.reset(enabled=False)
    stats.reset()


def _ids(items):
    return [x["id"] for x in items]


@pytest.fixture
async def scene(session, make_club, make_user):
    now = datetime.now(timezone.utc)
    tech = EventCategories(name="Tech")
    session.add(tech)
    await session.flush()
    owner, club = await make_club(name="Bound Club", email="bound-a@example.com")
    club.lat, club.lng = BLR

    def ev(name, start, hours, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        e = Events(slug=f"b-{uuid.uuid4().hex[:10]}", name=name, images=[], event_datetime=start, duration=hours, club_id=club.id, category_id=tech.id, **kw)
        session.add(e)
        return e

    e = dict(
        # Started 71 hours ago and runs 72: on for one more hour.
        still_on=ev("Three Day Hackathon", now - timedelta(hours=71), 72.0, lifecycle=EventStatus.live),
        # Started 73 hours ago and ran 72: over an hour ago.
        ended=ev("Last Weekend's Fest", now - timedelta(hours=73), 72.0, lifecycle=EventStatus.live),
        # Long over, still "published" (its organiser turned the clock off).
        stale=ev("Old Published Meetup", now - timedelta(days=200), 2.0, status_clock=False),
        soon=ev("Tomorrow's Talk", now + timedelta(days=1), 2.0),
    )
    await session.commit()
    return dict(now=now, owner=owner, club=club, tech=tech, e=e, h=auth_header(await _token(session, owner)))


async def _token(session, user):
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token

    await session.refresh(user)
    return create_access_token(data=AuthTokenData(user_id=user.id, token_type="access_token", token_version=user.token_version).model_dump())


# ─── 1. The lower bound on what is on ─────────────────────────────────────


async def test_an_event_still_on_after_71_hours_shows_and_one_over_does_not(client, session, scene, make_user, mint_token):
    e, club = scene["e"], scene["club"]
    want = [e["still_on"].id, e["soon"].id]

    assert _ids((await client.get(f"{API}/events")).json()["items"]) == want
    assert _ids((await client.get(f"{API}/clubs/{club.slug}/events")).json()["items"]) == want
    page = (await client.get(f"{API}/clubs/{club.slug}")).json()
    assert _ids(page["upcoming"]) == want and page["club"]["upcoming_events"] == 2
    home = (await client.get(f"{API}/home")).json()
    assert _ids(home["this_week"]) == want and home["stats"]["upcoming_events"] == 2
    # Its own page: on (live), with the other as "more from the club".
    still = (await client.get(f"{API}/events/{e['still_on'].slug}")).json()
    assert still["event"]["status"] == "live" and _ids(still["more_from_club"]) == [e["soon"].id]
    # The one that ended is held, not on: its page is there; no list has it.
    assert (await client.get(f"{API}/events/{e['ended'].slug}")).json()["event"]["status"] == "ended"
    token = auth_header(await mint_token(await make_user(email="bound-u1@example.com")))
    app_list = (await client.get("/api/v1/events/list", params={"is_ended": "false"}, headers=token)).json()["items"]
    assert [x["id"] for x in app_list] == want
    feed = (await client.get("/api/v1/events/for-you", headers=token)).json()["items"]
    assert sorted(x["id"] for x in feed) == sorted(want)


async def test_the_bound_is_the_length_limit_unless_a_longer_event_saved_before_it_still_runs(client, session, scene):
    now = scene["now"]
    assert await upcoming.hours(session, now) == MAX_EVENT_HOURS
    # A five-day festival saved before the limit, four days in.
    fest = Events(slug=f"b-{uuid.uuid4().hex[:10]}", name="Five Day Festival", images=[], event_datetime=now - timedelta(hours=96), duration=120.0,
                  club_id=scene["club"].id, category_id=scene["tech"].id, lifecycle=EventStatus.live)
    session.add(fest)
    await session.commit()
    assert await upcoming.hours(session, now) == 120.0
    assert fest.id in _ids((await client.get(f"{API}/events")).json()["items"])
    assert fest.id in _ids((await client.get(f"{API}/clubs/{scene['club'].slug}/events")).json()["items"])
    # Once it is over, the bound is the limit again.
    assert await upcoming.hours(session, now + timedelta(hours=25)) == MAX_EVENT_HOURS


async def test_the_walks_start_near_now_not_at_the_oldest_published_event(client, session, scene, statements_seen):
    await client.get(f"{API}/events")
    page_sql = next(s for s in statements_seen if "ORDER BY events.event_datetime" in s and "LIMIT" in s)
    assert "events.event_datetime >=" in page_sql
    # The search's typo fallback looks only at what can be on: one column's
    # trigram test, beside the lower bound on the start.
    statements_seen.clear()
    body = (await client.get(f"{API}/events", params={"q": "hackathn"})).json()
    assert _ids(body["items"]) == [scene["e"]["still_on"].id]  # "Three Day Hackathon", forgiven
    fuzzy = next(s for s in statements_seen if "%>" in s and "count(" in s.lower())
    assert "events.search_text %>" in fuzzy and "events.event_datetime >=" in fuzzy
    # The letters are nowhere, so what is on was never read to find none of them.
    assert not any("LIKE" in s and "count(" in s.lower() for s in statements_seen), statements_seen
    # Letters that are somewhere are looked for among what is on, as always,
    # even when only an event already over has them.
    statements_seen.clear()
    assert (await client.get(f"{API}/events", params={"q": "weekend's fest"})).json()["items"] == []
    assert any("LIKE" in s and "count(" in s.lower() for s in statements_seen)


@pytest.fixture
def statements_seen(session):
    from sqlalchemy import event as sa_event

    seen: list[str] = []
    conn = session.bind.sync_connection

    def before(_conn, _cursor, statement, *_args):
        seen.append(statement)

    sa_event.listen(conn, "before_cursor_execute", before)
    yield seen
    sa_event.remove(conn, "before_cursor_execute", before)


# ─── 2. The length limit ──────────────────────────────────────────────────


def test_duration_is_checked_and_an_older_longer_event_keeps_its_length():
    check_duration(MAX_EVENT_HOURS)
    check_duration(0.5)
    with pytest.raises(CustomHTTPException) as e:
        check_duration(MAX_EVENT_HOURS + 1)
    assert e.value.status_code == 422
    check_duration(120.0, was=120.0)  # saved before the limit, left alone
    with pytest.raises(CustomHTTPException):
        check_duration(100.0, was=120.0)  # changed, still too long
    check_duration(10.0, was=120.0)


async def test_create_edit_and_copy_keep_events_within_the_limit(client, session, scene):
    h, now, tech = scene["h"], scene["now"], scene["tech"]
    form = {"event_datetime": (now + timedelta(days=5)).isoformat(), "has_fee": "false", "status": "published", "category_id": str(tech.id)}
    too_long = await client.post("/api/v1/events/create", data={**form, "name": "Endless Jam", "duration": str(MAX_EVENT_HOURS + 1)}, headers=h)
    assert too_long.status_code == 422, too_long.text
    made = await client.post("/api/v1/events/create", data={**form, "name": "Long Jam", "duration": str(MAX_EVENT_HOURS)}, headers=h)
    assert made.status_code in (200, 201), made.text

    # An event saved before the limit, longer than it.
    old = Events(slug=f"b-{uuid.uuid4().hex[:10]}", name="Old Week Long Fest", images=[], event_datetime=now + timedelta(days=9), duration=168.0,
                 club_id=scene["club"].id, category_id=tech.id, lifecycle=EventStatus.published)
    session.add(old)
    await session.commit()
    icat = InterestCategory(name="Limit interests")
    session.add(icat)
    await session.flush()
    music = Interests(name="Limit Music", category_id=icat.id)
    session.add(music)
    await session.commit()
    edit = {**form, "event_datetime": old.event_datetime.isoformat(), "name": "Old Week Long Fest", "interest_ids": str(music.id)}
    r = await client.put(f"/api/v1/events/update/{old.id}", data={**edit, "duration": "168"}, headers=h)
    assert r.status_code == 200, r.text
    assert (await client.put(f"/api/v1/events/update/{old.id}", data={**edit, "duration": "100"}, headers=h)).status_code == 422
    assert (await client.put(f"/api/v1/events/update/{old.id}", data={**edit, "duration": "48"}, headers=h)).status_code == 200

    # A copy is a new event: within the limit, whatever its source was.
    await session.execute(update(Events).where(Events.id == old.id).values(duration=168.0))
    await session.commit()
    body = {"name": "Fest Again", "event_datetime": (now + timedelta(days=60)).isoformat()}
    assert (await client.post(f"/api/v1/events/{old.id}/duplicate", json=body, headers=h)).status_code == 422
    assert (await client.post(f"/api/v1/events/{old.id}/duplicate", json={**body, "duration": 200}, headers=h)).status_code == 422
    copied = await client.post(f"/api/v1/events/{old.id}/duplicate", json={**body, "duration": 24}, headers=h)
    assert copied.status_code == 201, copied.text


# ─── 3. Totals that stop counting ─────────────────────────────────────────


async def test_totals_stop_at_the_cap_and_say_so_and_the_pages_go_on(client, session, scene, monkeypatch):
    now, club, tech = scene["now"], scene["club"], scene["tech"]
    for i in range(5):
        session.add(Events(slug=f"cap-{uuid.uuid4().hex[:8]}", name=f"Capped Night {i}", images=[], event_datetime=now + timedelta(days=2, hours=i),
                           duration=2.0, club_id=club.id, category_id=tech.id, lifecycle=EventStatus.published))
    for i in range(4):
        session.add(Clubs(slug=f"cap-club-{uuid.uuid4().hex[:6]}", name=f"Capped Club {i}", is_verified=True))
    for i in range(4):
        session.add(ClubThreads(id=uuid.uuid4(), club_id=club.id, body=f"Post {i}", created_at=now - timedelta(minutes=i)))
    await session.commit()

    # Under the cap: exact, and not capped.
    body = (await client.get(f"{API}/events")).json()
    assert body["total"] == 7 and body["total_capped"] is False

    monkeypatch.setattr(service, "COUNT_CAP", 3)
    body = (await client.get(f"{API}/events", params={"limit": 2})).json()
    assert body["total"] == 3 and body["total_capped"] is True and len(body["items"]) == 2
    # Past the capped total, pages still come.
    deeper = (await client.get(f"{API}/events", params={"offset": 4, "limit": 2})).json()
    assert len(deeper["items"]) == 2 and deeper["total_capped"] is True
    everything = []
    for offset in range(0, 10, 2):
        everything += _ids((await client.get(f"{API}/events", params={"offset": offset, "limit": 2})).json()["items"])
    assert len(everything) == 7 == len(set(everything))
    # Words: the same.
    words = (await client.get(f"{API}/events", params={"q": "capped"})).json()
    assert words["total"] == 3 and words["total_capped"] is True and len(words["items"]) == 5
    # Distance: the placed part's size is counted only as far as the page needs.
    for i, ev in enumerate((await session.scalars(select(Events).where(Events.name.like("Capped Night%")).order_by(Events.id))).all()):
        ev.lat, ev.lng = KORAMANGALA[0] + i * 0.01, KORAMANGALA[1]
    await session.commit()
    # Anywhere (radius 0), so the pages cross from the placed part to the rest.
    near = []
    for offset in range(0, 10, 3):
        params = {"lat": BLR[0], "lng": BLR[1], "radius_km": 0, "sort": "distance", "offset": offset, "limit": 3}
        near += _ids((await client.get(f"{API}/events", params=params)).json()["items"])
    assert len(near) == 7 == len(set(near))
    # Within the radius, only the five placed in it.
    inside = []
    for offset in range(0, 10, 3):
        inside += _ids((await client.get(f"{API}/events", params={"lat": BLR[0], "lng": BLR[1], "sort": "distance", "offset": offset, "limit": 3})).json()["items"])
    assert inside == near[:5] and len(set(inside)) == 5

    clubs = (await client.get(f"{API}/clubs")).json()
    assert clubs["total"] == 3 and clubs["total_capped"] is True and len(clubs["items"]) == 5
    threads = (await client.get(f"{API}/threads")).json()
    assert threads["total"] == 3 and threads["total_capped"] is True and len(threads["items"]) == 4


# ─── 4. For You: bounded candidates, kept and forgotten ───────────────────


@pytest.fixture
async def feed_world(session, make_club, make_user, mint_token, make_registration):
    now = datetime.now(timezone.utc)
    tech = EventCategories(name="Feed Tech")
    session.add(tech)
    icat = InterestCategory(name="Feed interests")
    session.add(icat)
    await session.flush()
    robotics = Interests(name="Feed Robotics", category_id=icat.id)
    session.add(robotics)
    owners, clubs = [], []
    for i, name in enumerate(("Followed Club", "Popular Club", "Other Club", "Far Club")):
        o, c = await make_club(name=name, email=f"feed-{i}@example.com")
        owners.append(o)
        clubs.append(c)
    _, ghost = await make_club(name="Feed Ghost", email="feed-ghost@example.com", is_verified=False)

    def ev(club, name, days, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        e = Events(slug=f"f-{uuid.uuid4().hex[:10]}", name=name, images=[], event_datetime=now + timedelta(days=days), duration=2.0,
                   club_id=club.id, category_id=tech.id, **kw)
        session.add(e)
        return e

    followed, popular, other, far = clubs
    e = dict(
        # Far off and nobody's going: only being from a followed club gets it in.
        followed_late=ev(followed, "Followed Late Night", 90),
        popular=ev(popular, "Popular Show", 5, page_views=500, lat=BLR[0], lng=BLR[1]),
        popular_2=ev(popular, "Popular Show Two", 6, page_views=400, lat=BLR[0], lng=BLR[1]),
        # Popular, but in Delhi.
        delhi=ev(far, "Delhi Big Night", 5, page_views=450, lat=DELHI[0], lng=DELHI[1]),
        # In no candidate set at all once the limits are small.
        quiet=ev(other, "Quiet Evening", 40, page_views=1),
        interest=ev(other, "Robotics Lab Day", 10),
        online=ev(other, "Online Session", 20, is_online=True, page_views=30),
        draft=ev(followed, "Followed Draft", 3, lifecycle=EventStatus.draft),
        ghost=ev(ghost, "Ghost Night", 3, page_views=999),
    )
    await session.flush()
    session.add(EventInterestsLink(event_id=e["interest"].id, interest_id=robotics.id))
    user = await make_user(email="feed-user@example.com")
    session.add(UserInterests(user_id=user.id, interest_id=robotics.id))
    session.add(ClubUsersLink(club_id=followed.id, user_id=user.id, is_following=True))
    await session.commit()
    return dict(now=now, user=user, clubs=clubs, e=e, h=auth_header(await mint_token(user)))


async def _feed(client, h, **params):
    r = await client.get("/api/v1/events/for-you", params={"limit": 20, "debug_scores": "true", **params}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_for_you_scores_only_its_candidates_and_a_followed_club_comes_first(client, session, feed_world, monkeypatch):
    e, h = feed_world["e"], feed_world["h"]
    # Everything fits in the candidates: every shown event, drafts and unverified clubs' never.
    every = await _feed(client, h)
    assert set(_ids(every)) == {e[k].id for k in ("followed_late", "popular", "popular_2", "delhi", "quiet", "interest", "online")}
    order = _ids(every)
    # The followed club's far-off, unattended event beats the popular ones: the club weight is the score's largest.
    assert order.index(e["followed_late"].id) < min(order.index(e[k].id) for k in ("popular", "popular_2", "delhi", "quiet", "online"))

    # Small limits: the followed club's events, the interest's, the most
    # popular (one) and the online (one) are the candidates; nothing else.
    for name, n in (("POPULAR_LIMIT", 1), ("ONLINE_LIMIT", 1), ("INTEREST_LIMIT", 5), ("FOLLOWED_LIMIT", 5)):
        monkeypatch.setattr(recommendations, name, n)
    bounded = await _feed(client, h)
    assert _ids(bounded).index(e["followed_late"].id) < _ids(bounded).index(e["popular"].id)
    assert set(_ids(bounded)) == {e["followed_late"].id, e["interest"].id, e["popular"].id, e["online"].id}
    assert e["quiet"].id not in _ids(bounded)
    # With a place, the most popular are the region's: Delhi's big night is not near Bengaluru.
    monkeypatch.setattr(recommendations, "POPULAR_LIMIT", 2)
    here = await _feed(client, h, lat=BLR[0], lng=BLR[1])
    assert e["popular_2"].id in _ids(here) and e["delhi"].id not in _ids(here)
    there = await _feed(client, h, lat=DELHI[0], lng=DELHI[1])
    assert e["delhi"].id in _ids(there) and e["popular"].id not in _ids(there)


async def test_for_you_is_kept_for_the_user_and_forgotten_when_their_clubs_change(client, session, feed_world, kept, statements_seen, make_registration):
    e, h, user = feed_world["e"], feed_world["h"], feed_world["user"]
    first = _ids(await _feed(client, h))
    statements_seen.clear()
    again = _ids(await _feed(client, h))
    assert again == first
    # Kept: no ranking query, only the check of what is kept and the page's rows.
    assert not any("for_you_candidates" in s for s in statements_seen), statements_seen

    # Registered since: gone at once, though the ranking is kept.
    await make_registration(event=e["popular"], user=user)
    await session.commit()
    assert e["popular"].id not in _ids(await _feed(client, h))

    # Following a club: worked out again, so its events come in now.
    async def quiet_score():
        events, scores = await recommendations.for_you_events(session, user.id, limit=20)
        return dict(zip([x.id for x in events], scores))[e["quiet"].id]

    other = feed_world["clubs"][2]
    assert await quiet_score() < 2.5
    assert (await client.post(f"/api/v1/clubs/follow/{other.id}", headers=h)).status_code in (200, 201)
    statements_seen.clear()
    await _feed(client, h)
    assert any("for_you_candidates" in s for s in statements_seen)
    assert await quiet_score() > 2.5  # now followed

    # Unfollowing: forgotten again.
    assert (await client.post(f"/api/v1/clubs/unfollow/{other.id}", headers=h)).status_code in (200, 201)
    assert await quiet_score() < 2.5


# ─── 5. Club places ───────────────────────────────────────────────────────


async def _place(session, club_id):
    return tuple((await session.execute(select(Clubs.place_lat, Clubs.place_lng).where(Clubs.id == club_id))).one())


async def test_a_clubs_place_follows_its_point_and_its_events(session, make_club):
    _, club = await make_club(name="Placed Club", email="place-a@example.com")
    _, other = await make_club(name="Other Placed Club", email="place-b@example.com")
    cat = EventCategories(name="Place Cat")
    session.add(cat)
    await session.flush()
    assert await _place(session, club.id) == (None, None)

    def ev(c, point=None, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        e = Events(slug=f"p-{uuid.uuid4().hex[:10]}", name="Placed", images=[], event_datetime=datetime.now(timezone.utc) + timedelta(days=3), duration=2.0,
                   club_id=c.id, category_id=cat.id, lat=point[0] if point else None, lng=point[1] if point else None, **kw)
        session.add(e)
        return e

    a = ev(club, (10.0, 70.0))
    await session.flush()
    assert await _place(session, club.id) == (10.0, 70.0)
    b = ev(club, (12.0, 72.0))
    # Not counted: online, a draft, deleted, or without a point.
    ev(club, (50.0, 50.0), is_online=True)
    draft = ev(club, (50.0, 50.0), lifecycle=EventStatus.draft)
    ev(club, (50.0, 50.0), is_deleted=True)
    ev(club)
    await session.flush()
    assert await _place(session, club.id) == pytest.approx((11.0, 71.0))
    # A draft published counts; deleted again, it does not.
    draft.lifecycle = EventStatus.published
    await session.flush()
    assert await _place(session, club.id) == pytest.approx((24.0, 64.0))
    draft.is_deleted = True
    await session.flush()
    assert await _place(session, club.id) == pytest.approx((11.0, 71.0))
    # A point moved; an event moved to another club: both clubs follow.
    a.lat = 14.0
    await session.flush()
    assert await _place(session, club.id) == pytest.approx((13.0, 71.0))
    b.club_id = other.id
    await session.flush()
    assert await _place(session, club.id) == pytest.approx((14.0, 70.0))
    assert await _place(session, other.id) == pytest.approx((12.0, 72.0))
    # The status clock moving a published event to live or closed changes nothing.
    a.lifecycle = EventStatus.closed
    await session.flush()
    assert await _place(session, club.id) == pytest.approx((14.0, 70.0))
    # Its own point wins; dropping it falls back to its events.
    await session.execute(update(Clubs).where(Clubs.id == club.id).values(lat=1.5, lng=2.5))
    assert await _place(session, club.id) == (1.5, 2.5)
    a.lat = 20.0  # its events no longer move it
    await session.flush()
    assert await _place(session, club.id) == (1.5, 2.5)
    await session.execute(update(Clubs).where(Clubs.id == club.id).values(lat=None, lng=None))
    assert await _place(session, club.id) == pytest.approx((20.0, 70.0))
    # A hard delete of its last placed event leaves it without a place.
    await session.execute(text("DELETE FROM events WHERE id = :id"), {"id": a.id})
    assert await _place(session, club.id) == (None, None)
    assert await places.drift(session) == []


async def test_a_place_written_with_triggers_off_is_found_and_put_right(client, session, make_club, make_admin, mint_token):
    _, club = await make_club(name="Drifted Club", email="drift-a@example.com")
    await session.execute(update(Clubs).where(Clubs.id == club.id).values(lat=12.0, lng=77.0))
    await session.commit()
    await session.execute(text("UPDATE clubs SET place_lat = 1, place_lng = 1 WHERE id = :id"), {"id": club.id})
    await session.commit()
    assert await places.drift(session) == [club.id]
    h = auth_header(await mint_token(await make_admin(email="drift-admin@example.com")))
    r = await client.post(f"{API.replace('/discover', '/admin/discover')}/recompute-club-places", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"clubs": 1}
    assert await _place(session, club.id) == (12.0, 77.0) and await places.drift(session) == []
    r = await client.post(f"{API.replace('/discover', '/admin/discover')}/recompute-club-places", headers=h)
    assert r.json() == {"clubs": 0}


async def test_clubs_near_use_the_kept_place(client, session, make_club, make_user):
    cat = EventCategories(name="Near Cat")
    session.add(cat)
    _, own = await make_club(name="Own Point Club", email="near-a@example.com")
    _, by_events = await make_club(name="Events Point Club", email="near-b@example.com")
    _, far = await make_club(name="Far Away Club", email="near-c@example.com")
    _, nowhere = await make_club(name="Nowhere Club", email="near-d@example.com")
    await session.flush()
    await session.execute(update(Clubs).where(Clubs.id == own.id).values(lat=BLR[0], lng=BLR[1]))
    await session.execute(update(Clubs).where(Clubs.id == far.id).values(lat=DELHI[0], lng=DELHI[1]))
    session.add(Events(slug=f"n-{uuid.uuid4().hex[:8]}", name="Koramangala Jam", images=[], event_datetime=datetime.now(timezone.utc) + timedelta(days=2),
                       duration=2.0, club_id=by_events.id, category_id=cat.id, lat=KORAMANGALA[0], lng=KORAMANGALA[1], lifecycle=EventStatus.published))
    await session.commit()

    body = (await client.get(f"{API}/clubs", params={"lat": BLR[0], "lng": BLR[1], "sort": "nearby", "radius_km": 50})).json()
    got = {x["id"]: x["distance_km"] for x in body["items"]}
    assert _ids(body["items"])[:2] == [own.id, by_events.id]
    assert got[own.id] == 0.0 and got[by_events.id] == pytest.approx(geo.km(*BLR, *KORAMANGALA), abs=0.05)
    assert far.id not in got  # outside the radius
    assert got[nowhere.id] is None  # no place: never ruled out, after every placed one
    # The front page's nearest clubs, from the same places.
    home = (await client.get(f"{API}/home", params={"lat": BLR[0], "lng": BLR[1]})).json()
    assert _ids(home["clubs"])[:2] == [own.id, by_events.id]
    # Anywhere: Delhi comes in, by distance.
    anywhere = (await client.get(f"{API}/clubs", params={"lat": BLR[0], "lng": BLR[1], "sort": "nearby", "radius_km": 0})).json()
    assert _ids(anywhere["items"])[:3] == [own.id, by_events.id, far.id]


async def test_the_nearest_clubs_widen_until_enough_are_found(session, make_club):
    made = []
    for i in range(3):
        _, c = await make_club(name=f"Ring Club {i}", email=f"ring-{i}@example.com")
        made.append(c)
    await session.flush()
    for c, point in zip(made, (BLR, MYSURU, DELHI)):
        await session.execute(update(Clubs).where(Clubs.id == c.id).values(lat=point[0], lng=point[1]))
    r, every = await places.nearest(session, BLR, 2)
    assert r == pytest.approx(geo.km(*BLR, *MYSURU), abs=1e-6) and every == []  # within the second ring
    r, every = await places.nearest(session, BLR, 3)
    assert r == pytest.approx(geo.km(*BLR, *DELHI), abs=1e-6)  # everywhere
    r, every = await places.nearest(session, BLR, 50)
    assert r is None and {c[0] for c in every} >= {c.id for c in made}


# ─── 6. The paged sitemap ─────────────────────────────────────────────────


async def test_the_sitemap_index_lists_every_page_and_each_page_its_entries(client, session, scene, monkeypatch):
    now, club, tech = scene["now"], scene["club"], scene["tech"]
    for i in range(4):
        session.add(Events(slug=f"sm-{uuid.uuid4().hex[:8]}", name=f"Mapped {i}", images=[], event_datetime=now + timedelta(days=3 + i), duration=2.0,
                           club_id=club.id, category_id=tech.id, lifecycle=EventStatus.published))
    for i in range(2):
        session.add(Clubs(slug=f"sm-club-{uuid.uuid4().hex[:6]}", name=f"Mapped Club {i}", is_verified=True))
    ghost = Clubs(slug=f"sm-ghost-{uuid.uuid4().hex[:6]}", name="Unlisted Club", is_verified=False)
    session.add(ghost)
    await session.flush()
    session.add(Events(slug=f"sm-ghost-ev-{uuid.uuid4().hex[:8]}", name="Unlisted Night", images=[], event_datetime=now + timedelta(days=4), duration=2.0,
                       club_id=ghost.id, category_id=tech.id, lifecycle=EventStatus.published))
    # Held two years ago: past the one-page sitemap's year, still a public page.
    ancient = Events(slug=f"sm-old-{uuid.uuid4().hex[:8]}", name="Founding Meetup", images=[], event_datetime=now - timedelta(days=730), duration=2.0,
                     club_id=club.id, category_id=tech.id, lifecycle=EventStatus.archived)
    session.add(ancient)
    session.add(Events(slug=f"sm-draft-{uuid.uuid4().hex[:8]}", name="Never Published", images=[], event_datetime=now - timedelta(days=30), duration=2.0,
                       club_id=club.id, category_id=tech.id, lifecycle=EventStatus.draft))
    await session.commit()
    old = (await client.get(f"{API}/sitemap")).json()
    assert ancient.slug not in {x["slug"] for x in old["events"]}
    monkeypatch.setattr(service, "SITEMAP_PAGE", 2)

    index = (await client.get(f"{API}/sitemap/index")).json()
    assert index["page_size"] == 2
    pages = {(p["kind"], p["page"]): p for p in index["pages"]}
    seen = {"events": [], "clubs": []}
    for (kind, n), p in pages.items():
        body = (await client.get(f"{API}/sitemap/{kind}/{n}")).json()
        assert body["kind"] == kind and body["page"] == n and len(body["items"]) == p["entries"] <= 2
        assert body["pages"] == sum(1 for k, _ in pages if k == kind)
        assert p["updated_at"] == max(x["updated_at"] for x in body["items"])
        seen[kind] += [x["slug"] for x in body["items"]]
    # Every page together is what the one-page sitemap lists (it has room for
    # all of them here), and the event held two years ago; never a draft.
    assert sorted(seen["events"]) == sorted([x["slug"] for x in old["events"]] + [ancient.slug])
    assert sorted(seen["clubs"]) == sorted(x["slug"] for x in old["clubs"])
    assert len(seen["events"]) == len(set(seen["events"])) == 9  # the scene's four (held, on, to come), four more, the old one
    assert not any(slug.startswith(("sm-draft-", "sm-ghost-ev-")) for slug in seen["events"])
    # In id order, so a page keeps its entries from one crawl to the next.
    ids = {e.slug: e.id for e in (await session.scalars(select(Events).where(Events.slug.in_(seen["events"])))).all()}
    assert [ids[x] for x in seen["events"]] == sorted(ids[x] for x in seen["events"])
    assert "Unlisted" not in " ".join(seen["clubs"])
    last = max(n for k, n in pages if k == "events")
    assert (await client.get(f"{API}/sitemap/events/{last + 1}")).status_code == 404
    assert (await client.get(f"{API}/sitemap/places/1")).status_code == 422


# ─── 7. The front page's cities ───────────────────────────────────────────


async def test_cities_are_kept_in_a_table_and_counted_again_when_stale(client, session, scene, kept):
    assert (await client.get(f"{API}/home")).json()["stats"]["cities"] == 0  # no venues yet, nothing stored
    stored = await session.scalar(select(stats.DiscoverStats.value).where(stats.DiscoverStats.name == stats.CITIES))
    assert stored == 0  # the request's refresh stored it
    e = scene["e"]["soon"]
    # A city is a place on the map: words alone are no city.
    e.location_name, e.lat, e.lng = "Koramangala Hub", 12.9352, 77.6245
    await session.commit()
    cache.shared.clear()
    # Read from the table: not counted again until it is stale.
    assert (await client.get(f"{API}/home")).json()["stats"]["cities"] == 0
    assert await stats.refresh() is None  # fresh: nothing to do
    later = datetime.now(timezone.utc) + stats.EVERY + timedelta(minutes=1)
    assert await stats.refresh(later) == 1
    cache.shared.clear()
    assert (await client.get(f"{API}/home")).json()["stats"]["cities"] == 1


async def test_for_you_with_nothing_on_and_nothing_known_is_empty(client, make_user, mint_token):
    # No interests, no clubs, nothing popular or online: one arm left, and no rows.
    h = auth_header(await mint_token(await make_user(email="feed-empty@example.com")))
    assert await _feed(client, h) == []


# ─── 8. What the feeds read off a card ────────────────────────────────────


def test_a_summary_is_one_plain_line_cut_at_a_word():
    assert service.summary_of(None) is None and service.summary_of("  \n ") is None
    assert service.summary_of("<p>Build <b>robots</b> &amp; drones.</p>\n\n## Bring\n- **a laptop** and [a friend](https://x.example)") == (
        "Build robots & drones. Bring a laptop and a friend"
    )
    assert service.summary_of("snake_case names and 3*4 stay as written") == "snake_case names and 3*4 stay as written"
    long = "Robots " * 60
    got = service.summary_of(long)
    assert len(got) <= service.SUMMARY_MAX and got.endswith("Robots…") and "  " not in got
    assert service.summary_of("x" * 300) == "x" * (service.SUMMARY_MAX - 1) + "…"  # no word to cut at


async def test_cards_carry_a_summary_and_their_dates_and_newest_comes_first(client, session, scene):
    now, club, tech = scene["now"], scene["club"], scene["tech"]
    made = []
    for i, days_ago in enumerate((30, 2, 10)):
        e = Events(slug=f"nw-{uuid.uuid4().hex[:8]}", name=f"Newest Night {i}", images=[], event_datetime=now + timedelta(days=5 + i), duration=2.0,
                   club_id=club.id, category_id=tech.id, lifecycle=EventStatus.published, about=f"<p>Night {i}: <b>talks</b> and demos.</p>",
                   created_at=now - timedelta(days=days_ago))
        session.add(e)
        made.append(e)
    await session.commit()

    body = (await client.get(f"{API}/events", params={"q": "newest night", "sort": "newest"})).json()
    assert _ids(body["items"]) == [made[1].id, made[2].id, made[0].id]
    card = body["items"][0]
    assert card["summary"] == "Night 1: talks and demos."
    assert datetime.fromisoformat(card["created_at"]) == made[1].created_at
    assert datetime.fromisoformat(card["updated_at"]) == made[1].updated_at
    # Without words: everything on, the latest made first.
    newest = (await client.get(f"{API}/events", params={"sort": "newest"})).json()["items"]
    made_at = [datetime.fromisoformat(x["created_at"]) for x in newest]
    assert made_at == sorted(made_at, reverse=True)
    # The front page's cards and the event's own page carry them too.
    home = (await client.get(f"{API}/home")).json()
    assert all("summary" in x and x["created_at"] and x["updated_at"] for x in home["this_week"])
    page = (await client.get(f"{API}/events/{made[1].slug}")).json()["event"]
    assert page["updated_at"] == card["updated_at"] and page["created_at"] == card["created_at"] and page["summary"] == card["summary"]
