"""The test-data seeder in scripts/seed_test_events.py.

It writes to whichever database the app is pointed at, which in a deployed
container is production, so it is worth knowing it does what it says --
particularly that --remove takes out everything it created and nothing else.
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.clubs.models import Notes  # noqa: E402
from app.api.events.models import EventInterestsLink, Events  # noqa: E402
from scripts.seed_test_events import MARKER, remove, seed  # noqa: E402


@pytest.fixture
async def world(session, make_club, make_user):
    """Clubs whose slugs the seeder looks for, plus a category and interests."""
    from app.api.events.models import EventCategories
    from app.api.interests.models import InterestCategory, Interests

    owner, _ = await make_club(name="LetMeGoo", email="seed-lmg@example.com")
    await make_club(name="Yasya", email="seed-yasya@example.com")
    await make_club(name="DOTSLASH 25", email="seed-dot@example.com")
    await make_club(name="Yukthi", email="seed-yukthi@example.com")

    session.add_all([
        EventCategories(name="Workshop", created_by_id=owner.id),
        EventCategories(name="Competition", created_by_id=owner.id),
    ])
    category = InterestCategory(name="General")
    session.add(category)
    await session.flush()
    for name in ("Coding", "Cybersecurity", "Entrepreneurship", "UI/UX",
                 "Product Management", "Singing", "Drawing"):
        session.add(Interests(name=name, category_id=category.id))
    await session.flush()


async def _marked(session):
    return (
        await session.scalars(
            select(Events).where(
                Events.name.like(f"{MARKER}%"), Events.is_deleted == False
            )
        )
    ).all()


async def test_seed_creates_marked_events_with_interest_tags(session, world):
    await seed(session)

    events = await _marked(session)
    assert len(events) == 12
    # The marker is what --remove keys on, so every row must carry it.
    assert all(e.name.startswith(MARKER) for e in events)

    tags = (
        await session.scalars(
            select(EventInterestsLink).where(
                EventInterestsLink.event_id.in_([e.id for e in events])
            )
        )
    ).all()
    assert len(tags) >= 12


async def test_the_edge_cases_are_actually_edge_cases(session, world):
    await seed(session)
    by_name = {e.name.replace(f"{MARKER} ", ""): e for e in await _marked(session)}

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # The three the feed must exclude, each for a different reason.
    assert by_name["Registration Shut"].reg_enddate < now
    assert by_name["Finished Last Week"].event_datetime < now
    assert by_name["Muted Club Event"].club_id is not None

    # And the one that looks finished but is not: started, still running.
    running = by_name["Running Right Now"]
    assert running.event_datetime <= now
    end = running.event_datetime.timestamp() + running.duration * 3600
    assert end > now.timestamp()


async def test_seeded_events_are_spread_across_clubs(session, world):
    await seed(session)
    events = await _marked(session)

    # If everything landed on one club, the club-affinity half of the ranking
    # is untested by the seed.
    assert len({e.club_id for e in events}) >= 3


async def test_remove_takes_out_the_seed_and_leaves_real_data(
    session, world, make_club, make_event,
):
    owner, club = await make_club(name="Real Club", email="seed-real@example.com")
    real = await make_event(club=club, created_by=owner, name="A Real Event")

    await seed(session)
    assert len(await _marked(session)) == 12

    await remove(session)

    assert await _marked(session) == []
    # The real event is untouched -- the marker is the only thing removed.
    await session.refresh(real)
    assert real.is_deleted is False

    notes = (
        await session.scalars(
            select(Notes).where(Notes.title.like(f"{MARKER}%"), Notes.is_deleted == False)
        )
    ).all()
    assert notes == []


async def test_remove_is_a_soft_delete(session, world):
    await seed(session)
    before = {e.id for e in await _marked(session)}

    await remove(session)

    # Soft, like the rest of the platform, so a mistake is recoverable. Read
    # with include_deleted: the session hides deleted rows, as production's
    # does (app.db.listeners).
    still_there = (
        await session.scalars(
            select(Events).where(Events.id.in_(before)).execution_options(include_deleted=True)
        )
    ).all()
    assert len(still_there) == len(before)
    assert all(e.is_deleted for e in still_there)


async def test_custom_fields_land_on_the_events_that_declare_them(session, world):
    from scripts.seed_test_events import CUSTOM_FIELDS

    await seed(session)
    by_name = {e.name.replace(f"{MARKER} ", ""): e for e in await _marked(session)}

    for title, fields in CUSTOM_FIELDS.items():
        event = by_name[title]
        assert event.additional_details is not None, title
        assert len(event.additional_details) == len(fields)

    # And an event that declares none has none, so the plain registration
    # path is still exercised by the seed.
    assert by_name["Cybersecurity CTF"].additional_details in (None, [])


async def test_a_tshirt_field_is_a_select_with_options(session, world):
    await seed(session)
    by_name = {e.name.replace(f"{MARKER} ", ""): e for e in await _marked(session)}

    fields = {f["key"]: f for f in by_name["Secure Coding Sprint"].additional_details}
    tshirt = fields["tshirt_size"]

    # select/radio/checkbox without options are rejected by event creation,
    # so the seed must not produce one.
    assert tshirt["field_type"] == "select"
    assert tshirt["options"] == ["S", "M", "L", "XL", "XXL"]

    for field in by_name["Secure Coding Sprint"].additional_details:
        if field["field_type"] in ("select", "radio", "checkbox"):
            assert field["options"], field["key"]


async def test_the_seed_includes_an_optional_field(session, world):
    # Registration validation never reads `required`, so an optional field is
    # rejected when omitted just like a mandatory one. Seeding one makes that
    # reproducible rather than theoretical.
    await seed(session)
    by_name = {e.name.replace(f"{MARKER} ", ""): e for e in await _marked(session)}

    fields = {f["key"]: f for f in by_name["Secure Coding Sprint"].additional_details}

    assert fields["dietary"]["required"] is False


async def test_award_badges_is_partial_and_idempotent(session, world, make_user):
    from app.api.badges.models import BadgeDefinitions, BadgeType, UserBadges
    from scripts.seed_test_events import AWARD_BADGES, award_badges

    for slug in AWARD_BADGES + ["not_awarded"]:
        session.add(
            BadgeDefinitions(
                slug=slug, name=slug.replace("_", " ").title(),
                description="seeded for test", emoji="*",
                badge_type=BadgeType.user, trigger_metric="events_attended",
                threshold=1,
            )
        )
    await session.flush()
    user = await make_user(email="badges@example.com")

    await award_badges(session, user.id)
    first = (
        await session.scalars(select(UserBadges).where(UserBadges.user_id == user.id))
    ).all()
    assert len(first) == len(AWARD_BADGES)

    # Partial on purpose: an unearned badge has to remain, or the locked
    # state has nothing to render.
    awarded_slugs = {
        (await session.get(BadgeDefinitions, b.badge_id)).slug for b in first
    }
    assert "not_awarded" not in awarded_slugs

    # Running it twice must not double-award.
    await award_badges(session, user.id)
    second = (
        await session.scalars(select(UserBadges).where(UserBadges.user_id == user.id))
    ).all()
    assert len(second) == len(first)
