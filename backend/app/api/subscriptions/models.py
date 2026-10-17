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

from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
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
    #: Checkout opened, nothing confirmed by Razorpay yet. Grants nothing: the
    #: club keeps whatever it had before (see `previous_state`), or Free.
    pending = "pending"
    #: Razorpay confirmed the mandate and the plan has a trial the club has
    #: not used before. Entitled from the `authenticated` webhook on.
    trial = "trial"
    active = "active"
    #: A charge failed. The club keeps access while Razorpay retries -- but
    #: only if this subscription was ever paid for (`activated_at`).
    past_due = "past_due"
    #: Paused at Razorpay. Not charged, so not entitled.
    paused = "paused"
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

    #: The plan the pricing page highlights ("Most popular").
    featured = Column(Boolean, nullable=False, default=False, server_default="false")
    #: Priced on request: the pricing page says "Talk to us" instead of a
    #: price, and a club cannot start it at checkout.
    contact_sales = Column(Boolean, nullable=False, default=False, server_default="false")

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

#: The pricing page's feature list: each flag whose feature exists in the
#: product, worded the way the product says it, in the order a comparison
#: table reads (roughly the tier that first includes it).
#:
#: A flag with nothing built behind it yet (`UNBUILT_FEATURES`) stays in the
#: admin editor but is left out here, so the public page never sells something
#: a club cannot use. When one is built, add it here; the public list follows.
FEATURE_CATALOG: tuple[tuple[str, str], ...] = (
    ("checkin", "Check-in at the door"),
    ("certificates", "Certificates"),
    ("regform", "Custom registration questions"),
    ("reviews", "Ratings and reviews"),
    ("threads", "Threads to your followers"),
    ("tickets", "Paid tickets with Razorpay"),
    ("website", "Club website"),
    ("quiz", "Live room: quiz"),
    ("polls", "Live room: polls"),
    ("qa", "Live room: Q&A"),
    ("networking", "Attendee networking"),
    ("links", "Tracking links"),
    ("report", "Post-event reports"),
    ("merch", "Merchandise store"),
    ("swag", "Swag handouts at the door"),
    ("badges", "ID cards and badge printing"),
    ("roles", "Team roles and access"),
    ("teams", "Competitions: teams and submissions"),
    ("judging", "Competitions: judging"),
    ("rounds", "Competitions: rounds and leaderboard"),
    ("finance", "Finance and expenses"),
)

#: Flags in the editor with no feature behind them yet: waitlist, coupons,
#: per-session check-in, custom domain, removing the "Made with MyOtherApp"
#: footer, enforced 2FA for a team, next-day payouts, student SSO, API access.
UNBUILT_FEATURES = frozenset(FEATURE_KEYS) - {key for key, _ in FEATURE_CATALOG}


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

    # The billing state machine (see service.is_entitled). Each timestamp is
    # when Razorpay -- or an admin assigning the plan -- confirmed that step.
    #: When this checkout was opened; a `pending` older than a day falls back.
    pending_since = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The `subscription.authenticated` webhook: the club authorised the mandate.
    authenticated_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The first `activated`/`charged`: this subscription has been paid for.
    activated_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When the club's trial began. Never cleared: a club gets one trial, ever.
    trial_started_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When the trial granted at checkout ends (null: no trial was granted).
    trial_ends_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The club cancelled; it keeps access until `current_period_end`.
    cancel_at_period_end = Column(Boolean, nullable=False, default=False, server_default="false")
    cancel_requested_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: What the club had before this checkout, restored if the checkout is
    #: abandoned (stale after a day) or cancelled before it was paid.
    previous_state = Column(JSON, nullable=True)

    # Grace (service.start_grace): Razorpay gave up retrying a renewal on a
    # subscription that had been paid for. The club stays `past_due` and keeps
    # its plan until `grace_ends_at`, then the hourly sweep moves it to Free.
    # A charge, a resume or an activation clears all three.
    #: When the grace began: Razorpay's `subscription.halted`, or an admin
    #: starting one by hand.
    halted_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When access ends unless the club pays. Past it, `is_entitled` is false
    #: whether or not the sweep has run.
    grace_ends_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When the "two days left" reminder went (club and admin).
    grace_reminded_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When the "your trial ends on ..." mail went. Never cleared: one per club.
    trial_reminded_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    plan = relationship("Plans", back_populates="subscriptions")
    club = relationship("Clubs")

    __table_args__ = (
        # One live subscription per club. `is_deleted` is in the key so a
        # cancelled-and-resubscribed club does not collide with its own
        # history -- the same shape the club/user link tables use.
        UniqueConstraint("club_id", "is_deleted", name="uq_club_subscription_live"),
        CheckConstraint("mrr_paise >= 0", name="ck_subscription_mrr_non_negative"),
    )


class SubscriptionEmails(AbstractSQLModel):
    """Each billing mail a club was sent (subscriptions.notices).

    The unique key is what makes "at most once" hold when two webhooks race:
    a mail is recorded before it is sent, and the second insert of the same
    (subscription, kind, episode) finds the first and sends nothing.

    `episode` names what the mail is about, so the same kind can be sent again
    for something new: the Razorpay subscription for a welcome or a trial
    reminder, the grace's start for grace mails, the failed payment for a
    failure.
    """

    __tablename__ = "subscription_emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscription_id = Column(
        Integer, ForeignKey("club_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    kind = Column(String(40), nullable=False)
    episode = Column(String(160), nullable=False)
    recipient = Column(String(254), nullable=True)
    created_at = Column(
        TZAwareDateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint("subscription_id", "kind", "episode", name="uq_subscription_email_once"),
    )
