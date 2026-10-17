"""Plan catalogue and subscription endpoints.

Two routers: one for the admin, behind `manage_money` for anything that
changes what a club is charged, and one for a club reading its own plan.

Nothing here charges a card except `/clubs/subscription/start`, which creates
an authorisation the club completes itself, and `/retry`, which asks Razorpay
to re-attempt a charge that already failed.
"""

from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.audit import service as audit
from app.api.clubs.models import Clubs
from app.api.subscriptions import billing, service
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
    AssignPlanIn,
    EntitlementsOut,
    PlanIn,
    PlanOut,
    PublicPlansOut,
    StartSubscriptionIn,
    StartSubscriptionOut,
    SubscriptionOut,
    SubscriptionTotals,
    plan_to_dict,
    public_plan_dict,
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
            billing.sync_plan(plan, cycle)
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


@admin_router.get("/subscriptions", summary="List club subscriptions (admin)")
async def list_subscriptions(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
    status: Optional[SubscriptionStatus] = Query(None),
    plan_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None, description="Club name"),
) -> PaginatedResponse[SubscriptionOut]:
    criteria = [ClubSubscriptions.is_deleted == False]
    if status is not None:
        criteria.append(ClubSubscriptions.status == status)
    if plan_id is not None:
        criteria.append(ClubSubscriptions.plan_id == plan_id)
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

    items = [
        {
            "id": row.id,
            "club": {"id": row.club.id, "name": row.club.name} if row.club else None,
            "plan": plan_to_dict(row.plan) if row.plan else None,
            "cycle": getattr(row.cycle, "value", row.cycle),
            "status": getattr(row.status, "value", row.status),
            "current_period_end": row.current_period_end,
            "mrr_paise": row.mrr_paise,
            "last_failure_reason": row.last_failure_reason,
            "razorpay_subscription_id": row.razorpay_subscription_id,
        }
        for row in rows
    ]
    return paginated_response(items, request, schema=SubscriptionOut, total=total)


@admin_router.put(
    "/subscriptions/club/{club_id}/plan", summary="Move a club to a plan (admin)"
)
async def assign_plan(
    club_id: int,
    payload: AssignPlanIn,
    session: SessionDep,
    user: MoneyAuth,
    request: Request = None,
) -> SubscriptionOut:
    """
    Grant a plan directly, without taking payment.

    For bespoke deals and corrections. It deliberately does not touch
    Razorpay: if the club has a live Razorpay subscription it keeps billing at
    the old price until someone cancels it there, and the response says so.
    """
    plan = await service.get_plan(session, payload.plan_id)
    subscription = await service.assign_plan(
        session, club_id=club_id, plan=plan, cycle=payload.cycle, status=payload.status
    )

    await audit.record(
        session,
        actor=user,
        action="subscription.assign",
        entity_type="club",
        entity_id=club_id,
        summary=f"Moved club {club_id} to plan “{plan.name}” ({payload.cycle.value})",
        request=request,
    )
    await session.commit()

    fresh = await service.get_club_subscription(session, club_id)
    club = await session.get(Clubs, club_id)
    return {
        "id": fresh.id,
        "club": {"id": club.id, "name": club.name} if club else None,
        "plan": plan_to_dict(fresh.plan) if fresh.plan else None,
        "cycle": getattr(fresh.cycle, "value", fresh.cycle),
        "status": getattr(fresh.status, "value", fresh.status),
        "current_period_end": fresh.current_period_end,
        "mrr_paise": fresh.mrr_paise,
        "last_failure_reason": fresh.last_failure_reason,
        "razorpay_subscription_id": fresh.razorpay_subscription_id,
    }


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

    billing.retry_charge(subscription.razorpay_subscription_id)

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


# ---------------------------------------------------------------------------
# A club's own plan
# ---------------------------------------------------------------------------


@club_router.get("/subscription", summary="The club's own plan")
async def my_subscription(session: SessionDep, user: ClubAuth) -> SubscriptionOut | None:
    club = (
        await session.execute(select(Clubs).where(Clubs.user_id == user.id))
    ).scalar_one_or_none()
    if not club:
        raise CustomHTTPException(status_code=404, message="No club for this account")

    row = await service.get_club_subscription(session, club.id)
    if not row:
        return None
    return {
        "id": row.id,
        "club": {"id": club.id, "name": club.name},
        "plan": plan_to_dict(row.plan) if row.plan else None,
        "cycle": getattr(row.cycle, "value", row.cycle),
        "status": getattr(row.status, "value", row.status),
        "current_period_end": row.current_period_end,
        "mrr_paise": row.mrr_paise,
        "last_failure_reason": row.last_failure_reason,
        "razorpay_subscription_id": row.razorpay_subscription_id,
    }


@club_router.get("/entitlements", summary="What this club may do")
async def my_entitlements(session: SessionDep, user: ClubAuth) -> EntitlementsOut:
    club = (
        await session.execute(select(Clubs).where(Clubs.user_id == user.id))
    ).scalar_one_or_none()
    if not club:
        raise CustomHTTPException(status_code=404, message="No club for this account")

    resolved = await service.resolve_entitlements(session, club.id)
    subscription = resolved.subscription
    return {
        "plan_key": resolved.plan.key if resolved.plan else None,
        "plan_name": resolved.plan.name if resolved.plan else None,
        "status": (
            getattr(subscription.status, "value", subscription.status)
            if subscription
            else None
        ),
        "cycle": (
            getattr(subscription.cycle, "value", subscription.cycle)
            if subscription
            else None
        ),
        "current_period_end": subscription.current_period_end if subscription else None,
        "limits": resolved.limits,
        "features": resolved.features,
        "configured": resolved.configured,
        "requested_plan": club.requested_plan,
        "requested_cycle": club.requested_cycle,
    }


@club_router.post("/subscription/start", summary="Subscribe this club to a plan")
async def start_subscription(
    payload: StartSubscriptionIn,
    session: SessionDep,
    user: ClubAuth,
    request: Request = None,
) -> StartSubscriptionOut:
    """
    Begin a paid subscription.

    Returns a Razorpay checkout URL the club completes themselves. Nothing is
    charged here -- the first charge arrives as a webhook once they have
    authorised it, which is also when the plan takes effect.
    """
    club = (
        await session.execute(select(Clubs).where(Clubs.user_id == user.id))
    ).scalar_one_or_none()
    if not club:
        raise CustomHTTPException(status_code=404, message="No club for this account")

    plan = await service.get_plan(session, payload.plan_id)
    if plan.visibility == PlanVisibility.legacy:
        raise CustomHTTPException(
            status_code=422, message="This plan is no longer offered."
        )
    if plan.contact_sales:
        # Priced per deal: an admin assigns it once terms are agreed, and a
        # checkout here would charge whatever placeholder price it carries.
        raise CustomHTTPException(
            status_code=422,
            message="This plan is arranged with our team. Talk to us and we will set it up.",
        )
    if service.price_for(plan, payload.cycle) <= 0:
        raise CustomHTTPException(
            status_code=422,
            message="This plan is free — no subscription is needed for it.",
        )

    result = billing.start_subscription(
        plan=plan,
        cycle=payload.cycle,
        club_id=club.id,
        club_name=club.name,
        notify_email=club.contact_email,
        notify_phone=club.contact_phone,
    )

    # Recorded as trial/pending rather than active: it becomes active when
    # Razorpay says it has been paid, not when we asked for it.
    subscription = await service.assign_plan(
        session,
        club_id=club.id,
        plan=plan,
        cycle=payload.cycle,
        status=SubscriptionStatus.trial if plan.trial_days else SubscriptionStatus.past_due,
    )
    subscription.razorpay_subscription_id = result["razorpay_subscription_id"]
    # No MRR until it is actually paid; counting an unauthorised subscription
    # would inflate the headline number on the admin screen.
    subscription.mrr_paise = 0

    await session.commit()
    return result
