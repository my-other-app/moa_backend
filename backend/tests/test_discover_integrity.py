"""What the public may see, counters that cannot lose a write, and lists that
stay small at size: blocked organisations, double taps, counted views, the
app's club list, the follower's feed, comments, insights, discover's paging
and words, popularity, For You's variety, and tracking links."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.discover import cache, counters, service
from app.api.events import recommendations
from app.api.events.models import EventCategories, EventPageViews, Events
from app.api.links.models import TrackingClicks
from app.api.orgs.models import Organizations, OrgTypes
from app.api.threads import service as threads_service
from app.api.threads.models import ClubThreads, ThreadComments, ThreadLikes, ThreadPollOptions, ThreadViews, ThreadVotes
from app.core.notifications import service as push
from app.core.rate_limit import limiter
from tests.conftest import auth_header
from tests.racing import beaten_to, statements

API = "/api/v1/discover"
BLR = (12.9716, 77.5946)


@pytest.fixture
async def world(client, session, make_club, make_user, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    pushed: list = []

    async def fake_push(session_, user_ids, title, body, data=None, image_url=None):
        pushed.append(sorted(user_ids))
        return len(user_ids)

    monkeypatch.setattr(push, "send_notification_to_users", fake_push)

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(threads_service, "session_scope", scope, raising=False)  # (no such hook before followers were told after the answer)
    now = datetime.now(timezone.utc)
    tech = EventCategories(name="Tech")
    session.add(tech)
    await session.flush()
    owner, club = await make_club(name="Integrity Club", email="integrity@example.com")
    club.lat, club.lng = BLR
    fans = [await make_user(email=f"integrity-fan-{i}@example.com") for i in range(3)]
    await session.commit()
    return dict(
        now=now, tech=tech, owner=owner, club=club, fans=fans, pushed=pushed,
        h=auth_header(await mint_token(owner)), fan_h=[auth_header(await mint_token(f)) for f in fans],
    )


async def _event(session, world, name, *, days=3, club=None, **kw):
    kw.setdefault("lifecycle", EventStatus.published)
    e = Events(slug=f"i-{uuid.uuid4().hex[:10]}", name=name, images=[], event_datetime=world["now"] + timedelta(days=days), duration=2.0,
               club_id=(club or world["club"]).id, category_id=world["tech"].id, **kw)
    session.add(e)
    await session.flush()
    return e


def _ids(items):
    return [x["id"] for x in items]


async def _refresh_popularity(session):
    """As the counter loop does every few minutes."""
    from app.api.discover import popularity

    return await popularity.refresh(session)


# --- 4. Blocked organisations ----------------------------------------------------------


async def test_a_blocked_organisations_clubs_and_events_leave_every_public_list(client, session, world, mint_token, make_user):
    club = world["club"]
    e = await _event(session, world, "Blocked Org Night", lat=BLR[0], lng=BLR[1])
    post = (await client.post("/api/v1/clubs/threads", data={"body": "Hello from a college", "notify_followers": "false"}, headers=world["h"])).json()
    org = Organizations(name="Blocked College", type=OrgTypes.college)
    session.add(org)
    await session.flush()
    club.org_id = org.id
    reader = await make_user(email="integrity-reader@example.com")
    session.add(ClubUsersLink(club_id=club.id, user_id=reader.id, is_following=True))
    await session.commit()
    try:
        await _refresh_popularity(session)
    except ImportError:  # before popularity was stored, "popular" needed no refresh
        pass
    rh = auth_header(await mint_token(reader))

    async def seen():
        home = (await client.get(f"{API}/home")).json()
        return dict(
            events=e.id in _ids((await client.get(f"{API}/events")).json()["items"]),
            popular=e.id in _ids(home["popular"]),
            clubs=club.id in _ids((await client.get(f"{API}/clubs")).json()["items"]),
            page=(await client.get(f"{API}/events/{e.slug}")).status_code == 200,
            club_page=(await client.get(f"{API}/clubs/{club.slug}")).status_code == 200,
            sitemap=e.slug in {x["slug"] for x in (await client.get(f"{API}/sitemap")).json()["events"]},
            threads=post["id"] in [t["id"] for t in (await client.get(f"{API}/threads")).json()["items"]],
            post=(await client.get(f"/api/v1/threads/{post['id']}")).status_code == 200,
            feed=post["id"] in [t["id"] for t in (await client.get("/api/v1/threads/feed", headers=rh)).json()["items"]],
            for_you=e.id in [x["id"] for x in (await client.get("/api/v1/events/for-you", headers=rh)).json()["items"]],
            app_list=club.id in [c["id"] for c in (await client.get("/api/v1/clubs/list", headers=rh)).json()["items"]],
            club_info=(await client.get(f"/api/v1/clubs/info/{club.id}", headers=rh)).status_code == 200,
            live=(await client.get(f"/api/v1/events/{e.id}/live")).status_code == 200,
        )

    shown = await seen()
    assert all(shown.values()), shown
    org.is_blocked = True
    await session.commit()
    recommendations.forget(reader.id)
    hidden = await seen()
    assert not any(hidden.values()), hidden  # was public everywhere but the app's club list and home
    assert await session.scalar(select(Clubs.listed).where(Clubs.id == club.id)) is False
    org.is_blocked = False
    await session.commit()
    recommendations.forget(reader.id)
    assert all((await seen()).values())


# --- 3. Counters that cannot lose a write ----------------------------------------------


async def test_a_double_tap_on_like_or_vote_is_answered_and_the_counts_stay_exact(client, session, world):
    h, fan_h = world["h"], world["fan_h"]
    post = (await client.post("/api/v1/clubs/threads", data={"body": "Which night?", "notify_followers": "false", "poll_options": '["Friday","Saturday"]'}, headers=h)).json()
    tid = post["id"]
    fan = world["fans"][0]
    # The other tap's like lands between this one's check and its insert.
    with beaten_to(session, "thread_likes", "INSERT INTO thread_likes (id, thread_id, user_id, created_at, updated_at) VALUES (gen_random_uuid(), $1::uuid, $2, now(), now())", tid, fan.id) as fired:
        r = await client.post(f"/api/v1/threads/{tid}/like", headers=fan_h[0])
    assert fired and r.status_code == 200, r.text
    likes, rows = (await session.execute(text("SELECT likes_count, (SELECT count(*) FROM thread_likes WHERE thread_id = :t) FROM club_threads WHERE id = :t"), {"t": tid})).one()
    assert likes == rows  # the count follows the rows, whichever tap won
    friday = post["poll"]["options"][0]["id"]
    with beaten_to(
        session, "thread_votes",
        "INSERT INTO thread_votes (id, thread_id, option_id, user_id, created_at, updated_at) VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, now(), now())",
        tid, friday, fan.id,
    ) as fired:
        r = await client.post(f"/api/v1/threads/{tid}/vote", json={"option_id": friday}, headers=fan_h[0])
    assert fired and r.status_code == 200, r.text
    assert r.json()["votes"] == 1  # one vote, not two
    # Many readers at once never lose one: the counts are the rows.
    for i, fh in enumerate(fan_h):
        await client.post(f"/api/v1/threads/{tid}/like", headers=fh)
        await client.post(f"/api/v1/threads/{tid}/vote", json={"option_id": post["poll"]["options"][i % 2]["id"]}, headers=fh)
        await client.post(f"/api/v1/threads/{tid}/comments", json={"body": f"Comment {i}"}, headers=fh)
    assert await counters.drift_more(session) == {"posts": [], "poll_options": [], "questions": []}


async def test_counters_put_wrong_by_hand_are_found_and_recounted(client, session, world, make_admin, mint_token):
    post = (await client.post("/api/v1/clubs/threads", data={"body": "Drift", "notify_followers": "false", "poll_options": '["A","B"]'}, headers=world["h"])).json()
    tid = uuid.UUID(post["id"])
    await client.post(f"/api/v1/threads/{tid}/like", headers=world["fan_h"][0])
    await client.post(f"/api/v1/threads/{tid}/comments", json={"body": "One"}, headers=world["fan_h"][0])
    await client.post(f"/api/v1/threads/{tid}/vote", json={"option_id": post["poll"]["options"][0]["id"]}, headers=world["fan_h"][1])
    await client.get(f"/api/v1/threads/{tid}", headers=world["fan_h"][2])
    # What a restore with triggers off, or a hand edit, leaves behind.
    await session.execute(text("UPDATE club_threads SET likes_count = 9, comments_count = 0, views_count = 40 WHERE id = :t"), {"t": tid})
    await session.execute(text("UPDATE thread_poll_options SET votes_count = 7 WHERE thread_id = :t"), {"t": tid})
    found = await counters.drift_more(session, size=1)
    assert found["posts"] == [tid] and len(found["poll_options"]) == 2
    admin = await make_admin(email="integrity-admin@example.com")
    r = await client.post("/api/v1/admin/discover/reconcile-counters", headers=auth_header(await mint_token(admin)))
    assert r.json() == {"events": 0, "clubs": 0, "posts": 1, "poll_options": 2, "questions": 0}
    t = (await session.execute(text("SELECT likes_count, comments_count, views_count FROM club_threads WHERE id = :t"), {"t": tid})).one()
    assert tuple(t) == (1, 1, 1)
    assert await counters.drift_more(session) == {"posts": [], "poll_options": [], "questions": []}


async def test_views_are_counted_in_batches_once_a_visitor_and_never_from_the_first_forwarded_hop(client, session, world, make_event):
    from app.api.discover import visitors
    from app.api.events import views as event_views

    post = (await client.post("/api/v1/clubs/threads", data={"body": "Seen?", "notify_followers": "false"}, headers=world["h"])).json()
    url = f"/api/v1/clubs/{world['club'].id}/threads"
    cache.reset(enabled=True)  # the process's deferrals on: views wait in the buffer
    try:
        with statements(session) as seen:
            for i in range(5):
                await client.get(url, headers={"user-agent": "phone", "x-forwarded-for": f"49.36.113.{i}"})
        assert not [s for s in seen if "thread_views" in s]  # reading writes nothing
        assert len(threads_service.VIEWS) == 5
        # A client cannot be five people by writing five first hops: the proxy's
        # own hop (the last) is who it is.
        for i in range(5):
            await client.get(url, headers={"user-agent": "phone", "x-forwarded-for": f"106.51.100.{i}, 49.36.113.250"})
        assert len(threads_service.VIEWS) == 6
        await threads_service.VIEWS.write(session)
        await session.commit()
        assert await session.scalar(select(ClubThreads.views_count).where(ClubThreads.id == uuid.UUID(post["id"]))) == 6
        assert await session.scalar(select(func.count()).select_from(ThreadViews)) == 6
        # Anonymous hashes are salted per day: none is a plain sha256 of address and browser.
        import hashlib

        plain = hashlib.sha256(b"49.36.113.0|phone").hexdigest()[:40]
        assert not await session.scalar(select(func.count()).select_from(ThreadViews).where(ThreadViews.visitor == f"a:{plain}"))
        assert visitors.anonymous(type("R", (), {"headers": {"user-agent": "phone"}, "client": type("C", (), {"host": "49.37.1.1"})()})(), day="2026-10-18") != \
            visitors.anonymous(type("R", (), {"headers": {"user-agent": "phone"}, "client": type("C", (), {"host": "49.37.1.1"})()})(), day="2026-10-19")

        # An event page: once a visitor a day, the club's own look not at all, rows and count together.
        event = await make_event(club=world["club"], created_by=world["owner"], name="Viewed Night", event_datetime=world["now"] + timedelta(days=2))
        await session.commit()
        for i in range(3):
            await client.get(f"/api/v1/events/count-view/{event.id}", headers={"user-agent": "browser", "x-forwarded-for": "49.36.113.9"})
        await client.get(f"/api/v1/events/count-view/{event.id}", headers={"user-agent": "browser", "x-forwarded-for": "49.36.113.10"})
        await client.get(f"/api/v1/events/count-view/{event.id}", headers=world["h"])
        assert (await client.get("/api/v1/events/count-view/999999999")).status_code == 404
        assert len(event_views.VIEWS) == 2
        await event_views.VIEWS.write(session)
        await session.commit()
        assert await session.scalar(select(Events.page_views).where(Events.id == event.id)) == 2
        ips = set((await session.scalars(select(EventPageViews.ip_address).where(EventPageViews.event_id == event.id))).all())
        assert ips == {"49.36.113.9", "49.36.113.10"}
    finally:
        cache.reset(enabled=False)


# --- 6. The app's club list, info, follow, pin and hide --------------------------------


async def test_the_apps_club_list_reads_the_counter_and_follows_are_upserts(client, session, world, make_club, make_user, mint_token):
    fan_h, fans = world["fan_h"], world["fans"]
    club = world["club"]
    for fh in fan_h:
        assert (await client.post(f"/api/v1/clubs/follow/{club.id}", headers=fh)).status_code == 200
    # A double tap on follow: the other tap's row lands between this one's
    # check and its insert; this one is a 409, not a 500 on the unique key.
    tapper = await make_user(email="integrity-tap2@example.com")
    th = auth_header(await mint_token(tapper))
    with beaten_to(
        session, "club_users_link",
        "INSERT INTO club_users_link (club_id, user_id, is_following, is_pinned, is_hidden, is_deleted, created_at, updated_at) VALUES ($1, $2, true, false, false, false, now(), now())",
        club.id, tapper.id,
    ) as fired:
        r = await client.post(f"/api/v1/clubs/follow/{club.id}", headers=th)
    assert fired and r.status_code == 409, r.text
    # Pin and hide flip in one statement each, so a double tap is two flips, never a 500.
    assert (await client.post(f"/api/v1/clubs/pin/{club.id}", headers=th)).json()["is_pinned"] is True
    assert (await client.post(f"/api/v1/clubs/pin/{club.id}", headers=th)).json()["is_pinned"] is False
    assert (await client.post(f"/api/v1/clubs/hide/{club.id}", headers=th)).json()["is_hidden"] is True

    listed = (await client.get("/api/v1/clubs/list", headers=fan_h[0])).json()
    me = next(c for c in listed["items"] if c["id"] == club.id)
    assert me["followers_count"] == 4 and me["is_following"] is True
    with statements(session) as seen:
        await client.get("/api/v1/clubs/list?limit=5", headers=fan_h[0])
    assert not [s for s in seen if "count(DISTINCT" in s or "count(distinct" in s], seen  # no count over every follow
    info = (await client.get(f"/api/v1/clubs/info/{club.id}", headers=fan_h[0])).json()
    assert info["followers_count"] == 4 and info["user_data"]["is_following"] is True
    # The followers, a page at a time by cursor: every follower once, newest first.
    first = (await client.get(f"/api/v1/clubs/followers/{club.id}?limit=2", headers=fan_h[0])).json()
    assert len(first["items"]) == 2 and first["total"] == 4 and "after=" in first["next"]
    second = (await client.get(first["next"], headers=fan_h[0])).json()
    assert len(second["items"]) == 2 and second["next"] is None
    assert len({i["user"]["id"] for i in first["items"] + second["items"]}) == 4


async def test_the_public_club_list_total_stops_at_the_cap_and_pages_go_on(client, session, world, make_club, monkeypatch):
    from app.api.clubs import service as clubs_service

    monkeypatch.setattr(clubs_service, "LIST_COUNT_CAP", 3)
    for i in range(5):
        await make_club(name=f"Capped Club {i}", email=f"capped-{i}@example.com")
    await session.commit()
    seen, url = [], "/api/v1/clubs/list?limit=2"
    while url:
        page = (await client.get(url, headers=world["fan_h"][0])).json()
        seen += [c["id"] for c in page["items"]]
        assert page["total"] >= 3  # "3+" past the cap, never a count of them all
        url = page["next"]
    assert len(seen) == len(set(seen)) == 6  # every listed club, each once: the pages went on past the cap


# --- 7. Threads --------------------------------------------------------------------------


async def test_the_followers_feed_merges_each_clubs_newest_and_pages_by_cursor(client, session, world, make_club, mint_token):
    fan_h = world["fan_h"][0]
    clubs = [world["club"]]
    for i in range(2):
        owner, c = await make_club(name=f"Feed Club {i}", email=f"feed-{i}@example.com")
        clubs.append(c)
    await session.commit()
    base = datetime.now(timezone.utc) - timedelta(days=1)
    made = []
    for n in range(15):
        c = clubs[n % 3]
        t = ClubThreads(club_id=c.id, body=f"Post {n}", kind="post", created_at=base + timedelta(minutes=n))
        session.add(t)
        made.append(t)
    await session.flush()
    for c in clubs[:2]:  # follows two of the three
        await client.post(f"/api/v1/clubs/follow/{c.id}", headers=fan_h)
    want = [str(t.id) for t in sorted(made, key=lambda t: t.created_at, reverse=True) if t.club_id in {clubs[0].id, clubs[1].id}]
    got, after = [], None
    while True:
        params = {"limit": 4} | ({"after": after} if after else {})
        page = (await client.get("/api/v1/threads/feed", params=params, headers=fan_h)).json()
        got += [x["id"] for x in page["items"]]
        assert page["total"] == len(want)
        after = page["next_cursor"]
        if not after:
            break
    assert got == want
    by_offset = (await client.get("/api/v1/threads/feed", params={"limit": 4, "offset": 4}, headers=fan_h)).json()
    assert [x["id"] for x in by_offset["items"]] == want[4:8]
    assert (await client.get("/api/v1/threads/feed", params={"after": "garbage"}, headers=fan_h)).status_code == 422
    assert (await client.get("/api/v1/threads/feed", params={"offset": 5000}, headers=fan_h)).status_code == 422


async def test_comments_page_by_cursor_and_insights_count_in_the_database(client, session, world):
    h, fan_h = world["h"], world["fan_h"]
    post = (await client.post("/api/v1/clubs/threads", data={"body": "Talk to us", "notify_followers": "false"}, headers=h)).json()
    for i in range(7):
        await client.post(f"/api/v1/threads/{post['id']}/comments", json={"body": f"Comment {i}"}, headers=fan_h[i % 3])
    seen, after = [], None
    while True:
        r = await client.get(f"/api/v1/threads/{post['id']}/comments", params={"limit": 3} | ({"after": after} if after else {}))
        seen += [c["body"] for c in r.json()]
        after = r.headers.get("x-next-cursor")
        if not after:
            break
    assert seen == [f"Comment {i}" for i in range(7)]
    await client.post(f"/api/v1/threads/{post['id']}/like", headers=fan_h[0])
    with statements(session) as stmts:
        i = (await client.get("/api/v1/clubs/threads/insights", headers=h)).json()
    assert i["weeks"][-1]["reactions"] == 8
    # Grouped by week in the database: no statement reads the reactions' rows one by one.
    assert not [s for s in stmts if "SELECT thread_likes.created_at" in s or "SELECT thread_comments.created_at" in s]


async def test_followers_are_told_after_the_post_is_answered(client, session, world, monkeypatch):
    for fh in world["fan_h"]:
        await client.post(f"/api/v1/clubs/follow/{world['club'].id}", headers=fh)
    told_during: list = []
    original = threads_service.create

    async def create(*args, **kwargs):
        out = await original(*args, **kwargs)
        told_during.append(len(world["pushed"]))  # nothing pushed while the post is made
        return out

    monkeypatch.setattr(threads_service, "create", create)
    monkeypatch.setattr(threads_service, "TELL_BATCH", 2)
    r = await client.post("/api/v1/clubs/threads", data={"body": "Big news", "notify_followers": "true"}, headers=world["h"])
    assert r.status_code == 201
    assert told_during == [0]
    # After the answer, in batches of TELL_BATCH: every follower, once.
    assert sorted(u for batch in world["pushed"] for u in batch) == sorted(f.id for f in world["fans"])
    assert [len(b) for b in world["pushed"]] == [2, 1]


# --- 8. Discover's paging, words, popularity, variety ---------------------------------


async def test_offset_stops_at_a_thousand_and_a_cursor_pages_every_order(client, session, world):
    for n in range(9):
        await _event(session, world, f"Paged {n}", days=1 + n, page_views=(n * 7) % 5, lat=BLR[0] + n * 0.01, lng=BLR[1])
    await _event(session, world, "Paged online", days=4, is_online=True)
    await session.commit()
    await _refresh_popularity(session)
    assert (await client.get(f"{API}/events", params={"offset": 1001})).status_code == 422
    assert (await client.get(f"{API}/clubs", params={"offset": 1001})).status_code == 422
    assert (await client.get(f"{API}/threads", params={"offset": 1001})).status_code == 422
    # By distance anywhere, the cursor crosses from the placed part to the rest (the online one).
    for params in ({"sort": "date"}, {"sort": "popular"}, {"sort": "newest"}, {"q": "paged"}, {"sort": "distance", "lat": BLR[0], "lng": BLR[1], "radius_km": 500},
                   {"sort": "distance", "lat": BLR[0], "lng": BLR[1], "radius_km": 0}):
        everything = _ids((await client.get(f"{API}/events", params={**params, "limit": 60})).json()["items"])
        got, after = [], None
        while True:
            page = (await client.get(f"{API}/events", params={**params, "limit": 3} | ({"after": after} if after else {}))).json()
            got += _ids(page["items"])
            after = page["next_cursor"]
            if not after:
                break
        assert got == everything and len(got) >= 9, params
    other = (await client.get(f"{API}/events", params={"sort": "date", "limit": 3})).json()["next_cursor"]
    assert (await client.get(f"{API}/events", params={"sort": "popular", "after": other})).status_code == 422  # another order's cursor


async def test_every_word_must_match_and_short_words_are_whole_words(client, session, world):
    ai = await _event(session, world, "AI Summit", location_name="Kochi")
    chair = await _event(session, world, "Chair Yoga", location_name="Kochi")
    robots = await _event(session, world, "Robotics Night", location_name="Lab 3")
    await session.commit()

    async def ids(q):
        return set(_ids((await client.get(f"{API}/events", params={"q": q})).json()["items"]))

    assert await ids("ai") == {ai.id}  # not "Chair"
    assert await ids("ai kochi") == {ai.id}  # both words, anywhere
    assert await ids("kochi") == {ai.id, chair.id}
    assert await ids("robotics lab") == {robots.id}  # was nothing: "robotics lab" appears nowhere as one string
    assert await ids("robotics kochi") == set()  # was every match of either before a typo pass
    assert await ids("yoga a") == {chair.id}  # a single letter is no word


async def test_popular_reads_the_stored_number_on_its_index(client, session, world):
    old = await _event(session, world, "Old Big", days=5, page_views=400)
    old.created_at = world["now"] - timedelta(days=150)
    new = await _event(session, world, "New Small", days=6, page_views=20)
    await session.commit()
    from app.api.discover import popularity

    assert await popularity.refresh(session) >= 2
    assert await popularity.refresh(session) == 0  # only what changed is written again
    got = _ids((await client.get(f"{API}/events", params={"sort": "popular"})).json()["items"])
    # Twenty times the attention, five months older: decayed, the new one ranks first.
    assert got.index(new.id) < got.index(old.id)
    v_old = await session.scalar(select(Events.popularity).where(Events.id == old.id))
    assert v_old == pytest.approx(popularity.value(0, 400, old.created_at))
    # The order can be read off the index, no sort (on a table this small the
    # planner may prefer a sort, so the choice is taken from it here).
    await session.execute(text("SET LOCAL enable_sort = off"))
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(r[0] for r in (await session.execute(text(
        "EXPLAIN SELECT id FROM events WHERE NOT is_deleted AND status IN ('published', 'live') ORDER BY popularity DESC, event_datetime, id LIMIT 24"
    ))).all())
    await session.execute(text("RESET enable_sort"))
    await session.execute(text("RESET enable_seqscan"))
    assert "ix_events_shown_popular" in plan and "Sort" not in plan, plan


async def test_for_you_shows_at_most_three_of_one_club_in_the_first_twenty(client, session, world, make_club, mint_token):
    busy = world["club"]
    for n in range(10):
        await _event(session, world, f"Busy {n}", days=1 + n * 0.1)
    others = []
    for i in range(4):
        owner, c = await make_club(name=f"Quiet Club {i}", email=f"quiet-{i}@example.com")
        others.append(c)
        await _event(session, world, f"Quiet {i}", days=5 + i, club=c)
    await session.commit()
    fan_h = world["fan_h"][0]
    await client.post(f"/api/v1/clubs/follow/{busy.id}", headers=fan_h)
    await _refresh_popularity(session)
    items = (await client.get("/api/v1/events/for-you", params={"limit": 20}, headers=fan_h)).json()["items"]
    clubs = [x["club"]["id"] for x in items]
    # The followed club's events score highest, but only three lead; then
    # every other club's, then the rest of the busy club's (there is nothing
    # else to fill the first twenty with).
    assert clubs[:3] == [busy.id] * 3
    assert set(clubs[3:3 + len(others)]) == {c.id for c in others}
    assert clubs[3 + len(others):] == [busy.id] * 7


async def test_near_you_stops_reading_once_it_is_settled(client, session, world, monkeypatch):
    monkeypatch.setattr(service, "NEAR_CHUNK", 4)
    for n in range(40):
        await _event(session, world, f"Near {n}", days=1 + n * 0.1, lat=BLR[0] + 0.001 * (n % 7), lng=BLR[1])
    await session.commit()
    with statements(session) as seen:
        home = (await client.get(f"{API}/home", params={"lat": BLR[0], "lng": BLR[1]})).json()
    assert len(home["near"]) == service.NEAR_COUNT
    # Each chunk walks the grid cells around the point (a few rows per cell, in date order).
    chunks = [s for s in seen if s.lstrip().startswith("SELECT per_cell.id, per_cell.lat, per_cell.lng, per_cell.event_datetime")]
    assert len(chunks) == 2  # 4 then 16: settled by the twelfth sure one, never all forty
    assert all("events.geocell = near_cells.cell" in s for s in chunks)


async def test_the_link_scan_reads_a_few_rows_and_skips_what_was_just_tried(session, world):
    from app.api.discover import geo

    for n in range(6):
        await _event(session, world, f"Linked {n}", location_link=f"https://maps.app.goo.gl/x{n}")
    await session.commit()
    first = await geo.missing(session, 4)
    assert len(first) == 4
    await session.execute(Events.__table__.update().where(Events.id.in_([i for _, i in first])).values(link_checked_at=datetime.now(timezone.utc)))
    later = await geo.missing(session, 4)
    assert not set(later) & set(first) and len(later) == 2


# --- 10. Tracking links ------------------------------------------------------------------


async def test_previews_crawlers_and_heads_are_not_clicks_and_the_short_link_is_limited(client, session, world, monkeypatch):
    link = (await client.post("/api/v1/clubs/links", json={"name": "Bio", "utm_source": "insta", "utm_medium": "bio"}, headers=world["h"]))
    if link.status_code == 403:  # the plan gate: links need a plan
        from app.config import settings

        monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", False)
        link = await client.post("/api/v1/clubs/links", json={"name": "Bio", "utm_source": "insta", "utm_medium": "bio"}, headers=world["h"])
    code = link.json()["code"]
    for ua in ("WhatsApp/2.23.20.0 A", "facebookexternalhit/1.1", "Twitterbot/1.0", "Mozilla/5.0 (compatible; Discordbot/2.0)", "TelegramBot (like TwitterBot)", "Slackbot-LinkExpanding 1.0", "LinkedInBot/1.0"):
        r = await client.get(f"/l/{code}", headers={"user-agent": ua})
        assert r.status_code == 302
    assert (await client.head(f"/l/{code}", headers={"user-agent": "Mozilla/5.0 (iPhone) Mobile Safari"})).status_code == 302
    assert await session.scalar(select(func.count()).select_from(TrackingClicks)) == 0
    assert (await client.get(f"/l/{code}", headers={"user-agent": "Mozilla/5.0 (iPhone) Mobile Safari"})).status_code == 302
    assert await session.scalar(select(func.count()).select_from(TrackingClicks)) == 1
    # Limited per address, generously.
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    from app.api.links import router as links_router

    hits = [await client.get(f"/l/{code}", headers={"user-agent": "x", "x-forwarded-for": "49.36.113.77"}) for _ in range(5)]
    assert all(h.status_code == 302 for h in hits)
    assert any(r.path == "/l/{code}" for r in links_router.public_router.routes)


async def test_the_tracking_click_index_is_declared(session):
    names = (await session.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'tracking_clicks'"))).scalars().all()
    assert "ix_tracking_clicks_link_created" in names


async def test_the_links_page_counts_in_the_database(client, session, world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", False)
    link = (await client.post("/api/v1/clubs/links", json={"name": "Poster", "utm_source": "poster", "utm_medium": "print"}, headers=world["h"])).json()
    for i in range(30):
        await client.get(f"/l/{link['code']}", headers={"user-agent": f"Mozilla/5.0 (iPhone) {i % 4}", "x-forwarded-for": f"49.36.113.{i % 6}"})
    await client.post(f"/api/v1/clubs/follow/{world['club'].id}?ref={link['code']}", headers=world["fan_h"][0])
    with statements(session) as seen:
        page = (await client.get("/api/v1/clubs/links/overview", headers=world["h"])).json()
    row = next(x for x in page["links"] if x["id"] == link["id"])
    pairs = len({(i % 4, i % 6) for i in range(30)})  # browser and address: the visitor
    assert row["clicks"] == 30 and row["uniques"] == pairs and row["conversions"] == 1 and page["totals"]["uniques"] == pairs
    assert sum(d["clicks"] for d in page["series"]) == 30 and sum(d["conversions"] for d in page["series"]) == 1
    # Grouped in the database: no statement reads the clicks themselves.
    assert not [s for s in seen if s.lstrip().startswith("SELECT tracking_clicks.id")], seen
    detail = (await client.get(f"/api/v1/clubs/links/{link['id']}/detail", headers=world["h"])).json()
    assert detail["clicks"] == 30 and sum(d["clicks"] for d in detail["devices"]) == 30 and sum(h["clicks"] for h in detail["hours"]) == 30


async def test_concurrent_misses_and_revalidation_build_once(session):
    small = cache.TTLCache(ttl=0.05, maxsize=10, register_it=False)
    calls = []

    async def build():
        calls.append(1)
        await asyncio.sleep(0.01)
        return len(calls)

    assert await small.remember(("x",), build, stale=10, refresh=build) == 1
    await asyncio.sleep(0.06)
    # Past its time: everyone gets the old value at once, one rebuild runs.
    got = await asyncio.gather(*(small.remember(("x",), build, stale=10, refresh=build) for _ in range(5)))
    assert got == [1] * 5
    await small.settle()
    assert calls == [1, 1] and await small.remember(("x",), build, stale=10, refresh=build) == 2
