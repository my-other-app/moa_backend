"""Plan catalogue, club subscriptions, and the entitlement resolver.

`resolve_entitlements` is the function the rest of the product asks "may this
club do this". Everything else here exists to keep the rows it reads correct.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.clubs.models import Clubs
from app.api.subscriptions.models import (
    FEATURE_KEYS,
    LIMIT_KEYS,
    BillingCycle,
    ClubSubscriptions,
    PlanFeatures,
    PlanLimits,
    PlanVisibility,
    Plans,
    SubscriptionStatus,
)
from app.response import CustomHTTPException

#: The plan a club falls back to with no subscription of its own.
DEFAULT_PLAN_KEY = "free"

#: Statuses that still entitle a club to its plan. `past_due` is deliberately
#: included: Razorpay retries a failed charge for days, and cutting a club off
#: at the first failure would take a paying customer's event offline over a
#: expired card.
ENTITLED_STATUSES = (
    SubscriptionStatus.trial,
    SubscriptionStatus.active,
    SubscriptionStatus.past_due,
)


class Entitlements:
    """What one club may do, resolved from its plan.

    `configured` is false when the platform has no free plan to fall back on.
    That is not the same as "this club may do nothing": see `unlimited`.
    """

    __slots__ = ("plan", "subscription", "limits", "features", "configured")

    def __init__(self, *, plan, subscription, limits, features, configured):
        self.plan = plan
        self.subscription = subscription
        self.limits = limits
        self.features = features
        self.configured = configured

    def allows(self, feature: str) -> bool:
        return bool(self.features.get(feature, False))

    def limit(self, key: str) -> int | None:
        """The cap, or None for unlimited."""
        return self.limits.get(key)

    def within(self, key: str, current: int) -> bool:
        cap = self.limit(key)
        return cap is None or current < cap


def _unlimited() -> Entitlements:
    """
    The pre-subscription status quo: everything on, nothing capped.

    Used when no free plan exists. Introducing this domain must not lock every
    existing club out of features they have been using for a year -- a product
    that silently stops working is worse than one that is not yet gated. The
    `configured` flag is false so the admin screen can say so out loud.
    """
    return Entitlements(
        plan=None,
        subscription=None,
        limits={key: None for key in LIMIT_KEYS},
        features={key: True for key in FEATURE_KEYS},
        configured=False,
    )


def _from_plan(plan: Plans, subscription: ClubSubscriptions | None) -> Entitlements:
    # A plan with no limits or features row is misconfigured rather than
    # restrictive. Treating a missing row as "all false" would quietly
    # withdraw every feature from everyone on that plan.
    limits = (
        {key: getattr(plan.limits, key) for key in LIMIT_KEYS}
        if plan.limits
        else {key: None for key in LIMIT_KEYS}
    )
    features = (
        {key: bool(getattr(plan.features, key)) for key in FEATURE_KEYS}
        if plan.features
        else {key: False for key in FEATURE_KEYS}
    )
    return Entitlements(
        plan=plan,
        subscription=subscription,
        limits=limits,
        features=features,
        configured=True,
    )


async def resolve_entitlements(session, club_id: int) -> Entitlements:
    """
    What this club may do.

    Reads the club's live subscription, or the default plan when it has none.
    A cancelled subscription is not live, so the club drops to the default
    plan rather than keeping what it stopped paying for.
    """
    subscription = (
        await session.execute(
            select(ClubSubscriptions)
            .where(
                ClubSubscriptions.club_id == club_id,
                ClubSubscriptions.is_deleted == False,
                ClubSubscriptions.status.in_(ENTITLED_STATUSES),
            )
            .options(
                selectinload(ClubSubscriptions.plan).selectinload(Plans.limits),
                selectinload(ClubSubscriptions.plan).selectinload(Plans.features),
            )
        )
    ).scalar_one_or_none()

    if subscription and subscription.plan and not subscription.plan.is_deleted:
        return _from_plan(subscription.plan, subscription)

    default = (
        await session.execute(
            select(Plans)
            .where(Plans.key == DEFAULT_PLAN_KEY, Plans.is_deleted == False)
            .options(selectinload(Plans.limits), selectinload(Plans.features))
        )
    ).scalar_one_or_none()

    return _from_plan(default, None) if default else _unlimited()


# ---------------------------------------------------------------------------
# Plan catalogue
# ---------------------------------------------------------------------------


async def list_plans(session, *, include_hidden: bool = False) -> list[tuple[Plans, int]]:
    """Plans with their subscriber counts, cheapest first.

    The count comes from a grouped query rather than a correlated subquery
    per row: there are a handful of plans and the join would fan out against
    the limits and features eager loads.
    """
    query = (
        select(Plans)
        .where(Plans.is_deleted == False)
        .options(selectinload(Plans.limits), selectinload(Plans.features))
        .order_by(Plans.sort_order, Plans.price_monthly_paise)
    )
    if not include_hidden:
        query = query.where(Plans.visibility == PlanVisibility.public)

    plans = (await session.execute(query)).scalars().all()
    if not plans:
        return []

    counts = dict(
        (
            await session.execute(
                select(ClubSubscriptions.plan_id, func.count(ClubSubscriptions.id))
                .where(
                    ClubSubscriptions.plan_id.in_([p.id for p in plans]),
                    ClubSubscriptions.is_deleted == False,
                    ClubSubscriptions.status.in_(ENTITLED_STATUSES),
                )
                .group_by(ClubSubscriptions.plan_id)
            )
        ).all()
    )
    return [(plan, counts.get(plan.id, 0)) for plan in plans]


async def public_plans(session) -> list[Plans]:
    """The plans on the pricing page: public, not deleted, in display order.

    `legacy` is excluded by the visibility filter itself -- an archived plan
    keeps its subscribers but is no longer for sale.
    """
    return list(
        (
            await session.execute(
                select(Plans)
                .where(Plans.is_deleted == False, Plans.visibility == PlanVisibility.public)
                .options(selectinload(Plans.limits), selectinload(Plans.features))
                .order_by(Plans.sort_order, Plans.price_monthly_paise, Plans.id)
            )
        )
        .scalars()
        .all()
    )


async def requested_plan_key(session, raw: str | None) -> str | None:
    """
    The plan a sign-up form asked for, if it names one on the pricing page.

    Anything else -- a typo, a withdrawn plan, a hidden bespoke one -- is
    dropped rather than refused: the choice is a hint for the admin, and an
    application should not fail over it.
    """
    key = (raw or "").strip().lower()
    if not key or len(key) > 40:
        return None
    found = await session.scalar(
        select(Plans.key).where(
            Plans.key == key,
            Plans.is_deleted == False,
            Plans.visibility == PlanVisibility.public,
        )
    )
    return found


def requested_cycle_value(raw: str | None) -> str | None:
    value = (raw or "").strip().lower()
    return value if value in {cycle.value for cycle in BillingCycle} else None


async def get_plan(session, plan_id: int) -> Plans:
    plan = (
        await session.execute(
            select(Plans)
            .where(Plans.id == plan_id, Plans.is_deleted == False)
            .options(selectinload(Plans.limits), selectinload(Plans.features))
        )
    ).scalar_one_or_none()
    if not plan:
        raise CustomHTTPException(status_code=404, message="Plan not found")
    return plan


def validate_prices(price_monthly_paise: int, price_yearly_paise: int) -> None:
    """
    A yearly price above twelve months of the monthly one is rejected.

    Yearly billing is a discount for paying up front; charging more for it is
    always a slipped decimal, and it would be charged before anyone noticed.
    """
    if price_yearly_paise > price_monthly_paise * 12:
        raise CustomHTTPException(
            status_code=422,
            message=(
                "The yearly price is more than twelve times the monthly price. "
                "Yearly billing should not cost more than paying monthly."
            ),
        )


async def subscriber_count(session, plan_id: int) -> int:
    return (
        await session.scalar(
            select(func.count(ClubSubscriptions.id)).where(
                ClubSubscriptions.plan_id == plan_id,
                ClubSubscriptions.is_deleted == False,
                ClubSubscriptions.status.in_(ENTITLED_STATUSES),
            )
        )
    ) or 0


async def archive_or_delete_plan(session, plan: Plans) -> str:
    """
    Withdraw a plan from sale.

    A plan with subscribers is archived to `legacy`, never deleted: deleting
    it would orphan their entitlements and drop them to the free plan without
    anyone deciding that. Only a plan nobody is on can actually go.

    Returns "archived" or "deleted" so the caller can say which happened.
    """
    if await subscriber_count(session, plan.id) > 0:
        plan.visibility = PlanVisibility.legacy
        return "archived"

    plan.soft_delete()
    return "deleted"


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def price_for(plan: Plans, cycle: BillingCycle) -> int:
    return (
        plan.price_yearly_paise
        if cycle == BillingCycle.yearly
        else plan.price_monthly_paise
    )


def mrr_for(plan: Plans, cycle: BillingCycle) -> int:
    """
    Monthly recurring revenue contribution, in paise.

    A yearly subscription contributes a twelfth of its price per month, not
    the whole thing -- summing yearly prices into MRR overstates it by an
    order of magnitude, which is the most common way this number is wrong.
    """
    if cycle == BillingCycle.yearly:
        return plan.price_yearly_paise // 12
    return plan.price_monthly_paise


async def get_club_subscription(session, club_id: int) -> ClubSubscriptions | None:
    return (
        await session.execute(
            select(ClubSubscriptions)
            .where(
                ClubSubscriptions.club_id == club_id,
                ClubSubscriptions.is_deleted == False,
            )
            .options(
                selectinload(ClubSubscriptions.plan).selectinload(Plans.limits),
                selectinload(ClubSubscriptions.plan).selectinload(Plans.features),
            )
        )
    ).scalar_one_or_none()


async def assign_plan(
    session,
    *,
    club_id: int,
    plan: Plans,
    cycle: BillingCycle,
    status: SubscriptionStatus = SubscriptionStatus.active,
) -> ClubSubscriptions:
    """
    Put a club on a plan, without charging anything.

    This is the admin's lever -- moving a club to a bespoke plan, or granting
    one. Taking money is `billing.start_subscription`, deliberately separate:
    nothing in this module should be able to charge a card as a side effect of
    an admin changing a dropdown.
    """
    club = await session.get(Clubs, club_id)
    if not club or club.is_deleted:
        raise CustomHTTPException(status_code=404, message="Club not found")

    subscription = await get_club_subscription(session, club_id)
    if subscription is None:
        subscription = ClubSubscriptions(club_id=club_id)
        session.add(subscription)

    subscription.plan_id = plan.id
    subscription.cycle = cycle
    subscription.status = status
    subscription.mrr_paise = mrr_for(plan, cycle)
    subscription.last_failure_reason = None
    await session.flush()
    return subscription


async def mrr_totals(session) -> dict:
    """Headline numbers for the admin screen."""
    rows = (
        await session.execute(
            select(
                func.coalesce(func.sum(ClubSubscriptions.mrr_paise), 0),
                func.count(ClubSubscriptions.id),
            ).where(
                ClubSubscriptions.is_deleted == False,
                ClubSubscriptions.status.in_(ENTITLED_STATUSES),
            )
        )
    ).one()
    mrr, active = rows

    by_status = dict(
        (
            await session.execute(
                select(ClubSubscriptions.status, func.count(ClubSubscriptions.id))
                .where(ClubSubscriptions.is_deleted == False)
                .group_by(ClubSubscriptions.status)
            )
        ).all()
    )

    return {
        "mrr_paise": int(mrr or 0),
        # Annual run rate, the number a board asks for. Derived here so the
        # dashboard cannot multiply it by the wrong factor.
        "arr_paise": int(mrr or 0) * 12,
        "active_subscriptions": int(active or 0),
        "trialing": int(by_status.get(SubscriptionStatus.trial, 0)),
        "past_due": int(by_status.get(SubscriptionStatus.past_due, 0)),
        "cancelled": int(by_status.get(SubscriptionStatus.cancelled, 0)),
    }


def mark_charged(subscription: ClubSubscriptions, *, period_end: datetime) -> None:
    subscription.status = SubscriptionStatus.active
    subscription.current_period_end = period_end
    subscription.last_failure_reason = None


def mark_failed(subscription: ClubSubscriptions, *, reason: str | None) -> None:
    subscription.status = SubscriptionStatus.past_due
    subscription.last_failure_reason = (reason or "")[:255] or None


def mark_cancelled(subscription: ClubSubscriptions) -> None:
    subscription.status = SubscriptionStatus.cancelled
    subscription.mrr_paise = 0
    subscription.current_period_end = datetime.now(timezone.utc)
