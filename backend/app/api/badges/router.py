"""
Badge API endpoints.
"""
import logging
from fastapi import APIRouter
from app.db.core import SessionDep
from app.api.auth.router import DependsAuth
from app.api.badges import service
from app.api.badges.schemas import (
    BadgeDefinitionResponse,
    UserBadgesResponse,
    ClubBadgesResponse,
    AwardedBadgeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/badges", tags=["Badges"])


@router.get("/definitions", summary="List all badge definitions")
async def list_badge_definitions(
    session: SessionDep = SessionDep,
) -> list[BadgeDefinitionResponse]:
    """Get all available badge definitions (both user and club)."""
    definitions = await service.get_all_badge_definitions(session)
    return [
        BadgeDefinitionResponse(
            id=d.id,
            slug=d.slug,
            name=d.name,
            description=d.description,
            emoji=d.emoji,
            badge_type=d.badge_type.value,
            trigger_metric=d.trigger_metric,
            threshold=d.threshold,
        )
        for d in definitions
    ]


@router.get("/me", summary="Get my badges")
async def get_my_badges(
    session: SessionDep = SessionDep,
    user: DependsAuth = None,
) -> UserBadgesResponse:
    """Get the current user's awarded badges. Also triggers a badge check."""
    # Check for new badges first
    await service.check_and_award_user_badges(session, user.id)

    badges = await service.get_user_badges(session, user.id)
    return UserBadgesResponse(
        user_id=user.id,
        badges=[
            AwardedBadgeResponse(
                id=b.id,
                badge=BadgeDefinitionResponse(
                    id=b.badge.id,
                    slug=b.badge.slug,
                    name=b.badge.name,
                    description=b.badge.description,
                    emoji=b.badge.emoji,
                    badge_type=b.badge.badge_type.value,
                    trigger_metric=b.badge.trigger_metric,
                    threshold=b.badge.threshold,
                ),
                awarded_at=b.awarded_at,
            )
            for b in badges
        ],
        total=len(badges),
    )


@router.get("/user/{user_id}", summary="Get user's badges")
async def get_user_badges(
    user_id: int,
    session: SessionDep = SessionDep,
    user: DependsAuth = None,
) -> UserBadgesResponse:
    """Get a specific user's awarded badges."""
    badges = await service.get_user_badges(session, user_id)
    return UserBadgesResponse(
        user_id=user_id,
        badges=[
            AwardedBadgeResponse(
                id=b.id,
                badge=BadgeDefinitionResponse(
                    id=b.badge.id,
                    slug=b.badge.slug,
                    name=b.badge.name,
                    description=b.badge.description,
                    emoji=b.badge.emoji,
                    badge_type=b.badge.badge_type.value,
                    trigger_metric=b.badge.trigger_metric,
                    threshold=b.badge.threshold,
                ),
                awarded_at=b.awarded_at,
            )
            for b in badges
        ],
        total=len(badges),
    )


@router.get("/club/{club_id}", summary="Get club's badges")
async def get_club_badges(
    club_id: int,
    session: SessionDep = SessionDep,
) -> ClubBadgesResponse:
    """Get a club's awarded badges."""
    badges = await service.get_club_badges(session, club_id)
    return ClubBadgesResponse(
        club_id=club_id,
        badges=[
            AwardedBadgeResponse(
                id=b.id,
                badge=BadgeDefinitionResponse(
                    id=b.badge.id,
                    slug=b.badge.slug,
                    name=b.badge.name,
                    description=b.badge.description,
                    emoji=b.badge.emoji,
                    badge_type=b.badge.badge_type.value,
                    trigger_metric=b.badge.trigger_metric,
                    threshold=b.badge.threshold,
                ),
                awarded_at=b.awarded_at,
            )
            for b in badges
        ],
        total=len(badges),
    )


# ─── Admin Management ─────────────────────────────────────────────────
from app.api.auth.router import AdminAuth
from app.api.badges.schemas import BadgeAdminResponse, BadgeDefinitionCreate, BadgeDefinitionUpdate

@router.get("/admin", summary="List all badges (Admin)")
async def admin_list_badges(
    session: SessionDep,
    admin: AdminAuth,
) -> list[BadgeAdminResponse]:
    """Admin endpoint to get all badges with claimed counts."""
    admin_badges = await service.get_admin_badges(session)
    return [
        BadgeAdminResponse(
            id=b["id"],
            slug=b["slug"],
            name=b["name"],
            description=b["description"],
            emoji=b["emoji"],
            badge_type=b["badge_type"].value,
            trigger_metric=b["trigger_metric"],
            threshold=b["threshold"],
            claimed_count=b["claimed_count"],
        )
        for b in admin_badges
    ]


@router.post("/admin", summary="Create badge (Admin)")
async def admin_create_badge(
    badge_data: BadgeDefinitionCreate,
    session: SessionDep,
    admin: AdminAuth,
) -> BadgeDefinitionResponse:
    """Admin endpoint to create a new badge definition."""
    badge = await service.create_badge_definition(session, badge_data.model_dump())
    return BadgeDefinitionResponse.model_validate(badge)


@router.put("/admin/{badge_id}", summary="Update badge (Admin)")
async def admin_update_badge(
    badge_id: int,
    badge_data: BadgeDefinitionUpdate,
    session: SessionDep,
    admin: AdminAuth,
) -> BadgeDefinitionResponse:
    """Admin endpoint to update an existing badge definition."""
    update_data = badge_data.model_dump(exclude_unset=True)
    badge = await service.update_badge_definition(session, badge_id, update_data)
    return BadgeDefinitionResponse.model_validate(badge)
