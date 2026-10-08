"""Plans, their limits and features, and what each club is subscribed to.

This is the keystone domain: plan limits and feature flags gate the whole
Clubs app, so everything Pro-only depends on `resolve_entitlements` finding a
row here.

Four tables rather than one wide one, deliberately:

- `plans` is the catalogue -- the thing an admin edits and a club picks from.
- `plan_limits` is one row per plan with ten numeric columns. NULL means
  unlimited, which is why they are nullable integers and not zeroes.
- `plan_features` is one row per plan with the boolean flags. Columns rather
  than a key/value table because every read resolves all of them at once, and
  a missing row must not silently mean "no features" -- it means the plan is
  misconfigured, and the resolver says so.
- `club_subscriptions` is the link, carrying the billing state that Razorpay
  owns and we mirror.

Money is stored in paise as an integer. Floats cannot represent ₹1,499.00
exactly, and a subscription price that drifts by a hundredth of a rupee a
month is the kind of bug that surfaces in an annual reconciliation.
"""

from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class PlanVisibility(PyEnum):
    #: Offered to clubs on the pricing page.
    public = "public"
    #: Reachable only by an admin assigning it. Used for bespoke deals.
    hidden = "hidden"
    #: Withdrawn from sale. Existing subscribers keep it; nobody new gets it.
    legacy = "legacy"


class BillingCycle(PyEnum):
    monthly = "monthly"
    yearly = "yearly"


class SubscriptionStatus(PyEnum):
    trial = "trial"
    active = "active"
    #: A charge failed. The club keeps access while Razorpay retries.
    past_due = "past_due"
    cancelled = "cancelled"


class Plans(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: Stable machine name (`free`, `growth`, `pro`). Referenced in code and
    #: in Razorpay notes, so it never changes once subscribers exist.
    key = Column(String(40), nullable=False, unique=True, index=True)
    name = Column(String(80), nullable=False)
    tagline = Column(String(160), nullable=True)

    price_monthly_paise = Column(Integer, nullable=False, default=0)
    price_yearly_paise = Column(Integer, nullable=False, default=0)
    trial_days = Column(Integer, nullable=False, default=0)

    visibility = Column(
        Enum(PlanVisibility),
        nullable=False,
        default=PlanVisibility.hidden,
        server_default="hidden",
    )
    #: Display order on the pricing page, low to high.
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")

    #: Razorpay's own plan ids, one per cycle. Null until the plan is synced;
    #: a paid plan cannot be subscribed to until both exist.
    razorpay_plan_id_monthly = Column(String(64), nullable=True)
    razorpay_plan_id_yearly = Column(String(64), nullable=True)

    limits = relationship(
        "PlanLimits", uselist=False, back_populates="plan", cascade="all, delete-orphan"
    )
    features = relationship(
        "PlanFeatures", uselist=False, back_populates="plan", cascade="all, delete-orphan"
    )
    subscriptions = relationship("ClubSubscriptions", back_populates="plan")

    __table_args__ = (
        # A negative price is never intended and would be charged.
        CheckConstraint("price_monthly_paise >= 0", name="ck_plans_monthly_non_negative"),
        CheckConstraint("price_yearly_paise >= 0", name="ck_plans_yearly_non_negative"),
        CheckConstraint("trial_days >= 0", name="ck_plans_trial_non_negative"),
    )


#: The ten limit columns, NULL meaning unlimited. Named here as well as on the
#: model so the API, the resolver and the admin form all iterate one list.
LIMIT_KEYS = (
    "events_per_month",
    "registrations_per_month",
    "registrations_per_event",
    "ticket_types_per_event",
    "coupons_per_event",
    "merch_items_per_event",
    "committee_seats",
    "storage_gb",
    "announcements_per_month",
    "website_templates",
)


class PlanLimits(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "plan_limits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(
        Integer, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    # NULL is unlimited throughout. Zero is a real value meaning "none
    # allowed", so the two must stay distinguishable -- a default of 0 here
    # would silently forbid everything on a plan that meant to allow it.
    events_per_month = Column(Integer, nullable=True)
    registrations_per_month = Column(Integer, nullable=True)
    registrations_per_event = Column(Integer, nullable=True)
    ticket_types_per_event = Column(Integer, nullable=True)
    coupons_per_event = Column(Integer, nullable=True)
    merch_items_per_event = Column(Integer, nullable=True)
    committee_seats = Column(Integer, nullable=True)
    storage_gb = Column(Integer, nullable=True)
    announcements_per_month = Column(Integer, nullable=True)
    website_templates = Column(Integer, nullable=True)

    plan = relationship("Plans", back_populates="limits")


#: Feature flags, grouped the way the pricing page and the admin editor show
#: them. The groups carry no behaviour -- they are presentation -- but keeping
#: them here means one list to add a flag to.
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "every_event": (
        "tickets",
        "certificates",
        "checkin",
        "reviews",
        "threads",
        "regform",
        "waitlist",
    ),
    "grow": (
        "coupons",
        "links",
        "merch",
        "swag",
        "quiz",
        "qa",
        "polls",
        "networking",
    ),
    "competitions": (
        "teams",
        "judging",
        "rounds",
        "badges",
        "sessions",
        "report",
    ),
    "club": (
        "website",
        "domain",
        "nobadge",
        "finance",
        "roles",
        "tfa",
        "payouts",
        "sso",
        "api",
    ),
}

FEATURE_KEYS = tuple(key for keys in FEATURE_GROUPS.values() for key in keys)


def _flag() -> Column:
    """A feature flag. Off by default: a new flag must be granted, never
    assumed -- adding one should not hand every existing plan a feature its
    subscribers have not paid for."""
    return Column(Boolean, nullable=False, default=False, server_default="false")


class PlanFeatures(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "plan_features"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(
        Integer, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    # every_event
    tickets = _flag()
    certificates = _flag()
    checkin = _flag()
    reviews = _flag()
    threads = _flag()
    regform = _flag()
    waitlist = _flag()

    # grow
    coupons = _flag()
    links = _flag()
    merch = _flag()
    swag = _flag()
    quiz = _flag()
    qa = _flag()
    polls = _flag()
    networking = _flag()

    # competitions
    teams = _flag()
    judging = _flag()
    rounds = _flag()
    badges = _flag()
    sessions = _flag()
    report = _flag()

    # club
    website = _flag()
    domain = _flag()
    nobadge = _flag()
    finance = _flag()
    roles = _flag()
    tfa = _flag()
    payouts = _flag()
    sso = _flag()
    api = _flag()

    plan = relationship("Plans", back_populates="features")


class ClubSubscriptions(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "club_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(
        Integer, ForeignKey("clubs.id"), nullable=False, index=True
    )
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False, index=True)

    cycle = Column(Enum(BillingCycle), nullable=False, default=BillingCycle.monthly)
    status = Column(
        Enum(SubscriptionStatus),
        nullable=False,
        default=SubscriptionStatus.active,
        server_default="active",
    )

    current_period_end = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: What this subscription contributes to monthly recurring revenue, in
    #: paise. Stored rather than derived because a club on a legacy plan keeps
    #: the price it signed up at, which the plan row no longer remembers.
    mrr_paise = Column(Integer, nullable=False, default=0, server_default="0")

    #: Razorpay's subscription id. Null for a plan that costs nothing and for
    #: a subscription an admin assigned by hand.
    razorpay_subscription_id = Column(
        String(64), nullable=True, unique=True, index=True
    )
    #: Why the last charge failed, verbatim from Razorpay, for the admin
    #: screen. Cleared on a successful charge.
    last_failure_reason = Column(String(255), nullable=True)

    plan = relationship("Plans", back_populates="subscriptions")
    club = relationship("Clubs")

    __table_args__ = (
        # One live subscription per club. `is_deleted` is in the key so a
        # cancelled-and-resubscribed club does not collide with its own
        # history -- the same shape the club/user link tables use.
        UniqueConstraint("club_id", "is_deleted", name="uq_club_subscription_live"),
        CheckConstraint("mrr_paise >= 0", name="ck_subscription_mrr_non_negative"),
    )
