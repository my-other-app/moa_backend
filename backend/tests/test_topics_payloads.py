"""Topics on the app's and the dashboards' events and clubs: each carries
`topics` (the distinct topics of its interests, in topic order; [] when it
has none, never left out), and each interest its topic's id and its slug.
Read with the interests, in the same statements: a longer list costs no
more statements than a short one.

Run as production runs (soft-deleted rows hidden from every ORM statement)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink
from app.api.events.models import EventInterestsLink
from app.api.interests.models import InterestCategory, Interests
from app.db.listeners import add_loader_criteria
from tests.conftest import auth_header
from tests.racing import statements

API = "/api/v1"


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


@pytest.fixture
async def scene(session, make_club, make_event, make_user, make_admin, mint_token):
    dev = InterestCategory(name="Development", slug="development", color="#2563EB", sort_order=1)
    arts = InterestCategory(name="Arts", slug="arts", color="#EA580C", sort_order=2)
    session.add_all([dev, arts])
    await session.flush()
    web = Interests(name="Web Development", slug="web-development", category_id=dev.id)
    drawing = Interests(name="Drawing", slug="drawing", category_id=arts.id)
    session.add_all([web, drawing])
    await session.flush()

    owner, club = await make_club(name="Payload Club", email="topics-payload@example.com")
    _, bare = await make_club(name="Bare Club", email="topics-bare@example.com")
    _, pending = await make_club(name="Pending Payload Club", email="topics-pending@example.com", is_verified=False)
    # Picked in the other order from the topics': the payload is in topic order.
    for c in (club, pending):
        session.add(ClubInterestsLink(club_id=c.id, interest_id=drawing.id))
        session.add(ClubInterestsLink(club_id=c.id, interest_id=web.id))
    event = await make_event(club=club, created_by=owner, name="Payload Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=1), lifecycle=EventStatus.published)
    plain = await make_event(club=club, created_by=owner, name="Plain Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=2), lifecycle=EventStatus.published)
    session.add(EventInterestsLink(event_id=event.id, interest_id=drawing.id))
    session.add(EventInterestsLink(event_id=event.id, interest_id=web.id))
    await session.commit()
    person = await make_user(email="topics-payload-person@example.com")
    return dict(
        dev=dev, arts=arts, web=web, drawing=drawing, club=club, bare=bare, pending=pending, event=event, plain=plain,
        both=[
            {"id": dev.id, "slug": "development", "name": "Development", "color": "#2563EB"},
            {"id": arts.id, "slug": "arts", "name": "Arts", "color": "#EA580C"},
        ],
        h_person=auth_header(await mint_token(person)),
        h_owner=auth_header(await mint_token(owner)),
        h_admin=auth_header(await mint_token(await make_admin(email="topics-payload-admin@example.com"))),
    )


def _interest(row) -> dict:
    return {"id": row.id, "name": row.name, "icon": None, "icon_type": None, "category_id": row.category_id, "slug": row.slug}


async def _get(client, path, headers):
    r = await client.get(f"{API}{path}", headers=headers)
    assert r.status_code == 200, (path, r.text)
    return r.json()


async def test_an_events_detail_carries_its_topics(client, scene):
    body = await _get(client, f"/events/info/{scene['event'].id}", scene["h_person"])
    assert body["topics"] == scene["both"]
    assert sorted(body["interests"], key=lambda i: i["id"]) == sorted([_interest(scene["web"]), _interest(scene["drawing"])], key=lambda i: i["id"])

    plain = await _get(client, f"/events/info/{scene['plain'].id}", scene["h_person"])
    assert plain["topics"] == [] and plain["interests"] == []


async def test_a_clubs_detail_carries_its_topics_wherever_it_is_read(client, scene):
    club, both = scene["club"], scene["both"]
    for path, headers in (
        (f"/clubs/info/{club.id}", scene["h_person"]),
        ("/clubs/info", scene["h_owner"]),
        (f"/clubs/admin/{club.id}", scene["h_admin"]),
        (f"/clubs/admin/{scene['pending'].id}", scene["h_admin"]),
    ):
        body = await _get(client, path, headers)
        assert body["topics"] == both, path
        assert {i["slug"]: i["category_id"] for i in body["interests"]} == {"web-development": scene["dev"].id, "drawing": scene["arts"].id}
    bare = await _get(client, f"/clubs/info/{scene['bare'].id}", scene["h_person"])
    assert bare["topics"] == [] and bare["interests"] == []


async def test_club_and_event_lists_carry_topics(client, scene):
    items = {c["id"]: c for c in (await _get(client, "/clubs/list?limit=50", scene["h_person"]))["items"]}
    assert items[scene["club"].id]["topics"] == scene["both"] and items[scene["bare"].id]["topics"] == []
    admin = {c["id"]: c for c in (await _get(client, "/clubs/admin/list?limit=50", scene["h_admin"]))["items"]}
    assert admin[scene["pending"].id]["topics"] == scene["both"]
    assert admin[scene["pending"].id]["interests"][0]["category_id"] in (scene["dev"].id, scene["arts"].id)
    events = {e["id"]: e for e in (await _get(client, "/events/admin/list?limit=50", scene["h_admin"]))["items"]}
    assert events[scene["event"].id]["topics"] == scene["both"] and events[scene["plain"].id]["topics"] == []

    r = await client.post(f"{API}/clubs/{scene['pending'].id}/approve", headers=scene["h_admin"])
    assert r.status_code == 200, r.text
    assert r.json()["topics"] == scene["both"]


async def test_a_deleted_topic_is_on_nothing(client, session, scene):
    scene["arts"].soft_delete()
    await session.commit()
    body = await _get(client, f"/events/info/{scene['event'].id}", scene["h_person"])
    assert body["topics"] == scene["both"][:1]


async def test_more_clubs_cost_no_more_statements(client, session, scene, make_club):
    async def count() -> int:
        with statements(session) as seen:
            await _get(client, "/clubs/list?limit=50", scene["h_person"])
        return len([s for s in seen if "SAVEPOINT" not in s])

    few = await count()
    for n in range(10):
        _, c = await make_club(name=f"More Payload {n}", email=f"topics-more-{n}@example.com")
        session.add(ClubInterestsLink(club_id=c.id, interest_id=(scene["web"] if n % 2 else scene["drawing"]).id))
    await session.commit()
    assert await count() == few


async def test_a_new_event_is_pushed_to_people_by_its_topics_not_its_type(client, session, scene, make_user, monkeypatch):
    """The interest push matched the event's type id against topic ids. Now:
    whoever picked an interest under one of the event's topics (not only
    the same interest), and nobody for the topic sharing the type's number."""
    from app.api.events.models import EventCategories
    from app.api.notifications.models import Notifications
    from app.api.users.models import UserInterests
    from app.config import settings
    from sqlalchemy import select

    monkeypatch.setattr(settings, "INTEREST_PUSH_MAX_PER_EVENT", 100)
    monkeypatch.setattr(settings, "INTEREST_PUSH_MAX_PER_USER_PER_DAY", 5)
    painter = await make_user(email="topics-push-painter@example.com")
    coder = await make_user(email="topics-push-coder@example.com")
    session.add(UserInterests(user_id=painter.id, interest_id=scene["drawing"].id))
    session.add(UserInterests(user_id=coder.id, interest_id=scene["web"].id))
    sketching = Interests(name="Sketching", slug="sketching", category_id=scene["arts"].id)
    session.add(sketching)
    # The event's type, and a topic with the same number: what the old rule went by.
    workshop = EventCategories(name="Topic Workshop")
    session.add(workshop)
    await session.flush()
    if workshop.id == scene["arts"].id:
        workshop = EventCategories(name="Topic Workshop Again")
        session.add(workshop)
        await session.flush()
    if workshop.id == scene["dev"].id:
        decoy = coder
    else:
        numbered = InterestCategory(id=workshop.id, name="Numbered Topic", slug="numbered-topic")
        session.add(numbered)
        await session.flush()
        decoy = await make_user(email="topics-push-decoy@example.com")
        picked = Interests(name="Numbered Interest", slug="numbered-interest", category_id=numbered.id)
        session.add(picked)
        await session.flush()
        session.add(UserInterests(user_id=decoy.id, interest_id=picked.id))
    await session.commit()
    form = {
        "name": "Sketch Night", "event_datetime": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat(), "duration": "2",
        "has_fee": "false", "status": "published", "category_id": str(workshop.id), "interest_ids": str(sketching.id),
    }

    r = await client.post(f"{API}/events/create", data=form, headers=scene["h_owner"])

    assert r.status_code == 200, r.text
    assert r.json()["topics"] == scene["both"][1:]
    told = set((await session.scalars(select(Notifications.user_id).where(Notifications.type == "nearby_event", Notifications.event_id == r.json()["id"]))).all())
    assert told == {painter.id} and decoy.id not in told
