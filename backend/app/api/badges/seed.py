"""
Seed badge definitions into the database.
Call this function on app startup or via a management command.
"""
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.badges.models import BadgeDefinitions, BadgeType

logger = logging.getLogger(__name__)

USER_BADGES = [
    {
        "slug": "first_event",
        "name": "First Steps",
        "description": "Registered for your first event",
        "emoji": "🎟️",
        "trigger_metric": "events_registered",
        "threshold": 1,
    },
    {
        "slug": "winner",
        "name": "Winner",
        "description": "Won an event",
        "emoji": "🏆",
        "trigger_metric": "events_won",
        "threshold": 1,
    },
    {
        "slug": "event_explorer",
        "name": "Event Explorer",
        "description": "Attended 5 events",
        "emoji": "⭐",
        "trigger_metric": "events_attended",
        "threshold": 5,
    },
    {
        "slug": "event_enthusiast",
        "name": "Event Enthusiast",
        "description": "Attended 15 events",
        "emoji": "🔥",
        "trigger_metric": "events_attended",
        "threshold": 15,
    },
    {
        "slug": "event_legend",
        "name": "Event Legend",
        "description": "Attended 50 events",
        "emoji": "🌟",
        "trigger_metric": "events_attended",
        "threshold": 50,
    },
    {
        "slug": "critic",
        "name": "Critic",
        "description": "Rated 5 events",
        "emoji": "📝",
        "trigger_metric": "events_rated",
        "threshold": 5,
    },
    {
        "slug": "supporter",
        "name": "Supporter",
        "description": "Paid for 3 events",
        "emoji": "💰",
        "trigger_metric": "events_paid",
        "threshold": 3,
    },
    {
        "slug": "runner_up",
        "name": "Runner Up",
        "description": "Finished 2nd in an event",
        "emoji": "🥈",
        "trigger_metric": "runner_up_count",
        "threshold": 1,
    },
    {
        "slug": "podium_finish",
        "name": "Podium Finish",
        "description": "Finished 3rd in an event",
        "emoji": "🥉",
        "trigger_metric": "podium_count",
        "threshold": 1,
    },
    {
        "slug": "serial_winner",
        "name": "Serial Winner",
        "description": "Won 5 events",
        "emoji": "🏅",
        "trigger_metric": "events_won",
        "threshold": 5,
    },
]

CLUB_BADGES = [
    {
        "slug": "first_event_hosted",
        "name": "First Event",
        "description": "Hosted your first event",
        "emoji": "🎉",
        "trigger_metric": "events_hosted",
        "threshold": 1,
    },
    {
        "slug": "event_machine",
        "name": "Event Machine",
        "description": "Hosted 10 events",
        "emoji": "📅",
        "trigger_metric": "events_hosted",
        "threshold": 10,
    },
    {
        "slug": "event_powerhouse",
        "name": "Event Powerhouse",
        "description": "Hosted 50 events",
        "emoji": "🏟️",
        "trigger_metric": "events_hosted",
        "threshold": 50,
    },
    {
        "slug": "growing_community",
        "name": "Growing Community",
        "description": "Reached 50 followers",
        "emoji": "👥",
        "trigger_metric": "follower_count",
        "threshold": 50,
    },
    {
        "slug": "popular_club",
        "name": "Popular Club",
        "description": "Reached 200 followers",
        "emoji": "🌐",
        "trigger_metric": "follower_count",
        "threshold": 200,
    },
    {
        "slug": "highly_rated",
        "name": "Highly Rated",
        "description": "Maintained an average rating of 4.0+",
        "emoji": "⭐",
        "trigger_metric": "avg_rating",
        "threshold": 4.0,
    },
    {
        "slug": "hundred_attendees",
        "name": "100 Attendees",
        "description": "100 total attendances across all events",
        "emoji": "🎓",
        "trigger_metric": "total_attendees",
        "threshold": 100,
    },
    {
        "slug": "verified_club",
        "name": "Verified",
        "description": "Club has been verified",
        "emoji": "✅",
        "trigger_metric": "is_verified",
        "threshold": 1,
    },
]


async def seed_badges(session: AsyncSession):
    """Insert or update badge definitions. Safe to call multiple times."""
    created = 0
    updated = 0

    all_badges = [
        {**b, "badge_type": BadgeType.user} for b in USER_BADGES
    ] + [
        {**b, "badge_type": BadgeType.club} for b in CLUB_BADGES
    ]

    for badge_data in all_badges:
        existing = await session.scalar(
            select(BadgeDefinitions).where(BadgeDefinitions.slug == badge_data["slug"])
        )
        if existing:
            # Update fields if changed
            for key, val in badge_data.items():
                setattr(existing, key, val)
            updated += 1
        else:
            session.add(BadgeDefinitions(**badge_data))
            created += 1

    await session.commit()
    logger.info(f"Badge seed complete: {created} created, {updated} updated")
