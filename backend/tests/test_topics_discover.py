"""Topics on the public site (discover): events, the calendar and clubs
narrowed by topic (a slug or an id) or interest, the topic facet beside the
search, and each card's, club's and event page's topics, read for a whole
page at once.

Run as production runs: the session hides soft-deleted rows from every ORM
statement (app.db.listeners), which is ANDed onto the outer joins too."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink, ClubUsersLink
from app.api.discover import cache
from app.api.events.models import EventInterestsLink
from app.api.interests.models import InterestCategory, Interests
from app.api.users.models import Users
from app.db.listeners import add_loader_criteria
from tests.racing import statements

API = "/api/v1/discover"
IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


def _ids(items):
    return [x["id"] for x in items]


def ref(topic) -> dict:
    return {"id": topic.id, "slug": topic.slug, "name": topic.name, "color": topic.color}


@pytest.fixture
async def scene(session, make_club, make_event):
    now = datetime.now(timezone.utc)
    dev = InterestCategory(name="Development", slug="development", color="#2563EB", sort_order=1)
    arts = InterestCategory(name="Arts", slug="arts", color="#EA580C", sort_order=2)
    community = InterestCategory(name="Community", slug="community", sort_order=3)
    gone = InterestCategory(name="Gone", slug="gone", sort_order=0, is_deleted=True)
    session.add_all([dev, arts, community, gone])
    await session.flush()
    i = dict(
        web=Interests(name="Web Development", slug="web-development", category_id=dev.id, sort_order=1),
        cyber=Interests(name="Cybersecurity", slug="cybersecurity", category_id=dev.id, sort_order=2),
        music=Interests(name="Music", slug="music", category_id=arts.id, sort_order=1),
        drawing=Interests(name="Drawing", slug="drawing", category_id=arts.id, sort_order=2),
        old=Interests(name="Old Thing", slug="old-thing", category_id=gone.id),
    )
    session.add_all(i.values())
    await session.flush()

    owner, coders = await make_club(name="Coders Guild", email="topics-coders@example.com")
    owner_b, painters = await make_club(name="Painters Circle", email="topics-painters@example.com")
    _, quiet = await make_club(name="Quiet Club", email="topics-quiet@example.com")
    for club, picks in ((coders, ["web", "cyber"]), (painters, ["drawing", "music", "web"]), (quiet, [])):
        for name in picks:
            session.add(ClubInterestsLink(club_id=club.id, interest_id=i[name].id))
    session.add(ClubUsersLink(club_id=painters.id, user_id=owner.id, is_following=True))
    session.add(ClubInterestsLink(club_id=quiet.id, interest_id=i["cyber"].id, is_deleted=True))

    async def ev(name, club, by, picks, days, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        e = await make_event(club=club, created_by=by, name=name, event_datetime=now + timedelta(days=days), **kw)
        for p in picks:
            session.add(EventInterestsLink(event_id=e.id, interest_id=i[p].id))
        return e

    e = dict(
        hack=await ev("Hack Night", coders, owner, ["web", "cyber"], 1),
        ctf=await ev("Capture the Flag", coders, owner, ["cyber"], 2),
        sketch=await ev("Sketch Jam", painters, owner_b, ["drawing"], 3),
        mixed=await ev("Code and Canvas", painters, owner_b, ["drawing", "web"], 4),
        plain=await ev("Open Mic", painters, owner_b, [], 5),
        held=await ev("Old Hack", coders, owner, ["web"], -10),
    )
    # A link taken off counts for nothing.
    session.add(EventInterestsLink(event_id=e["plain"].id, interest_id=i["music"].id, is_deleted=True))
    await session.commit()
    return dict(now=now, dev=dev, arts=arts, community=community, i=i, coders=coders, painters=painters, quiet=quiet, e=e)


async def _get(client, path, **params):
    r = await client.get(f"{API}{path}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ─── Narrowing by topic and interest ─────────────────────────────────────


async def test_events_narrow_by_topic_slug_or_id_and_by_interest(client, scene):
    e, dev, arts = scene["e"], scene["dev"], scene["arts"]
    every = [e[k].id for k in ("hack", "ctf", "sketch", "mixed", "plain")]
    assert _ids((await _get(client, "/events"))["items"]) == every

    coding = [e["hack"].id, e["ctf"].id, e["mixed"].id]
    assert _ids((await _get(client, "/events", topic="development"))["items"]) == coding
    assert _ids((await _get(client, "/events", topic=str(dev.id)))["items"]) == coding
    assert _ids((await _get(client, "/events", topic="arts"))["items"]) == [e["sketch"].id, e["mixed"].id]
    body = await _get(client, "/events", topic="development", sort="newest")
    assert _ids(body["items"]) == coding[::-1] and body["total"] == 3
    assert _ids((await _get(client, "/events", topic="development", sort="popular"))["items"]) == coding

    # Interest; and interest over topic when the two disagree.
    assert _ids((await _get(client, "/events", interest=scene["i"]["cyber"].id))["items"]) == [e["hack"].id, e["ctf"].id]
    assert _ids((await _get(client, "/events", topic="arts", interest=scene["i"]["cyber"].id))["items"]) == [e["hack"].id, e["ctf"].id]
    # With words, and with the rest of the filters.
    assert _ids((await _get(client, "/events", topic="development", q="canvas"))["items"]) == [e["mixed"].id]

    # A topic with nothing, a deleted one, one that never was: nothing, not an error.
    assert _ids((await _get(client, "/events", topic=" Development "))["items"]) == coding  # a slug in any case
    for nothing in ("community", "gone", "no-such-topic", "999999", "99999999999"):
        body = await _get(client, "/events", topic=nothing)
        assert body["items"] == [] and body["total"] == 0, nothing
    assert (await _get(client, "/events", interest=scene["i"]["old"].id))["items"] == []


async def test_the_topic_facet_sits_beside_the_categories(client, scene):
    body = await _get(client, "/events", topic="arts")

    # Every live topic, in topic order, with what is on in it (not only what this search found).
    assert body["topics"] == [
        dict(ref(scene["dev"]), upcoming=3),
        dict(ref(scene["arts"]), upcoming=2),
        dict(ref(scene["community"]), upcoming=0),
    ]
    assert body["categories"]


def _days(events, month: str) -> dict[str, int]:
    """The calendar's answer worked out by hand: each event on every day in
    India it touches, from its start to just before its end."""
    out: dict[str, int] = {}
    for e in events:
        start = e.event_datetime.astimezone(IST)
        last = (start + timedelta(hours=e.duration) - timedelta(microseconds=1)).date()
        day = start.date()
        while day <= last:
            if day.strftime("%Y-%m") == month:
                out[day.isoformat()] = out.get(day.isoformat(), 0) + 1
            day += timedelta(days=1)
    return out


async def test_the_calendar_narrows_by_topic_and_keeps_each_answer_apart(client, scene):
    cache.reset(enabled=True)
    e = scene["e"]
    month = scene["now"].astimezone(IST).strftime("%Y-%m")

    async def days(**params):
        return (await _get(client, "/calendar", month=month, **params))["days"]

    assert await days() == _days(e.values(), month)
    assert await days(topic="development") == _days([e["hack"], e["ctf"], e["mixed"], e["held"]], month)
    assert await days(topic=str(scene["arts"].id)) == _days([e["sketch"], e["mixed"]], month)
    cyber = _days([e["hack"], e["ctf"]], month)
    assert await days(interest=scene["i"]["cyber"].id) == cyber
    assert await days(topic="arts", interest=scene["i"]["cyber"].id) == cyber
    assert await days(topic="no-such-topic") == {}
    # Kept apart: one answer each, and a topic beside an interest is no part of the key.
    assert len(cache.shared._items.get("calendar", {})) == 5


async def test_clubs_narrow_by_topic_and_interest(client, scene):
    coders, painters, quiet = scene["coders"], scene["painters"], scene["quiet"]
    assert set(_ids((await _get(client, "/clubs"))["items"])) == {coders.id, painters.id, quiet.id}
    assert set(_ids((await _get(client, "/clubs", topic="development"))["items"])) == {coders.id, painters.id}
    assert _ids((await _get(client, "/clubs", topic=str(scene["arts"].id)))["items"]) == [painters.id]
    assert _ids((await _get(client, "/clubs", topic="development", interest=scene["i"]["music"].id))["items"]) == [painters.id]
    for nothing in ("community", "gone", "nope"):
        body = await _get(client, "/clubs", topic=nothing)
        assert body["items"] == [] and body["total"] == 0, nothing


# ─── Topics on what is shown ─────────────────────────────────────────────


async def test_cards_pages_and_clubs_carry_their_topics(client, scene):
    e, dev, arts = scene["e"], ref(scene["dev"]), ref(scene["arts"])
    cards = {c["id"]: c for c in (await _get(client, "/events"))["items"]}
    # Distinct, in topic order, whatever order the event picked them in; none for none.
    assert cards[e["hack"].id]["topics"] == [dev]
    assert cards[e["mixed"].id]["topics"] == [dev, arts]
    assert cards[e["sketch"].id]["topics"] == [arts]
    assert cards[e["plain"].id]["topics"] == []

    home = await _get(client, "/home")
    assert {c["id"]: c["topics"] for c in home["this_week"]}[e["mixed"].id] == [dev, arts]

    clubs = {c["id"]: c for c in (await _get(client, "/clubs"))["items"]}
    assert clubs[scene["painters"].id]["topics"] == [dev, arts]
    assert clubs[scene["painters"].id]["interests"] == ["Drawing", "Music", "Web Development"]
    assert clubs[scene["quiet"].id]["topics"] == []

    page = await _get(client, f"/clubs/{scene['painters'].slug}")
    assert page["club"]["topics"] == [dev, arts]
    assert {c["id"]: c["topics"] for c in page["upcoming"]} == {e["sketch"].id: [arts], e["mixed"].id: [dev, arts], e["plain"].id: []}
    past = (await _get(client, f"/clubs/{scene['coders'].slug}"))["past"]
    assert past[0]["topics"] == [dev]
    assert (await _get(client, f"/clubs/{scene['coders'].slug}/events", when="upcoming"))["items"][0]["topics"] == [dev]

    ev = await _get(client, f"/events/{e['mixed'].slug}")
    assert ev["event"]["topics"] == [dev, arts] and ev["event"]["interests"] == ["Drawing", "Web Development"]
    assert {c["id"]: c["topics"] for c in ev["more_from_club"]}[e["sketch"].id] == [arts]


async def test_a_page_of_cards_reads_its_topics_in_one_statement(client, session, scene, make_event):
    """However many events a page shows and topics they have, the search's
    statements stay the same: the topics come with the going counts."""
    async def count(**params):
        with statements(session) as seen:
            await _get(client, "/events", **params)
        return len([s for s in seen if "SAVEPOINT" not in s])

    few = [await count(), await count(topic="development"), await count(interest=scene["i"]["web"].id)]
    owner = await session.get(Users, scene["coders"].user_id)
    for n in range(20):
        ev = await make_event(club=scene["coders"], created_by=owner, name=f"More {n}", event_datetime=scene["now"] + timedelta(days=6 + n), lifecycle=EventStatus.published)
        for name in (["web", "drawing"] if n % 2 else ["cyber", "music"]):
            session.add(EventInterestsLink(event_id=ev.id, interest_id=scene["i"][name].id))
    await session.commit()
    many = [await count(), await count(topic="development"), await count(interest=scene["i"]["web"].id)]
    assert many == few, (few, many)
