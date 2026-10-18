"""The docs page's example icon, "string", out of the data and kept out.

Production's early interests and its "arts" topic carried the icon "string"
marked as an emoji, and two topics made the same way were never given an
interest. The migration's `clean` is imported from its file and run inside
the test's transaction, as the topics seed's test does."""
import importlib.util
import pathlib

import pytest
from sqlalchemy import text

from app.api.interests.models import InterestCategory, InterestIconType, Interests
from tests.conftest import auth_header

API = "/api/v1/interests"


def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "topic_placeholders.py"
    spec = importlib.util.spec_from_file_location("topic_placeholders_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _clean(session, module) -> dict:
    connection = await session.connection()
    return await connection.run_sync(lambda sync: module.clean(sync))


@pytest.fixture
async def placeholders(session):
    emoji = InterestIconType.emoji
    arts = InterestCategory(name="Arts", slug="arts", icon="string", icon_type=emoji, sort_order=4)
    design = InterestCategory(name="Design", slug="design", icon="🖌️", icon_type=emoji, sort_order=3)
    tech = InterestCategory(name="tech", slug="tech", icon="string", icon_type=emoji, sort_order=9)
    trends = InterestCategory(name="emerging trend interests", slug="emerging-trend-interests", icon=" String ", icon_type=emoji, sort_order=10)
    kept = InterestCategory(name="Clubs' own", slug="clubs-own", icon="string", icon_type=emoji, sort_order=11)
    session.add_all([arts, design, tech, trends, kept])
    await session.flush()
    music = Interests(name="Music", slug="music", icon="string", icon_type=emoji, category_id=arts.id)
    ui = Interests(name="UI/UX", slug="ui-ux", icon="🎛️", icon_type=emoji, category_id=design.id)
    own = Interests(name="Own thing", slug="own-thing", category_id=kept.id)
    session.add_all([music, ui, own])
    await session.commit()
    return dict(arts=arts.id, design=design.id, tech=tech.id, trends=trends.id, kept=kept.id, music=music.id, ui=ui.id)


async def _row(session, table: str, id_: int) -> dict:
    return dict((await session.execute(text(
        f"SELECT icon, icon_type::text AS icon_type, is_deleted FROM {table} WHERE id = :id"
    ), {"id": id_})).one()._mapping)


async def test_the_placeholders_go_and_arts_gets_its_emoji(session, placeholders):
    module = _migration()
    assert module.down_revision == "topics" and module.revision == "topic_placeholders"

    done = await _clean(session, module)
    await session.commit()

    # Cleared on all four that had the word, the two it deleted included.
    assert done == dict(topics_deleted=2, interest_icons_cleared=1, topic_icons_cleared=4, topic_icons_given=1)
    assert await _row(session, "interest_categories", placeholders["arts"]) == dict(icon="🎭", icon_type="emoji", is_deleted=False)
    # An admin's own emoji is never touched.
    assert await _row(session, "interest_categories", placeholders["design"]) == dict(icon="🖌️", icon_type="emoji", is_deleted=False)
    assert await _row(session, "interests", placeholders["ui"]) == dict(icon="🎛️", icon_type="emoji", is_deleted=False)
    # The two empty topics made with the placeholder are gone; one with an interest stays, without the word.
    assert (await _row(session, "interest_categories", placeholders["tech"]))["is_deleted"] is True
    assert (await _row(session, "interest_categories", placeholders["trends"]))["is_deleted"] is True
    assert await _row(session, "interest_categories", placeholders["kept"]) == dict(icon=None, icon_type=None, is_deleted=False)
    assert await _row(session, "interests", placeholders["music"]) == dict(icon=None, icon_type=None, is_deleted=False)

    again = await _clean(session, module)
    assert again == dict(topics_deleted=0, interest_icons_cleared=0, topic_icons_cleared=0, topic_icons_given=0)


@pytest.fixture
async def admin(make_admin, mint_token):
    user = await make_admin(email="placeholder-admin@example.com")
    return auth_header(await mint_token(user))


async def test_the_api_takes_an_emoji_only_as_an_emoji(client, session, admin, placeholders):
    def topic(icon, icon_type="emoji", name="Robotics Club"):
        return client.post(f"{API}/category/create", json={"name": name, "icon": icon, "icon_type": icon_type}, headers=admin)

    for word in ("string", " String ", "abc", "x" * 20):
        assert (await topic(word)).status_code == 422
    assert (await topic("🤖")).status_code == 200
    assert (await topic("https://cdn.example.com/robot.png", "url", name="Robots")).status_code == 200
    assert (await topic("robot.png", "url", name="Robots Two")).status_code == 422
    # No icon, or an icon with no type, is still fine.
    assert (await client.post(f"{API}/category/create", json={"name": "Quiet Topic"}, headers=admin)).status_code == 200

    update = await client.put(f"{API}/category/{placeholders['design']}", json={"name": "Design", "icon": "string", "icon_type": "emoji"}, headers=admin)
    assert update.status_code == 422
    interest = await client.post(f"{API}/create", json={"name": "Origami", "category_id": placeholders["arts"], "icon": "string", "icon_type": "emoji"}, headers=admin)
    assert interest.status_code == 422
    moved = await client.put(
        f"{API}/{placeholders['ui']}", json={"name": "UI/UX", "category_id": placeholders["arts"], "icon": "🎛️", "icon_type": "emoji"}, headers=admin,
    )
    assert moved.status_code == 200
