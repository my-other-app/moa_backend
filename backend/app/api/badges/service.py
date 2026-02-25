"""
Badge awarding and retrieval logic.
"""
import logging
from datetime import datetime, timezone
from sqlalchemy import select, func, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.badges.models import BadgeDefinitions, BadgeType, UserBadges, ClubBadges
from app.api.events.models import (
    EventRegistrationsLink,
    EventRatingsLink,
    Events,
)
from app.api.clubs.models import Clubs, ClubUsersLink

logger = logging.getLogger(__name__)


# ─── User badge checks ───────────────────────────────────────────────

async def _get_user_metrics(session: AsyncSession, user_id: int) -> dict:
    """Compute all metrics needed for user badge checks in one pass."""

    # events_registered
    events_registered = await session.scalar(
        select(func.count()).where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_deleted == False,
        )
    ) or 0

    # events_attended
    events_attended = await session.scalar(
        select(func.count()).where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_attended == True,
            EventRegistrationsLink.is_deleted == False,
        )
    ) or 0

    # events_won (position == 1)
    events_won = await session.scalar(
        select(func.count()).where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.position == 1,
            EventRegistrationsLink.is_deleted == False,
        )
    ) or 0

    # runner_up_count (position == 2)
    runner_up_count = await session.scalar(
        select(func.count()).where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.position == 2,
            EventRegistrationsLink.is_deleted == False,
        )
    ) or 0

    # podium_count (position == 3)
    podium_count = await session.scalar(
        select(func.count()).where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.position == 3,
            EventRegistrationsLink.is_deleted == False,
        )
    ) or 0

    # events_rated
    events_rated = await session.scalar(
        select(func.count()).where(
            EventRatingsLink.user_id == user_id,
            EventRatingsLink.is_deleted == False,
        )
    ) or 0

    # events_paid
    events_paid = await session.scalar(
        select(func.count()).where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_paid == True,
            EventRegistrationsLink.is_deleted == False,
        )
    ) or 0

    return {
        "events_registered": events_registered,
        "events_attended": events_attended,
        "events_won": events_won,
        "runner_up_count": runner_up_count,
        "podium_count": podium_count,
        "events_rated": events_rated,
        "events_paid": events_paid,
    }


async def check_and_award_user_badges(session: AsyncSession, user_id: int) -> list[str]:
    """
    Check all user-type badge definitions and award any newly earned badges.
    Returns list of newly awarded badge slugs.
    """
    metrics = await _get_user_metrics(session, user_id)

    # Get all user badge definitions
    result = await session.execute(
        select(BadgeDefinitions).where(BadgeDefinitions.badge_type == BadgeType.user)
    )
    definitions = result.scalars().all()

    # Get already-awarded badge IDs
    result = await session.execute(
        select(UserBadges.badge_id).where(UserBadges.user_id == user_id)
    )
    awarded_ids = set(result.scalars().all())

    newly_awarded = []

    for badge_def in definitions:
        if badge_def.id in awarded_ids:
            continue

        metric_value = metrics.get(badge_def.trigger_metric, 0)
        if metric_value >= badge_def.threshold:
            session.add(UserBadges(
                user_id=user_id,
                badge_id=badge_def.id,
                awarded_at=datetime.now(timezone.utc),
            ))
            newly_awarded.append(badge_def.slug)
            logger.info(f"Awarded badge '{badge_def.slug}' to user {user_id}")

    if newly_awarded:
        await session.commit()

    return newly_awarded


# ─── Club badge checks ───────────────────────────────────────────────

async def _get_club_metrics(session: AsyncSession, club_id: int) -> dict:
    """Compute all metrics needed for club badge checks."""

    # events_hosted
    events_hosted = await session.scalar(
        select(func.count()).where(
            Events.club_id == club_id,
            Events.is_deleted == False,
        )
    ) or 0

    # follower_count
    follower_count = await session.scalar(
        select(func.count()).where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,
            ClubUsersLink.is_deleted == False,
        )
    ) or 0

    # avg_rating
    club = await session.scalar(
        select(Clubs).where(Clubs.id == club_id)
    )
    avg_rating = float(club.rating) if club else 0.0

    # total_attendees across all events
    total_attendees = await session.scalar(
        select(func.count()).where(
            EventRegistrationsLink.event_id.in_(
                select(Events.id).where(
                    Events.club_id == club_id,
                    Events.is_deleted == False,
                )
            ),
            EventRegistrationsLink.is_attended == True,
            EventRegistrationsLink.is_deleted == False,
        )
    ) or 0

    # is_verified
    is_verified = 1 if (club and club.is_verified) else 0

    return {
        "events_hosted": events_hosted,
        "follower_count": follower_count,
        "avg_rating": avg_rating,
        "total_attendees": total_attendees,
        "is_verified": is_verified,
    }


async def check_and_award_club_badges(session: AsyncSession, club_id: int) -> list[str]:
    """
    Check all club-type badge definitions and award any newly earned badges.
    Returns list of newly awarded badge slugs.
    """
    metrics = await _get_club_metrics(session, club_id)

    result = await session.execute(
        select(BadgeDefinitions).where(BadgeDefinitions.badge_type == BadgeType.club)
    )
    definitions = result.scalars().all()

    result = await session.execute(
        select(ClubBadges.badge_id).where(ClubBadges.club_id == club_id)
    )
    awarded_ids = set(result.scalars().all())

    newly_awarded = []

    for badge_def in definitions:
        if badge_def.id in awarded_ids:
            continue

        metric_value = metrics.get(badge_def.trigger_metric, 0)
        if metric_value >= badge_def.threshold:
            session.add(ClubBadges(
                club_id=club_id,
                badge_id=badge_def.id,
                awarded_at=datetime.now(timezone.utc),
            ))
            newly_awarded.append(badge_def.slug)
            logger.info(f"Awarded badge '{badge_def.slug}' to club {club_id}")

    if newly_awarded:
        await session.commit()

    return newly_awarded


# ─── Badge retrieval ──────────────────────────────────────────────────

async def get_user_badges(session: AsyncSession, user_id: int) -> list:
    """Get all badges awarded to a user."""
    result = await session.execute(
        select(UserBadges)
        .where(UserBadges.user_id == user_id)
        .options(joinedload(UserBadges.badge))
        .order_by(UserBadges.awarded_at.desc())
    )
    return result.scalars().all()


async def get_club_badges(session: AsyncSession, club_id: int) -> list:
    """Get all badges awarded to a club."""
    result = await session.execute(
        select(ClubBadges)
        .where(ClubBadges.club_id == club_id)
        .options(joinedload(ClubBadges.badge))
        .order_by(ClubBadges.awarded_at.desc())
    )
    return result.scalars().all()


async def get_all_badge_definitions(session: AsyncSession) -> list:
    """Get all badge definitions."""
    result = await session.execute(
        select(BadgeDefinitions).order_by(BadgeDefinitions.badge_type, BadgeDefinitions.threshold)
    )
    return result.scalars().all()


# ─── Admin Management ─────────────────────────────────────────────────

async def get_admin_badges(session: AsyncSession) -> list[dict]:
    """Get all badge definitions along with the count of users/clubs who claimed them."""
    result = await session.execute(
        select(BadgeDefinitions).order_by(BadgeDefinitions.badge_type, BadgeDefinitions.threshold)
    )
    definitions = result.scalars().all()

    # Pre-calculate counts
    user_counts_result = await session.execute(
        select(UserBadges.badge_id, func.count()).group_by(UserBadges.badge_id)
    )
    user_counts = dict(user_counts_result.all())

    club_counts_result = await session.execute(
        select(ClubBadges.badge_id, func.count()).group_by(ClubBadges.badge_id)
    )
    club_counts = dict(club_counts_result.all())

    admin_badges = []
    for badge in definitions:
        claimed_count = 0
        if badge.badge_type == BadgeType.user:
            claimed_count = user_counts.get(badge.id, 0)
        elif badge.badge_type == BadgeType.club:
            claimed_count = club_counts.get(badge.id, 0)

        admin_badges.append({
            "id": badge.id,
            "slug": badge.slug,
            "name": badge.name,
            "description": badge.description,
            "emoji": badge.emoji,
            "badge_type": badge.badge_type,
            "trigger_metric": badge.trigger_metric,
            "threshold": badge.threshold,
            "claimed_count": claimed_count,
        })
    return admin_badges


async def create_badge_definition(session: AsyncSession, badge_data: dict) -> BadgeDefinitions:
    """Create a new badge definition."""
    from app.response import CustomHTTPException
    # Check if slug exists
    exists_query = await session.scalar(
        select(func.count()).where(BadgeDefinitions.slug == badge_data["slug"])
    )
    if exists_query > 0:
        raise CustomHTTPException(400, "A badge with this slug already exists.")

    new_badge = BadgeDefinitions(
        slug=badge_data["slug"],
        name=badge_data["name"],
        description=badge_data["description"],
        emoji=badge_data["emoji"],
        badge_type=BadgeType(badge_data["badge_type"]),
        trigger_metric=badge_data["trigger_metric"],
        threshold=badge_data["threshold"],
    )
    session.add(new_badge)
    await session.commit()
    await session.refresh(new_badge)
    return new_badge


async def update_badge_definition(session: AsyncSession, badge_id: int, update_data: dict) -> BadgeDefinitions:
    """Update an existing badge definition."""
    from app.response import CustomHTTPException
    badge = await session.get(BadgeDefinitions, badge_id)
    if not badge:
        raise CustomHTTPException(404, "Badge definition not found.")

    if "slug" in update_data and update_data["slug"] != badge.slug:
        exists_query = await session.scalar(
            select(func.count()).where(
                BadgeDefinitions.slug == update_data["slug"],
                BadgeDefinitions.id != badge_id
            )
        )
        if exists_query > 0:
            raise CustomHTTPException(400, "A badge with this slug already exists.")

    for key, value in update_data.items():
        if hasattr(badge, key):
            setattr(badge, key, value)

    await session.commit()
    await session.refresh(badge)
    return badge


# ─── Analytics & Recipients ───────────────────────────────────────────

async def get_badge_recipients(session: AsyncSession, badge_id: int) -> list[dict]:
    """Get all users/clubs who have earned a specific badge."""
    from app.response import CustomHTTPException
    from app.api.users.models import Users

    badge = await session.get(BadgeDefinitions, badge_id)
    if not badge:
        raise CustomHTTPException(404, "Badge not found.")

    recipients = []
    if badge.badge_type == BadgeType.user:
        result = await session.execute(
            select(UserBadges, Users)
            .join(Users, UserBadges.user_id == Users.id)
            .where(UserBadges.badge_id == badge_id)
            .order_by(UserBadges.awarded_at.desc())
        )
        for ub, user in result.all():
            recipients.append({
                "id": ub.id,
                "entity_id": user.id,
                "entity_name": user.full_name,
                "entity_email": user.email,
                "awarded_at": ub.awarded_at,
            })
    else:
        result = await session.execute(
            select(ClubBadges, Clubs)
            .join(Clubs, ClubBadges.club_id == Clubs.id)
            .where(ClubBadges.badge_id == badge_id)
            .order_by(ClubBadges.awarded_at.desc())
        )
        for cb, club in result.all():
            recipients.append({
                "id": cb.id,
                "entity_id": club.id,
                "entity_name": club.name,
                "entity_email": club.contact_email,
                "awarded_at": cb.awarded_at,
            })

    return recipients


async def get_badge_stats(session: AsyncSession, badge_id: int) -> dict:
    """Get analytics stats for a specific badge."""
    from datetime import timedelta
    from app.response import CustomHTTPException

    badge = await session.get(BadgeDefinitions, badge_id)
    if not badge:
        raise CustomHTTPException(404, "Badge not found.")

    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    if badge.badge_type == BadgeType.user:
        table = UserBadges
    else:
        table = ClubBadges

    total = await session.scalar(
        select(func.count()).where(table.badge_id == badge_id)
    ) or 0

    last_7 = await session.scalar(
        select(func.count()).where(
            table.badge_id == badge_id,
            table.awarded_at >= seven_days_ago,
        )
    ) or 0

    last_30 = await session.scalar(
        select(func.count()).where(
            table.badge_id == badge_id,
            table.awarded_at >= thirty_days_ago,
        )
    ) or 0

    return {
        "total_awarded": total,
        "awards_last_7_days": last_7,
        "awards_last_30_days": last_30,
    }


async def manual_award_badge(session: AsyncSession, badge_id: int, entity_id: int) -> dict:
    """Manually award a badge to a user or club."""
    from app.response import CustomHTTPException
    from app.api.users.models import Users

    badge = await session.get(BadgeDefinitions, badge_id)
    if not badge:
        raise CustomHTTPException(404, "Badge not found.")

    if badge.badge_type == BadgeType.user:
        # Check user exists
        user = await session.get(Users, entity_id)
        if not user:
            raise CustomHTTPException(404, "User not found.")

        # Check if already awarded
        existing = await session.scalar(
            select(func.count()).where(
                UserBadges.user_id == entity_id,
                UserBadges.badge_id == badge_id,
            )
        )
        if existing > 0:
            raise CustomHTTPException(400, "Badge already awarded to this user.")

        award = UserBadges(user_id=entity_id, badge_id=badge_id)
        session.add(award)
        await session.commit()
        await session.refresh(award)
        return {
            "id": award.id,
            "entity_id": entity_id,
            "entity_name": user.full_name,
            "entity_email": user.email,
            "awarded_at": award.awarded_at,
        }
    else:
        club = await session.get(Clubs, entity_id)
        if not club:
            raise CustomHTTPException(404, "Club not found.")

        existing = await session.scalar(
            select(func.count()).where(
                ClubBadges.club_id == entity_id,
                ClubBadges.badge_id == badge_id,
            )
        )
        if existing > 0:
            raise CustomHTTPException(400, "Badge already awarded to this club.")

        award = ClubBadges(club_id=entity_id, badge_id=badge_id)
        session.add(award)
        await session.commit()
        await session.refresh(award)
        return {
            "id": award.id,
            "entity_id": entity_id,
            "entity_name": club.name,
            "entity_email": club.contact_email,
            "awarded_at": award.awarded_at,
        }


async def revoke_badge(session: AsyncSession, badge_id: int, entity_id: int) -> None:
    """Revoke a badge from a user or club."""
    from app.response import CustomHTTPException

    badge = await session.get(BadgeDefinitions, badge_id)
    if not badge:
        raise CustomHTTPException(404, "Badge not found.")

    if badge.badge_type == BadgeType.user:
        result = await session.execute(
            select(UserBadges).where(
                UserBadges.user_id == entity_id,
                UserBadges.badge_id == badge_id,
            )
        )
        award = result.scalar_one_or_none()
        if not award:
            raise CustomHTTPException(404, "Badge was not awarded to this user.")
        await session.delete(award)
    else:
        result = await session.execute(
            select(ClubBadges).where(
                ClubBadges.club_id == entity_id,
                ClubBadges.badge_id == badge_id,
            )
        )
        award = result.scalar_one_or_none()
        if not award:
            raise CustomHTTPException(404, "Badge was not awarded to this club.")
        await session.delete(award)

    await session.commit()
