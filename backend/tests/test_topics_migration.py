"""The topics migration's seed, on production's shape: one "arts" topic
holding all 27 interests, chosen by people, clubs and events.

The seed is imported from the migration file itself and run on the suite's
schema (which create_all already gave the new columns), inside the test's
transaction. What it must do: every interest under its topic in the tree's
order, every id kept (so every link still means the same interest), the new
topics, interests and event types made, and nothing at all on a second run."""
import importlib.util
import pathlib

import pytest
from sqlalchemy import select, text

from app.api.clubs.models import ClubInterestsLink
from app.api.events.models import EventCategories, EventInterestsLink
from app.api.interests.models import InterestCategory, Interests
from app.api.users.models import UserInterests

#: Production's 27 interests, as they are named there (2026-10).
PRODUCTION = (
    "Web Development", "Artificial Intelligence", "Software Engineering", "Drawing", "Singing", "Coding", "UI/UX",
    "Data Science", "Entrepreneurship", "Marketing", "Finance", "Analytics", "Cybersecurity", "Product Management",
    "Photography", "Music", "Film", "Animation", "Writing", "Fashion", "Gaming", "Blockchain", "VR/AR",
    "Memes & Internet Culture", "Content Creation", "E-Sports", "Space Exploration",
)

#: What the tree must read afterwards, topic by topic (the contract's table).
EXPECTED = {
    "development": ("Development", "#2563EB", [
        "Web Development", "App Development", "Software Engineering", "Coding", "Open Source", "Cloud & DevOps",
        "Cybersecurity", "Data Science",
    ]),
    "deep-tech": ("Deep Tech", "#7C3AED", [
        "Artificial Intelligence", "Robotics", "IoT & Hardware", "Blockchain", "VR/AR", "Quantum Computing", "Space Exploration",
    ]),
    "design": ("Design", "#DB2777", ["UI/UX", "Graphic Design", "Product Design", "Animation"]),
    "arts": ("Arts", "#EA580C", ["Drawing", "Music", "Singing", "Dance", "Theatre", "Photography", "Film", "Writing", "Fashion"]),
    "business": ("Business", "#059669", ["Entrepreneurship", "Product Management", "Marketing", "Finance", "Analytics"]),
    "gaming-media": ("Gaming & Media", "#DC2626", ["Gaming", "E-Sports", "Content Creation", "Memes & Internet Culture"]),
    "community": ("Community", "#0D9488", ["Volunteering", "Sustainability", "Public Speaking", "Debate & MUN", "Quizzing"]),
    "sports-fitness": ("Sports & Fitness", "#CA8A04", ["Sports", "Fitness", "Outdoors & Adventure"]),
}

TYPES = (
    "Workshop", "Talk", "Hackathon", "Competition", "Meetup", "Conference", "Fest", "Webinar", "Performance",
    "Exhibition", "Bootcamp",
)


def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "topics.py"
    spec = importlib.util.spec_from_file_location("topics_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed(session, module) -> dict:
    connection = await session.connection()
    return await connection.run_sync(lambda sync: module.seed(sync))


async def _tree(session) -> list[dict]:
    """The live topics in order, each with its live interests in order, as rows."""
    topics = [dict(r._mapping) for r in await session.execute(text(
        "SELECT id, slug, name, icon, icon_type::text AS icon_type, color, description, sort_order FROM interest_categories "
        "WHERE NOT is_deleted ORDER BY sort_order, lower(name), id"
    ))]
    interests = [dict(r._mapping) for r in await session.execute(text(
        "SELECT id, slug, name, category_id, sort_order FROM interests WHERE NOT is_deleted ORDER BY sort_order, lower(name), id"
    ))]
    for t in topics:
        t["interests"] = [i for i in interests if i["category_id"] == t["id"]]
    return topics


async def _snapshot(session) -> dict:
    """Every row of the tables the seed touches, every column (updated_at too)."""
    out = {}
    for table in ("interest_categories", "interests", "event_categories", "user_interests", "clubs_interests_link", "event_interests_link"):
        rows = await session.execute(text(f"SELECT * FROM {table} ORDER BY id"))
        out[table] = [tuple(r) for r in rows.all()]
    return out


@pytest.fixture
async def production(session, make_user, make_club, make_event):
    """Production's interests as they are: one "arts", 27 interests, chosen."""
    arts = InterestCategory(name="arts")
    session.add(arts)
    await session.flush()
    interests = {}
    for name in PRODUCTION:
        interests[name] = Interests(name=name, category_id=arts.id)
        session.add(interests[name])
    await session.flush()

    people = [await make_user(email=f"topics-seed-{i}@example.com") for i in range(3)]
    for person, picks in zip(people, (["Web Development", "Cybersecurity"], ["Music", "UI/UX", "Gaming"], ["Finance"])):
        for name in picks:
            session.add(UserInterests(user_id=person.id, interest_id=interests[name].id))
    owner, club = await make_club(name="Seed Club", email="topics-seed-club@example.com")
    for name in ("Coding", "Artificial Intelligence", "Photography"):
        session.add(ClubInterestsLink(club_id=club.id, interest_id=interests[name].id))
    event = await make_event(club=club, created_by=owner, name="Seed Hack")
    for name in ("Web Development", "Blockchain"):
        session.add(EventInterestsLink(event_id=event.id, interest_id=interests[name].id))
    # Event types: one the product has under another case, one a club made, one deleted.
    session.add_all([
        EventCategories(name="workshop"),
        EventCategories(name="Hackathon", created_by_id=owner.id),
        EventCategories(name="Talk", is_deleted=True),
    ])
    await session.commit()
    return dict(arts=arts.id, interests={n: i.id for n, i in interests.items()}, people=people, club=club, event=event)


async def test_the_seed_puts_productions_interests_under_their_topics(session, production):
    module = _migration()
    assert module.down_revision == "discover_integrity" and module.revision == "topics"
    before = await _snapshot(session)
    links_before = {t: before[t] for t in ("user_interests", "clubs_interests_link", "event_interests_link")}

    done = await _seed(session, module)
    await session.commit()

    assert done == dict(topics_created=7, topics_changed=1, interests_created=18, interests_changed=27, event_types_created=9)
    tree = await _tree(session)
    assert [t["slug"] for t in tree] == list(EXPECTED)
    for topic in tree:
        name, color, names = EXPECTED[topic["slug"]]
        assert (topic["name"], topic["color"]) == (name, color)
        assert [i["name"] for i in topic["interests"]] == names, topic["slug"]
        assert [i["sort_order"] for i in topic["interests"]] == list(range(1, len(names) + 1))
        assert all(i["category_id"] == topic["id"] for i in topic["interests"])
        assert topic["icon"] and topic["icon_type"] == "emoji" and topic["description"]
    assert [t["sort_order"] for t in tree] == list(range(1, 9))

    # "arts" is still id 1's row (here, the fixture's), now "Arts".
    arts = next(t for t in tree if t["slug"] == "arts")
    assert arts["id"] == production["arts"]
    # Every one of the 27 kept its id; 18 are new, 45 in all.
    by_name = {i["name"]: i["id"] for t in tree for i in t["interests"]}
    assert len(by_name) == 45
    assert {n: by_name[n] for n in PRODUCTION} == production["interests"]
    assert by_name["UI/UX"] == production["interests"]["UI/UX"]
    slugs = {i["name"]: i["slug"] for t in tree for i in t["interests"]}
    assert slugs["UI/UX"] == "ui-ux" and slugs["Memes & Internet Culture"] == "memes-internet-culture" and slugs["Cloud & DevOps"] == "cloud-devops"

    # Every person's, club's and event's choice is the row it was, so it still means the same interest.
    after = await _snapshot(session)
    assert {t: after[t] for t in links_before} == links_before

    # The event types: made where no live one has the name (in any case), whoever made it.
    types = (await session.execute(select(EventCategories.name, EventCategories.created_by_id).where(EventCategories.is_deleted == False))).all()  # noqa: E712
    # (and the "General" the event fixture made, a club's)
    assert sorted(n.lower() for n, _ in types if n != "General") == sorted(t.lower() for t in TYPES)
    made = {n for n, by in types if by is None and n != "workshop"}
    assert made == set(TYPES) - {"Workshop", "Hackathon"}


async def test_running_the_seed_again_changes_nothing(session, production):
    module = _migration()
    await _seed(session, module)
    await session.commit()
    first = await _snapshot(session)

    again = await _seed(session, module)
    await session.commit()

    assert again == dict(topics_created=0, topics_changed=0, interests_created=0, interests_changed=0, event_types_created=0)
    assert await _snapshot(session) == first


async def test_the_seed_keeps_what_an_admin_set_and_what_the_tree_does_not_name(session, production):
    """Colour, icon and description are filled only where empty; an interest
    the tree does not name stays in its topic, after the named ones; a topic
    the tree does not name goes after the tree's; a deleted interest of a
    name the tree wants is brought back, not duplicated."""
    arts = await session.get(InterestCategory, production["arts"])
    arts.color, arts.icon = "#000000", "🖌️"
    extra = Interests(name="Calligraphy", category_id=arts.id)
    robotics = Interests(name="robotics", category_id=arts.id, is_deleted=True)
    tech = InterestCategory(name="Tech Talk")
    session.add_all([extra, robotics, tech])
    await session.commit()

    await _seed(session, _migration())
    await session.commit()

    rows = {
        r.name: r for r in (await session.execute(
            select(InterestCategory).execution_options(populate_existing=True)
        )).scalars()
    }
    assert (rows["Arts"].color, rows["Arts"].icon) == ("#000000", "🖌️")
    assert rows["Arts"].description  # empty before: filled
    assert rows["Tech Talk"].sort_order == 9
    calligraphy = await session.scalar(select(Interests).where(Interests.name == "Calligraphy").execution_options(populate_existing=True))
    assert calligraphy.category_id == arts.id and calligraphy.sort_order == 10  # after the tree's nine
    revived = await session.scalar(select(Interests).where(Interests.id == robotics.id).execution_options(populate_existing=True))
    assert revived.is_deleted is False and revived.deleted_at is None and revived.category_id == rows["Deep Tech"].id
    assert await session.scalar(select(Interests.id).where(Interests.name == "Robotics")) is None  # no second one


async def test_the_backfill_gives_every_row_a_slug_of_its_own(session):
    """On tables as they were before the migration (no slugs), in a schema of
    their own inside the test's transaction: names that make the same slug
    take -2, -3… in id order."""
    module = _migration()
    connection = await session.connection()

    def run(sync):
        sync.execute(text("CREATE SCHEMA topics_backfill"))
        sync.execute(text("SET LOCAL search_path TO topics_backfill"))
        for table in ("interest_categories", "interests"):
            sync.execute(text(f"CREATE TABLE {table} (id serial PRIMARY KEY, name text NOT NULL, slug varchar(80))"))
        sync.execute(text("INSERT INTO interest_categories (name) VALUES ('arts'), ('Arts!'), ('Café Society')"))
        sync.execute(text("INSERT INTO interests (name) VALUES ('UI/UX'), ('UI UX'), ('ui-ux'), ('&&')"))
        sync.execute(text("UPDATE interests SET slug = 'taken' WHERE name = '&&'"))
        written = module.backfill_slugs(sync)
        got = {
            table: [tuple(r) for r in sync.execute(text(f"SELECT name, slug FROM {table} ORDER BY id"))]
            for table in ("interest_categories", "interests")
        }
        sync.execute(text("SET LOCAL search_path TO public"))
        return written, got

    written, got = await connection.run_sync(run)
    assert written == 6
    assert got["interest_categories"] == [("arts", "arts"), ("Arts!", "arts-2"), ("Café Society", "cafe-society")]
    assert got["interests"] == [("UI/UX", "ui-ux"), ("UI UX", "ui-ux-2"), ("ui-ux", "ui-ux-3"), ("&&", "taken")]
