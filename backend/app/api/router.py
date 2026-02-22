from uuid import UUID
from fastapi import APIRouter
from app.api.auth.router import router as auth_router
from app.api.orgs.router import router as org_router
from app.api.users.router import router as user_router
from app.api.clubs.router import router as club_router
from app.api.events.router import router as events_router
from app.api.interests.router import router as interests_router
from app.api.home.router import router as home_router
from app.api.notifications.router import router as notifications_router
from app.api.payments.router import router as payments_router
from app.api import service
from app.api.schemas import BackgroundTaskLogResponseSchema
from app.db.core import SessionDep
from app.core.auth.dependencies import AdminAuth

api_router = APIRouter(
    prefix="/api/v1",
    responses={404: {"description": "Not found"}},
)

api_router.include_router(router=auth_router, tags=["auth"])
api_router.include_router(router=org_router, tags=["organization"])
api_router.include_router(router=user_router, tags=["user"])
api_router.include_router(router=club_router, tags=["clubs"])
api_router.include_router(router=events_router, tags=["events"])
api_router.include_router(router=interests_router, tags=["interests"])
api_router.include_router(router=home_router, tags=["home"])
api_router.include_router(router=notifications_router, tags=["notifications"])
api_router.include_router(router=payments_router, tags=["payments"])


@api_router.get("/background-task-log/{task_id}", tags=["root"])
async def get_background_task_log(
    task_id: UUID, session: SessionDep, user: AdminAuth
) -> BackgroundTaskLogResponseSchema:
    """
    Retrieve a background task log by ID
    """
    return await service.get_background_task_log(session, task_id)


@api_router.get("/admin/analytics", tags=["admin"])
async def get_admin_dashboard_analytics(
    session: SessionDep, user: AdminAuth
) -> dict:
    """
    Retrieve system-wide analytics for the admin dashboard.
    """
    from sqlalchemy import select, func
    from app.api.users.models import Users
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events

    # Run efficient count queries
    total_users = await session.execute(select(func.count(Users.id)))
    verified_clubs = await session.execute(
        select(func.count(Clubs.id)).where(Clubs.is_verified == True)
    )
    events_hosted = await session.execute(select(func.count(Events.id)))

    # For now, platform revenue returns a static or partially static payload
    # until the Razorpay orders API is fully integrated
    return {
        "total_users": total_users.scalar() or 0,
        "verified_clubs": verified_clubs.scalar() or 0,
        "events_hosted": events_hosted.scalar() or 0,
        "platform_revenue": 0, 
    }
