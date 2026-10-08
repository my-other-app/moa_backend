from datetime import datetime

from pydantic import Field, model_validator

from app.api.subscriptions.models import (
    FEATURE_GROUPS,
    FEATURE_KEYS,
    LIMIT_KEYS,
    BillingCycle,
    PlanVisibility,
    SubscriptionStatus,
)
from app.core.response.base_model import CustomBaseModel


class PlanLimitsIn(CustomBaseModel):
    """The ten caps. Absent or null means unlimited, zero means none allowed.

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
    storage_gb: int | None = Field(None, ge=0)
    announcements_per_month: int | None = Field(None, ge=0)
    website_templates: int | None = Field(None, ge=0)


def _feature_fields() -> dict:
    return {key: (bool, False) for key in FEATURE_KEYS}


class PlanFeaturesIn(CustomBaseModel):
    """The thirty flags, all defaulting off.

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
    domain: bool = False
    nobadge: bool = False
    finance: bool = False
    roles: bool = False
    tfa: bool = False
    payouts: bool = False
    sso: bool = False
    api: bool = False


class PlanIn(CustomBaseModel):
    key: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-z0-9_]+$")
    name: str = Field(..., min_length=2, max_length=80)
    tagline: str | None = Field(None, max_length=160)
    price_monthly_paise: int = Field(0, ge=0)
    price_yearly_paise: int = Field(0, ge=0)
    trial_days: int = Field(0, ge=0, le=365)
    visibility: PlanVisibility = PlanVisibility.hidden
    sort_order: int = Field(0, ge=0)
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


class SubscriptionOut(CustomBaseModel):
    id: int
    club: PlanClubMin | None = None
    plan: PlanOut | None = None
    cycle: str
    status: str
    current_period_end: datetime | None = None
    mrr_paise: int = 0
    last_failure_reason: str | None = None
    razorpay_subscription_id: str | None = None


class SubscriptionTotals(CustomBaseModel):
    mrr_paise: int = 0
    arr_paise: int = 0
    active_subscriptions: int = 0
    trialing: int = 0
    past_due: int = 0
    cancelled: int = 0


class AssignPlanIn(CustomBaseModel):
    plan_id: int
    cycle: BillingCycle = BillingCycle.monthly
    status: SubscriptionStatus = SubscriptionStatus.active


class EntitlementsOut(CustomBaseModel):
    """What a club may do. The shape the Clubs app gates on."""

    plan_key: str | None = None
    plan_name: str | None = None
    status: str | None = None
    cycle: str | None = None
    current_period_end: datetime | None = None
    limits: dict = Field(default_factory=dict)
    features: dict = Field(default_factory=dict)
    #: False when the platform has no free plan configured, in which case
    #: everything is permitted -- see service._unlimited. Surfaced so the
    #: admin screen can warn rather than the product silently ungating.
    configured: bool = True


class StartSubscriptionIn(CustomBaseModel):
    plan_id: int
    cycle: BillingCycle = BillingCycle.monthly


class StartSubscriptionOut(CustomBaseModel):
    checkout_url: str | None = None
    razorpay_subscription_id: str
    status: str | None = None


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
