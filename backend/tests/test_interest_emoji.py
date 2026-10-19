"""Every interest of the topics tree gets its emoji (interest_emoji).

The migration's `give` and `take_back` are imported from its file and run
inside the test's transaction, as the topics seed's test does: an interest
with no icon, a blank one or the docs page's placeholder gets its emoji; one
an admin chose keeps it; a slug no interest has is passed over; and the
downgrade takes back only what is still the migration's emoji."""
import importlib.util
import pathlib

import pytest
from sqlalchemy import text

from app.api.interests.models import InterestCategory, InterestIconType, Interests
from app.api.interests.schemas import InterestCreate


def _load(name: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(session, fn) -> list[str]:
    connection = await session.connection()
    return await connection.run_sync(lambda sync: fn(sync))


async def _icons(session) -> dict:
    """Every interest's (icon, icon_type), by slug."""
    rows = await session.execute(text("SELECT slug, icon, icon_type::text AS icon_type FROM interests ORDER BY id"))
    return {r.slug: (r.icon, r.icon_type) for r in rows}


async def test_every_interest_the_topics_seed_makes_gets_its_emoji(session):
    """On a database the topics migration seeded (a fresh install): all 45
    interests get their emoji, each one the API takes back as it is when an
    admin opens the interest in the editor and saves it."""
    module, topics = _load("interest_emoji"), _load("topics")
    assert (module.revision, module.down_revision) == ("interest_emoji", "w2_live")
    # The tree's interests, by the slug the seed gives each, in the tree's order.
    assert list(module.ICONS) == [topics.slugify(name) for *_, names in topics.TREE for name in names]
    assert len(module.ICONS) == 45
    # Written out as emoji, joiners and presentation selectors included.
    assert [hex(ord(c)) for c in module.ICONS["software-engineering"]] == ["0x1f9d1", "0x200d", "0x1f4bb"]
    assert [hex(ord(c)) for c in module.ICONS["coding"]] == ["0x2328", "0xfe0f"]

    connection = await session.connection()
    await connection.run_sync(lambda sync: topics.seed(sync))
    await session.commit()

    assert await _run(session, module.give) == sorted(module.ICONS)
    await session.commit()

    assert await _icons(session) == {slug: (icon, "emoji") for slug, icon in module.ICONS.items()}
    for slug, icon in module.ICONS.items():
        InterestCreate(name=slug, category_id=1, icon=icon, icon_type="emoji")


@pytest.fixture
async def interests(session):
    """Interests as production may have them: no icon, a blank one, the
    placeholder in any case, an admin's emoji, a url icon, and one the tree
    does not name. "fitness" is not there at all."""
    emoji, url = InterestIconType.emoji, InterestIconType.url
    topic = InterestCategory(name="Everything", slug="everything")
    session.add(topic)
    await session.flush()
    session.add_all([
        Interests(name="Web Development", slug="web-development", category_id=topic.id),
        Interests(name="App Development", slug="app-development", icon="", category_id=topic.id),
        Interests(name="Music", slug="music", icon="string", icon_type=emoji, category_id=topic.id),
        Interests(name="Coding", slug="coding", icon=" String ", icon_type=emoji, category_id=topic.id),
        Interests(name="Gaming", slug="gaming", icon="STRING", category_id=topic.id),
        Interests(name="UI/UX", slug="ui-ux", icon="🎛️", icon_type=emoji, category_id=topic.id),
        Interests(name="Robotics", slug="robotics", icon="https://cdn.example.com/robot.png", icon_type=url, category_id=topic.id),
        Interests(name="Calligraphy", slug="calligraphy", category_id=topic.id),
    ])
    await session.commit()
    return topic


async def test_only_an_interest_without_an_icon_of_its_own_gets_its_emoji(session, interests):
    module = _load("interest_emoji")

    given = await _run(session, module.give)
    await session.commit()

    assert given == ["app-development", "coding", "gaming", "music", "web-development"]
    assert await _icons(session) == {
        "web-development": ("🌐", "emoji"),
        "app-development": ("📱", "emoji"),
        "music": ("🥁", "emoji"),
        "coding": ("⌨️", "emoji"),
        "gaming": ("🎮", "emoji"),
        # An admin's emoji and url stay as they are; an interest the tree does not name is left alone.
        "ui-ux": ("🎛️", "emoji"),
        "robotics": ("https://cdn.example.com/robot.png", "url"),
        "calligraphy": (None, None),
    }

    # Again: nothing left to give.
    before = (await session.execute(text("SELECT * FROM interests ORDER BY id"))).all()
    assert await _run(session, module.give) == []
    assert (await session.execute(text("SELECT * FROM interests ORDER BY id"))).all() == before


async def test_the_downgrade_takes_back_only_what_is_still_its_emoji(session, interests):
    module = _load("interest_emoji")
    await _run(session, module.give)
    # An admin picks another emoji for Music after the migration.
    await session.execute(text("UPDATE interests SET icon = '🎸' WHERE slug = 'music'"))
    await session.commit()

    taken = await _run(session, module.take_back)
    await session.commit()

    assert taken == ["app-development", "coding", "gaming", "web-development"]
    assert await _icons(session) == {
        "web-development": (None, None),
        "app-development": (None, None),
        "music": ("🎸", "emoji"),
        "coding": (None, None),
        "gaming": (None, None),
        "ui-ux": ("🎛️", "emoji"),
        "robotics": ("https://cdn.example.com/robot.png", "url"),
        "calligraphy": (None, None),
    }
    assert await _run(session, module.take_back) == []

    # And up again: the same emoji where there is none.
    assert await _run(session, module.give) == ["app-development", "coding", "gaming", "web-development"]


async def test_the_interests_list_carries_the_emoji(client, session, interests):
    await _run(session, _load("interest_emoji").give)
    await session.commit()

    response = await client.get("/api/v1/interests/list")
    assert response.status_code == 200
    topic = next(t for t in response.json() if t["id"] == interests.id)
    shown = {i["slug"]: (i["icon"], i["icon_type"]) for i in topic["interests"]}
    assert shown["web-development"] == ("🌐", "emoji")
    assert shown["music"] == ("🥁", "emoji")
    assert shown["ui-ux"] == ("🎛️", "emoji")
