"""Plan catalogue, club subscriptions, and the entitlement resolver.

`resolve_entitlements` is the function the rest of the product asks "may this
club do this". Everything else here exists to keep the rows it reads correct.
"""

from datetime import datetime, timedelta, timezone

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

#: Statuses counted as subscribers on the admin screen (MRR, "N clubs on this
#: plan", whether a plan can be deleted). Whether one row actually grants its
#: plan is `is_entitled`, which also asks whether Razorpay confirmed it.
ENTITLED_STATUSES = (
    SubscriptionStatus.trial,
    SubscriptionStatus.active,
    SubscriptionStatus.past_due,
)

#: Statuses with a Razorpay subscription still able to charge: a second
#: checkout on top of one of these would bill the club twice.
LIVE_BILLED_STATUSES = (
    SubscriptionStatus.pending,
    SubscriptionStatus.trial,
    SubscriptionStatus.active,
    SubscriptionStatus.past_due,
    SubscriptionStatus.paused,
)

#: How long an unconfirmed checkout holds before the club falls back to what
#: it had. The Razorpay checkout link is made to expire at the same moment.
CHECKOUT_TTL = timedelta(hours=24)

#: How long a club that had paid keeps its plan after Razorpay stops retrying
#: a failed renewal (`subscription.halted`). Then it moves to Free.
GRACE_DAYS = 7
#: The club and the admin hear this long before a grace runs out.
GRACE_REMINDER = timedelta(hours=48)
#: A club on a trial hears this long before the trial ends (and billing starts).
TRIAL_REMINDER = timedelta(hours=72)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status(value) -> SubscriptionStatus | None:
    if value is None or isinstance(value, SubscriptionStatus):
        return value
    return SubscriptionStatus(value)


def _when(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _entitled(*, status, billed, authenticated_at, activated_at, trial_started_at,
              cancel_at_period_end, current_period_end, now, grace_ends_at=None) -> bool:
    status = _status(status)
    if status == SubscriptionStatus.active:
        return True
    if status == SubscriptionStatus.trial:
        # A trial begins when Razorpay says the mandate is authorised, not when
        # checkout opened: a club that closes the checkout gets nothing.
        return not billed or authenticated_at is not None
    if status == SubscriptionStatus.past_due:
        # Razorpay retries a failed renewal for days and a paying club keeps
        # its plan meanwhile. A subscription never paid for has nothing to keep.
        # Once Razorpay gives up, the grace it was given ends on time even if
        # the sweep that moves the club to Free has not run yet.
        if grace_ends_at is not None and grace_ends_at <= now:
            return False
        return not billed or activated_at is not None
    if status == SubscriptionStatus.cancelled:
        # Cancelled by the club at period end: what it paid for (or the trial
        # it was given) runs to the end of the period.
        return bool(
            cancel_at_period_end
            and current_period_end is not None
            and current_period_end > now
            and (activated_at is not None or trial_started_at is not None)
        )
    # pending, paused
    return False


def is_entitled(subscription: ClubSubscriptions, now: datetime | None = None) -> bool:
    """Whether this row grants its plan right now.

    A row with no Razorpay subscription was put there by an admin, whose word
    is enough; one Razorpay bills must have been confirmed by Razorpay.
    """
    return _entitled(
        status=subscription.status,
        billed=subscription.razorpay_subscription_id is not None,
        authenticated_at=subscription.authenticated_at,
        activated_at=subscription.activated_at,
        trial_started_at=subscription.trial_started_at,
        cancel_at_period_end=subscription.cancel_at_period_end,
        current_period_end=subscription.current_period_end,
        now=now or _now(),
        grace_ends_at=subscription.grace_ends_at,
    )


def in_grace(subscription: ClubSubscriptions, now: datetime | None = None) -> bool:
    """Past due, Razorpay has stopped retrying, and the grace has not run out."""
    return (
        _status(subscription.status) == SubscriptionStatus.past_due
        and subscription.grace_ends_at is not None
        and subscription.grace_ends_at > (now or _now())
    )


def previous_entitled(subscription: ClubSubscriptions, now: datetime | None = None) -> bool:
    """Whether what the club had before this checkout still grants its plan."""
    prev = subscription.previous_state or {}
    if not prev.get("plan_id"):
        return False
    return _entitled(
        status=prev.get("status"),
        billed=prev.get("razorpay_subscription_id") is not None,
        authenticated_at=_when(prev.get("authenticated_at")),
        activated_at=_when(prev.get("activated_at")),
        trial_started_at=subscription.trial_started_at,
        cancel_at_period_end=prev.get("cancel_at_period_end"),
        current_period_end=_when(prev.get("current_period_end")),
        now=now or _now(),
    )


class Entitlements:
    """What one club may do, resolved from its plan.

    `configured` is false when the platform has no free plan to fall back on.
    That is not the same as "this club may do nothing": see `unlimited`.
    """

    __slots__ = ("plan", "subscription", "limits", "features", "configured", "row", "grant")

    def __init__(self, *, plan, subscription, limits, features, configured):
        self.plan = plan
        #: The subscription that grants `plan`; None when it is the default.
        self.subscription = subscription
        self.limits = limits
        self.features = features
        self.configured = configured
        #: The club's own subscription row whatever its state (a checkout
        #: pending, a cancelled one), or None.
        self.row = None
        #: status / cycle / current_period_end of whatever grants `plan`.
        self.grant = (
            {
                "status": getattr(subscription.status, "value", subscription.status),
                "cycle": getattr(subscription.cycle, "value", subscription.cycle),
                "current_period_end": subscription.current_period_end,
            }
            if subscription is not None
            else {"status": None, "cycle": None, "current_period_end": None}
        )

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

    The club's subscription when `is_entitled` says it grants its plan; while
    a checkout is pending, what the club had before it; otherwise the default
    plan. Nothing is granted on the strength of a checkout alone: a club that
    opens Razorpay and closes it keeps exactly what it had.
    """
    now = _now()
    row = await get_club_subscription(session, club_id)
    resolved = await _resolve(session, row, now)
    resolved.row = row
    return resolved


async def _resolve(session, row, now) -> Entitlements:
    if row is not None:
        if is_entitled(row, now):
            if row.plan and not row.plan.is_deleted:
                return _from_plan(row.plan, row)
        elif row.status == SubscriptionStatus.pending and previous_entitled(row, now):
            # Mid-checkout: the plan the club had keeps going until Razorpay
            # confirms the new one.
            previous = (
                await session.execute(
                    select(Plans)
                    .where(Plans.id == row.previous_state["plan_id"], Plans.is_deleted == False)
                    .options(selectinload(Plans.limits), selectinload(Plans.features))
                )
            ).scalar_one_or_none()
            if previous is not None:
                resolved = _from_plan(previous, None)
                prev = row.previous_state
                resolved.grant = {
                    "status": prev.get("status"),
                    "cycle": prev.get("cycle"),
                    "current_period_end": _when(prev.get("current_period_end")),
                }
                return resolved

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


async def purchasable_plan(session, *, plan_id: int | None = None, plan_key: str | None = None) -> Plans:
    """
    The plan a club may check out: on the pricing page, by id or key.

    Hidden, legacy and deleted plans answer exactly like a plan that does not
    exist (404 PLAN_NOT_AVAILABLE): a bespoke plan is assigned by an admin,
    and its existence is nobody else's business.
    """
    query = (
        select(Plans)
        .where(Plans.is_deleted == False, Plans.visibility == PlanVisibility.public)
        .options(selectinload(Plans.limits), selectinload(Plans.features))
    )
    if plan_id is not None:
        query = query.where(Plans.id == plan_id)
    else:
        query = query.where(Plans.key == (plan_key or "").strip().lower())
    plan = (await session.execute(query)).scalar_one_or_none()
    if plan is None:
        raise CustomHTTPException(
            status_code=404,
            message="That plan is not available.",
            error_code="PLAN_NOT_AVAILABLE",
        )
    return plan


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
    # BillingCycle(...) also takes "yearly": request models keep enum values.
    return (
        plan.price_yearly_paise
        if BillingCycle(cycle) == BillingCycle.yearly
        else plan.price_monthly_paise
    )


def mrr_for(plan: Plans, cycle: BillingCycle) -> int:
    """
    Monthly recurring revenue contribution, in paise.

    A yearly subscription contributes a twelfth of its price per month, not
    the whole thing -- summing yearly prices into MRR overstates it by an
    order of magnitude, which is the most common way this number is wrong.
    """
    if BillingCycle(cycle) == BillingCycle.yearly:
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
    subscription.mrr_paise = mrr_for(plan, cycle) if status != SubscriptionStatus.cancelled else 0
    subscription.last_failure_reason = None
    # The admin's word stands in for Razorpay's: a plan granted by hand is
    # entitled whether or not a Razorpay subscription sits on the row.
    now = _now()
    if status == SubscriptionStatus.trial:
        subscription.authenticated_at = subscription.authenticated_at or now
        subscription.trial_started_at = subscription.trial_started_at or now
    if status in (SubscriptionStatus.active, SubscriptionStatus.past_due):
        subscription.activated_at = subscription.activated_at or now
    subscription.pending_since = None
    subscription.previous_state = None
    # The admin decided what the club has now; a grace that was running for
    # the old arrangement no longer applies.
    clear_grace(subscription)
    await session.flush()
    return subscription


# ---------------------------------------------------------------------------
# Checkout: pending until Razorpay confirms, and back to what was there if not
# ---------------------------------------------------------------------------

#: The fields a checkout replaces and an abandoned one puts back.
_SNAPSHOT = (
    "plan_id", "cycle", "status", "current_period_end", "mrr_paise", "razorpay_subscription_id",
    "authenticated_at", "activated_at", "trial_ends_at", "cancel_at_period_end",
    "cancel_requested_at", "last_failure_reason",
)


def _snapshot(subscription: ClubSubscriptions) -> dict:
    out = {}
    for field in _SNAPSHOT:
        value = getattr(subscription, field)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif hasattr(value, "value"):
            value = value.value
        out[field] = value
    return out


async def trial_used(session, club_id: int) -> bool:
    """Whether this club has ever had a trial, on any subscription row."""
    return bool(
        await session.scalar(
            select(func.count(ClubSubscriptions.id)).where(
                ClubSubscriptions.club_id == club_id,
                ClubSubscriptions.trial_started_at.isnot(None),
            )
        )
    )


def record_checkout(
    session,
    *,
    club_id: int,
    subscription: ClubSubscriptions | None,
    plan: Plans,
    cycle: BillingCycle,
    razorpay_subscription_id: str,
    trial_days: int,
    replacing_pending: bool,
    now: datetime,
) -> ClubSubscriptions:
    """
    Note a checkout as `pending`. Grants nothing (see `is_entitled`).

    What the club had is kept in `previous_state` so an abandoned checkout can
    put it back. Replacing a checkout that was itself pending keeps the state
    from before the first one, not the pending one.
    """
    if subscription is None:
        subscription = ClubSubscriptions(club_id=club_id)
        session.add(subscription)
    elif not replacing_pending:
        subscription.previous_state = _snapshot(subscription)

    subscription.plan_id = plan.id
    subscription.plan = plan
    subscription.cycle = cycle
    subscription.status = SubscriptionStatus.pending
    subscription.razorpay_subscription_id = razorpay_subscription_id
    subscription.mrr_paise = 0
    subscription.current_period_end = None
    subscription.pending_since = now
    subscription.authenticated_at = None
    subscription.activated_at = None
    subscription.trial_ends_at = now + timedelta(days=trial_days) if trial_days else None
    subscription.cancel_at_period_end = False
    subscription.cancel_requested_at = None
    subscription.last_failure_reason = None
    clear_grace(subscription)
    return subscription


def revert_checkout(subscription: ClubSubscriptions) -> None:
    """
    Undo a checkout that was never paid for: back to what the club had, or,
    when it had nothing, a cancelled row (which grants nothing, so Free).

    The abandoned Razorpay id stays on a row with nothing before it, so a
    payment that does arrive late still finds the plan it was for.
    """
    prev = subscription.previous_state
    if prev:
        for field in _SNAPSHOT:
            value = prev.get(field)
            if field == "cycle" and value is not None:
                value = BillingCycle(value)
            elif field == "status" and value is not None:
                value = SubscriptionStatus(value)
            elif field.endswith("_at") or field == "current_period_end":
                value = _when(value)
            elif field == "cancel_at_period_end":
                value = bool(value)
            elif field == "mrr_paise":
                value = int(value or 0)
            setattr(subscription, field, value)
    else:
        subscription.status = SubscriptionStatus.cancelled
        subscription.mrr_paise = 0
    subscription.pending_since = None
    subscription.previous_state = None
    if prev:
        # plan_id moved back; the loaded `plan` still names the abandoned one.
        # Sessions here keep objects across commits, so it is expired for the
        # next query to load afresh rather than served stale.
        from sqlalchemy.orm import object_session

        owner = object_session(subscription)
        if owner is not None:
            owner.expire(subscription, ["plan"])


def settle_stale_pending(subscription: ClubSubscriptions | None, now: datetime | None = None) -> bool:
    """A checkout still pending a day after it opened falls back. Returns
    whether it did. The Razorpay link expires at the same time, so the club
    cannot complete it afterwards."""
    if subscription is None or subscription.status != SubscriptionStatus.pending:
        return False
    since = subscription.pending_since
    if since is None or since > (now or _now()) - CHECKOUT_TTL:
        return False
    revert_checkout(subscription)
    return True


def checkout_confirmed(subscription: ClubSubscriptions) -> None:
    """Razorpay confirmed the checkout: what came before is no longer needed."""
    subscription.pending_since = None
    subscription.previous_state = None


# ---------------------------------------------------------------------------
# Usage against the limits
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))


def month_start(now: datetime | None = None) -> datetime:
    """The first instant of this calendar month in India, as UTC."""
    local = (now or _now()).astimezone(IST)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def usage(session, club_id: int, now: datetime | None = None) -> dict:
    """
    What the club has used of the limits that are counted per club. One
    bounded query each. Per-event limits (registrations, ticket types,
    coupons and merchandise per event), storage and website templates are not
    here: they are not one number for the club, or nothing records them yet.
    """
    from app.api.club_access.models import ClubMembers
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.notifications.models import Notifications

    since = month_start(now)
    club_events = select(Events.id).where(Events.club_id == club_id)

    events = await session.scalar(
        select(func.count(Events.id)).where(
            Events.club_id == club_id, Events.is_deleted == False, Events.created_at >= since
        )
    )
    registrations = await session.scalar(
        select(func.count(EventRegistrationsLink.id)).where(
            EventRegistrationsLink.event_id.in_(club_events),
            EventRegistrationsLink.is_deleted == False,
            EventRegistrationsLink.created_at >= since,
        )
    )
    seats = await session.scalar(
        select(func.count(ClubMembers.id)).where(
            ClubMembers.club_id == club_id, ClubMembers.status != "removed"
        )
    )
    # One announcement reaches many phones as one row each; a send is one
    # title, body and event within a minute -- the Announcements page's rule.
    announcements = await session.scalar(
        select(
            func.count(
                func.distinct(
                    func.concat_ws(
                        "|",
                        Notifications.title,
                        Notifications.description,
                        Notifications.event_id,
                        func.date_trunc("minute", Notifications.created_at),
                    )
                )
            )
        ).where(
            Notifications.event_id.in_(club_events),
            Notifications.type == "club_announcement",
            Notifications.is_deleted == False,
            Notifications.created_at >= since,
        )
    )
    return {
        "events_per_month": int(events or 0),
        "registrations_per_month": int(registrations or 0),
        "committee_seats": int(seats or 0),
        "announcements_per_month": int(announcements or 0),
    }


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
    in_grace_count = await session.scalar(
        select(func.count(ClubSubscriptions.id)).where(
            ClubSubscriptions.is_deleted == False,
            ClubSubscriptions.status == SubscriptionStatus.past_due,
            ClubSubscriptions.grace_ends_at > _now(),
        )
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
        "pending": int(by_status.get(SubscriptionStatus.pending, 0)),
        "paused": int(by_status.get(SubscriptionStatus.paused, 0)),
        # Past due, Razorpay done retrying, still within its grace: the ones
        # about to drop to Free unless someone acts.
        "in_grace": int(in_grace_count or 0),
    }


def mark_charged(subscription: ClubSubscriptions, *, period_end: datetime | None) -> None:
    subscription.status = SubscriptionStatus.active
    if period_end is not None:
        subscription.current_period_end = period_end
    subscription.last_failure_reason = None
    subscription.activated_at = subscription.activated_at or _now()
    checkout_confirmed(subscription)


def mark_failed(subscription: ClubSubscriptions, *, reason: str | None) -> None:
    subscription.last_failure_reason = (reason or "")[:255] or None
    # A checkout that never went through stays pending (and grants nothing);
    # past_due is for a subscription that was running.
    if subscription.status != SubscriptionStatus.pending:
        subscription.status = SubscriptionStatus.past_due


def mark_cancelled(subscription: ClubSubscriptions) -> None:
    now = _now()
    subscription.status = SubscriptionStatus.cancelled
    subscription.mrr_paise = 0
    # Cancelled by the club at period end: access runs to the end of what was
    # paid for (is_entitled). Otherwise it ends now.
    if not (
        subscription.cancel_at_period_end
        and subscription.current_period_end is not None
        and subscription.current_period_end > now
    ):
        subscription.current_period_end = now


# ---------------------------------------------------------------------------
# Grace: a paying club keeps its plan for GRACE_DAYS after Razorpay gives up
# ---------------------------------------------------------------------------


def start_grace(subscription: ClubSubscriptions, now: datetime | None = None, *, days: int = GRACE_DAYS) -> bool:
    """
    Razorpay has stopped retrying a renewal: the club keeps its plan for
    `days` more, then the sweep moves it to Free. Returns whether a grace
    began (False: one was already running -- it is lengthened, never cut).

    Only for a subscription that was paid for and is now past due; a checkout
    or trial that never charged has nothing to keep (`is_entitled`).
    """
    now = now or _now()
    if _status(subscription.status) != SubscriptionStatus.past_due or subscription.activated_at is None:
        return False
    ends = now + timedelta(days=days)
    if subscription.halted_at is not None and subscription.grace_ends_at is not None:
        if subscription.grace_ends_at < ends:
            subscription.grace_ends_at = ends
        return False
    subscription.halted_at = now
    subscription.grace_ends_at = ends
    subscription.grace_reminded_at = None
    return True


def clear_grace(subscription: ClubSubscriptions) -> bool:
    """Paid, resumed or reactivated: the grace is over. Returns whether one was running."""
    running = subscription.halted_at is not None or subscription.grace_ends_at is not None
    subscription.halted_at = None
    subscription.grace_ends_at = None
    subscription.grace_reminded_at = None
    return running


def extend_grace(subscription: ClubSubscriptions, days: int, now: datetime | None = None) -> None:
    """An admin gives a past-due club `days` more: from the grace's end, or
    from now when it has already run out. A past-due club with no grace yet
    (Razorpay still retrying) gets one starting now."""
    now = now or _now()
    if subscription.grace_ends_at is None:
        subscription.halted_at = subscription.halted_at or now
        subscription.grace_ends_at = now + timedelta(days=days)
        subscription.grace_reminded_at = None
        return
    base = max(subscription.grace_ends_at, now)
    subscription.grace_ends_at = base + timedelta(days=days)
    if subscription.grace_ends_at - now > GRACE_REMINDER:
        # A new end far enough off deserves its own "two days left".
        subscription.grace_reminded_at = None


def end_now(subscription: ClubSubscriptions, now: datetime | None = None) -> str:
    """
    Take the plan away now: back to an admin's grant from before the checkout
    when `previous_state` holds one that would still stand, otherwise
    cancelled (so Free). Returns "restored" or "cancelled".
    """
    now = now or _now()
    prev = subscription.previous_state or {}
    if prev.get("plan_id") and not prev.get("razorpay_subscription_id") and previous_entitled(subscription, now):
        revert_checkout(subscription)
        return "restored"
    subscription.cancel_at_period_end = False
    subscription.cancel_requested_at = subscription.cancel_requested_at or now
    subscription.pending_since = None
    subscription.previous_state = None
    mark_cancelled(subscription)
    return "cancelled"
