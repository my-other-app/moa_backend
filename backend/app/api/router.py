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
    from sqlalchemy import select, func, and_, cast, Date
    from app.api.users.models import Users
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)
    sixty_days_ago = now - timedelta(days=60)

    # 1. Users & Delta
    total_users_result = await session.execute(select(func.count(Users.id)))
    total_users = total_users_result.scalar() or 0

    users_last_30_result = await session.execute(select(func.count(Users.id)).where(Users.created_at >= thirty_days_ago))
    users_last_30 = users_last_30_result.scalar() or 0

    users_prev_30_result = await session.execute(select(func.count(Users.id)).where(and_(Users.created_at >= sixty_days_ago, Users.created_at < thirty_days_ago)))
    users_prev_30 = users_prev_30_result.scalar() or 0

    if users_prev_30 > 0:
        user_growth_pct = int(((users_last_30 - users_prev_30) / users_prev_30) * 100)
    else:
        user_growth_pct = 100 if users_last_30 > 0 else 0
    
    total_users_delta = f"+{user_growth_pct}% from last month" if user_growth_pct >= 0 else f"{user_growth_pct}% from last month"

    # 2. Clubs & Delta
    verified_clubs_result = await session.execute(select(func.count(Clubs.id)).where(Clubs.is_verified == True))
    verified_clubs = verified_clubs_result.scalar() or 0

    new_clubs_7_result = await session.execute(select(func.count(Clubs.id)).where(and_(Clubs.is_verified == True, Clubs.created_at >= seven_days_ago)))
    new_clubs_7 = new_clubs_7_result.scalar() or 0
    verified_clubs_delta = f"+{new_clubs_7} new this week"

    # 3. Events & Delta
    events_hosted_result = await session.execute(select(func.count(Events.id)))
    events_hosted = events_hosted_result.scalar() or 0

    new_events_30_result = await session.execute(select(func.count(Events.id)).where(Events.created_at >= thirty_days_ago))
    new_events_30 = new_events_30_result.scalar() or 0
    events_hosted_delta = f"+{new_events_30} this month"

    # 4. Usage Growth Chart Data (Last 7 Days)
    growth_query = select(
        cast(Users.created_at, Date).label("creation_date"),
        func.count(Users.id)
    ).where(
        Users.created_at >= seven_days_ago
    ).group_by(
        cast(Users.created_at, Date)
    ).order_by(
        cast(Users.created_at, Date)
    )
    growth_results = await session.execute(growth_query)
    growth_rows = growth_results.all()

    # Pre-fill empty days for the chart
    user_growth_data = []
    for i in range(6, -1, -1):
        d = now - timedelta(days=i)
        user_growth_data.append({
            "name": d.strftime("%a"), 
            "date": d.date(),
            "users": 0
        })
    
    # Hydrate real counts
    for row in growth_rows:
        row_date = row[0]
        row_count = row[1]
        for gd in user_growth_data:
            if gd["date"] == row_date:
                gd["users"] = row_count
                break

    # Clean off exact datetime keys before sending to React Recharts
    for gd in user_growth_data:
        del gd["date"]

    return {
        "total_users": total_users,
        "total_users_delta": total_users_delta,
        "verified_clubs": verified_clubs,
        "verified_clubs_delta": verified_clubs_delta,
        "events_hosted": events_hosted,
        "events_hosted_delta": events_hosted_delta,
        "platform_revenue": 0, 
        "platform_revenue_delta": "+0% from last month",
        "user_growth_data": user_growth_data,
    }
