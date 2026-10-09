"""Seed (or remove) clearly-marked test events for verifying the For You feed.

    docker compose exec -T backend python scripts/seed_test_events.py --confirm
    docker compose exec -T backend python scripts/seed_test_events.py --remove --confirm

Every row this creates is named with the MARKER prefix, so --remove finds the
whole set and nothing else. Both modes refuse to run without --confirm.

Why a script rather than POST /events/create: the API path calls
notify_followers_of_new_event and notify_users_by_interest, which send real
push notifications. Seeding a dozen events through it would put a dozen
notifications on every follower's phone. These inserts go through the models
directly and touch no notification code.
"""

import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, ".")

from app.api.badges.models import BadgeDefinitions, UserBadges  # noqa: E402
from app.api.clubs.models import Clubs, Notes  # noqa: E402
from app.api.events.models import (  # noqa: E402
    EventInterestsLink,
    Events,
)
from app.core.utils.keys import generate_slug  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402

MARKER = "[TEST]"

# (title, club slug fragment, days out, duration, category, interest names,
#  fee, prize, online, registration ends in N days, what it proves)
PLAN = [
    ("Secure Coding Sprint",      "letmegoo",  2,  6.0, "Competition",
     ["Coding", "Cybersecurity"],      0,  5000, False,  1,
     "pinned club + two interests + imminent"),
    ("Founders Coding Jam",       "yasya",     6,  5.0, "Competition",
     ["Coding", "Entrepreneurship"],   0,  2000, False,  5,
     "followed club + two interests"),
    ("Cybersecurity CTF",         "dotslash",  7,  8.0, "Competition",
     ["Cybersecurity", "Coding"],      0, 10000, False,  6,
     "interests alone, no club tie"),
    ("Product Teardown Lab",      "letmegoo",  9,  3.0, "Workshop",
     ["Product Management"],           0,     0, False,  8,
     "pinned club + one interest"),
    ("UX Research Clinic",        "yasya",    16,  2.0, "Workshop",
     ["UI/UX"],                      150,     0, True,  14,
     "followed club + UI/UX + paid"),
    ("Annual Entrepreneur Summit", "letmegoo", 30, 8.0, "Workshop",
     ["Entrepreneurship", "Product Management"], 500, 50000, False, 28,
     "pinned club but far out, so urgency adds nothing"),
    ("Classical Music Evening",   "yasya",    11,  3.0, "Workshop",
     ["Singing"],                      0,     0, False, 10,
     "followed club, no interest match"),
    ("Gardening Basics",          "dotslash", 13,  2.0, "Workshop",
     ["Drawing"],                      0,     0, False, 12,
     "no club tie, no interest -- should rank last"),
    ("Running Right Now",         "letmegoo",  0,  9.0, "Workshop",
     ["Coding", "UI/UX"],              0,     0, False,  1,
     "started already but still running, must stay visible"),

    # Each of these must be absent from the feed.
    ("Muted Club Event",          "yukthi",    4,  4.0, "Competition",
     ["Coding"],                       0,     0, False,  3,
     "club is hidden for the test user, must not appear"),
    ("Registration Shut",         "letmegoo",  8,  4.0, "Workshop",
     ["Coding"],                       0,     0, False, -1,
     "registration closed, must not appear"),
    ("Finished Last Week",        "letmegoo", -7,  3.0, "Workshop",
     ["Coding"],                       0,     0, False, -8,
     "already ended, must not appear"),
]

# Custom registration fields, stored on Events.additional_details and
# validated in app/api/events/registration/service.py at registration time.
#
# NOTE on `required`: that validator rejects any field whose key is absent
# from the submission without ever consulting field.required, so an optional
# field is optional in name only. "Dietary requirements" below is marked
# required=False precisely so the gap is visible when testing.
CUSTOM_FIELDS = {
    "Secure Coding Sprint": [
        {"key": "tshirt_size", "label": "T-shirt size", "field_type": "select",
         "required": True, "options": ["S", "M", "L", "XL", "XXL"]},
        {"key": "experience", "label": "Experience level", "field_type": "radio",
         "required": True, "options": ["Beginner", "Intermediate", "Advanced"]},
        {"key": "dietary", "label": "Dietary requirements",
         "field_type": "textarea", "required": False, "options": None},
        {"key": "emergency_contact", "label": "Emergency contact",
         "field_type": "tel", "required": True, "options": None},
    ],
    "Founders Coding Jam": [
        {"key": "tshirt_size", "label": "T-shirt size", "field_type": "select",
         "required": True, "options": ["S", "M", "L", "XL"]},
        {"key": "team_name", "label": "Team name", "field_type": "text",
         "required": True, "options": None},
        {"key": "github", "label": "GitHub profile", "field_type": "url",
         "required": False, "options": None},
    ],
    "Annual Entrepreneur Summit": [
        {"key": "company", "label": "Company or idea", "field_type": "text",
         "required": True, "options": None},
        {"key": "stage", "label": "Stage", "field_type": "select",
         "required": True, "options": ["Idea", "Prototype", "Revenue", "Scaling"]},
        {"key": "pitch_slot", "label": "Preferred pitch slot",
         "field_type": "time", "required": True, "options": None},
        {"key": "consent", "label": "Agree to be recorded",
         "field_type": "checkbox", "required": True, "options": ["Yes", "No"]},
    ],
    # The rest are left plain on purpose, so a registration with no custom
    # fields is testable alongside one that has them.
}

# Badges to award to --user-id, so both the earned and the unearned state
# have something to show. Production holds 18 definitions and had awarded
# none of them to anybody.
AWARD_BADGES = ["first_event", "event_explorer", "critic", "runner_up"]

NOTES = [
    ("Doors open early", "Doors open 30 minutes before each session. Bring a laptop."),
    ("Limited seats", "Seats are limited this term -- register early."),
    ("Certificates", "Certificates are issued to everyone who attends in full."),
]


async def _lookup(session: AsyncSession):
    """Resolve the names in PLAN to the ids this database actually uses."""
    from app.api.events.models import EventCategories
    from app.api.interests.models import Interests

    clubs = (await session.scalars(select(Clubs).where(Clubs.is_deleted == False))).all()
    categories = (
        await session.scalars(
            select(EventCategories).where(EventCategories.is_deleted == False)
        )
    ).all()
    interests = (
        await session.scalars(select(Interests).where(Interests.is_deleted == False))
    ).all()
    return (
        {c.slug: c for c in clubs},
        {c.name: c.id for c in categories},
        {i.name: i.id for i in interests},
    )


async def seed(session: AsyncSession):
    clubs, categories, interests = await _lookup(session)
    if not categories:
        sys.exit("No event categories exist; cannot seed.")

    made = 0
    for (title, club_frag, days, dur, cat_name, interest_names, fee,
         prize, online, reg_end, why) in PLAN:
        club = next(
            (c for slug, c in clubs.items() if club_frag in slug.lower()), None
        )
        if club is None:
            print(f"  skip {title}: no club matching {club_frag!r}")
            continue

        category_id = categories.get(cat_name) or next(iter(categories.values()))
        name = f"{MARKER} {title}"
        now = datetime.now(timezone.utc)

        event = Events(
            slug=generate_slug(name),
            name=name,
            images=[],
            event_datetime=now + timedelta(days=days),
            duration=dur,
            has_fee=fee > 0,
            reg_fee=fee or None,
            has_prize=prize > 0,
            prize_amount=prize or None,
            is_online=online,
            location_name="Online" if online else "Main Auditorium",
            about=f"Seeded for feed testing. Proves: {why}.",
            reg_startdate=now - timedelta(days=2),
            reg_enddate=now + timedelta(days=reg_end),
            category_id=category_id,
            club_id=club.id,
            max_participants=100,
            # A spread so the popularity term is not uniformly zero.
            page_views=random.randint(0, 60),
        )
        custom = CUSTOM_FIELDS.get(title)
        if custom:
            event.additional_details = custom

        session.add(event)
        await session.flush()

        for interest_name in interest_names:
            interest_id = interests.get(interest_name)
            if interest_id is None:
                continue
            session.add(
                EventInterestsLink(event_id=event.id, interest_id=interest_id)
            )

        made += 1
        extra = f"  [+{len(custom)} custom fields]" if custom else ""
        print(f"  + {name}  ({club.name}, {why}){extra}")

    notes_made = 0
    for slug, club in clubs.items():
        if not any(f in slug.lower() for f in ("letmegoo", "yasya")):
            continue
        for title, body in NOTES:
            session.add(
                Notes(
                    club_id=club.id,
                    user_id=club.user_id,
                    title=f"{MARKER} {title}",
                    note=body,
                )
            )
            notes_made += 1

    await session.commit()
    print(f"\nSeeded {made} events and {notes_made} notes.")


async def award_badges(session: AsyncSession, user_id: int):
    """Give the user a few badges so the earned state has something in it.

    Deliberately partial: four of the eighteen definitions, so the profile
    shows earned and unearned side by side. Awarded directly rather than by
    driving the real triggers, which would mean registering for and attending
    events that have not happened.
    """
    definitions = {
        d.slug: d
        for d in (
            await session.scalars(
                select(BadgeDefinitions).where(
                    BadgeDefinitions.slug.in_(AWARD_BADGES)
                )
            )
        ).all()
    }
    existing = {
        b.badge_id
        for b in (
            await session.scalars(
                select(UserBadges).where(UserBadges.user_id == user_id)
            )
        ).all()
    }

    added = 0
    for slug in AWARD_BADGES:
        definition = definitions.get(slug)
        if definition is None:
            print(f"  ? no badge definition {slug!r}, skipping")
            continue
        if definition.id in existing:
            print(f"  = {definition.emoji} {definition.name} already awarded")
            continue
        session.add(UserBadges(user_id=user_id, badge_id=definition.id))
        added += 1
        print(f"  + {definition.emoji} {definition.name}")

    await session.commit()
    print(f"\nAwarded {added} badges to user {user_id}.")


async def remove(session: AsyncSession):
    events = (
        await session.scalars(
            select(Events).where(
                Events.name.like(f"{MARKER}%"), Events.is_deleted == False
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    for event in events:
        event.is_deleted = True
        event.deleted_at = now

    notes = (
        await session.scalars(
            select(Notes).where(
                Notes.title.like(f"{MARKER}%"), Notes.is_deleted == False
            )
        )
    ).all()
    for note in notes:
        note.is_deleted = True
        note.deleted_at = now

    await session.commit()
    # Soft delete, matching how the rest of the platform removes things,
    # so this is reversible if it takes out something unexpected.
    print(f"Soft-deleted {len(events)} events and {len(notes)} notes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="undo a previous seed")
    parser.add_argument(
        "--award-badges",
        type=int,
        metavar="USER_ID",
        help="also award a few badges to this user, so the profile has some",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required: this writes to whichever database the app is configured for",
    )
    args = parser.parse_args()

    if not args.confirm:
        sys.exit(
            "Refusing to run without --confirm. This writes to the configured "
            "database, which in a deployed container is production."
        )

    async def _run():
        async with AsyncSessionLocal() as session:
            if args.remove:
                await remove(session)
                return
            await seed(session)
            if args.award_badges:
                print()
                await award_badges(session, args.award_badges)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
