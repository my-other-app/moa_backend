"""Telling the public site that a page changed (app/core/site_revalidate.py):
after the commit, one POST per burst, with the event's slug and id, its
club's slug and the sitemap, and nothing for what no visitor sees. The site
is a fake here; nothing reaches the network."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.api.closeout.models import EventStatus
from app.api.clubs.models import Clubs, ClubTeamMembers
from app.api.discover import cache
from app.api.events.models import EventCategories, Events, EventSpeakers
from app.config import settings
from app.core import after_commit, site_revalidate
from tests.conftest import auth_header

URL = "https://site.test/api/revalidate"
SECRET = "revalidate-secret-for-the-suite"


@pytest.fixture
def site(monkeypatch):
    """The site: every POST it gets, as {url, auth, body}."""
    got = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append({"url": str(request.url), "auth": request.headers.get("authorization"), "body": json.loads(request.content)})
        return httpx.Response(200, json={"revalidated": True})

    monkeypatch.setattr(settings, "SITE_REVALIDATE_SECRET", SECRET)
    monkeypatch.setattr(settings, "SITE_REVALIDATE_URL", URL)
    monkeypatch.setattr(site_revalidate, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=site_revalidate.TIMEOUT_SECONDS))
    site_revalidate.reset_for_tests()
    yield got
    site_revalidate.reset_for_tests()


async def told(got) -> tuple[set, set]:
    """What the site has been told since last asked: (event refs, club slugs)."""
    await site_revalidate.flush()
    events = {e for g in got for e in g["body"]["events"]}
    clubs = {c for g in got for c in g["body"]["clubs"]}
    got.clear()
    return events, clubs


def refs(event) -> set:
    return {event.slug, str(event.id)}


@pytest.fixture
async def world(session, site, make_club, make_event, mint_token):
    from app.api.interests.models import InterestCategory, Interests

    owner, club = await make_club(name="Revalidate Society", email="reval-owner@example.com")
    category = EventCategories(name="Revalidate Talks")
    icat = InterestCategory(name="Revalidate interests")
    session.add_all([category, icat])
    await session.flush()
    interest = Interests(name="Revalidate AI", category_id=icat.id)
    session.add(interest)
    now = datetime.now(timezone.utc)
    event = await make_event(club=club, created_by=owner, name="Revalidate Night", event_datetime=now + timedelta(days=9), category_id=category.id)
    await session.commit()
    form = {
        "event_datetime": (now + timedelta(days=12)).isoformat(), "duration": "2",
        "has_fee": "false", "status": "published", "category_id": str(category.id), "interest_ids": str(interest.id),
    }
    await told(site)  # what setting the world up told is not what a test is about
    return dict(owner=owner, club=club, event=event, category=category, now=now, form=form, h=auth_header(await mint_token(owner)))


# ─── What is told ─────────────────────────────────────────────────────────


async def test_a_new_event_is_told_with_its_club_once_committed(client, session, site, world):
    made = await client.post("/api/v1/events/create", data={**world["form"], "name": "Brand New Night"}, headers=world["h"])
    assert made.status_code in (200, 201), made.text
    event = await session.get(Events, made.json()["id"])
    await site_revalidate.flush()

    assert len(site) == 1
    post = site[0]
    assert post["url"] == URL
    assert post["auth"] == f"Bearer {SECRET}"
    assert refs(event) <= set(post["body"]["events"])
    assert world["club"].slug in post["body"]["clubs"]
    assert post["body"]["sitemap"] is True
    assert set(post["body"]) == {"events", "clubs", "sitemap"}


async def test_an_edit_is_told_through_the_dashboard(client, session, site, world):
    made = await client.post("/api/v1/events/create", data={**world["form"], "name": "Edited Night"}, headers=world["h"])
    event_id = made.json()["id"]
    await told(site)

    r = await client.put(f"/api/v1/events/update/{event_id}", data={**world["form"], "name": "Edited Night", "about": "Now with snacks"}, headers=world["h"])
    assert r.status_code == 200, r.text
    events, clubs = await told(site)
    assert str(event_id) in events and world["club"].slug in clubs


async def test_counters_and_housekeeping_tell_nothing(session, site, world):
    event = world["event"]
    event.page_views = (event.page_views or 0) + 1
    event.going_count = 5
    event.kiosk_pin = "1234"
    event.retention_hours = 24
    await session.commit()
    assert await told(site) == (set(), set())

    event.about = "A real change"
    await session.commit()
    events, clubs = await told(site)
    assert refs(event) <= events and clubs == {world["club"].slug}


async def test_a_renamed_slug_tells_the_old_page_and_the_new(session, site, world):
    event = world["event"]
    old = event.slug
    event.slug = "revalidate-night-renamed"
    await session.commit()
    events, _ = await told(site)
    assert {old, "revalidate-night-renamed", str(event.id)} <= events


async def test_drafts_are_quiet_until_published_and_told_when_unpublished(client, session, site, world, make_event):
    draft = await make_event(club=world["club"], created_by=world["owner"], name="Secret Plans", category_id=world["category"].id, lifecycle=EventStatus.draft)
    await session.commit()
    draft.about = "still a secret"
    await session.commit()
    assert await told(site) == (set(), set())

    url = f"/api/v1/events/{draft.id}/closeout/settings"
    assert (await client.patch(url, json={"status": "published"}, headers=world["h"])).status_code == 200
    assert refs(draft) <= (await told(site))[0]

    assert (await client.patch(url, json={"status": "draft"}, headers=world["h"])).status_code == 200
    assert refs(draft) <= (await told(site))[0]

    await session.refresh(draft)
    draft.about = "back to secret"
    await session.commit()
    assert await told(site) == (set(), set())


async def test_cancelling_an_event_is_told(client, session, site, world):
    event = world["event"]
    r = await client.delete(f"/api/v1/events/delete/{event.id}", headers=world["h"])
    assert r.status_code == 200, r.text
    events, clubs = await told(site)
    assert refs(event) <= events and world["club"].slug in clubs


async def test_the_status_clock_is_told(session, site, world):
    from app.api.closeout.service import advance_clock

    event = world["event"]
    assert await advance_clock(session, now=event.event_datetime + timedelta(minutes=5)) == 1
    assert refs(event) <= (await told(site))[0]


async def test_a_club_profile_change_is_told_with_its_events(client, session, site, world, make_event):
    draft = await make_event(club=world["club"], created_by=world["owner"], name="Club Draft", category_id=world["category"].id, lifecycle=EventStatus.draft)
    await session.commit()
    await told(site)

    r = await client.put("/api/v1/clubs/update", data={"name": world["club"].name, "tagline": "Now with a tagline"}, headers=world["h"])
    assert r.status_code == 200, r.text
    events, clubs = await told(site)
    assert clubs == {world["club"].slug}
    # The club's events show the club, so they go too; its draft has no page.
    assert refs(world["event"]) <= events
    assert draft.slug not in events


async def test_approval_is_told_and_rejecting_a_club_that_never_had_a_page_is_not(client, session, site, make_club, make_admin, mint_token):
    admin = await make_admin(email="reval-admin@example.com")
    h = auth_header(await mint_token(admin))
    _, pending = await make_club(name="Pending Club", email="reval-pending@example.com", is_verified=False)
    _, hopeless = await make_club(name="Hopeless Club", email="reval-hopeless@example.com", is_verified=False)
    _, live = await make_club(name="Live Club", email="reval-live@example.com")
    await session.commit()
    # A new club with a page is told; one waiting for approval has none.
    assert await told(site) == (set(), {live.slug})

    assert (await client.post(f"/api/v1/clubs/{pending.id}/approve", headers=h)).status_code == 200
    assert (await told(site))[1] == {pending.slug}

    assert (await client.post(f"/api/v1/clubs/{hopeless.id}/reject", headers=h)).status_code == 200
    assert await told(site) == (set(), set())

    assert (await client.post(f"/api/v1/clubs/{live.id}/reject", headers=h)).status_code == 200
    assert (await told(site))[1] == {live.slug}


async def test_the_rows_on_a_page_are_told(session, site, world):
    session.add(EventSpeakers(event_id=world["event"].id, name="Dr. Rao"))
    session.add(ClubTeamMembers(club_id=world["club"].id, name="Asha"))
    await session.commit()
    events, clubs = await told(site)
    assert refs(world["event"]) <= events and clubs == {world["club"].slug}


# ─── When, and how ────────────────────────────────────────────────────────


def test_the_app_watches_every_session():
    import doth
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    assert vars(doth)["site_revalidate"] is site_revalidate  # the app imports the watcher...
    assert event.contains(Session, "after_flush", site_revalidate._after_flush)  # ...which hooks every session


async def test_nothing_is_told_without_a_secret(session, site, world, monkeypatch):
    monkeypatch.setattr(settings, "SITE_REVALIDATE_SECRET", "")
    world["event"].about = "changed"
    await session.commit()
    assert await told(site) == (set(), set())


async def test_a_change_rolled_back_is_never_told(session, site, world):
    event = world["event"]
    nested = await session.begin_nested()
    event.about = "never happened"
    await session.flush()
    await nested.rollback()
    await session.commit()
    assert await told(site) == (set(), set())


async def test_a_burst_is_one_post(session, site, world, make_event):
    other = await make_event(club=world["club"], created_by=world["owner"], name="Second Night", category_id=world["category"].id)
    await session.commit()
    await told(site)

    for i in range(3):
        world["event"].about = f"take {i}"
        other.about = f"take {i}"
        await session.commit()
    await site_revalidate.flush()
    assert len(site) == 1
    assert refs(world["event"]) | refs(other) <= set(site[0]["body"]["events"])


async def test_the_post_goes_by_itself_a_few_seconds_later(session, site, world, monkeypatch):
    monkeypatch.setattr(site_revalidate, "DEBOUNCE_SECONDS", 0.05)
    world["event"].about = "on its own"
    await session.commit()
    await after_commit.drain()
    for _ in range(100):
        if site:
            break
        await asyncio.sleep(0.02)
    assert len(site) == 1


async def test_the_discover_cache_is_emptied_before_the_site_asks_again(session, site, world, monkeypatch):
    cache.reset(enabled=True)
    cache.shared.put(("home", 1), {"stale": True})
    seen = []
    real = site_revalidate._send

    def send(payload):
        seen.append(len(cache.shared))
        real(payload)

    monkeypatch.setattr(site_revalidate, "_send", send)
    world["event"].about = "fresh"
    await session.commit()
    await site_revalidate.flush()
    assert seen == [0]


async def test_a_site_that_is_down_or_refuses_never_breaks_a_save(session, site, world, monkeypatch, caplog):
    def down(request):
        raise httpx.ConnectError("no route to host")

    for transport in (httpx.MockTransport(down), httpx.MockTransport(lambda r: httpx.Response(500))):
        monkeypatch.setattr(site_revalidate, "_http", lambda t=transport: httpx.Client(transport=t))
        world["event"].about = f"saved anyway via {id(transport)}"
        await session.commit()
        await site_revalidate.flush()
    await session.refresh(world["event"])
    assert world["event"].about.startswith("saved anyway")
    assert SECRET not in caplog.text


async def test_a_big_change_is_split_into_several_posts(site, monkeypatch):
    monkeypatch.setattr(site_revalidate, "MAX_PER_POST", 2)
    site_revalidate._queue(None, frozenset({"a", "b", "c"}), frozenset({"x"}))
    assert [g["body"] for g in site] == [
        {"events": ["a", "b"], "clubs": ["x"], "sitemap": True},
        {"events": ["c"], "clubs": [], "sitemap": True},
    ]


def test_without_a_loop_a_script_tells_the_site_at_once(site):
    site_revalidate._queue(None, frozenset({"some-event", "7"}), frozenset({"some-club"}))
    assert site[0]["body"] == {"events": ["7", "some-event"], "clubs": ["some-club"], "sitemap": True}


def test_the_post_is_short_and_follows_no_redirect():
    http = site_revalidate._http()
    try:
        assert http.timeout == httpx.Timeout(site_revalidate.TIMEOUT_SECONDS)
        assert site_revalidate.TIMEOUT_SECONDS <= 5
        assert http.follow_redirects is False
    finally:
        http.close()


async def test_a_club_slug_is_found_for_an_event_whose_club_was_not_loaded(session, site, world):
    """The status clock and the background map lookups load events alone."""
    event_id, club_slug = world["event"].id, world["club"].slug
    session.expunge_all()
    event = await session.scalar(select(Events).where(Events.id == event_id))
    assert "club" not in event.__dict__
    event.about = "loaded alone"
    await session.commit()
    events, clubs = await told(site)
    assert str(event_id) in events and clubs == {club_slug}
    assert (await session.scalar(select(Clubs.slug).where(Clubs.slug == club_slug))) == club_slug
