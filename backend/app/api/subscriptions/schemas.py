from datetime import datetime, timezone

from pydantic import Field, model_validator

from app.api.subscriptions.models import (
    FEATURE_CATALOG,
    FEATURE_GROUPS,
    FEATURE_KEYS,
    LIMIT_KEYS,
    BillingCycle,
    PlanVisibility,
    SubscriptionStatus,
)
from app.core.response.base_model import CustomBaseModel


class PlanLimitsIn(CustomBaseModel):
    """The caps. Absent or null means unlimited, zero means none allowed.

    Every field is optional so a caller can send only what it changes, but the
    handler treats an omitted key as unlimited on create -- see the router,
    which distinguishes the two with `exclude_unset`.
    """

    events_per_month: int | None = Field(None, ge=0)
    registrations_per_month: int | None = Field(None, ge=0)
    registrations_per_event: int | None = Field(None, ge=0)
    ticket_types_per_event: int | None = Field(None, ge=0)
    coupons_per_event: int | None = Field(None, ge=0)
    merch_items_per_event: int | None = Field(None, ge=0)
    committee_seats: int | None = Field(None, ge=0)
    announcements_per_month: int | None = Field(None, ge=0)
    website_templates: int | None = Field(None, ge=0)
    ai_actions_per_month: int | None = Field(None, ge=0)
    #: Emails and WhatsApp messages a club sends in a month (app/api/message_quota).
    emails_per_month: int | None = Field(None, ge=0)
    whatsapp_per_month: int | None = Field(None, ge=0)


def _feature_fields() -> dict:
    return {key: (bool, False) for key in FEATURE_KEYS}


class PlanFeaturesIn(CustomBaseModel):
    """The flags, all defaulting off.

    Declared explicitly rather than generated, because a generated model gives
    no autocomplete, no mypy and an OpenAPI schema nobody can read -- and this
    is the payload an admin screen posts.
    """

    # every_event
    tickets: bool = False
    certificates: bool = False
    checkin: bool = False
    reviews: bool = False
    threads: bool = False
    regform: bool = False
    waitlist: bool = False
    # grow
    coupons: bool = False
    links: bool = False
    merch: bool = False
    swag: bool = False
    quiz: bool = False
    qa: bool = False
    polls: bool = False
    networking: bool = False
    # competitions
    teams: bool = False
    judging: bool = False
    rounds: bool = False
    badges: bool = False
    sessions: bool = False
    report: bool = False
    # club
    website: bool = False
    custom_domain: bool = False
    nobadge: bool = False
    finance: bool = False
    roles: bool = False
    tfa: bool = False
    payouts: bool = False
    sso: bool = False
    api: bool = False
    ai_assist: bool = False
    ai_byok: bool = False
    mcp: bool = False


class PlanIn(CustomBaseModel):
    key: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-z0-9_]+$")
    name: str = Field(..., min_length=2, max_length=80)
    tagline: str | None = Field(None, max_length=160)
    price_monthly_paise: int = Field(0, ge=0)
    price_yearly_paise: int = Field(0, ge=0)
    trial_days: int = Field(0, ge=0, le=365)
    visibility: PlanVisibility = PlanVisibility.hidden
    sort_order: int = Field(0, ge=0)
    #: Omitted means "leave as it is" on update and false on create. The
    #: admin editor predates both and sends the whole plan on every save, so
    #: a default of false would quietly unfeature a plan whenever someone
    #: corrected its tagline.
    featured: bool | None = None
    contact_sales: bool | None = None
    #: What each message past the monthly limit costs, in paise. Omitted
    #: means "leave as it is" on update, and the owner's defaults (10 and
    #: 100: ₹0.10 an email, ₹1 a WhatsApp message) on create.
    email_overage_paise: int | None = Field(None, ge=0, le=100_000)
    whatsapp_overage_paise: int | None = Field(None, ge=0, le=100_000)
    limits: PlanLimitsIn = Field(default_factory=PlanLimitsIn)
    features: PlanFeaturesIn = Field(default_factory=PlanFeaturesIn)

    @model_validator(mode="after")
    def _yearly_is_a_discount(self) -> "PlanIn":
        # Mirrored in the service so a caller that bypasses this schema still
        # cannot create one; stated here so the admin form gets a 422 with a
        # readable message instead of a 500 from deeper down.
        if self.price_yearly_paise > self.price_monthly_paise * 12:
            raise ValueError(
                "The yearly price is more than twelve times the monthly price."
            )
        return self


class PlanOut(CustomBaseModel):
    id: int
    key: str
    name: str
    tagline: str | None = None
    price_monthly_paise: int = 0
    price_yearly_paise: int = 0
    trial_days: int = 0
    visibility: str
    sort_order: int = 0
    featured: bool = False
    contact_sales: bool = False
    #: Each email / WhatsApp message past the monthly limit, in paise.
    email_overage_paise: int = 0
    whatsapp_overage_paise: int = 0
    #: Whether the plan can actually be billed. A paid plan with no Razorpay
    #: plan id cannot be subscribed to, and the admin screen says so rather
    #: than letting someone publish it and find out at checkout.
    billable: bool = False
    subscriber_count: int = 0
    limits: dict = Field(default_factory=dict)
    features: dict = Field(default_factory=dict)
    #: The flags arranged the way the editor groups them, so the UI does not
    #: hardcode a copy of the grouping that can drift from the backend's.
    feature_groups: dict = Field(default_factory=lambda: {k: list(v) for k, v in FEATURE_GROUPS.items()})


class PlanClubMin(CustomBaseModel):
    id: int
    name: str


class PlanBrief(CustomBaseModel):
    key: str
    name: str


class EnterpriseTrialOut(CustomBaseModel):
    """An Enterprise trial an admin granted, laid over what the club has."""

    plan: PlanBrief | None = None
    started_at: datetime | None = None
    ends_at: datetime


class SubscriptionOut(CustomBaseModel):
    id: int
    club: PlanClubMin | None = None
    plan: PlanOut | None = None
    cycle: str
    #: pending | trial | active | past_due | paused | cancelled
    status: str
    current_period_end: datetime | None = None
    mrr_paise: int = 0
    last_failure_reason: str | None = None
    razorpay_subscription_id: str | None = None
    #: Whether this subscription grants its plan right now (service.is_entitled).
    entitled: bool = False
    #: The club cancelled; access runs until `current_period_end`.
    cancel_at_period_end: bool = False
    cancel_requested_at: datetime | None = None
    #: When the trial granted at checkout ends (null: no trial).
    trial_ends_at: datetime | None = None
    #: While pending: when checkout opened, and when it falls back unpaid.
    pending_since: datetime | None = None
    checkout_expires_at: datetime | None = None
    #: Grace: Razorpay stopped retrying a failed renewal at `halted_at` (or an
    #: admin started a grace then); the club keeps its plan until
    #: `grace_ends_at`, then moves to Free. Null when no grace is running.
    halted_at: datetime | None = None
    grace_ends_at: datetime | None = None
    #: Past due with a grace that has not run out.
    in_grace: bool = False
    #: A plan MyOtherApp gives the club for nothing, until `comp_ends_at`
    #: (null: no end). Nothing is charged; the club cannot start a checkout
    #: while it runs (409 COMPLIMENTARY_PLAN).
    complimentary: bool = False
    comp_ends_at: datetime | None = None
    #: The free trial (Pro, no card) is running: the club has `trial_plan`
    #: until `trial_ends_at`, whatever `plan` and `status` say.
    free_trial: bool = False
    trial_plan: PlanBrief | None = None
    #: A paid plan picked during the free trial: `plan` here, billed by
    #: Razorpay from this date (the trial's end). Null otherwise.
    billing_starts_at: datetime | None = None
    #: An Enterprise trial running, laid over everything above; null when none.
    enterprise_trial: EnterpriseTrialOut | None = None


class PlanUserMin(CustomBaseModel):
    id: int
    name: str | None = None


class AdminSubscriptionOut(SubscriptionOut):
    """The admin's view: also why a comp was given, and by whom. Neither is
    ever shown to the club."""

    comp_reason: str | None = None
    #: The admin who last assigned the plan (a comp or a deal by hand).
    granted_by: PlanUserMin | None = None
    #: Whether this club has ever had a trial (the free trial is once per club).
    trial_used: bool = False
    trial_started_at: datetime | None = None
    #: The admin who granted the running Enterprise trial.
    enterprise_trial_by: PlanUserMin | None = None


class ClubPlanOut(CustomBaseModel):
    """One club's plan, for its page in the admin app: the row when it has
    one (null for a club on Free that never subscribed), and the plan it
    resolves to either way."""

    club: PlanClubMin
    plan_key: str | None = None
    plan_name: str | None = None
    subscription: AdminSubscriptionOut | None = None


class SubscriptionTotals(CustomBaseModel):
    mrr_paise: int = 0
    arr_paise: int = 0
    #: Trial, active and past due, complimentary plans not included.
    active_subscriptions: int = 0
    #: What MRR is made of: active, or past due within Razorpay's retries or
    #: the grace; never a trial or a complimentary plan.
    paying: int = 0
    #: Complimentary plans still running. Worth nothing in MRR.
    complimentary: int = 0
    trialing: int = 0
    past_due: int = 0
    cancelled: int = 0
    #: Checkouts Razorpay has not confirmed yet.
    pending: int = 0
    paused: int = 0
    #: Past due, Razorpay done retrying, grace still running (also counted in past_due).
    in_grace: int = 0
    #: Free Pro trials running (no card; also counted in trialing).
    free_trials: int = 0
    #: Enterprise trials an admin granted, running.
    enterprise_trials: int = 0


class RevenueCounts(CustomBaseModel):
    #: Active, or past due within Razorpay's retries or the grace. What MRR is made of.
    paying: int = 0
    #: Trials that grant their plan now.
    trialing: int = 0
    #: Complimentary plans still running: counted, never valued.
    complimentary: int = 0
    past_due: int = 0
    #: Past due, Razorpay done retrying, grace still running (also in past_due).
    in_grace: int = 0
    paused: int = 0
    #: Checkouts Razorpay has not confirmed.
    pending: int = 0
    #: Approved, live clubs on the default plan (no plan of their own that holds now).
    free_clubs: int = 0
    #: Approved, live clubs.
    total_clubs: int = 0


class RevenuePlanRow(CustomBaseModel):
    plan_id: int
    plan_key: str
    plan_name: str
    paying: int = 0
    trialing: int = 0
    complimentary: int = 0
    mrr_paise: int = 0


class RevenueCycleRow(CustomBaseModel):
    paying: int = 0
    mrr_paise: int = 0


class RevenueCollected(CustomBaseModel):
    """Gross payments Razorpay took, by when it took them (months in India).
    `refunded_paise` is every refund ever; subtract for net."""

    this_month_paise: int = 0
    last_month_paise: int = 0
    year_to_date_paise: int = 0
    all_time_paise: int = 0
    refunded_paise: int = 0


class RevenueMonth(CustomBaseModel):
    #: YYYY-MM, a calendar month in India.
    month: str
    collected_paise: int = 0
    #: Refunds of that month's payments.
    refunded_paise: int = 0
    #: Razorpay subscriptions whose first payment fell in the month.
    new_paying: int = 0
    #: Trials that began in the month.
    new_trials: int = 0
    #: Cancellations the admins were alerted to (club, admin, Razorpay, a
    #: grace running out); complimentary plans revoked are not counted.
    cancelled: int = 0
    #: Complimentary plans granted in the month.
    new_complimentary: int = 0


class RevenueRenewals(CustomBaseModel):
    #: What Razorpay is due to take in the next 30 days from active, renewing
    #: subscriptions: each at what it last charged (or its price).
    next_30_days_paise: int = 0
    count: int = 0


class TicketSales(CustomBaseModel):
    this_month_paise: int = 0
    all_time_paise: int = 0
    note: str


class RevenueOut(CustomBaseModel):
    #: Paying subscriptions only; a yearly price counts a twelfth a month.
    mrr_paise: int = 0
    arr_paise: int = 0
    counts: RevenueCounts
    by_plan: list[RevenuePlanRow]
    by_cycle: dict[str, RevenueCycleRow]
    collected: RevenueCollected
    #: The last `months` months, oldest first, every month present.
    series: list[RevenueMonth]
    upcoming_renewals: RevenueRenewals
    #: For context only: never part of the figures above.
    ticket_sales: TicketSales


class ChargeClub(CustomBaseModel):
    id: int
    name: str
    slug: str | None = None


class ChargeOut(CustomBaseModel):
    id: int
    club: ChargeClub | None = None
    plan_name: str | None = None
    cycle: str | None = None
    amount_paise: int
    refunded_paise: int = 0
    #: captured | refunded | partially_refunded
    status: str
    razorpay_payment_id: str
    charged_at: datetime


class ChargesPageOut(CustomBaseModel):
    items: list[ChargeOut]
    total: int = 0
    #: Every matching payment, not just this page; gross.
    sum_paise: int = 0
    refunded_paise: int = 0
    offset: int = 0
    limit: int = 20


class BackfillOut(CustomBaseModel):
    #: Razorpay subscriptions asked about.
    scanned: int = 0
    #: Payments added to the ledger.
    inserted: int = 0
    #: Payments already there whose refunds changed.
    updated: int = 0
    #: Subscriptions Razorpay would not answer for; safe to run again.
    failed: int = 0


class EnterpriseTrialIn(CustomBaseModel):
    #: How long the Enterprise trial runs, from now. Granting again replaces the end.
    days: int = Field(30, ge=1, le=365)


class EndTrialIn(CustomBaseModel):
    #: Which trial to end: "enterprise" or "free". Omitted: the Enterprise
    #: trial when one is running, else the free trial.
    trial: str | None = Field(None, pattern=r"^(enterprise|free)$")


class ExtendGraceIn(CustomBaseModel):
    #: Days added to the grace's end (or, with no grace yet, from now).
    days: int = Field(..., ge=1, le=30)


class AdminCancelIn(CustomBaseModel):
    #: True: the club keeps what it paid for until the period ends. False: now.
    at_period_end: bool = True


class AssignPlanIn(CustomBaseModel):
    """
    Put a club on a plan without charging it.

    `complimentary` (the default): the plan is given for nothing -- active,
    worth 0, never revenue -- with a `reason` (required, up to 200
    characters, for the admins only) until `ends_at` (in the future; null for
    no end). `status` is ignored: a comp is always active. A club whose
    Razorpay subscription can still charge it is refused (409
    LIVE_SUBSCRIPTION) unless `cancel_razorpay` is true, which stops it at
    Razorpay first.

    `complimentary: false` is a deal invoiced by hand: the plan at its price
    in MRR, in the `status` given. `reason` and `ends_at` do not apply.
    """

    plan_id: int
    cycle: BillingCycle = BillingCycle.monthly
    status: SubscriptionStatus = SubscriptionStatus.active
    complimentary: bool = True
    reason: str | None = Field(None, max_length=200)
    ends_at: datetime | None = None
    cancel_razorpay: bool = False

    @model_validator(mode="after")
    def _a_state_an_admin_can_grant(self) -> "AssignPlanIn":
        # pending and paused are Razorpay's to set: one is a checkout nobody
        # has paid, the other a subscription Razorpay stopped charging.
        if SubscriptionStatus(self.status) in (SubscriptionStatus.pending, SubscriptionStatus.paused):
            raise ValueError("An admin can grant trial, active, past_due or cancelled.")
        if self.complimentary:
            self.reason = (self.reason or "").strip()
            if not self.reason:
                raise ValueError("Say why this plan is complimentary (reason).")
            if self.ends_at is not None:
                ends = self.ends_at if self.ends_at.tzinfo else self.ends_at.replace(tzinfo=timezone.utc)
                if ends <= datetime.now(timezone.utc):
                    raise ValueError("ends_at must be in the future, or null for no end.")
        elif self.ends_at is not None:
            raise ValueError("ends_at is for a complimentary plan.")
        return self


class EntitlementsOut(CustomBaseModel):
    """What a club may do. The shape the Clubs app gates on."""

    plan_key: str | None = None
    plan_name: str | None = None
    status: str | None = None
    cycle: str | None = None
    current_period_end: datetime | None = None
    #: Every limit; emails_per_month and whatsapp_per_month are an
    #: Enterprise club's own when an admin set them (app/api/message_quota).
    limits: dict = Field(default_factory=dict)
    #: What each email / WhatsApp message past the month's limit costs, in paise.
    email_overage_paise: int = 0
    whatsapp_overage_paise: int = 0
    features: dict = Field(default_factory=dict)
    #: False when the platform has no free plan configured, in which case
    #: everything is permitted -- see service._unlimited. Surfaced so the
    #: admin screen can warn rather than the product silently ungating.
    configured: bool = True
    #: What the club picked on the sign-up form, so the dashboard can
    #: preselect it once the club is approved. Null when it picked nothing.
    requested_plan: str | None = None
    requested_cycle: str | None = None
    #: The club's own subscription state, whatever grants the plan above:
    #: e.g. "pending" while a checkout waits on Razorpay. Null with none.
    subscription_status: str | None = None
    #: Counted against the per-club limits: events_per_month,
    #: registrations_per_month, committee_seats, announcements_per_month.
    usage: dict[str, int] = Field(default_factory=dict)
    #: Whether the plan is enforced at all (the PLAN_ENFORCEMENT switch). When
    #: false every gate lets everything through, whatever `features` says.
    enforced: bool = True
    #: Events created before this keep their paid ticketing and are not
    #: capped on registrations (subscriptions.gates.grandfathered).
    grandfathered_before: datetime | None = None
    #: The plan above is given by MyOtherApp for nothing, until
    #: `comp_ends_at` (null: no end). Checkout is refused while it runs.
    complimentary: bool = False
    comp_ends_at: datetime | None = None
    #: The trial the plan above comes from, or null: the free trial
    #: ("free_trial": Pro, no card) or an admin's ("enterprise_trial").
    trial: "TrialOut | None" = None
    #: A paid plan picked during the free trial, billed from its end.
    chosen_plan: "ChosenPlanOut | None" = None
    #: The club had its free trial and it is over, and it is on the default
    #: (Free) plan now: the dashboard says what Free limits.
    trial_ended: bool = False
    #: How long the free trial every new club gets is, for the copy.
    free_trial_days: int = 14


class TrialOut(CustomBaseModel):
    #: "free_trial" | "enterprise_trial"
    kind: str
    plan_key: str
    plan_name: str
    ends_at: datetime
    #: Whole days left, rounded up (0 on the last hours of the last day is 1).
    days_left: int


class ChosenPlanOut(CustomBaseModel):
    plan_key: str
    plan_name: str
    cycle: str
    #: What Razorpay charges each cycle, in paise.
    price_paise: int = 0
    #: When billing starts: the free trial's end.
    billing_starts_at: datetime
    #: "pending" until the club authorises the mandate at Razorpay, then "trial".
    status: str


class StartSubscriptionIn(CustomBaseModel):
    """The plan by id or by key (the key the pricing page shows); one of the two."""

    plan_id: int | None = None
    plan_key: str | None = Field(None, max_length=40)
    cycle: BillingCycle = BillingCycle.monthly

    @model_validator(mode="after")
    def _one_plan(self) -> "StartSubscriptionIn":
        if self.plan_id is None and not (self.plan_key or "").strip():
            raise ValueError("Name the plan: plan_id or plan_key.")
        return self


class StartSubscriptionOut(CustomBaseModel):
    checkout_url: str | None = None
    razorpay_subscription_id: str
    #: Always "pending": nothing is granted until Razorpay confirms.
    status: str = "pending"
    #: What Razorpay called the new subscription ("created").
    razorpay_status: str | None = None
    plan_key: str
    cycle: str
    #: The trial this checkout carries: the plan's, or 0 when the club has
    #: had a trial before; during the free trial, the days left of it.
    trial_days: int = 0
    #: When Razorpay first charges: the free trial's end for a plan picked
    #: during it; null when it charges as soon as the mandate is authorised.
    billing_starts_at: datetime | None = None
    #: When the checkout link stops working and the club falls back unpaid.
    expires_at: datetime


# ---------------------------------------------------------------------------
# The public pricing page
# ---------------------------------------------------------------------------


class PublicPlanFeature(CustomBaseModel):
    key: str
    label: str
    included: bool


class PublicPlan(CustomBaseModel):
    """One plan as the pricing page shows it. No Razorpay ids or subscriber
    counts: nothing a visitor has any use for. The id is there so checkout can
    name the plan either way."""

    id: int
    key: str
    name: str
    tagline: str | None = None
    price_monthly_paise: int = 0
    price_yearly_paise: int = 0
    trial_days: int = 0
    #: Priced on request: show "Talk to us" rather than the (zero) price.
    contact_sales: bool = False
    #: The plan to highlight ("Most popular").
    featured: bool = False
    #: What each email and WhatsApp message past the monthly limit costs,
    #: in paise (`limits.emails_per_month`, `limits.whatsapp_per_month`).
    email_overage_paise: int = 0
    whatsapp_overage_paise: int = 0
    #: Every limit (LIMIT_KEYS); null is unlimited, 0 is none allowed.
    limits: dict[str, int | None]
    #: The whole public catalog in a fixed order, each marked included or
    #: not, so every plan has the same rows and a comparison table lines up.
    features: list[PublicPlanFeature]


class PublicTrialOffer(CustomBaseModel):
    """The free trial every new club starts with."""

    days: int
    #: The plan it gives (Pro).
    plan_key: str
    plan_name: str
    card_required: bool = False
    #: The plan a club is on when the trial ends without a paid plan picked.
    then_plan_key: str = "free"


class PublicPlansOut(CustomBaseModel):
    currency: str = "INR"
    #: The free trial: null when the platform has no plan to give it on.
    trial: PublicTrialOffer | None = None
    #: How long an Enterprise trial runs, granted by the team after a call.
    enterprise_trial_days: int | None = None
    #: How many months a yearly plan saves, when every paid plan's yearly
    #: price is the same whole number of monthly prices ("2 months free").
    #: Null when they differ or none is discounted.
    yearly_months_free: int | None = None
    plans: list[PublicPlan]


def public_plan_dict(plan) -> dict:
    return {
        "id": plan.id,
        "key": plan.key,
        "name": plan.name,
        "tagline": plan.tagline,
        "price_monthly_paise": plan.price_monthly_paise,
        "price_yearly_paise": plan.price_yearly_paise,
        "trial_days": plan.trial_days,
        "contact_sales": bool(plan.contact_sales),
        "featured": bool(plan.featured),
        "email_overage_paise": int(plan.email_overage_paise or 0),
        "whatsapp_overage_paise": int(plan.whatsapp_overage_paise or 0),
        "limits": (
            {key: getattr(plan.limits, key) for key in LIMIT_KEYS}
            if plan.limits
            else {key: None for key in LIMIT_KEYS}
        ),
        "features": [
            {
                "key": key,
                "label": label,
                "included": bool(plan.features and getattr(plan.features, key)),
            }
            for key, label in FEATURE_CATALOG
        ],
    }


def yearly_months_free(plans) -> int | None:
    """
    The "N months free" a pricing page's monthly/yearly switch can promise.

    Only stated when it is true of every paid plan: one plan priced at eleven
    months and another at ten would make any single number wrong for one of
    them. Free and priced-on-request plans have nothing to discount.
    """
    found = set()
    for plan in plans:
        monthly, yearly = plan.price_monthly_paise, plan.price_yearly_paise
        if plan.contact_sales or monthly <= 0 or yearly <= 0:
            continue
        months, remainder = divmod(yearly, monthly)
        if remainder or months >= 12:
            return None
        found.add(12 - months)
    return found.pop() if len(found) == 1 else None


def plan_to_dict(plan, subscriber_count: int = 0) -> dict:
    """One place that turns a Plans row into the API shape."""
    return {
        "id": plan.id,
        "key": plan.key,
        "name": plan.name,
        "tagline": plan.tagline,
        "price_monthly_paise": plan.price_monthly_paise,
        "price_yearly_paise": plan.price_yearly_paise,
        "trial_days": plan.trial_days,
        "visibility": getattr(plan.visibility, "value", plan.visibility),
        "sort_order": plan.sort_order,
        "featured": bool(plan.featured),
        "contact_sales": bool(plan.contact_sales),
        "email_overage_paise": int(plan.email_overage_paise or 0),
        "whatsapp_overage_paise": int(plan.whatsapp_overage_paise or 0),
        "billable": _is_billable(plan),
        "subscriber_count": subscriber_count,
        "limits": (
            {key: getattr(plan.limits, key) for key in LIMIT_KEYS}
            if plan.limits
            else {key: None for key in LIMIT_KEYS}
        ),
        "features": (
            {key: bool(getattr(plan.features, key)) for key in FEATURE_KEYS}
            if plan.features
            else {key: False for key in FEATURE_KEYS}
        ),
        "feature_groups": {k: list(v) for k, v in FEATURE_GROUPS.items()},
    }


def subscription_dict(row, club=None, *, admin: bool = False, granter=None) -> dict:
    """One place that turns a ClubSubscriptions row into SubscriptionOut, or
    with `admin` into AdminSubscriptionOut. The club's own view never carries
    `comp_reason` or `granted_by`. `granter` is the Users row behind
    `granted_by`, when the caller has it loaded."""
    from app.api.subscriptions.service import (
        CHECKOUT_TTL,
        chosen_plan,
        enterprise_trial_live,
        free_trial_live,
        in_grace,
        is_entitled,
    )

    club = club if club is not None else getattr(row, "club", None)
    trial_plan = _loaded(row, "trial_plan")
    ent_plan = _loaded(row, "enterprise_trial_plan")
    out = {
        "id": row.id,
        "club": {"id": club.id, "name": club.name} if club else None,
        "plan": plan_to_dict(row.plan) if row.plan else None,
        "cycle": getattr(row.cycle, "value", row.cycle),
        "status": getattr(row.status, "value", row.status),
        "current_period_end": row.current_period_end,
        "mrr_paise": row.mrr_paise,
        "last_failure_reason": row.last_failure_reason,
        "razorpay_subscription_id": row.razorpay_subscription_id,
        "entitled": is_entitled(row),
        "cancel_at_period_end": bool(row.cancel_at_period_end),
        "cancel_requested_at": row.cancel_requested_at,
        "trial_ends_at": row.trial_ends_at,
        "pending_since": row.pending_since,
        "checkout_expires_at": row.pending_since + CHECKOUT_TTL if row.pending_since else None,
        "halted_at": row.halted_at,
        "grace_ends_at": row.grace_ends_at,
        "in_grace": in_grace(row),
        "complimentary": bool(row.complimentary),
        "comp_ends_at": row.comp_ends_at if row.complimentary else None,
        "free_trial": free_trial_live(row),
        "trial_plan": _brief(trial_plan) if free_trial_live(row) else None,
        "billing_starts_at": row.trial_ends_at if chosen_plan(row) else None,
        "enterprise_trial": (
            {"plan": _brief(ent_plan), "started_at": row.enterprise_trial_started_at,
             "ends_at": row.enterprise_trial_ends_at}
            if enterprise_trial_live(row)
            else None
        ),
    }
    if admin:
        out["comp_reason"] = row.comp_reason
        out["granted_by"] = (
            {"id": granter.id, "name": granter.full_name or granter.username or granter.email}
            if granter is not None
            else ({"id": row.granted_by, "name": None} if row.granted_by else None)
        )
        out["trial_started_at"] = row.trial_started_at
        out["trial_used"] = row.trial_started_at is not None
        ent_by = _loaded(row, "enterprise_trial_granter")
        out["enterprise_trial_by"] = (
            {"id": ent_by.id, "name": ent_by.full_name or ent_by.username or ent_by.email}
            if ent_by is not None and enterprise_trial_live(row)
            else ({"id": row.enterprise_trial_by, "name": None}
                  if row.enterprise_trial_by and enterprise_trial_live(row) else None)
        )
    return out


def _loaded(row, name: str):
    """A relationship's value if it is already loaded, else None: reading an
    unloaded one would lazy-load, which an async session cannot do here."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm.base import NO_VALUE

    value = sa_inspect(row).attrs[name].loaded_value
    return None if value is NO_VALUE else value


def _brief(plan) -> dict | None:
    return {"key": plan.key, "name": plan.name} if plan is not None else None


def _is_billable(plan) -> bool:
    """A free plan is always billable (there is nothing to bill); a paid one
    needs the Razorpay plan for the cycles it charges."""
    if plan.price_monthly_paise == 0 and plan.price_yearly_paise == 0:
        return True
    if plan.price_monthly_paise > 0 and not plan.razorpay_plan_id_monthly:
        return False
    if plan.price_yearly_paise > 0 and not plan.razorpay_plan_id_yearly:
        return False
    return True


EntitlementsOut.model_rebuild()
