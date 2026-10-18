"""Topics through /interests: the tree in order (empty topics too), the
topics with what is on in each (kept a few minutes, dropped when an admin
changes the tree), and the admin's editor: topics made, renamed, recoloured,
deleted with their interests moved, and topics and interests reordered."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink
from app.api.discover import cache
from app.api.events.models import EventInterestsLink
from app.api.interests.models import InterestCategory, InterestIconType, Interests
from app.api.users.models import AdminRoles
from tests.conftest import auth_header

API = "/api/v1/interests"


@pytest.fixture
async def tree(session):
    """Development (two interests, out of name order) and Arts (two at the
    same place: by name), an empty Community, and a deleted topic and interest."""
    dev = InterestCategory(name="Development", slug="development", color="#2563EB", sort_order=1, icon="💻", icon_type=InterestIconType.emoji, description="Building software.")
    arts = InterestCategory(name="Arts", slug="arts", sort_order=2)
    community = InterestCategory(name="Community", slug="community", sort_order=3)
    old = InterestCategory(name="Old Topic", slug="old-topic", sort_order=0, is_deleted=True)
    session.add_all([dev, arts, community, old])
    await session.flush()
    rows = dict(
        web=Interests(name="Web Development", slug="web-development", category_id=dev.id, sort_order=2),
        cyber=Interests(name="Cybersecurity", slug="cybersecurity", category_id=dev.id, sort_order=1),
        music=Interests(name="Music", slug="music", category_id=arts.id, sort_order=1),
        drawing=Interests(name="Drawing", slug="drawing", category_id=arts.id, sort_order=1),
        gone=Interests(name="Gone Interest", slug="gone-interest", category_id=dev.id, sort_order=0, is_deleted=True),
    )
    session.add_all(rows.values())
    await session.commit()
    return dict(dev=dev, arts=arts, community=community, old=old, **rows)


@pytest.fixture
async def admin(make_admin, mint_token):
    user = await make_admin(email="topics-admin@example.com")
    return auth_header(await mint_token(user))


async def _audit(session, action):
    return list((await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == action))).all())


def _names(tree_):
    return {t["name"]: [i["name"] for i in t["interests"]] for t in tree_}


# ─── The tree ────────────────────────────────────────────────────────────


async def test_the_list_is_every_topic_in_order_with_its_interests_in_order(client, tree):
    r = await client.get(f"{API}/list")

    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "public, max-age=300"
    body = r.json()
    # Topic order, then each interest's, then name; an empty topic is listed; nothing deleted.
    assert [(t["name"], [i["name"] for i in t["interests"]]) for t in body] == [
        ("Development", ["Cybersecurity", "Web Development"]),
        ("Arts", ["Drawing", "Music"]),
        ("Community", []),
    ]
    dev = body[0]
    assert {k: v for k, v in dev.items() if k != "interests"} == {
        "id": tree["dev"].id, "slug": "development", "name": "Development", "icon": "💻", "icon_type": "emoji",
        "color": "#2563EB", "description": "Building software.", "sort_order": 1,
    }
    assert dev["interests"][0] == {
        "id": tree["cyber"].id, "slug": "cybersecurity", "name": "Cybersecurity", "icon": None, "icon_type": None,
        "category_id": tree["dev"].id, "sort_order": 1,
    }


# ─── Topics with what is on ──────────────────────────────────────────────


@pytest.fixture
async def on(session, tree, make_club, make_event):
    """Two listed clubs and a pending one; events on, to come, drafted, over and deleted."""
    now = datetime.now(timezone.utc)
    owner, club = await make_club(name="Topic Club", email="topic-club@example.com")
    _, other = await make_club(name="Other Topic Club", email="topic-other@example.com")
    _, pending = await make_club(name="Pending Topic Club", email="topic-pending@example.com", is_verified=False)

    async def event(name, picks, *, at=None, club_=club, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        e = await make_event(club=club_, created_by=owner, name=name, event_datetime=at or now + timedelta(days=2), **kw)
        for interest in picks:
            session.add(EventInterestsLink(event_id=e.id, interest_id=tree[interest].id))
        return e

    events = dict(
        both=await event("Web and Security Night", ["web", "cyber"]),  # two Development interests: once for the topic
        mixed=await event("Code and Canvas", ["web", "drawing"]),
        live=await event("Live Jam", ["music"], at=now - timedelta(hours=1), lifecycle=EventStatus.live),
        draft=await event("Draft Hack", ["web"], lifecycle=EventStatus.draft),
        over=await event("Old Hack", ["web"], at=now - timedelta(days=5)),
        deleted=await event("Deleted Hack", ["cyber"], is_deleted=True),
        hidden=await event("Pending Club Hack", ["cyber"], club_=pending),
    )
    for c, picks in ((club, ["web", "cyber", "music"]), (other, ["drawing"]), (pending, ["web"])):
        for interest in picks:
            session.add(ClubInterestsLink(club_id=c.id, interest_id=tree[interest].id))
    session.add(ClubInterestsLink(club_id=other.id, interest_id=tree["music"].id, is_deleted=True))
    await session.commit()
    return dict(club=club, other=other, pending=pending, events=events, owner=owner, now=now)


async def test_topics_count_what_is_on_and_the_clubs_that_pick_them(client, tree, on):
    r = await client.get(f"{API}/topics")

    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "public, max-age=300"
    body = r.json()
    assert [t["slug"] for t in body] == ["development", "arts", "community"]
    dev, arts, community = body
    assert {k: v for k, v in dev.items() if k != "interests"} == {
        "id": tree["dev"].id, "slug": "development", "name": "Development", "icon": "💻", "icon_type": "emoji",
        "color": "#2563EB", "description": "Building software.", "upcoming_events": 2, "clubs": 1,
    }
    assert dev["interests"] == [
        {"id": tree["cyber"].id, "slug": "cybersecurity", "name": "Cybersecurity", "upcoming_events": 1},
        {"id": tree["web"].id, "slug": "web-development", "name": "Web Development", "upcoming_events": 2},
    ]
    # Arts: the live jam and the mixed night; both listed clubs (one by Music, one by Drawing).
    assert (arts["upcoming_events"], arts["clubs"]) == (2, 2)
    assert {i["name"]: i["upcoming_events"] for i in arts["interests"]} == {"Drawing": 1, "Music": 1}
    assert (community["upcoming_events"], community["clubs"], community["interests"]) == (0, 0, [])

    one = await client.get(f"{API}/topics/development")
    assert one.status_code == 200 and one.json() == dev
    assert one.headers["cache-control"] == "public, max-age=300"
    assert (await client.get(f"{API}/topics/{tree['arts'].id}")).json() == arts
    for missing in ("no-such-topic", "old-topic", "999999"):
        r = await client.get(f"{API}/topics/{missing}")
        assert r.status_code == 404, r.text
        assert r.json()["error_code"] == "TOPIC_NOT_FOUND"


async def test_the_counts_are_kept_and_an_admin_change_drops_them(client, session, tree, on, admin, make_event):
    cache.reset(enabled=True)
    first = (await client.get(f"{API}/topics")).json()
    assert first[1]["upcoming_events"] == 2

    # A new Arts event is not counted while the answer is kept...
    late = await make_event(club=on["club"], created_by=on["owner"], name="Late Sketch", event_datetime=on["now"] + timedelta(days=3), lifecycle=EventStatus.published)
    session.add(EventInterestsLink(event_id=late.id, interest_id=tree["drawing"].id))
    await session.commit()
    assert (await client.get(f"{API}/topics")).json() == first

    # ...and each change to the tree drops it at once.
    r = await client.put(f"{API}/category/{tree['arts'].id}", json={"name": "Arts & Crafts"}, headers=admin)
    assert r.status_code == 200, r.text
    arts = (await client.get(f"{API}/topics/arts")).json()
    assert arts["name"] == "Arts & Crafts" and arts["upcoming_events"] == 3

    changes = [
        ("post", f"{API}/category/create", {"name": "Sports"}),
        ("post", f"{API}/create", {"name": "Football", "category_id": tree["community"].id}),
        ("put", f"{API}/{tree['music'].id}", {"name": "Music", "category_id": tree["community"].id}),
        ("post", f"{API}/reorder", {"topics": [tree["community"].id, tree["arts"].id, tree["dev"].id]}),
        ("delete", f"{API}/{tree['cyber'].id}", None),
        ("delete", f"{API}/category/{tree['dev'].id}?move_to={tree['arts'].id}", None),
    ]
    for method, path, body in changes:
        kept = (await client.get(f"{API}/topics")).json()
        kwargs = {"headers": admin} | ({"json": body} if body is not None else {})
        r = await getattr(client, method)(path, **kwargs)
        assert r.status_code == 200, (path, r.text)
        assert (await client.get(f"{API}/topics")).json() != kept, path

    assert [t["slug"] for t in (await client.get(f"{API}/topics")).json()] == ["community", "arts", "sports"]


# ─── The admin's editor: topics ──────────────────────────────────────────


async def test_a_topic_is_made_with_a_slug_from_its_name_and_goes_last(client, session, tree, admin):
    r = await client.post(f"{API}/category/create", json={"name": "Gaming & Media", "color": "#DC2626", "description": "Games."}, headers=admin)

    assert r.status_code == 200, r.text
    made = r.json()
    assert {k: made[k] for k in ("name", "slug", "color", "description", "sort_order")} == {
        "name": "Gaming & Media", "slug": "gaming-media", "color": "#DC2626", "description": "Games.", "sort_order": 4,
    }
    [line] = await _audit(session, "topic.create")
    assert line.entity_id == str(made["id"]) and line.entity_type == "topic"

    # A name that makes a slug already taken gets the next free one; one given is taken as it is.
    second = (await client.post(f"{API}/category/create", json={"name": "Gaming - Media"}, headers=admin)).json()
    assert second["slug"] == "gaming-media-2"
    third = (await client.post(f"{API}/category/create", json={"name": "Deep Tech", "slug": "frontier", "sort_order": 0}, headers=admin)).json()
    assert (third["slug"], third["sort_order"]) == ("frontier", 0)
    assert (await client.get(f"{API}/list")).json()[0]["slug"] == "frontier"


@pytest.mark.parametrize("body,field", [
    ({"name": "Arts"}, "name"),  # live already
    ({"name": "Fresh", "slug": "development"}, "slug"),  # taken
])
async def test_a_topic_cannot_take_a_live_ones_name_or_slug(client, tree, admin, body, field):
    r = await client.post(f"{API}/category/create", json=body, headers=admin)
    assert r.status_code == 400, r.text
    assert field in r.json()["errors"]


@pytest.mark.parametrize("body", [
    {"name": "Bad", "slug": "Not A Slug"},
    {"name": "Bad", "slug": "trailing-"},
    {"name": "Bad", "slug": "a" * 61},
    {"name": "Bad", "color": "blue"},
    {"name": "Bad", "color": "#12345"},
    {"name": "B"},
])
async def test_a_bad_slug_or_colour_is_refused(client, tree, admin, body):
    assert (await client.post(f"{API}/category/create", json=body, headers=admin)).status_code == 422
    r = await client.put(f"{API}/category/{tree['arts'].id}", json=dict(body, name="Arts" if len(body) > 1 else body["name"]), headers=admin)
    assert r.status_code == 422, r.text


async def test_editing_a_topic_changes_what_is_sent_and_nothing_else(client, session, tree, admin):
    dev = tree["dev"].id
    r = await client.put(f"{API}/category/{dev}", json={"name": "Dev"}, headers=admin)
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in ("name", "slug", "color", "description", "icon", "icon_type", "sort_order")} == {
        "name": "Dev", "slug": "development", "color": "#2563EB", "description": "Building software.", "icon": "💻",
        "icon_type": "emoji", "sort_order": 1,
    }
    # null clears a colour and a description; a slug sent is taken, one left out kept.
    r = await client.put(f"{API}/category/{dev}", json={"name": "Dev", "color": None, "description": None, "slug": "dev"}, headers=admin)
    assert (r.json()["color"], r.json()["description"], r.json()["slug"]) == (None, None, "dev")
    r = await client.put(f"{API}/category/{dev}", json={"name": "Dev", "slug": None, "color": "#111111"}, headers=admin)
    assert (r.json()["slug"], r.json()["color"]) == ("dev", "#111111")

    taken = await client.put(f"{API}/category/{dev}", json={"name": "Dev", "slug": "arts"}, headers=admin)
    assert taken.status_code == 400 and "slug" in taken.json()["errors"]
    named = await client.put(f"{API}/category/{dev}", json={"name": "Arts"}, headers=admin)
    assert named.status_code == 400 and "name" in named.json()["errors"]
    for missing in (tree["old"].id, 999999):
        r = await client.put(f"{API}/category/{missing}", json={"name": "Whatever"}, headers=admin)
        assert r.status_code == 404 and r.json()["error_code"] == "TOPIC_NOT_FOUND"
    assert len(await _audit(session, "topic.update")) == 3


async def test_a_topic_with_interests_is_deleted_only_by_moving_them(client, session, tree, admin, make_club):
    dev, arts = tree["dev"].id, tree["arts"].id
    _, club = await make_club(name="Deleting Club", email="topic-deleting@example.com")
    session.add(ClubInterestsLink(club_id=club.id, interest_id=tree["web"].id))
    await session.commit()

    r = await client.delete(f"{API}/category/{dev}", headers=admin)
    assert r.status_code == 409, r.text
    assert r.json()["error_code"] == "TOPIC_NOT_EMPTY"
    for move_to in (dev, 999999, tree["old"].id):
        r = await client.delete(f"{API}/category/{dev}?move_to={move_to}", headers=admin)
        assert r.status_code == 400 and "move_to" in r.json()["errors"], move_to

    r = await client.delete(f"{API}/category/{dev}?move_to={arts}", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Topic deleted", "moved": 2, "move_to": arts}
    # Its interests now follow Arts' own, in their order; the club still has its pick.
    assert _names((await client.get(f"{API}/list")).json()) == {
        "Arts": ["Drawing", "Music", "Cybersecurity", "Web Development"], "Community": [],
    }
    link = await session.scalar(select(ClubInterestsLink).where(ClubInterestsLink.club_id == club.id))
    assert link.interest_id == tree["web"].id and not link.is_deleted
    row = await session.scalar(select(InterestCategory).where(InterestCategory.id == dev).execution_options(include_deleted=True, populate_existing=True))
    assert row.is_deleted and row.deleted_at is not None
    [line] = await _audit(session, "topic.delete")
    assert line.details["moved"] == 2

    # An empty topic goes without a move; a deleted topic's name and slug can be used again.
    assert (await client.delete(f"{API}/category/{tree['community'].id}", headers=admin)).json()["moved"] == 0
    again = await client.post(f"{API}/category/create", json={"name": "Development"}, headers=admin)
    assert again.status_code == 200 and again.json()["slug"] == "development"


async def test_only_admins_who_may_change_content_edit_topics(client, tree, make_admin, make_user, mint_token):
    support = auth_header(await mint_token(await make_admin(email="topics-support@example.com", role=AdminRoles.support)))
    person = auth_header(await mint_token(await make_user(email="topics-person@example.com")))
    routes = [
        ("post", f"{API}/category/create", {"name": "Nope"}),
        ("put", f"{API}/category/{tree['dev'].id}", {"name": "Nope"}),
        ("delete", f"{API}/category/{tree['community'].id}", None),
        ("post", f"{API}/reorder", {"topics": [tree["arts"].id]}),
        ("put", f"{API}/{tree['web'].id}", {"name": "Nope", "category_id": tree["dev"].id}),
    ]
    for headers, expected in ((support, {403}), (person, {401, 403}), ({}, {401, 403})):
        for method, path, body in routes:
            kwargs = {"headers": headers} | ({"json": body} if body is not None else {})
            r = await getattr(client, method)(path, **kwargs)
            assert r.status_code in expected, (path, r.status_code)


# ─── Order ───────────────────────────────────────────────────────────────


async def test_reordering_puts_topics_and_interests_in_the_order_sent(client, session, tree, admin):
    dev, arts, community = tree["dev"].id, tree["arts"].id, tree["community"].id
    r = await client.post(f"{API}/reorder", json={
        "topics": [community, arts, dev],
        "interests": {str(arts): [tree["music"].id, tree["web"].id, tree["drawing"].id]},
    }, headers=admin)

    assert r.status_code == 200, r.text
    # The answer is the tree; Web Development moved to Arts by being listed there.
    assert [(t["name"], [i["name"] for i in t["interests"]]) for t in r.json()] == [
        ("Community", []), ("Arts", ["Music", "Web Development", "Drawing"]), ("Development", ["Cybersecurity"]),
    ]
    assert [t["sort_order"] for t in r.json()] == [1, 2, 3]
    assert [i["sort_order"] for i in r.json()[1]["interests"]] == [1, 2, 3]
    assert r.json() == (await client.get(f"{API}/list")).json()
    [line] = await _audit(session, "topics.reorder")
    assert "1 moved topic" in line.summary

    # Either part alone.
    r = await client.post(f"{API}/reorder", json={"interests": {str(dev): [tree["cyber"].id]}}, headers=admin)
    assert r.status_code == 200
    assert [t["name"] for t in r.json()] == ["Community", "Arts", "Development"]


@pytest.mark.parametrize("body", [
    {"topics": [999999]},
    {"topics": ["dev", "dev"]},
    {"interests": {"999999": []}},
    {"interests": {"dev": [999999]}},
    {"interests": {"dev": ["web", "web"]}},
    {"interests": {"dev": ["web"], "arts": ["web"]}},
])
async def test_a_reorder_naming_what_is_not_there_or_twice_is_refused(client, tree, admin, body):
    def ids(v):
        return tree[v].id if isinstance(v, str) and v in tree else v

    payload = {
        "topics": [ids(v) for v in body.get("topics", [])] or None,
        "interests": {str(ids(k)): [ids(v) for v in vs] for k, vs in body.get("interests", {}).items()} or None,
    }
    r = await client.post(f"{API}/reorder", json=payload, headers=admin)
    assert r.status_code == 400, r.text


# ─── The admin's editor: interests ───────────────────────────────────────


async def test_an_interest_is_made_last_in_its_topic_with_a_slug_from_its_name(client, session, tree, admin):
    r = await client.post(f"{API}/create", json={"name": "Cloud & DevOps", "category_id": tree["dev"].id}, headers=admin)

    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in ("name", "slug", "category_id", "sort_order")} == {
        "name": "Cloud & DevOps", "slug": "cloud-devops", "category_id": tree["dev"].id, "sort_order": 3,
    }
    given = await client.post(f"{API}/create", json={"name": "Open Source", "slug": "oss", "sort_order": 0, "category_id": tree["dev"].id}, headers=admin)
    assert (given.json()["slug"], given.json()["sort_order"]) == ("oss", 0)
    assert [i["name"] for i in (await client.get(f"{API}/list")).json()[0]["interests"]] == [
        "Open Source", "Cybersecurity", "Web Development", "Cloud & DevOps",
    ]
    for body, field in (
        ({"name": "Music", "category_id": tree["dev"].id}, "name"),
        ({"name": "Fresh", "slug": "music", "category_id": tree["dev"].id}, "slug"),
        ({"name": "Fresh", "category_id": tree["old"].id}, "category_id"),
    ):
        r = await client.post(f"{API}/create", json=body, headers=admin)
        assert r.status_code == 400 and field in r.json()["errors"], body
    assert len(await _audit(session, "interest.create")) == 2


async def test_an_interest_moves_to_another_topic_and_goes_last_there(client, session, tree, admin, make_user, mint_token):
    from app.api.users.models import UserInterests

    person = await make_user(email="topics-mover@example.com")
    session.add(UserInterests(user_id=person.id, interest_id=tree["web"].id))
    await session.commit()

    # As the dashboard sends it: everything as it was, with the new topic.
    r = await client.put(f"{API}/{tree['web'].id}", json={
        "name": "Web Development", "icon": None, "icon_type": None, "slug": "web-development", "category_id": tree["arts"].id,
    }, headers=admin)

    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in ("name", "slug", "category_id", "sort_order")} == {
        "name": "Web Development", "slug": "web-development", "category_id": tree["arts"].id, "sort_order": 2,
    }
    assert _names((await client.get(f"{API}/list")).json())["Arts"] == ["Drawing", "Music", "Web Development"]
    # Who picked it still has it.
    mine = await session.scalar(select(UserInterests.interest_id).where(UserInterests.user_id == person.id))
    assert mine == tree["web"].id

    # A place sent is kept; a slug left out is kept; one taken is refused.
    r = await client.put(f"{API}/{tree['web'].id}", json={"name": "Web Dev", "category_id": tree["arts"].id, "sort_order": 0}, headers=admin)
    assert (r.json()["slug"], r.json()["sort_order"]) == ("web-development", 0)
    r = await client.put(f"{API}/{tree['web'].id}", json={"name": "Web Dev", "category_id": tree["arts"].id, "slug": "music"}, headers=admin)
    assert r.status_code == 400 and "slug" in r.json()["errors"]
    r = await client.put(f"{API}/999999", json={"name": "Nothing", "category_id": tree["arts"].id}, headers=admin)
    assert r.status_code == 404 and r.json()["error_code"] == "INTEREST_NOT_FOUND"
    r = await client.delete(f"{API}/999999", headers=admin)
    assert r.status_code == 404 and r.json()["error_code"] == "INTEREST_NOT_FOUND"
    [*_, line] = await _audit(session, "interest.update")
    assert line.details["category_id"] == tree["arts"].id


# ─── Routing and the site ────────────────────────────────────────────────


async def test_the_topic_reads_are_routes_of_their_own(client):
    """PUT and DELETE /interests/{interest_id} share the path's shape: a read
    of /interests/topics must reach the topics, never answer 405."""
    r = await client.get(f"{API}/topics")
    assert r.status_code == 200 and r.json() == [], r.text
    r = await client.get(f"{API}/topics/development")
    assert r.status_code == 404 and r.json()["error_code"] == "TOPIC_NOT_FOUND", r.text


async def test_an_admin_change_tells_the_site_its_topics_changed(client, tree, admin, monkeypatch):
    import json

    import httpx

    from app.config import settings
    from app.core import site_revalidate

    got = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(json.loads(request.content))
        return httpx.Response(200, json={"revalidated": True})

    monkeypatch.setattr(settings, "SITE_REVALIDATE_SECRET", "topics-secret")
    monkeypatch.setattr(settings, "SITE_REVALIDATE_URL", "https://site.test/api/revalidate")
    monkeypatch.setattr(site_revalidate, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    site_revalidate.reset_for_tests()
    try:
        r = await client.put(f"{API}/category/{tree['arts'].id}", json={"name": "Arts", "color": "#EA580C"}, headers=admin)
        assert r.status_code == 200, r.text
        await site_revalidate.flush()
        assert got == [{"events": [], "clubs": [], "sitemap": True, "topics": True}]

        # A refused change tells nothing.
        got.clear()
        assert (await client.delete(f"{API}/category/{tree['arts'].id}", headers=admin)).status_code == 409
        await site_revalidate.flush()
        assert got == []
    finally:
        site_revalidate.reset_for_tests()
