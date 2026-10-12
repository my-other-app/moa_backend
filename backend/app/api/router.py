from uuid import UUID
from fastapi import APIRouter, Query
from app.api.auth.router import router as auth_router
from app.api.orgs.router import router as org_router
from app.api.users.router import router as user_router
from app.api.clubs.router import router as club_router
from app.api.events.router import router as events_router
from app.api.event_designs.router import router as event_designs_router
from app.api.deliveries.router import router as deliveries_router
from app.api.certificates.router import public_router as verify_router, router as certificates_router
from app.api.checkins.router import router as checkins_router
from app.api.idcards.router import router as idcards_router
from app.api.kiosk.router import admin_router as kiosks_admin_router, kiosk_router, self_router
from app.api.agenda.router import router as agenda_router
from app.api.merch.router import router as merch_router, gate_router as merch_gate_router
from app.api.closeout.router import router as closeout_router
from app.api.hooks.router import router as hooks_router
from app.api.contacts.router import router as contacts_router
from app.api.events.duplicate_router import router as duplicate_router
from app.api.admission.router import router as admission_router
from app.api.analytics.router import router as analytics_router
from app.api.club_designs.router import router as club_designs_router
from app.api.finance.router import router as finance_router
from app.api.security.router import router as security_router
from app.api.threads.router import public_router as threads_public_router, router as threads_router
from app.api.store.router import public_router as store_public_router, router as store_router
from app.api.links.router import router as links_router
from app.api.emails.router import (
    library_router as email_library_router,
    messages_router,
    public_router as files_router,
    router as emails_router,
)
from app.api.interests.router import router as interests_router
from app.api.home.router import router as home_router
from app.api.notifications.router import router as notifications_router
from app.api.payments.router import router as payments_router
from app.api.ticket_templates.router import router as ticket_templates_router
from app.api.badges.router import router as badges_router
from app.api.audit.router import router as audit_router
from app.api.backups.router import router as backups_router
from app.api.subscriptions.router import (
    admin_router as subscriptions_admin_router,
    club_router as subscriptions_club_router,
)
from app.api import admin_overview
from app.api.certificate_templates.router import router as certificate_templates_router
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
api_router.include_router(router=analytics_router, tags=["analytics"])
api_router.include_router(router=club_designs_router, tags=["club-designs"])
api_router.include_router(router=links_router, tags=["tracking-links"])
api_router.include_router(router=finance_router, tags=["finance"])
api_router.include_router(router=threads_router, tags=["threads"])
api_router.include_router(router=security_router, tags=["security"])
api_router.include_router(router=threads_public_router, tags=["threads"])
api_router.include_router(router=store_router, tags=["store"])
api_router.include_router(router=store_public_router, tags=["store"])
api_router.include_router(router=club_router, tags=["clubs"])
# Before the events router: these sit under /events/{event_id} and the
# events router owns the rest of that space.
api_router.include_router(router=event_designs_router, tags=["event-designs"])
api_router.include_router(router=deliveries_router, tags=["deliveries"])
api_router.include_router(router=certificates_router, tags=["certificates"])
api_router.include_router(router=verify_router, tags=["certificates"])
api_router.include_router(router=emails_router, tags=["emails"])
api_router.include_router(router=checkins_router, tags=["check-ins"])
api_router.include_router(router=idcards_router, tags=["id-cards"])
api_router.include_router(router=kiosks_admin_router, tags=["kiosks"])
api_router.include_router(router=kiosk_router, tags=["kiosks"])
api_router.include_router(router=self_router, tags=["kiosks"])
api_router.include_router(router=agenda_router, tags=["agenda"])
api_router.include_router(router=merch_router, tags=["merch"])
api_router.include_router(router=merch_gate_router, tags=["merch"])
api_router.include_router(router=closeout_router, tags=["closeout"])
api_router.include_router(router=hooks_router, tags=["hooks"])
api_router.include_router(router=contacts_router, tags=["contacts"])
api_router.include_router(router=duplicate_router, tags=["events"])
api_router.include_router(router=admission_router, tags=["admission"])
api_router.include_router(router=messages_router, tags=["emails"])
api_router.include_router(router=files_router, tags=["emails"])
api_router.include_router(router=events_router, tags=["events"])
api_router.include_router(router=interests_router, tags=["interests"])
api_router.include_router(router=home_router, tags=["home"])
api_router.include_router(router=notifications_router, tags=["notifications"])
api_router.include_router(router=payments_router, tags=["payments"])
api_router.include_router(router=ticket_templates_router, tags=["ticket-templates"])
api_router.include_router(router=certificate_templates_router, tags=["certificate-templates"])
api_router.include_router(router=badges_router, tags=["badges"])
api_router.include_router(router=audit_router)
api_router.include_router(router=backups_router)
# Before the clubs router: these are literal paths under /clubs, and the
# clubs router owns the parameterised ones.
api_router.include_router(router=subscriptions_club_router)
api_router.include_router(router=email_library_router, tags=["emails"])
api_router.include_router(router=subscriptions_admin_router)


@api_router.get("/background-task-log/{task_id}", tags=["root"])
async def get_background_task_log(
    task_id: UUID, session: SessionDep, user: AdminAuth
) -> BackgroundTaskLogResponseSchema:
    """
    Retrieve a background task log by ID
    """
    return await service.get_background_task_log(session, task_id)


@api_router.get("/admin/overview", tags=["admin"])
async def get_admin_overview(
    session: SessionDep,
    user: AdminAuth,
    days: int = Query(30, ge=1, le=365, description="Comparison window"),
    top_by: str = Query(
        "followers", description="followers, registrations or revenue"
    ),
) -> dict:
    """
    Everything the admin Overview screen shows, in one response.

    The screen needs roughly fifteen aggregates. As separate endpoints that is
    fifteen round trips for one page load against a single backend instance;
    here it is one, with every figure computed in the database.
    """
    return await admin_overview.get_overview(session, days=days, top_by=top_by)


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
    from app.api.payments.models import PaymentLogs, PaymentStatus
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)
    sixty_days_ago = now - timedelta(days=60)

    # 1. Total Users Base Growth
    total_users_result = await session.execute(select(func.count(Users.id)))
    total_users = total_users_result.scalar() or 0

    users_last_30_result = await session.execute(select(func.count(Users.id)).where(Users.created_at >= thirty_days_ago))
    users_last_30 = users_last_30_result.scalar() or 0

    users_30_days_ago = total_users - users_last_30

    if users_30_days_ago > 0:
        # e.g., if we had 100 users 30 days ago, and 12 joined in the last 30 days, we grew by 12%
        base_growth_pct = int((users_last_30 / users_30_days_ago) * 100)
    else:
        # If we had 0 users before, we grew by 100% implicitly if ANY users joined
        base_growth_pct = 100 if users_last_30 > 0 else 0
    
    total_users_delta = f"+{base_growth_pct}% from last month" if base_growth_pct >= 0 else f"{base_growth_pct}% from last month"

    # 2. Clubs & Delta
    # `is_deleted` matters here: every list endpoint filters it, so counting
    # deleted rows made this card disagree with the table it links to.
    verified_clubs_result = await session.execute(
        select(func.count(Clubs.id)).where(
            Clubs.is_verified == True, Clubs.is_deleted == False
        )
    )
    verified_clubs = verified_clubs_result.scalar() or 0

    new_clubs_7_result = await session.execute(
        select(func.count(Clubs.id)).where(
            and_(
                Clubs.is_verified == True,
                Clubs.is_deleted == False,
                Clubs.created_at >= seven_days_ago,
            )
        )
    )
    new_clubs_7 = new_clubs_7_result.scalar() or 0
    verified_clubs_delta = f"+{new_clubs_7} new this week"

    # 3. Events & Delta
    events_hosted_result = await session.execute(
        select(func.count(Events.id)).where(Events.is_deleted == False)
    )
    events_hosted = events_hosted_result.scalar() or 0

    new_events_30_result = await session.execute(
        select(func.count(Events.id)).where(
            Events.is_deleted == False, Events.created_at >= thirty_days_ago
        )
    )
    new_events_30 = new_events_30_result.scalar() or 0
    events_hosted_delta = f"+{new_events_30} this month"

    # 4. Revenue
    #
    # This reported a literal 0 with a "+0% from last month" caption, so the
    # dashboard has always shown no revenue regardless of takings. Read from
    # the payment log and net of refunds, so it agrees with the payments page
    # rather than being a second, larger number for the same thing.
    async def _captured_between(start=None, end=None) -> float:
        filters = [
            PaymentLogs.status == PaymentStatus.captured,
            PaymentLogs.is_deleted == False,
        ]
        if start is not None:
            filters.append(PaymentLogs.created_at >= start)
        if end is not None:
            filters.append(PaymentLogs.created_at < end)
        total = await session.scalar(
            select(func.coalesce(func.sum(PaymentLogs.amount), 0)).where(*filters)
        )
        return float(total or 0)

    refunded_total = await session.scalar(
        select(func.coalesce(func.sum(PaymentLogs.amount), 0)).where(
            PaymentLogs.status == PaymentStatus.refunded,
            PaymentLogs.is_deleted == False,
        )
    )
    platform_revenue = await _captured_between() - float(refunded_total or 0)

    revenue_30 = await _captured_between(start=thirty_days_ago)
    revenue_previous_30 = await _captured_between(
        start=sixty_days_ago, end=thirty_days_ago
    )
    if revenue_previous_30 > 0:
        revenue_change = int(
            ((revenue_30 - revenue_previous_30) / revenue_previous_30) * 100
        )
        platform_revenue_delta = (
            f"{'+' if revenue_change >= 0 else ''}{revenue_change}% from last month"
        )
    elif revenue_30 > 0:
        # No baseline to divide by; "+100%" would be an invention.
        platform_revenue_delta = "first revenue this month"
    else:
        platform_revenue_delta = "no revenue yet"

    # 5. Usage Growth Chart Data (Last 7 Days)
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
        "platform_revenue": platform_revenue,
        "platform_revenue_delta": platform_revenue_delta,
        "user_growth_data": user_growth_data,
    }


@api_router.get("/admin/analytics/trends", tags=["admin"])
async def get_admin_trends(
    session: SessionDep,
    user: AdminAuth,
    days: int = Query(30, ge=1, le=365, description="Window length in days"),
) -> dict:
    """
    Daily users, events, registrations and revenue, with the preceding window
    for comparison. The dashboard's other figures are counters; these are the
    ones that say whether anything is moving.
    """
    from app.api import admin_insights

    return await admin_insights.get_trends(session, days=days)


@api_router.get("/admin/moderation-queue", tags=["admin"])
async def get_admin_moderation_queue(
    session: SessionDep,
    user: AdminAuth,
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Everything waiting on an admin decision, in one response."""
    from app.api import admin_insights

    return await admin_insights.get_moderation_queue(session, limit=limit)
