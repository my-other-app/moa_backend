"""Plan catalogue and subscription endpoints.

Two routers: one for the admin, behind `manage_money` for anything that
changes what a club is charged, and one for a club reading its own plan.

Nothing here charges a card except `/clubs/subscription/start`, which creates
an authorisation the club completes itself, and `/retry`, which asks Razorpay
to re-attempt a charge that already failed.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.admin_alerts import notify
from app.api.audit import service as audit
from app.api.clubs.models import Clubs
from app.api.users.models import Users
from app.api.subscriptions import billing, gates, ledger, notices, service, sweep
from app.api.subscriptions import revenue as revenue_service
from app.api.subscriptions.models import (
    BillingCycle,
    ClubSubscriptions,
    PlanFeatures,
    PlanLimits,
    PlanVisibility,
    Plans,
    SubscriptionStatus,
)
from app.api.subscriptions.schemas import (
    AdminCancelIn,
    AdminSubscriptionOut,
    AssignPlanIn,
    BackfillOut,
    ChargesPageOut,
    ClubPlanOut,
    EntitlementsOut,
    ExtendGraceIn,
    PlanIn,
    PlanOut,
    PublicPlansOut,
    RevenueOut,
    StartSubscriptionIn,
    StartSubscriptionOut,
    SubscriptionOut,
    SubscriptionTotals,
    plan_to_dict,
    public_plan_dict,
    subscription_dict,
    yearly_months_free,
)
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.auth.permissions import MoneyAuth
from app.core.net import client_address
from app.core.rate_limit import limiter
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.db.core import SessionDep
from app.response import CustomHTTPException

admin_router = APIRouter(prefix="/admin", tags=["subscriptions"])
club_router = APIRouter(prefix="/clubs", tags=["subscriptions"])
public_router = APIRouter(prefix="/plans", tags=["subscriptions"])

#: The same budget as the discover routes behind the public site.
PUBLIC_LIMIT = "120/minute"


def _visitor(request: Request) -> str:
    return client_address(request) or "unknown"


# ---------------------------------------------------------------------------
# The pricing page
# ---------------------------------------------------------------------------


@public_router.get("", summary="The plans on the pricing page (public)")
@limiter.limit(PUBLIC_LIMIT, key_func=_visitor)
async def pricing(request: Request, response: Response, session: SessionDep) -> PublicPlansOut:
    """
    Public plans in display order, each with every limit (null = unlimited)
    and the whole public feature catalog marked included or not.

    Cacheable for five minutes: a price an admin changes reaches the page
    within that, and the page does not ask the database on every visit.
    """
    plans = await service.public_plans(session)
    response.headers["Cache-Control"] = "public, max-age=300"
    return {
        "currency": "INR",
        "yearly_months_free": yearly_months_free(plans),
        "plans": [public_plan_dict(plan) for plan in plans],
    }


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@admin_router.get("/plans", summary="List plans with subscriber counts (admin)")
async def list_plans(
    session: SessionDep,
    user: AdminAuth,
    include_hidden: bool = Query(True, description="Include hidden and legacy plans"),
) -> list[PlanOut]:
    rows = await service.list_plans(session, include_hidden=include_hidden)
    return [plan_to_dict(plan, count) for plan, count in rows]


@admin_router.post("/plans", summary="Create a plan (admin)")
async def create_plan(
    payload: PlanIn,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> PlanOut:
    clash = await session.scalar(
        select(func.count(Plans.id)).where(
            Plans.key == payload.key, Plans.is_deleted == False
        )
    )
    if clash:
        raise CustomHTTPException(
            status_code=409, message=f"A plan with the key “{payload.key}” exists."
        )

    service.validate_prices(payload.price_monthly_paise, payload.price_yearly_paise)

    plan = Plans(
        key=payload.key,
        name=payload.name,
        tagline=payload.tagline,
        price_monthly_paise=payload.price_monthly_paise,
        price_yearly_paise=payload.price_yearly_paise,
        trial_days=payload.trial_days,
        visibility=payload.visibility,
        sort_order=payload.sort_order,
        featured=bool(payload.featured),
        contact_sales=bool(payload.contact_sales),
    )
    session.add(plan)
    await session.flush()

    session.add(PlanLimits(plan_id=plan.id, **payload.limits.model_dump()))
    session.add(PlanFeatures(plan_id=plan.id, **payload.features.model_dump()))
    await session.flush()

    await audit.record(
        session,
        actor=user,
        action="plan.create",
        entity_type="plan",
        entity_id=plan.id,
        summary=f"Created plan “{plan.name}”",
        request=request,
    )
    await session.commit()
    return plan_to_dict(await service.get_plan(session, plan.id))


@admin_router.get("/plans/{plan_id}", summary="One plan in full (admin)")
async def get_plan(plan_id: int, session: SessionDep, user: AdminAuth) -> PlanOut:
    plan = await service.get_plan(session, plan_id)
    return plan_to_dict(plan, await service.subscriber_count(session, plan.id))


@admin_router.put("/plans/{plan_id}", summary="Update a plan's limits and features (admin)")
async def update_plan(
    plan_id: int,
    payload: PlanIn,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> PlanOut:
    plan = await service.get_plan(session, plan_id)
    service.validate_prices(payload.price_monthly_paise, payload.price_yearly_paise)

    price_changed = (
        plan.price_monthly_paise != payload.price_monthly_paise
        or plan.price_yearly_paise != payload.price_yearly_paise
    )

    plan.name = payload.name
    plan.tagline = payload.tagline
    plan.price_monthly_paise = payload.price_monthly_paise
    plan.price_yearly_paise = payload.price_yearly_paise
    plan.trial_days = payload.trial_days
    plan.visibility = payload.visibility
    plan.sort_order = payload.sort_order
    # Only when sent: see PlanIn. Omitting them keeps what is there.
    if payload.featured is not None:
        plan.featured = payload.featured
    if payload.contact_sales is not None:
        plan.contact_sales = payload.contact_sales

    if price_changed:
        # Razorpay plans are immutable, so the old ids no longer describe this
        # price. Clearing them makes the next subscribe create a fresh Razorpay
        # plan; everyone already subscribed keeps billing against the old one,
        # which is the price they agreed to.
        plan.razorpay_plan_id_monthly = None
        plan.razorpay_plan_id_yearly = None

    if plan.limits is None:
        session.add(PlanLimits(plan_id=plan.id, **payload.limits.model_dump()))
    else:
        for key, value in payload.limits.model_dump().items():
            setattr(plan.limits, key, value)

    if plan.features is None:
        session.add(PlanFeatures(plan_id=plan.id, **payload.features.model_dump()))
    else:
        for key, value in payload.features.model_dump().items():
            setattr(plan.features, key, value)

    await audit.record(
        session,
        actor=user,
        action="plan.update",
        entity_type="plan",
        entity_id=plan.id,
        summary=(
            f"Updated plan “{plan.name}”"
            + (" (price changed)" if price_changed else "")
        ),
        request=request,
    )
    await session.commit()
    return plan_to_dict(await service.get_plan(session, plan_id))


@admin_router.post("/plans/{plan_id}/duplicate", summary="Duplicate a plan (admin)")
async def duplicate_plan(
    plan_id: int,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> PlanOut:
    """
    Copy a plan, hidden and unsubscribed.

    Hidden on purpose: the point of duplicating is to change something before
    anyone is on it, and a copy that went straight onto the pricing page would
    be sold before it was finished.
    """
    source = await service.get_plan(session, plan_id)

    suffix = 2
    while await session.scalar(
        select(func.count(Plans.id)).where(
            Plans.key == f"{source.key}_copy{suffix}", Plans.is_deleted == False
        )
    ):
        suffix += 1

    copy = Plans(
        key=f"{source.key}_copy{suffix}",
        name=f"{source.name} (copy)",
        tagline=source.tagline,
        price_monthly_paise=source.price_monthly_paise,
        price_yearly_paise=source.price_yearly_paise,
        trial_days=source.trial_days,
        visibility=PlanVisibility.hidden,
        sort_order=source.sort_order,
        contact_sales=source.contact_sales,
        # Not featured: the highlight belongs to the plan it was given to,
        # and a copy made public later would make two "Most popular" plans.
        featured=False,
        # Deliberately not copied: a Razorpay plan belongs to the plan that
        # created it, and sharing the id would bill this plan's subscribers
        # against the other one.
    )
    session.add(copy)
    await session.flush()

    from app.api.subscriptions.models import FEATURE_KEYS, LIMIT_KEYS

    session.add(
        PlanLimits(
            plan_id=copy.id,
            **{
                key: (getattr(source.limits, key) if source.limits else None)
                for key in LIMIT_KEYS
            },
        )
    )
    session.add(
        PlanFeatures(
            plan_id=copy.id,
            **{
                key: (bool(getattr(source.features, key)) if source.features else False)
                for key in FEATURE_KEYS
            },
        )
    )

    await audit.record(
        session,
        actor=user,
        action="plan.duplicate",
        entity_type="plan",
        entity_id=copy.id,
        summary=f"Duplicated “{source.name}” as “{copy.name}”",
        request=request,
    )
    await session.commit()
    return plan_to_dict(await service.get_plan(session, copy.id))


@admin_router.post("/plans/{plan_id}/archive", summary="Withdraw a plan from sale (admin)")
async def archive_plan(
    plan_id: int,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> dict:
    plan = await service.get_plan(session, plan_id)
    outcome = await service.archive_or_delete_plan(session, plan)

    await audit.record(
        session,
        actor=user,
        action=f"plan.{outcome}",
        entity_type="plan",
        entity_id=plan.id,
        summary=f"{outcome.capitalize()} plan “{plan.name}”",
        request=request,
    )
    await session.commit()
    return {
        "outcome": outcome,
        "message": (
            f"“{plan.name}” has subscribers, so it moved to legacy. "
            "They keep it; nobody new can choose it."
            if outcome == "archived"
            else f"“{plan.name}” was deleted."
        ),
    }


@admin_router.delete("/plans/{plan_id}", summary="Delete an unsubscribed plan (admin)")
async def delete_plan(
    plan_id: int,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> dict:
    plan = await service.get_plan(session, plan_id)
    count = await service.subscriber_count(session, plan.id)
    if count:
        # Refused rather than silently archived: DELETE promises removal, and
        # quietly doing something else is how an admin ends up believing a
        # plan is gone while clubs are still being billed for it.
        raise CustomHTTPException(
            status_code=409,
            message=(
                f"“{plan.name}” has {count} subscriber{'s' if count != 1 else ''}. "
                "Archive it instead — they keep the plan and nobody new can choose it."
            ),
        )

    plan.soft_delete()
    await audit.record(
        session,
        actor=user,
        action="plan.delete",
        entity_type="plan",
        entity_id=plan.id,
        summary=f"Deleted plan “{plan.name}”",
        request=request,
    )
    await session.commit()
    return {"outcome": "deleted"}


@admin_router.post("/plans/{plan_id}/sync", summary="Create this plan at Razorpay (admin)")
async def sync_plan(
    plan_id: int,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> PlanOut:
    """
    Register the plan with Razorpay so clubs can subscribe to it.

    Separate from create and update because it reaches a third party and can
    fail for reasons that have nothing to do with the plan being valid --
    folding it into the save would make a Razorpay outage look like a
    rejected form.
    """
    plan = await service.get_plan(session, plan_id)
    synced = []
    for cycle in (BillingCycle.monthly, BillingCycle.yearly):
        if service.price_for(plan, cycle) > 0:
            await billing.sync_plan(plan, cycle)
            synced.append(cycle.value)

    await audit.record(
        session,
        actor=user,
        action="plan.sync",
        entity_type="plan",
        entity_id=plan.id,
        summary=f"Synced “{plan.name}” to Razorpay ({', '.join(synced) or 'nothing to sync'})",
        request=request,
    )
    await session.commit()
    return plan_to_dict(await service.get_plan(session, plan_id))


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@admin_router.get("/subscriptions/totals", summary="MRR and subscription counts (admin)")
async def subscription_totals(session: SessionDep, user: AdminAuth) -> SubscriptionTotals:
    return await service.mrr_totals(session)


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------


@admin_router.get("/revenue", summary="Subscriptions and what they earned (admin)")
async def revenue_figures(
    session: SessionDep,
    user: AdminAuth,
    months: int = Query(12, ge=1, le=60, description="Months in the series, this one included"),
) -> RevenueOut:
    """
    Run rate (MRR, ARR, counts, by plan and by cycle, renewals due in 30
    days) from the subscriptions as they stand; money (collected, refunded,
    the monthly series) from the ledger of what Razorpay took. Months are
    calendar months in India. Complimentary plans are counted, never valued;
    ticket money is shown apart and is not platform revenue.
    """
    return await revenue_service.revenue(session, months=months)


@admin_router.get("/revenue/charges", summary="Every subscription payment (admin)")
async def revenue_charges(
    session: SessionDep,
    user: AdminAuth,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    club_id: Optional[int] = Query(None),
    month: Optional[str] = Query(None, description="YYYY-MM, a calendar month in India"),
) -> ChargesPageOut:
    """Newest first. `sum_paise` is the total of every payment matching the
    filters, not only this page (gross; `refunded_paise` beside it)."""
    return await revenue_service.list_charges(session, offset=offset, limit=limit, club_id=club_id, month=month)


@admin_router.post("/revenue/backfill", summary="Fill the ledger from Razorpay (admin)")
async def revenue_backfill(session: SessionDep, user: MoneyAuth, request: Request = None) -> BackfillOut:
    """
    Ask Razorpay (read-only) for the invoices of every subscription we know
    of and add any payment the ledger is missing. Safe to run again: a
    payment already recorded is only brought up to date with its refunds.
    `failed` counts subscriptions Razorpay would not answer for.
    """
    done = await ledger.backfill(session)
    await audit.record(
        session,
        actor=user,
        action="revenue.backfill",
        entity_type="revenue",
        summary=f"Backfilled the revenue ledger: {done['inserted']} payments added from {done['scanned']} subscriptions",
        details=done,
        request=request,
    )
    await session.commit()
    return done


@admin_router.get("/subscriptions", summary="List club subscriptions (admin)")
async def list_subscriptions(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
    status: Optional[SubscriptionStatus] = Query(None),
    plan_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None, description="Club name"),
    complimentary: Optional[bool] = Query(None, description="Only complimentary plans (true) or none of them (false)"),
    club_id: Optional[int] = Query(None, description="One club's row"),
) -> PaginatedResponse[AdminSubscriptionOut]:
    criteria = [ClubSubscriptions.is_deleted == False]
    if club_id is not None:
        criteria.append(ClubSubscriptions.club_id == club_id)
    if status is not None:
        criteria.append(ClubSubscriptions.status == status)
    if plan_id is not None:
        criteria.append(ClubSubscriptions.plan_id == plan_id)
    if complimentary is not None:
        criteria.append(ClubSubscriptions.complimentary == complimentary)
    if search and search.strip():
        criteria.append(
            ClubSubscriptions.club_id.in_(
                select(Clubs.id).where(Clubs.name.ilike(f"%{search.strip()}%"))
            )
        )

    total = (
        await session.scalar(
            select(func.count(ClubSubscriptions.id)).where(*criteria)
        )
    ) or 0

    rows = (
        (
            await session.execute(
                select(ClubSubscriptions)
                .where(*criteria)
                .options(
                    selectinload(ClubSubscriptions.club),
                    selectinload(ClubSubscriptions.granter),
                    selectinload(ClubSubscriptions.plan).selectinload(Plans.limits),
                    selectinload(ClubSubscriptions.plan).selectinload(Plans.features),
                )
                # Highest value first: the subscriptions worth attention are
                # the expensive ones and the broken ones, and `past_due`
                # sorts near the top of a revenue-ordered list anyway.
                .order_by(ClubSubscriptions.mrr_paise.desc(), ClubSubscriptions.id.desc())
                .offset(pagination.offset)
                .limit(pagination.limit)
            )
        )
        .scalars()
        .all()
    )

    items = [subscription_dict(row, admin=True, granter=row.granter) for row in rows]
    return paginated_response(items, request, schema=AdminSubscriptionOut, total=total)


@admin_router.get(
    "/subscriptions/club/{club_id}", summary="One club's plan, subscribed or not (admin)"
)
async def club_plan(club_id: int, session: SessionDep, user: AdminAuth) -> ClubPlanOut:
    """
    What a club is on, for a club's own page in the admin app: its
    subscription row when it has one, and the plan it resolves to either way
    -- every club on Free has no row, which is why such a club never appears
    under Subscriptions. Give it a plan with PUT .../plan.
    """
    club = await session.get(Clubs, club_id)
    if not club or club.is_deleted:
        raise CustomHTTPException(status_code=404, message="Club not found")
    row = await service.get_club_subscription(session, club_id)
    resolved = await service.resolve_entitlements(session, club_id)
    granter = await session.get(Users, row.granted_by) if row is not None and row.granted_by else None
    return {
        "club": {"id": club.id, "name": club.name},
        "plan_key": resolved.plan.key if resolved.plan else None,
        "plan_name": resolved.plan.name if resolved.plan else None,
        "subscription": subscription_dict(row, club, admin=True, granter=granter) if row is not None else None,
    }


@admin_router.put(
    "/subscriptions/club/{club_id}/plan", summary="Give a club a plan, free or by hand (admin)"
)
async def assign_plan(
    club_id: int,
    payload: AssignPlanIn,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> AdminSubscriptionOut:
    """
    Grant a plan directly, without taking payment, to any club -- one with a
    subscription row or one without (every club on Free).

    `complimentary: true` (the default): given for nothing, until `ends_at`
    or with no end; worth 0 and never revenue. A club whose Razorpay
    subscription can still charge it (trial, active, past due or paused) is
    refused with 409 LIVE_SUBSCRIPTION unless `cancel_razorpay: true`, which
    cancels it at Razorpay at once first (502 RAZORPAY_ERROR, and nothing
    granted, when Razorpay will not). An open, unpaid checkout is closed.

    `complimentary: false`: a deal invoiced by hand, at the plan's price in
    MRR. It does not touch Razorpay: a live Razorpay subscription keeps
    billing until someone cancels it.
    """
    plan = await service.get_plan(session, payload.plan_id)
    # CustomBaseModel keeps enum *values*, so these arrive as plain strings.
    cycle = BillingCycle(payload.cycle)
    club = await session.get(Clubs, club_id)
    if not club or club.is_deleted:
        raise CustomHTTPException(status_code=404, message="Club not found")

    if payload.complimentary:
        subscription = await _grant_complimentary(session, user, club, plan, cycle, payload, request)
    else:
        status = SubscriptionStatus(payload.status)
        subscription = await service.assign_plan(
            session, club_id=club_id, plan=plan, cycle=cycle, status=status, granted_by=getattr(user, "id", None)
        )
        await audit.record(
            session,
            actor=user,
            action="subscription.assign",
            entity_type="club",
            entity_id=club_id,
            summary=f"Moved club {club_id} to plan “{plan.name}” ({cycle.value})",
            request=request,
        )
        await notify(
            session, "plan_assigned",
            title=f"{_admin_name(user)} moved {club.name} to {plan.name}",
            body=f"{plan.name}, {cycle.value}, {status.value}. Nothing was charged.",
            club=club, subscription=subscription, dedupe=False,
            data={"plan": plan.key, "plan_name": plan.name, "cycle": cycle.value, "status": status.value,
                  "by": getattr(user, "email", None), "action": "assign", "complimentary": False},
        )
    await session.commit()
    return await _fresh(session, subscription.id)


async def _grant_complimentary(session, user, club, plan, cycle, payload, request) -> ClubSubscriptions:
    """The comp path of PUT .../plan: make sure Razorpay cannot charge the
    club, then grant. The row is locked first, so a webhook about the old
    Razorpay subscription waits for this and then finds it gone."""
    now = datetime.now(timezone.utc)
    row = await service.get_club_subscription(session, club.id, for_update=True)
    service.settle_stale_pending(row, now)
    closed = stopped = stop_outcome = None
    was = None
    if row is not None:
        was = {
            "plan_id": row.plan_id,
            "status": getattr(row.status, "value", row.status),
            "complimentary": bool(row.complimentary),
            "razorpay_subscription_id": row.razorpay_subscription_id,
        }
    if row is not None and row.status == SubscriptionStatus.pending and row.razorpay_subscription_id:
        # An open checkout: closed now, so it can never be paid while the
        # club has a plan for nothing (502 CHECKOUT_NOT_CLOSED if Razorpay
        # will not confirm it).
        closed = row.razorpay_subscription_id
        await billing.close_unpaid_checkout(closed)
        service.revert_checkout(row)
    if service.live_at_razorpay(row):
        if not payload.cancel_razorpay:
            raise CustomHTTPException(
                status_code=409,
                message=(
                    f"{club.name} pays through Razorpay ({row.status.value}). Cancel that subscription "
                    "first, or send cancel_razorpay: true to cancel it now and grant the plan."
                ),
                error_code="LIVE_SUBSCRIPTION",
                errors={"razorpay_subscription_id": row.razorpay_subscription_id, "status": row.status.value},
            )
        stopped = row.razorpay_subscription_id
        stop_outcome = await billing.stop_now(stopped)

    ends_at = payload.ends_at
    subscription = await service.grant_complimentary(
        session, club_id=club.id, plan=plan, cycle=cycle, reason=payload.reason, ends_at=ends_at,
        granted_by=getattr(user, "id", None), now=now,
    )
    until = f"until {notices.day(ends_at)}" if ends_at else "with no end date"
    entry = await audit.record(
        session,
        actor=user,
        action="subscription.comp_grant",
        entity_type="club",
        entity_id=club.id,
        summary=f"Gave {club.name} “{plan.name}” ({cycle.value}) free, {until}",
        details={
            "subscription_id": subscription.id, "plan": plan.key, "cycle": cycle.value,
            "reason": payload.reason, "ends_at": ends_at.isoformat() if ends_at else None,
            "razorpay_cancelled": stopped, "razorpay_outcome": stop_outcome, "checkout_closed": closed,
            "previous": was,
        },
        request=request,
    )
    # In the money ledger at zero, keyed by the audit entry the revenue page counts.
    from app.api.ledger import service as money_ledger

    await money_ledger.comp_granted(
        session, audit_id=entry.id if entry is not None else None, club_id=club.id, subscription_id=subscription.id,
        at=entry.created_at if entry is not None and entry.created_at else now,
    )
    await notify(
        session, "plan_assigned",
        title=f"{_admin_name(user)} gave {club.name} {plan.name} free",
        body=(
            f"Complimentary {plan.name}, {until}. Nothing is charged and it is not counted as revenue."
            + (f" Razorpay subscription {stopped} was cancelled." if stopped else "")
        ),
        club=club, subscription=subscription, dedupe=False,
        data={
            "plan": plan.key, "cycle": cycle.value, "status": "active", "by": getattr(user, "email", None),
            "action": "comp_grant", "complimentary": True, "plan_name": plan.name, "reason": payload.reason,
            "ends_at": ends_at.isoformat() if ends_at else None, "razorpay_cancelled": stopped,
        },
    )
    return subscription


@admin_router.post(
    "/subscriptions/{subscription_id}/retry", summary="Retry a failed charge (admin)"
)
async def retry_charge(
    subscription_id: int,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> dict:
    subscription = await session.get(ClubSubscriptions, subscription_id)
    if not subscription or subscription.is_deleted:
        raise CustomHTTPException(status_code=404, message="Subscription not found")
    if not subscription.razorpay_subscription_id:
        raise CustomHTTPException(
            status_code=422,
            message="This subscription was assigned by an admin and is not billed by Razorpay.",
        )
    if subscription.status != SubscriptionStatus.past_due:
        raise CustomHTTPException(
            status_code=422,
            message=f"Nothing to retry: this subscription is {subscription.status.value}.",
        )

    await billing.retry_charge(subscription.razorpay_subscription_id)

    await audit.record(
        session,
        actor=user,
        action="subscription.retry",
        entity_type="club",
        entity_id=subscription.club_id,
        summary=f"Asked Razorpay to retry the charge for club {subscription.club_id}",
        request=request,
    )
    await session.commit()
    # The outcome arrives as a webhook; saying "retried" would be claiming
    # more than we know.
    return {"message": "Razorpay has been asked to retry. The result arrives shortly."}


def _admin_name(user) -> str:
    return getattr(user, "email", None) or "An admin"


async def _admin_subscription(session, subscription_id: int) -> ClubSubscriptions:
    row = await session.scalar(
        select(ClubSubscriptions)
        .where(ClubSubscriptions.id == subscription_id, ClubSubscriptions.is_deleted == False)  # noqa: E712
        .options(
            selectinload(ClubSubscriptions.club),
            selectinload(ClubSubscriptions.plan).selectinload(Plans.limits),
            selectinload(ClubSubscriptions.plan).selectinload(Plans.features),
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise CustomHTTPException(status_code=404, message="Subscription not found")
    return row


async def _fresh(session, subscription_id: int) -> dict:
    """The row as the admin sees it, read afresh after a change."""
    row = await session.scalar(
        select(ClubSubscriptions)
        .where(ClubSubscriptions.id == subscription_id)
        .options(
            selectinload(ClubSubscriptions.club),
            selectinload(ClubSubscriptions.granter),
            selectinload(ClubSubscriptions.plan).selectinload(Plans.limits),
            selectinload(ClubSubscriptions.plan).selectinload(Plans.features),
        )
        .execution_options(populate_existing=True)
    )
    return subscription_dict(row, admin=True, granter=row.granter)


@admin_router.post(
    "/subscriptions/{subscription_id}/extend-grace", summary="Give a past-due club more time (admin)"
)
async def extend_grace(
    subscription_id: int,
    payload: ExtendGraceIn,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> AdminSubscriptionOut:
    """
    Move a past-due club's grace end `days` later (1 to 30): from its current
    end, or from now if it has already run out. A past-due club with no grace
    yet (Razorpay still retrying) gets one starting now. The club keeps its
    plan until the new end; nothing is charged or cancelled.

    Refused (422 NOT_PAST_DUE) for any other status, and (422 NEVER_PAID) for
    a Razorpay subscription that was never paid for -- it has no access to
    keep; move the club to a plan instead.
    """
    row = await _admin_subscription(session, subscription_id)
    if row.status != SubscriptionStatus.past_due:
        raise CustomHTTPException(
            status_code=422,
            message=f"Only a past-due subscription has a grace to extend; this one is {row.status.value}.",
            error_code="NOT_PAST_DUE",
        )
    if row.razorpay_subscription_id and row.activated_at is None:
        raise CustomHTTPException(
            status_code=422,
            message="This subscription was never paid for, so there is no access to keep. Move the club to a plan instead.",
            error_code="NEVER_PAID",
        )

    previous_end = row.grace_ends_at
    service.extend_grace(row, payload.days)
    club_name = row.club.name if row.club else f"club {row.club_id}"
    ends = notices.day(row.grace_ends_at)

    await audit.record(
        session,
        actor=user,
        action="subscription.extend_grace",
        entity_type="club",
        entity_id=row.club_id,
        summary=f"Extended {club_name}'s grace by {payload.days} day{'s' if payload.days != 1 else ''}, to {ends}",
        details={
            "subscription_id": row.id,
            "days": payload.days,
            "from": previous_end.isoformat() if previous_end else None,
            "to": row.grace_ends_at.isoformat(),
        },
        request=request,
    )
    await notify(
        session, "plan_assigned",
        title=f"{_admin_name(user)} extended {club_name}'s grace to {ends}",
        body=f"{payload.days} more day{'s' if payload.days != 1 else ''} on {row.plan.name if row.plan else 'its plan'}.",
        club=row.club, subscription=row, dedupe=False,
        data={
            "action": "extend_grace", "days": payload.days, "by": getattr(user, "email", None),
            "grace_ends_at": row.grace_ends_at.isoformat(),
            "previous_grace_ends_at": previous_end.isoformat() if previous_end else None,
        },
    )
    await session.commit()
    return await _fresh(session, row.id)


@admin_router.post(
    "/subscriptions/{subscription_id}/cancel", summary="Cancel a club's subscription (admin)"
)
async def admin_cancel_subscription(
    subscription_id: int,
    payload: AdminCancelIn,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> AdminSubscriptionOut:
    """
    Cancel for the club, the way the club's own cancel does.

    - `at_period_end: true`: the club keeps what it paid for (or its trial)
      until `current_period_end`, then Free. Razorpay stops at the end of the
      cycle for an active subscription, at once for any other.
    - `at_period_end: false`: Razorpay stops now and so does access: the club
      goes to Free, or back to an admin's grant it had before a checkout.
    - A checkout still pending is closed and the club goes back to what it had.
    - A plan an admin granted has nothing at Razorpay; it is cancelled here.
    - A complimentary plan is revoked now, whatever `at_period_end` says:
      the club goes to Free.

    409 NOTHING_TO_CANCEL when it is cancelled already (and, for `false`, no
    paid-for days are left to end). 502 RAZORPAY_ERROR when Razorpay refuses:
    nothing is changed here then, so the club is never shown cancelled while
    Razorpay can still charge it.
    """
    row = await _admin_subscription(session, subscription_id)
    now = datetime.now(timezone.utc)
    service.settle_stale_pending(row, now)
    at_period_end = bool(payload.at_period_end)
    rzp = row.razorpay_subscription_id
    club_name = row.club.name if row.club else f"club {row.club_id}"
    plan_name = row.plan.name if row.plan else "its plan"
    was = row.status.value
    comp = bool(row.complimentary)

    if comp:
        # Nothing at Razorpay, and nothing paid for to run out: revoked now.
        outcome = service.end_comp(row, now, restore=False)
    elif row.status == SubscriptionStatus.cancelled:
        if at_period_end or not service.is_entitled(row, now):
            await session.commit()
            raise CustomHTTPException(
                status_code=409, message="This subscription is already cancelled.", error_code="NOTHING_TO_CANCEL",
            )
        outcome = service.end_now(row, now)
    elif row.status == SubscriptionStatus.pending:
        if rzp:
            await billing.close_unpaid_checkout(rzp)
        service.revert_checkout(row)
        outcome = "checkout_closed"
    elif rzp:
        if not (at_period_end and row.cancel_at_period_end):
            try:
                await billing.cancel_subscription(
                    rzp, at_period_end=at_period_end and row.status == SubscriptionStatus.active
                )
            except Exception as exc:  # noqa: BLE001
                raise CustomHTTPException(
                    status_code=502,
                    message=f"Razorpay did not cancel the subscription: {str(exc)[:200]}",
                    error_code="RAZORPAY_ERROR",
                )
        if at_period_end:
            row.cancel_at_period_end = True
            row.cancel_requested_at = row.cancel_requested_at or now
            outcome = "at_period_end"
        else:
            outcome = service.end_now(row, now)
    else:
        row.cancel_at_period_end = at_period_end
        row.cancel_requested_at = now
        row.pending_since = None
        service.mark_cancelled(row)
        outcome = "at_period_end" if service.is_entitled(row, now) else "cancelled"

    await audit.record(
        session,
        actor=user,
        action="subscription.admin_cancel",
        entity_type="club",
        entity_id=row.club_id,
        summary=(
            f"Revoked {club_name}'s complimentary {plan_name}" if comp
            else f"Cancelled {club_name}'s {plan_name} subscription ({'at period end' if at_period_end else 'now'})"
        ),
        details={"subscription_id": row.id, "at_period_end": at_period_end, "outcome": outcome,
                 "razorpay_subscription_id": rzp, "previous_status": was, "complimentary": comp},
        request=request,
    )
    if outcome != "checkout_closed":
        await notify(
            session, "subscription_cancelled",
            title=(
                f"{_admin_name(user)} revoked {club_name}'s complimentary {plan_name}" if comp
                else f"{_admin_name(user)} cancelled {club_name}'s {plan_name} subscription"
            ),
            body=(
                f"Access runs to the end of the period ({notices.day(row.current_period_end)})."
                if service.is_entitled(row, now) and row.current_period_end
                else "Access ended now."
            ),
            club=row.club, subscription=row, dedupe=False,
            data={"by": "admin", "admin": getattr(user, "email", None), "at_period_end": at_period_end,
                  "outcome": outcome, "previous_status": was, "complimentary": comp},
        )
    await session.commit()
    return await _fresh(session, row.id)


# ---------------------------------------------------------------------------
# A club's own plan
# ---------------------------------------------------------------------------


async def _own_club(session, user) -> Clubs:
    club = (
        await session.execute(select(Clubs).where(Clubs.user_id == user.id))
    ).scalar_one_or_none()
    if not club:
        raise CustomHTTPException(status_code=404, message="No club for this account")
    return club


async def _settled_subscription(session, club_id: int):
    """The club's row, with a checkout pending for over a day put back to what
    the club had first (service.settle_stale_pending), and a complimentary
    plan whose end has passed put away the way the hourly sweep would
    (sweep.expire_comp: the club mailed once, the admins alerted)."""
    row = await service.get_club_subscription(session, club_id)
    if service.settle_stale_pending(row):
        await session.commit()
        row = await service.get_club_subscription(session, club_id)
    if await _settle_ended_comp(session, row):
        row = await service.get_club_subscription(session, club_id)
    return row


#: First key of the advisory lock a club's checkout takes.
_CHECKOUT_LOCK = 7_420_388


def _complimentary_refusal() -> CustomHTTPException:
    return CustomHTTPException(
        status_code=409,
        message="Your plan is provided by MyOtherApp — talk to us to change it.",
        error_code="COMPLIMENTARY_PLAN",
    )


async def _settle_ended_comp(session, row, now: datetime | None = None) -> bool:
    """Put away a complimentary plan the sweep has not reached yet. Returns
    whether there was one. Commits."""
    now = now or datetime.now(timezone.utc)
    if not service.comp_over(row, now):
        return False
    await sweep.expire_comp(session, row, now)
    await session.commit()
    return True


@club_router.get("/subscription", summary="The club's own plan")
async def my_subscription(session: SessionDep, user: ClubAuth) -> SubscriptionOut | None:
    club = await _own_club(session, user)
    row = await _settled_subscription(session, club.id)
    # The club's view: `complimentary` and `comp_ends_at`, never why it was
    # given or by whom (AdminSubscriptionOut).
    return subscription_dict(row, club) if row else None


@club_router.get("/entitlements", summary="What this club may do")
async def my_entitlements(session: SessionDep, user: ClubAuth) -> EntitlementsOut:
    club = await _own_club(session, user)
    await _settled_subscription(session, club.id)

    resolved = await service.resolve_entitlements(session, club.id)
    return {
        "plan_key": resolved.plan.key if resolved.plan else None,
        "plan_name": resolved.plan.name if resolved.plan else None,
        "status": resolved.grant["status"],
        "cycle": resolved.grant["cycle"],
        "current_period_end": resolved.grant["current_period_end"],
        "limits": resolved.limits,
        "features": resolved.features,
        "configured": resolved.configured,
        "enforced": gates.enforcing(),
        "grandfathered_before": gates.cutoff(),
        "requested_plan": club.requested_plan,
        "requested_cycle": club.requested_cycle,
        "subscription_status": (
            getattr(resolved.row.status, "value", resolved.row.status) if resolved.row else None
        ),
        "usage": await service.usage(session, club.id),
        "complimentary": service.comp_live(resolved.subscription),
        "comp_ends_at": (
            resolved.subscription.comp_ends_at if service.comp_live(resolved.subscription) else None
        ),
    }


@club_router.post("/subscription/start", summary="Subscribe this club to a plan")
async def start_subscription(
    payload: StartSubscriptionIn,
    session: SessionDep,
    user: ClubAuth,
    request: Request = None,
) -> StartSubscriptionOut:
    """
    Open a Razorpay checkout for a public plan, by `plan_id` or `plan_key`.

    Nothing is granted here. The subscription is recorded `pending` and the
    club keeps what it had until Razorpay confirms: `authenticated` starts a
    trial (when the plan has one and the club never had a trial), `activated`
    / `charged` make it active. An unpaid checkout falls back after a day,
    which is also when the Razorpay link stops working.

    Refused: a club on a complimentary plan (409 COMPLIMENTARY_PLAN: its plan
    is changed by talking to us; once the comp has ended, checkout works as
    usual), a plan not on the pricing page (404 PLAN_NOT_AVAILABLE), one
    priced on request (422 CONTACT_SALES), a free one (422 FREE_PLAN), and a
    second checkout while a Razorpay subscription can still charge the club
    (409 SUBSCRIPTION_EXISTS). A pending checkout the club never authorised is
    the exception: it is cancelled at Razorpay and replaced.
    """
    club = await _own_club(session, user)
    current = await service.get_club_subscription(session, club.id)
    if service.comp_live(current):
        raise _complimentary_refusal()
    # A comp that has ended but the sweep has not put away yet: put away now,
    # so the checkout starts from what the club really has.
    await _settle_ended_comp(session, current)
    # CustomBaseModel keeps enum *values*: the cycle arrives as a string.
    cycle = BillingCycle(payload.cycle)
    plan = await service.purchasable_plan(session, plan_id=payload.plan_id, plan_key=payload.plan_key)
    if plan.contact_sales:
        raise CustomHTTPException(
            status_code=422,
            message="This plan is arranged with our team. Talk to us and we will set it up.",
            error_code="CONTACT_SALES",
        )
    if service.price_for(plan, cycle) <= 0:
        raise CustomHTTPException(
            status_code=422,
            message="This plan is free — no subscription is needed for it.",
            error_code="FREE_PLAN",
        )

    now = datetime.now(timezone.utc)
    # One checkout per club at a time, from here to the commit: a double
    # click used to open two Razorpay subscriptions, each request reading the
    # row before the other wrote it, and the one not kept could still be
    # authorised and charge the club. The second click now waits, then finds
    # the first's checkout and replaces it (cancelled at Razorpay) as any
    # unauthorised checkout is. The row is locked as well, so a webhook about
    # the club's subscription waits too. Held across the Razorpay calls on
    # purpose -- the one place this module does: it is one club's own click,
    # each call is bounded by RAZORPAY_TIMEOUT, and nothing else waits on it.
    await session.execute(select(func.pg_advisory_xact_lock(_CHECKOUT_LOCK, club.id)))
    row = await service.get_club_subscription(session, club.id, for_update=True)
    if service.comp_live(row, now):
        raise _complimentary_refusal()
    service.settle_stale_pending(row, now)

    replacing = None
    if (
        row is not None
        and row.razorpay_subscription_id
        and row.status in service.LIVE_BILLED_STATUSES
    ):
        if row.status == SubscriptionStatus.pending and row.authenticated_at is None:
            replacing = row.razorpay_subscription_id
        else:
            raise CustomHTTPException(
                status_code=409,
                message=(
                    "This club already has a subscription. Cancel it first, or "
                    "wait for it to end, before starting another."
                ),
                error_code="SUBSCRIPTION_EXISTS",
            )

    trial_days = 0 if await service.trial_used(session, club.id) else plan.trial_days
    if replacing:
        await billing.close_unpaid_checkout(replacing)

    expires_at = now + service.CHECKOUT_TTL
    result = await billing.start_subscription(
        plan=plan,
        cycle=cycle,
        club_id=club.id,
        club_name=club.name,
        notify_email=club.contact_email,
        notify_phone=club.contact_phone,
        trial_days=trial_days,
        expire_by=int(expires_at.timestamp()),
    )

    service.record_checkout(
        session,
        club_id=club.id,
        subscription=row,
        plan=plan,
        cycle=cycle,
        razorpay_subscription_id=result["razorpay_subscription_id"],
        trial_days=trial_days,
        replacing_pending=replacing is not None,
        now=now,
    )
    await session.commit()
    return {
        "checkout_url": result.get("checkout_url"),
        "razorpay_subscription_id": result["razorpay_subscription_id"],
        "status": SubscriptionStatus.pending.value,
        "razorpay_status": result.get("status"),
        "plan_key": plan.key,
        "cycle": cycle.value,
        "trial_days": trial_days,
        "expires_at": expires_at,
    }


@club_router.post("/subscription/cancel", summary="Cancel this club's subscription")
async def cancel_my_subscription(
    session: SessionDep,
    user: ClubAuth,
    request: Request = None,
) -> SubscriptionOut:
    """
    Stop the club's Razorpay subscription. The club keeps its plan until
    `current_period_end` -- the end of what it paid for, or of its trial --
    and then falls to Free.

    A paid subscription is cancelled at Razorpay at the end of its cycle; a
    trial, a past-due or a paused one at once (nothing more is owed on it).
    A checkout still pending is cancelled at once and the club goes back to
    what it had. Asking twice changes nothing. A plan an admin granted by
    hand has nothing at Razorpay to cancel (409 NO_SUBSCRIPTION).
    """
    club = await _own_club(session, user)
    now = datetime.now(timezone.utc)
    row = await service.get_club_subscription(session, club.id)
    service.settle_stale_pending(row, now)

    if (
        row is None
        or not row.razorpay_subscription_id
        or row.status not in service.LIVE_BILLED_STATUSES
    ):
        await session.commit()
        raise CustomHTTPException(
            status_code=409,
            message="There is no paid subscription to cancel.",
            error_code="NO_SUBSCRIPTION",
        )

    alert = False
    if row.status == SubscriptionStatus.pending:
        await billing.close_unpaid_checkout(row.razorpay_subscription_id)
        service.revert_checkout(row)
    elif not row.cancel_at_period_end:
        await billing.cancel_subscription(
            row.razorpay_subscription_id,
            at_period_end=row.status == SubscriptionStatus.active,
        )
        row.cancel_at_period_end = True
        row.cancel_requested_at = now
        alert = True

    if alert:
        plan_name = row.plan.name if row.plan else "its plan"
        await notify(
            session, "subscription_cancelled",
            title=f"{club.name} cancelled its {plan_name} subscription",
            body=(
                f"Access runs to {notices.day(row.current_period_end)}, then Free."
                if row.current_period_end and row.current_period_end > now
                else "Access has ended; the club is on Free."
            ),
            club=club, subscription=row,
            data={"by": "club", "status": row.status.value,
                  "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None},
        )

    await audit.record(
        session,
        actor=user,
        action="subscription.cancel",
        entity_type="club",
        entity_id=club.id,
        summary=f"Club {club.id} cancelled its subscription",
        request=request,
    )
    await session.commit()
    fresh = await service.get_club_subscription(session, club.id)
    return subscription_dict(fresh, club)
