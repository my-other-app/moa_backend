"""
Badge awarding and retrieval logic.
"""
import logging
from datetime import datetime, timezone
from sqlalchemy import select, func, exists, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.badges.models import BadgeDefinitions, BadgeType, UserBadges, ClubBadges
from app.response import CustomHTTPException
from app.api.events.models import (
    EventRegistrationsLink,
    EventRatingsLink,
    Events,
)
from app.api.clubs.models import Clubs, ClubUsersLink

logger = logging.getLogger(__name__)


# ─── User badge checks ───────────────────────────────────────────────

async def _get_user_metrics(session: AsyncSession, user_id: int) -> dict:
    """Compute all metrics needed for user badge checks in one pass.

    One statement over the user's live registrations (ix_registrations_user),
    each metric a filtered count, with the ratings count beside it. It was
    seven statements, run after every registration."""
    R = EventRegistrationsLink
    live = R.is_deleted == False  # noqa: E712
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(R.is_attended == True),  # noqa: E712
                func.count().filter(R.position == 1),
                func.count().filter(R.position == 2),
                func.count().filter(R.position == 3),
                func.count().filter(R.is_paid == True),  # noqa: E712
                select(func.count())
                .where(EventRatingsLink.user_id == user_id, EventRatingsLink.is_deleted == False)  # noqa: E712
                .scalar_subquery(),
            ).where(R.user_id == user_id, live)
        )
    ).one()
    registered, attended, won, runner_up, podium, paid, rated = (int(v or 0) for v in row)
    return {
        "events_registered": registered,
        "events_attended": attended,
        "events_won": won,
        "runner_up_count": runner_up,
        "podium_count": podium,
        "events_rated": rated,
        "events_paid": paid,
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

async def _capped_count(session: AsyncSession, stmt, cap: int) -> int:
    """How many rows `stmt` finds, counting no further than `cap`: enough to
    say whether a threshold is met without reading every row there is."""
    return int(await session.scalar(select(func.count()).select_from(stmt.limit(cap).subquery())) or 0)


async def _get_club_metrics(session: AsyncSession, club_id: int, wanted: dict | None = None) -> dict:
    """The metrics the club's badges are earned on.

    `wanted` is {metric: the highest threshold still to earn}: only those
    are read, and a count stops at its threshold. A club's followers come
    from clubs.follower_count, which a trigger keeps; attended registrations
    of its events are counted up to the threshold and no further. This runs after
    every check-in, so it used to count the club's followers one row at a
    time, and every attended registration of every event the club ever
    held. Without `wanted`, everything is read in full."""
    club = await session.scalar(select(Clubs).where(Clubs.id == club_id))
    need = wanted if wanted is not None else {k: None for k in CLUB_METRICS}
    out: dict = {}
    if "events_hosted" in need:
        hosted = select(Events.id).where(Events.club_id == club_id, Events.is_deleted == False)  # noqa: E712
        cap = need["events_hosted"]
        out["events_hosted"] = await _capped_count(session, hosted, cap) if cap else int(await session.scalar(select(func.count()).select_from(hosted.subquery())) or 0)
    if "follower_count" in need:
        out["follower_count"] = int(club.follower_count or 0) if club else 0
    if "avg_rating" in need:
        out["avg_rating"] = float(club.rating) if club and club.rating is not None else 0.0
    if "total_attendees" in need:
        R = EventRegistrationsLink
        attended = (
            select(R.id)
            .join(Events, Events.id == R.event_id)
            .where(Events.club_id == club_id, Events.is_deleted == False, R.is_attended == True, R.is_deleted == False)  # noqa: E712
        )
        cap = need["total_attendees"]
        out["total_attendees"] = await _capped_count(session, attended, cap) if cap else int(await session.scalar(select(func.count()).select_from(attended.subquery())) or 0)
    if "is_verified" in need:
        out["is_verified"] = 1 if (club and club.is_verified) else 0
    return out


CLUB_METRICS = ("events_hosted", "follower_count", "avg_rating", "total_attendees", "is_verified")


async def check_and_award_club_badges(session: AsyncSession, club_id: int) -> list[str]:
    """
    Check all club-type badge definitions and award any newly earned badges.
    Returns list of newly awarded badge slugs.

    Only the badges the club has yet to earn are looked at, and only their
    metrics read, each no further than the highest threshold left.
    """
    result = await session.execute(
        select(BadgeDefinitions).where(BadgeDefinitions.badge_type == BadgeType.club)
    )
    definitions = result.scalars().all()

    result = await session.execute(
        select(ClubBadges.badge_id).where(ClubBadges.club_id == club_id)
    )
    awarded_ids = set(result.scalars().all())
    pending = [d for d in definitions if d.id not in awarded_ids]
    if not pending:
        return []

    wanted: dict = {}
    for d in pending:
        threshold = int(-(-float(d.threshold or 0) // 1))  # a count meets t once it reaches ceil(t)
        wanted[d.trigger_metric] = max(wanted.get(d.trigger_metric) or 0, threshold, 1)
    metrics = await _get_club_metrics(session, club_id, wanted)

    newly_awarded = []

    for badge_def in pending:
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

async def get_badge_definition(session: AsyncSession, badge_id: int) -> BadgeDefinitions:
    """
    Load a badge definition or 404.

    The recipients and stats endpoints used to `session.get(...)` and feed the
    result straight into `BadgeDefinitionResponse.model_validate`, which turns
    a missing badge into a ValidationError and a 500 instead of a 404.
    """
    badge = await session.get(BadgeDefinitions, badge_id)
    if not badge:
        raise CustomHTTPException(404, message="Badge not found")
    return badge


async def delete_badge_definition(session: AsyncSession, badge_id: int) -> dict:
    """
    Delete a badge definition and every award of it.

    BadgeDefinitions has no soft-delete mixin, so this is a real delete, and
    the award rows reference it by foreign key -- they have to go first or the
    delete violates the constraint. Both award tables are cleared, because a
    badge may be a user badge or a club badge and the counts are reported
    together.
    """
    badge = await get_badge_definition(session, badge_id)

    user_awards = await session.scalar(
        select(func.count(UserBadges.id)).where(UserBadges.badge_id == badge_id)
    )
    club_awards = await session.scalar(
        select(func.count(ClubBadges.id)).where(ClubBadges.badge_id == badge_id)
    )

    await session.execute(delete(UserBadges).where(UserBadges.badge_id == badge_id))
    await session.execute(delete(ClubBadges).where(ClubBadges.badge_id == badge_id))
    await session.delete(badge)
    await session.commit()

    return {
        "message": "Badge deleted successfully",
        "revoked_from": (user_awards or 0) + (club_awards or 0),
    }


async def get_badge_recipients(session: AsyncSession, badge_id: int) -> list[dict]:
    """Get all users/clubs who have earned a specific badge."""
    from app.response import CustomHTTPException
    from app.api.reports.service import avatar, picture_options
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
            # Each recipient's picture, loaded for the whole list at once.
            .options(*picture_options())
            .order_by(UserBadges.awarded_at.desc())
        )
        for ub, user in result.all():
            recipients.append({
                "id": ub.id,
                "entity_id": user.id,
                "entity_name": user.full_name,
                "entity_email": user.email,
                "awarded_at": ub.awarded_at,
                "avatar": avatar(user),
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
                "avatar": club.logo,
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
