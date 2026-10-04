"""
Badge API endpoints.
"""
import logging
from fastapi import APIRouter, Request
from app.db.core import SessionDep
from app.api.auth.router import DependsAuth
from app.api.badges import service
from app.api.audit import service as audit
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
from app.api.badges.schemas import (
    BadgeAdminResponse,
    BadgeDefinitionCreate,
    BadgeDefinitionUpdate,
    BadgeRecipientResponse,
    BadgeRecipientsListResponse,
    BadgeStatsResponse,
    ManualAwardRequest,
)

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


@router.delete("/admin/{badge_id}", summary="Delete badge (Admin)")
async def admin_delete_badge(
    badge_id: int,
    session: SessionDep,
    admin: AdminAuth,
    request: Request = None,
):
    """
    Delete a badge definition and every award of it.

    There was no way to remove a badge once created -- only to edit it. The
    definition has no soft-delete, so this removes the award rows first; the
    response says how many people lost the badge.
    """
    result = await service.delete_badge_definition(session, badge_id)
    await audit.record(
        session, actor=admin, action="badge.delete", entity_type="badge",
        entity_id=badge_id,
        summary=f"Deleted badge, revoked from {result['revoked_from']}",
        details=result, request=request,
    )
    await session.commit()
    return result


@router.get("/admin/{badge_id}/recipients", summary="Get badge recipients (Admin)")
async def admin_badge_recipients(
    badge_id: int,
    session: SessionDep,
    admin: AdminAuth,
) -> BadgeRecipientsListResponse:
    """Get all users or clubs who have earned a specific badge."""
    badge = await service.get_badge_definition(session, badge_id)
    recipients = await service.get_badge_recipients(session, badge_id)
    return BadgeRecipientsListResponse(
        badge=BadgeDefinitionResponse.model_validate(badge),
        recipients=[BadgeRecipientResponse(**r) for r in recipients],
        total=len(recipients),
    )


@router.get("/admin/{badge_id}/stats", summary="Get badge stats (Admin)")
async def admin_badge_stats(
    badge_id: int,
    session: SessionDep,
    admin: AdminAuth,
) -> BadgeStatsResponse:
    """Get analytics stats for a specific badge."""
    badge = await service.get_badge_definition(session, badge_id)
    stats = await service.get_badge_stats(session, badge_id)
    recent = await service.get_badge_recipients(session, badge_id)
    return BadgeStatsResponse(
        badge=BadgeDefinitionResponse.model_validate(badge),
        total_awarded=stats["total_awarded"],
        recent_awards=[BadgeRecipientResponse(**r) for r in recent[:10]],
        awards_last_30_days=stats["awards_last_30_days"],
        awards_last_7_days=stats["awards_last_7_days"],
    )


@router.post("/admin/{badge_id}/award", summary="Manually award badge (Admin)")
async def admin_award_badge(
    badge_id: int,
    body: ManualAwardRequest,
    session: SessionDep,
    admin: AdminAuth,
    request: Request = None,
) -> BadgeRecipientResponse:
    """Manually award a badge to a specific user or club."""
    result = await service.manual_award_badge(session, badge_id, body.entity_id)
    await audit.record(
        session, actor=admin, action="badge.award", entity_type="badge",
        entity_id=badge_id,
        summary=f"Awarded badge to {result.get('entity_name') or body.entity_id}",
        details={"entity_id": body.entity_id}, request=request,
    )
    await session.commit()
    return BadgeRecipientResponse(**result)


@router.delete("/admin/{badge_id}/revoke/{entity_id}", summary="Revoke badge (Admin)")
async def admin_revoke_badge(
    badge_id: int,
    entity_id: int,
    session: SessionDep,
    admin: AdminAuth,
    request: Request = None,
):
    """Revoke a badge from a user or club."""
    await service.revoke_badge(session, badge_id, entity_id)
    await audit.record(
        session, actor=admin, action="badge.revoke", entity_type="badge",
        entity_id=badge_id, summary=f"Revoked badge from #{entity_id}",
        details={"entity_id": entity_id}, request=request,
    )
    await session.commit()
    return {"message": "Badge revoked successfully"}
