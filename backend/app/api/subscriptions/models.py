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

    #: What each message past the plan's monthly limit costs, in paise
    #: (app/api/message_quota): added to the club's next Razorpay invoice.
    #: The owner's defaults are ₹0.10 an email and ₹1.50 a WhatsApp message
    #: (₹1 until migration pricing_adjust).
    #: Retired with overage billing (migration message_credits): no longer
    #: charged or shown; prepaid credit packs replace them.
    email_overage_paise = Column(Integer, nullable=False, default=10, server_default="10")
    whatsapp_overage_paise = Column(Integer, nullable=False, default=150, server_default="150")

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
    subscriptions = relationship("ClubSubscriptions", back_populates="plan", foreign_keys="ClubSubscriptions.plan_id")

    __table_args__ = (
        # A negative price is never intended and would be charged.
        CheckConstraint("price_monthly_paise >= 0", name="ck_plans_monthly_non_negative"),
        CheckConstraint("price_yearly_paise >= 0", name="ck_plans_yearly_non_negative"),
        CheckConstraint("trial_days >= 0", name="ck_plans_trial_non_negative"),
        CheckConstraint("email_overage_paise >= 0", name="ck_plans_email_overage_non_negative"),
        CheckConstraint("whatsapp_overage_paise >= 0", name="ck_plans_whatsapp_overage_non_negative"),
    )


#: The limit columns, NULL meaning unlimited. Named here as well as on the
#: model so the API, the resolver and the admin form all iterate one list.
LIMIT_KEYS = (
    "events_per_month",
    "registrations_per_month",
    "registrations_per_event",
    "ticket_types_per_event",
    "coupons_per_event",
    "merch_items_per_event",
    "committee_seats",
    "announcements_per_month",
    "website_templates",
    "ai_actions_per_month",
    "emails_per_month",
    "whatsapp_per_month",
    "whatsapp_marketing_per_month",
)

#: While a plan is given as the free trial (service.FREE_TRIAL_PLAN_KEY),
#: these allowances replace the plan's own: the trial keeps the plan's
#: features with smaller monthly allowances. Limit key -> its trial column
#: on plan_limits (null: the plan's own allowance).
TRIAL_LIMIT_COLUMNS = {
    "emails_per_month": "trial_emails_per_month",
    "whatsapp_per_month": "trial_whatsapp_per_month",
    "whatsapp_marketing_per_month": "trial_whatsapp_marketing_per_month",
    "ai_actions_per_month": "trial_ai_actions_per_month",
}


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
    #: No longer a limit (migration trial_pricing): nothing ever metered what
    #: a club stores, so it is not in LIMIT_KEYS, the API, the dashboards or
    #: the pricing page. The column stays, unread, because production migrates
    #: while the previous code still serves (and a rollback is code-only), and
    #: the shipped club_plans seed still writes it. Drop it in a later release.
    storage_gb = Column(Integer, nullable=True)
    announcements_per_month = Column(Integer, nullable=True)
    website_templates = Column(Integer, nullable=True)
    #: AI assist's actions a month on the platform's key (a draft, a piece of
    #: writing, a feedback summary each). A club's own key is not counted.
    ai_actions_per_month = Column(Integer, nullable=True)
    #: Every email and WhatsApp message a club sends to its people in a
    #: month in India (app/api/message_quota): tickets, certificates,
    #: reminders, announcements, campaigns, journey emails. Past it, on any
    #: plan, sends use the club's prepaid credits; with none left, what is
    #: not a ticket, certificate or payment message is held back.
    emails_per_month = Column(Integer, nullable=True)
    #: Through the platform's WhatsApp number only: a club's own number is
    #: paid to Meta by the club and not counted.
    whatsapp_per_month = Column(Integer, nullable=True)
    #: Of `whatsapp_per_month`, how many may be MARKETING (promotional)
    #: templates, which cost us about 7x a utility one (migration
    #: pricing_adjust: 20% of the allowance). Past it a promotional WhatsApp
    #: is held back on every plan; tickets, certificates and reminders
    #: (UTILITY) never are. Null: no separate cap.
    whatsapp_marketing_per_month = Column(Integer, nullable=True)

    # The free trial's own allowances (TRIAL_LIMIT_COLUMNS), used instead of
    # the ones above while this plan is given as the free trial. Null: the
    # plan's own.
    trial_emails_per_month = Column(Integer, nullable=True)
    trial_whatsapp_per_month = Column(Integer, nullable=True)
    trial_whatsapp_marketing_per_month = Column(Integer, nullable=True)
    trial_ai_actions_per_month = Column(Integer, nullable=True)

    plan = relationship("Plans", back_populates="limits")


def trial_limits_of(limits: "PlanLimits | None") -> dict:
    """The trial columns as {limit key: value or None (the plan's own)}."""
    return {key: (getattr(limits, column) if limits is not None else None) for key, column in TRIAL_LIMIT_COLUMNS.items()}


def trial_allowances(limits: "PlanLimits | None") -> dict:
    """What the free trial on this plan allows of each TRIAL_LIMIT_COLUMNS
    limit: its trial column, or the plan's own when that is null."""
    own = trial_limits_of(limits)
    return {
        key: own[key] if own[key] is not None else (getattr(limits, key) if limits is not None else None)
        for key in TRIAL_LIMIT_COLUMNS
    }


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
        "custom_domain",
        "nobadge",
        "finance",
        "roles",
        "tfa",
        "payouts",
        "sso",
        "api",
        "ai_assist",
        "ai_byok",
        "mcp",
        "whatsapp_own",
        "mailbox",
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
    ("waitlist", "Waiting lists for sold-out tickets"),
    ("coupons", "Discount codes"),
    ("website", "Club website"),
    ("quiz", "Live room: quiz"),
    ("polls", "Live room: polls"),
    ("qa", "Live room: Q&A"),
    ("networking", "Attendee networking"),
    ("links", "Tracking links"),
    ("report", "Post-event reports"),
    ("custom_domain", "Your own domain for the club website"),
    ("merch", "Merchandise store"),
    ("swag", "Swag handouts at the door"),
    ("badges", "ID cards and badge printing"),
    ("roles", "Team roles and access"),
    ("teams", "Competitions: teams and submissions"),
    ("judging", "Competitions: judging"),
    ("rounds", "Competitions: rounds and leaderboard"),
    ("finance", "Finance and expenses"),
    ("ai_assist", "AI assist: event drafts, writing and feedback summaries"),
    ("ai_byok", "AI assist with your own AI key"),
    ("mcp", "Connect AI assistants (Claude, ChatGPT) to your club"),
    ("whatsapp_own", "Your own WhatsApp Business number: inbox, templates and quality"),
    ("mailbox", "A club mailbox: yourclub@mail.myotherapp.in, in the dashboard"),
)

#: Flags in the editor with no feature behind them yet:
#: per-session check-in, removing the "Made with MyOtherApp"
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
    # Was `domain`, a placeholder, until the feature was built (migration club_domains).
    custom_domain = _flag()
    nobadge = _flag()
    finance = _flag()
    roles = _flag()
    tfa = _flag()
    payouts = _flag()
    sso = _flag()
    api = _flag()
    #: AI assist on the platform's key, up to `ai_actions_per_month`.
    ai_assist = _flag()
    #: AI assist on the club's own key (it pays its provider): every plan has it.
    ai_byok = _flag()
    # A club's team connecting AI assistants over MCP (app/api/mcp), from migration mcp_server.
    mcp = _flag()
    #: The club's own WhatsApp Business number (app/api/club_whatsapp): its
    #: inbox, its templates, and its tickets sent from it. Enterprise, from
    #: migration club_whatsapp.
    whatsapp_own = _flag()
    #: The club's mailbox, <handle>@mail.myotherapp.in (app/api/club_mailbox):
    #: mail received into the dashboard, answered and sent from it. Pro and
    #: Enterprise, from migration club_mailbox.
    mailbox = _flag()

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
    #: What the club pays each cycle, in paise: the plan's price when it
    #: checked out (or an admin assigned it), kept so an admin editing the
    #: plan's price later does not change what existing subscribers are
    #: counted at (Razorpay keeps charging them the old price). MRR is worked
    #: out from this (service.mrr_of). Null on rows from before it was kept:
    #: the plan's price then.
    price_paise = Column(Integer, nullable=True)
    #: Razorpay's `created_at` of the newest subscription event applied to
    #: this row. Razorpay does not deliver events in order; an older one
    #: arriving after it is not applied (a late `pending` after `charged`).
    last_event_at = Column(TZAwareDateTime(timezone=True), nullable=True)

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

    # Complimentary: a plan MyOtherApp gives a club for nothing -- a sponsored
    # or privileged club. Always `active`, `mrr_paise` 0, no Razorpay
    # subscription, and never counted as revenue. Until `comp_ends_at` (null:
    # no end), then the hourly sweep puts back what the club had before
    # (`previous_state`) or moves it to Free; `is_entitled` stops honouring it
    # the moment it ends whether or not the sweep has run. All four are
    # cleared when the comp ends, is revoked, or is replaced.
    complimentary = Column(Boolean, nullable=False, default=False, server_default="false")
    #: Why it was given. For the admins only: never shown to the club.
    comp_reason = Column(String(200), nullable=True)
    comp_ends_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The admin who granted it.
    granted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: When the "your complimentary plan ends in a week" mail went.
    comp_reminded_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    # The free trial (service.start_free_trial): every new club gets Pro for
    # FREE_TRIAL_DAYS with no card and no Razorpay, once. While
    # `trial_plan_id` is set and `trial_ends_at` is ahead, the club has that
    # plan whatever else the row says -- a paid plan it picks during the trial
    # is a Razorpay subscription starting at `trial_ends_at`, held on the row
    # as usual, and takes over only then. The hourly sweep clears it at the
    # end and the club is on its chosen plan, or Free. `trial_started_at`
    # (never cleared) is what makes it once per club.
    #: The plan the free trial gives; null when no free trial is running.
    trial_plan_id = Column(Integer, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)

    # An Enterprise trial an admin grants after a sales call
    # (service.grant_enterprise_trial). Laid over whatever the club has --
    # its paid subscription keeps running underneath, untouched -- until
    # `enterprise_trial_ends_at`; then the club is back on what it had (the
    # sweep, or an admin ending it early). Not a complimentary plan: that
    # replaces the subscription and stops Razorpay, which would leave a
    # paying club on Free when the trial ended.
    enterprise_trial_plan_id = Column(Integer, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
    enterprise_trial_started_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    enterprise_trial_ends_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The admin who granted it.
    enterprise_trial_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: When the "your Enterprise trial ends in three days" mail went.
    enterprise_trial_reminded_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    plan = relationship("Plans", back_populates="subscriptions", foreign_keys=[plan_id])
    trial_plan = relationship("Plans", foreign_keys=[trial_plan_id])
    enterprise_trial_plan = relationship("Plans", foreign_keys=[enterprise_trial_plan_id])
    club = relationship("Clubs")
    granter = relationship("Users", foreign_keys=[granted_by])
    enterprise_trial_granter = relationship("Users", foreign_keys=[enterprise_trial_by])

    __table_args__ = (
        # One live subscription per club. `is_deleted` is in the key so a
        # cancelled-and-resubscribed club does not collide with its own
        # history -- the same shape the club/user link tables use.
        UniqueConstraint("club_id", "is_deleted", name="uq_club_subscription_live"),
        CheckConstraint("mrr_paise >= 0", name="ck_subscription_mrr_non_negative"),
        CheckConstraint("price_paise IS NULL OR price_paise >= 0", name="ck_subscription_price_non_negative"),
    )


class ChargeStatus(PyEnum):
    captured = "captured"
    refunded = "refunded"
    partially_refunded = "partially_refunded"
    #: A charge Razorpay attempted and could not take (`payment.failed`).
    #: Listed so the admin sees it; never counted as money.
    failed = "failed"


class SubscriptionCharges(AbstractSQLModel, TimestampsMixin):
    """Every payment Razorpay took for a club's subscription: the platform's
    revenue ledger (subscriptions.ledger).

    One row per Razorpay payment, written from `subscription.charged` (or the
    backfill), the unique payment id making a repeated delivery a no-op; a
    `payment.failed` on a subscription is a `failed` row, which every sum
    leaves out. Refunds update `refunded_paise` and `status` in place. A
    complimentary plan has no Razorpay subscription, so it never has a row
    here.

    Plain strings for status rather than a Postgres enum: the ledger is read
    far more than it changes, and a new state should not need an enum
    migration.
    """

    __tablename__ = "subscription_charges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: Null only if the subscription row were ever removed; the money stays.
    subscription_id = Column(
        Integer, ForeignKey("club_subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True)
    cycle = Column(Enum(BillingCycle), nullable=True)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="INR", server_default="INR")
    razorpay_payment_id = Column(String(64), nullable=False, unique=True)
    razorpay_invoice_id = Column(String(64), nullable=True)
    #: Which Razorpay subscription the payment was for: a club's row is reused
    #: when it subscribes again, so this is what tells two of them apart.
    razorpay_subscription_id = Column(String(64), nullable=True, index=True)
    status = Column(String(20), nullable=False, default=ChargeStatus.captured.value,
                    server_default=ChargeStatus.captured.value)
    refunded_paise = Column(Integer, nullable=False, default=0, server_default="0")
    #: When Razorpay took the payment (its `created_at`), not when we heard.
    charged_at = Column(TZAwareDateTime(timezone=True), nullable=False, index=True)

    club = relationship("Clubs")
    plan = relationship("Plans")
    subscription = relationship("ClubSubscriptions")

    __table_args__ = (
        CheckConstraint("amount_paise >= 0", name="ck_subscription_charge_amount_non_negative"),
        CheckConstraint(
            "refunded_paise >= 0 AND refunded_paise <= amount_paise",
            name="ck_subscription_charge_refund_in_range",
        ),
        CheckConstraint(
            "status IN ('captured', 'refunded', 'partially_refunded', 'failed')",
            name="ck_subscription_charge_status",
        ),
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
