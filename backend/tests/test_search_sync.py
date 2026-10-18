"""The search outbox carried to Meilisearch (a fake: tests/fake_meili.py).

- A build puts every event that is not deleted or a draft, and every club
  not deleted, in its index, with the settings applied; a second look
  changes nothing.
- Every change reaches the index through the outbox: made, renamed, a topic
  or interest added, unpublished, published again, deleted (soft and hard),
  the status clock, a club unlisted (and so its events), a category
  renamed, a club's followers by a tenth.
- A failure leaves the outbox rows; the next drain sends them.
- Without Meilisearch the outbox is dropped, but a category, interest or
  topic change still rewrites search_text.
- The reconcile puts right a document that drifted, one missing and one
  that should not be there; the loop's tick builds missing indexes and runs
  the check on its schedule.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink
from app.api.events.models import EventCategories, EventInterestsLink, Events
from app.api.interests.models import InterestCategory, Interests
from app.api.orgs.models import Organizations, OrgTypes
from app.api.search import docs, engine, sync
from app.core import search
from app.db.listeners import add_loader_criteria
from tests import fake_meili


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


@pytest.fixture
async def meili(monkeypatch):
    fake = await fake_meili.install(monkeypatch)
    yield fake
    await search.reset()
    engine.reset()
    sync.reset()


@pytest.fixture
async def scene(session, make_club):
    tag = uuid.uuid4().hex[:6]
    _, club = await make_club(name=f"Sync Club {tag}", email=f"sync-{tag}@example.com")
    club.location_name = "Kochi"
    topic = InterestCategory(name=f"Sync Topic {tag}", slug=f"sync-topic-{tag}")
    session.add(topic)
    await session.flush()
    robotics = Interests(name=f"Robotics {tag}", slug=f"robotics-{tag}", category_id=topic.id)
    cat = EventCategories(name=f"Workshop {tag}")
    session.add_all([robotics, cat])
    await session.flush()
    now = datetime.now(timezone.utc)

    def event(name, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        return Events(slug=f"s-{uuid.uuid4().hex[:10]}", name=name, images=[], event_datetime=now + timedelta(days=2), duration=2.0,
                      club_id=club.id, category_id=cat.id, **kw)

    shown = event("Rover Night", lat=9.93, lng=76.26, location_name="Lab 3, Kochi 682022")
    closed = event("Held Before", lifecycle=EventStatus.closed)
    draft = event("Sketchy Draft", lifecycle=EventStatus.draft)
    gone = event("Gone Event", is_deleted=True)
    online = event("Web Meetup", is_online=True, lat=1.0, lng=2.0, has_fee=True, reg_fee=99)
    session.add_all([shown, closed, draft, gone, online])
    await session.flush()
    session.add(EventInterestsLink(event_id=shown.id, interest_id=robotics.id))
    await session.commit()
    return dict(tag=tag, club=club, topic=topic, robotics=robotics, cat=cat, shown=shown, closed=closed, draft=draft, gone=gone,
                online=online, event=event, now=now)


def _ours(fake, kind, scene):
    """The fake's documents for this test's rows (other tests' rows are rolled back, but ids are not reused)."""
    return fake.docs(kind)


async def test_the_build_puts_every_public_row_in_with_the_settings(session, scene, meili):
    built = await sync.ensure_built(session)
    assert built is not None and await sync.is_built(session)
    evs = meili.docs(search.EVENTS)
    for e in (scene["shown"], scene["closed"], scene["online"]):
        assert e.id in evs
    assert scene["draft"].id not in evs and scene["gone"].id not in evs
    d = evs[scene["shown"].id]
    assert d["name"] == "Rover Night" and d["club_name"].startswith("Sync Club") and d["club_location"] == "Kochi"
    assert d["topic_ids"] == [scene["topic"].id] and d["interest_ids"] == [scene["robotics"].id]
    assert d["interests"] == [scene["robotics"].name] and d["topics"] == [scene["topic"].name]
    assert d["_geo"] == {"lat": 9.93, "lng": 76.26} and d["has_point"] is True and d["city"] == "kochi"
    assert d["lifecycle"] == "published" and d["listed"] is True and d["is_online"] is False and d["has_fee"] is False
    assert d["ends_at"] - d["starts_at"] == 2 * 3600 * 1_000_000 and d["h"] == docs.doc_hash(d)
    o = evs[scene["online"].id]
    assert "_geo" not in o and o["has_point"] is True and o["is_online"] is True and o["fee"] == 99.0
    assert scene["club"].id in meili.docs(search.CLUBS)
    st = meili.indexes[search.index(search.EVENTS)]["settings"]
    assert st["rankingRules"][0] == "sort" and "topic_ids" in st["filterableAttributes"] and st["pagination"]["maxTotalHits"] == search.MAX_TOTAL_HITS
    assert st["synonyms"]["blore"] == ["bengaluru", "bangalore", "b'lore", "blr"] and "the" in st["stopWords"]
    # A second look: built, settings ours, nothing sent.
    before = len(meili.requests)
    assert await sync.ensure_built(session) is None
    assert not [r for r in meili.requests[before:] if r[0] in ("POST", "PATCH")]


async def test_every_change_reaches_the_index(session, scene, meili):
    await sync.ensure_built(session)
    await sync.drain(session)
    evs = lambda: meili.docs(search.EVENTS)  # noqa: E731
    e = scene["shown"]

    new = scene["event"]("Fresh Event")
    session.add(new)
    await session.commit()
    await sync.drain(session)
    assert evs()[new.id]["name"] == "Fresh Event"

    new.name = "Renamed Event"
    await session.commit()
    await sync.drain(session)
    assert evs()[new.id]["name"] == "Renamed Event"

    session.add(EventInterestsLink(event_id=new.id, interest_id=scene["robotics"].id))
    await session.commit()
    await sync.drain(session)
    assert evs()[new.id]["topic_ids"] == [scene["topic"].id]

    new.lifecycle = EventStatus.draft  # unpublished: out
    await session.commit()
    await sync.drain(session)
    assert new.id not in evs()
    new.lifecycle = EventStatus.published
    await session.commit()
    await sync.drain(session)
    assert new.id in evs()
    new.is_deleted = True  # deleted: out
    await session.commit()
    await sync.drain(session)
    assert new.id not in evs()

    e.lifecycle = EventStatus.live  # the status clock
    await session.commit()
    await sync.drain(session)
    assert evs()[e.id]["lifecycle"] == "live"

    hard = scene["event"]("Hard Delete")
    session.add(hard)
    await session.commit()
    await sync.drain(session)
    assert hard.id in evs()
    await session.execute(text("DELETE FROM events WHERE id = :id"), {"id": hard.id})
    await session.commit()
    await sync.drain(session)
    assert hard.id not in evs()

    # A category renamed: every event's document says so (the taxonomy path).
    scene["cat"].name = f"Atelier {scene['tag']}"
    await session.commit()
    await sync.drain(session)
    assert evs()[e.id]["category"] == f"Atelier {scene['tag']}"

    # The club's organisation blocked: the club and each of its events unlisted, in one change.
    org = Organizations(name=f"Org {scene['tag']}", type=OrgTypes.college)
    session.add(org)
    await session.flush()
    scene["club"].org_id = org.id
    await session.commit()
    await sync.drain(session)
    org.is_blocked = True
    await session.commit()
    await sync.drain(session)
    assert meili.docs(search.CLUBS)[scene["club"].id]["listed"] is False
    assert evs()[e.id]["listed"] is False and evs()[scene["closed"].id]["listed"] is False

    # Followers, by a tenth or so: a follow storm writes a few rows, not one per follow.
    await session.execute(text("DELETE FROM search_outbox"))
    for n in range(1, 40):
        await session.execute(text("UPDATE clubs SET follower_count = :n WHERE id = :id"), {"n": n, "id": scene["club"].id})
    rows = await session.scalar(text("SELECT count(*) FROM search_outbox WHERE kind = 'club'"))
    assert 5 <= rows <= 25
    await sync.drain(session)
    assert meili.docs(search.CLUBS)[scene["club"].id]["follower_count"] == 39


async def test_a_failure_leaves_the_outbox_for_next_time(session, scene, meili):
    await sync.ensure_built(session)
    await sync.drain(session)
    scene["shown"].name = "While Down"
    await session.commit()
    meili.down = True
    with pytest.raises(search.SearchError):
        await sync.drain_once(session)
    assert await session.scalar(text("SELECT count(*) FROM search_outbox WHERE kind = 'event' AND ref_id = :id"), {"id": scene["shown"].id}) >= 1
    meili.down = False
    await sync.drain(session)
    assert meili.docs(search.EVENTS)[scene["shown"].id]["name"] == "While Down"
    assert await session.scalar(text("SELECT count(*) FROM search_outbox")) == 0


async def test_without_meilisearch_the_outbox_is_dropped_but_search_text_is_kept(session, scene):
    assert not search.configured()
    scene["shown"].name = "Nobody Listening"
    scene["robotics"].name = f"Robotique {scene['tag']}"
    await session.commit()
    assert await session.scalar(text("SELECT count(*) FROM search_outbox")) >= 2
    await sync.drain(session)
    assert await session.scalar(text("SELECT count(*) FROM search_outbox")) == 0
    got = await session.scalar(text("SELECT search_text FROM events WHERE id = :id"), {"id": scene["shown"].id})
    assert got.startswith("nobody listening") and f"robotique {scene['tag']}" in got


async def test_the_reconcile_puts_right_what_drifted(session, scene, meili):
    await sync.ensure_built(session)
    await sync.drain(session)
    evs = meili.docs(search.EVENTS)
    evs[scene["shown"].id] = dict(evs[scene["shown"].id], name="tampered", h="0")
    del evs[scene["closed"].id]
    evs[2_000_000_000] = {"id": 2_000_000_000, "name": "stray", "h": "x"}
    out = await sync.reconcile(session, batch=2)
    assert out[search.EVENTS] == {"sent": 2, "removed": 1}
    assert evs[scene["shown"].id]["name"] == "Rover Night" and scene["closed"].id in evs and 2_000_000_000 not in evs
    assert (await sync.reconcile(session))[search.EVENTS] == {"sent": 0, "removed": 0}


async def test_the_tick_builds_what_is_missing_and_checks_on_schedule(session, scene, meili):
    out = await sync.tick(session)
    assert out["built"][search.EVENTS] >= 3 and "checked" in out
    assert (await sync.tick(session)).get("checked") is None  # six hours from now
    del meili.indexes[search.index(search.CLUBS)]  # the instance lost an index
    sync.reset()
    out = await sync.tick(session)
    assert out["built"] and scene["club"].id in meili.docs(search.CLUBS)
    # A search's readiness follows the build's record.
    engine.reset()
    assert await engine.usable(session)


async def test_the_reindex_script_rebuilds_applies_settings_checks_and_reports(session, scene, meili):
    from scripts import search_reindex

    rebuilt = await search_reindex.run(session, "rebuild")
    assert rebuilt["rebuild"][search.EVENTS] >= 3 and scene["club"].id in meili.docs(search.CLUBS)
    assert (await search_reindex.run(session, "settings"))["settings"] == {search.index(k): False for k in search.KINDS}
    assert (await search_reindex.run(session, "check"))["check"][search.EVENTS] == {"sent": 0, "removed": 0}
    got = await search_reindex.run(session, "status")
    assert got["configured"] and got["healthy"] and got["built"] and got[search.index(search.EVENTS)] >= 3
