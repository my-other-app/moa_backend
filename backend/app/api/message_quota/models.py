"""What clubs send, counted by the month, and the credits that pay for it.

Seven tables:

- `message_usage`: one counter per club, channel (email, whatsapp) and
  month in India, raised by one atomic upsert per message handed to the
  provider (service.record_message). Never read-modify-write: two workers
  sending for the same club at once each add one. It also keeps the terms
  the club was last sending under (its limit, the price of an extra one and
  how it would be billed), when the 80% and 100% warnings went, how many
  sends were held back (Free past its limit) and a count per kind.
- `message_usage_refs`: the idempotency keys of messages already counted
  (`delivery:<id>:<queued>`, `message:<id>`...). A message recorded twice
  -- a worker's record retried, a job run again -- is counted once. Kept
  for REF_DAYS, then pruned by the billing sweep.
- `message_statements`: the month-end overage statements of the retired
  pay-per-extra-message billing (migration message_credits). None is made
  any more; those still waiting to be charged were made `void`. Kept for
  the history they are.
- `club_message_quotas`: an admin's own limits for one club on Enterprise
  (custom: unlimited unless set). Its prices are no longer used.
- `message_credit_packs`: the top-up packs on sale (admin-editable).
- `message_credit_purchases`: one per pack a club set out to buy: the
  Razorpay order, then the payment, then any refund.
- `message_credit_lots`: credits a club holds: bought (one per paid
  purchase) or granted by an admin, each with what is left and when it
  expires (credits.py).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MessageUsage(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "message_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    #: The first day of the month, in India.
    period = Column(Date, nullable=False)
    #: email | whatsapp
    channel = Column(String(16), nullable=False)
    #: Messages handed to the provider this month.
    used = Column(Integer, nullable=False, default=0, server_default="0")
    #: Sends held back: nothing left of the plan's allowance (or its
    #: promotional share) and no bought credits.
    blocked = Column(Integer, nullable=False, default=0, server_default="0")
    # How `used` was paid for (migration message_credits): used = plan_used +
    # credits_used + uncovered for every message counted since.
    #: From the plan's monthly allowance ("plan credits").
    plan_used = Column(Integer, nullable=False, default=0, server_default="0")
    #: From the club's credit lots (bought or granted).
    credits_used = Column(Integer, nullable=False, default=0, server_default="0")
    #: Neither: a ticket, certificate or payment message with nothing left
    #: (they always go), or a send that raced the last credit. Never charged.
    uncovered = Column(Integer, nullable=False, default=0, server_default="0")
    #: Of `used`, the MARKETING (promotional) WhatsApp templates, counted
    #: against the plan's `whatsapp_marketing_per_month` (migration pricing_adjust).
    marketing = Column(Integer, nullable=False, default=0, server_default="0")
    #: `used` by kind: {"ticket": 120, "announcement": 40, ...}.
    kinds = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # The terms the club last sent under: the statement's, unless the plan
    # it ends the month on is kinder (billing.close_month).
    #: The month's limit (null: unlimited).
    quota = Column(Integer, nullable=True)
    #: What one extra costs, in paise.
    rate_paise = Column(Integer, nullable=False, default=0, server_default="0")
    #: razorpay | manual | none (service.Terms.billing).
    billing = Column(String(16), nullable=False, default="none", server_default="none")
    plan_key = Column(String(40), nullable=True)
    #: When the club was told it had used 80% and 100% (once each a month).
    warned_80_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    warned_100_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("club_id", "period", "channel", name="uq_message_usage_club_period_channel"),
        CheckConstraint("channel IN ('email', 'whatsapp')", name="ck_message_usage_channel"),
        CheckConstraint("used >= 0 AND blocked >= 0", name="ck_message_usage_counts"),
        # The month's close reads one period across clubs.
        Index("ix_message_usage_period", "period"),
    )


class MessageUsageRefs(AbstractSQLModel):
    __tablename__ = "message_usage_refs"

    ref = Column(String(200), primary_key=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (Index("ix_message_usage_refs_created", "created_at"),)


class StatementStatus:
    """What became of a month's extra. Plain strings, checked in the table."""

    #: Nothing past the limits (or nothing worth a charge).
    NOTHING_DUE = "nothing_due"
    #: To be added to the club's Razorpay subscription.
    PENDING = "pending"
    #: The add-on is being asked for right now (`charging_since`).
    CHARGING = "charging"
    #: On the club's next Razorpay invoice (`razorpay_addon_id`).
    CHARGED = "charged"
    #: Razorpay said no; tried again at `next_attempt_at`.
    FAILED = "failed"
    #: Razorpay never answered: whether it made the add-on is found out
    #: (by its reference) before anything is asked again.
    UNCONFIRMED = "unconfirmed"
    #: Tried MAX_ATTEMPTS times; an admin invoices it by hand.
    GAVE_UP = "gave_up"
    #: A complimentary plan, an Enterprise deal or trial, a deal by hand, or
    #: a Razorpay subscription that has ended: an admin invoices it.
    MANUAL = "manual"
    #: Free or the free trial: no payment method, so nothing to charge.
    NOT_BILLABLE = "not_billable"
    #: Less than Razorpay's smallest charge (₹1): not charged.
    WAIVED = "waived"
    #: Cancelled when overage billing was retired (migration message_credits):
    #: never charged.
    VOID = "void"

    ALL = (NOTHING_DUE, PENDING, CHARGING, CHARGED, FAILED, UNCONFIRMED, GAVE_UP, MANUAL, NOT_BILLABLE, WAIVED, VOID)


class MessageStatements(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "message_statements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    period = Column(Date, nullable=False)
    plan_key = Column(String(40), nullable=True)
    #: razorpay | manual | none
    billing = Column(String(16), nullable=False)

    email_used = Column(Integer, nullable=False, default=0, server_default="0")
    email_limit = Column(Integer, nullable=True)
    email_extra = Column(Integer, nullable=False, default=0, server_default="0")
    email_rate_paise = Column(Integer, nullable=False, default=0, server_default="0")
    whatsapp_used = Column(Integer, nullable=False, default=0, server_default="0")
    whatsapp_limit = Column(Integer, nullable=True)
    whatsapp_extra = Column(Integer, nullable=False, default=0, server_default="0")
    whatsapp_rate_paise = Column(Integer, nullable=False, default=0, server_default="0")
    #: The month's extra, in paise.
    amount_paise = Column(Integer, nullable=False, default=0, server_default="0")

    status = Column(String(20), nullable=False)
    #: The subscription the add-on went on, and the add-on Razorpay made.
    razorpay_subscription_id = Column(String(64), nullable=True)
    razorpay_addon_id = Column(String(64), nullable=True, unique=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(String(400), nullable=True)
    next_attempt_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When a sweep began asking Razorpay; a claim this old is a sweep that stopped.
    charging_since = Column(TZAwareDateTime(timezone=True), nullable=True)
    charged_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("club_id", "period", name="uq_message_statements_club_period"),
        CheckConstraint(
            "status IN ('nothing_due', 'pending', 'charging', 'charged', 'failed', 'unconfirmed', "
            "'gave_up', 'manual', 'not_billable', 'waived', 'void')",
            name="ck_message_statements_status",
        ),
        CheckConstraint("amount_paise >= 0", name="ck_message_statements_amount"),
        # The charge step: what is waiting, by when it may be tried.
        Index(
            "ix_message_statements_due", "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'charging', 'failed', 'unconfirmed')"),
        ),
    )


class ClubMessageQuotas(AbstractSQLModel, TimestampsMixin):
    """An admin's limits for one club, used while it is on Enterprise
    (`service.ENTERPRISE`): its emails and WhatsApp a month (null:
    unlimited). The prices of an extra one are from the retired overage
    billing and are no longer used."""

    __tablename__ = "club_message_quotas"

    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True)
    emails_per_month = Column(Integer, nullable=True)
    whatsapp_per_month = Column(Integer, nullable=True)
    email_overage_paise = Column(Integer, nullable=True)
    whatsapp_overage_paise = Column(Integer, nullable=True)
    #: Why, for the admins.
    note = Column(String(200), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(emails_per_month IS NULL OR emails_per_month >= 0) AND (whatsapp_per_month IS NULL OR whatsapp_per_month >= 0) "
            "AND (email_overage_paise IS NULL OR email_overage_paise >= 0) "
            "AND (whatsapp_overage_paise IS NULL OR whatsapp_overage_paise >= 0)",
            name="ck_club_message_quotas_non_negative",
        ),
    )



# ─── Prepaid credits ─────────────────────────────────────────────────────


class MessageCreditPacks(AbstractSQLModel, TimestampsMixin):
    """A top-up pack on sale: `quantity` messages of one channel for
    `price_paise`, GST included (as plan prices are). Never deleted, since
    purchases point at it: taken off sale with `active=False`."""

    __tablename__ = "message_credit_packs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: email | whatsapp
    channel = Column(String(16), nullable=False)
    quantity = Column(Integer, nullable=False)
    price_paise = Column(Integer, nullable=False)
    active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint("channel IN ('email', 'whatsapp')", name="ck_message_credit_packs_channel"),
        CheckConstraint("quantity > 0 AND price_paise > 0", name="ck_message_credit_packs_positive"),
    )


class PurchaseStatus:
    """What became of a pack a club set out to buy. Plain strings, checked in the table."""

    #: The Razorpay order is made; nothing is paid yet.
    CREATED = "created"
    #: Paid: the credits are in (its lot).
    PAID = "paid"
    #: An admin's refund: the credits are gone, Razorpay is being asked.
    REFUNDING = "refunding"
    #: Razorpay refused the refund or did not answer; the credits stay gone.
    #: Asking again checks Razorpay first, so it is never refunded twice.
    REFUND_FAILED = "refund_failed"
    #: Given back (`refunded_paise`, all or part of it).
    REFUNDED = "refunded"

    ALL = (CREATED, PAID, REFUNDING, REFUND_FAILED, REFUNDED)
    #: The ones a club paid for (its history and receipts).
    PAID_ONES = (PAID, REFUNDING, REFUND_FAILED, REFUNDED)


class MessageCreditPurchases(AbstractSQLModel, TimestampsMixin):
    """One pack a club set out to buy, with a one-time Razorpay order on
    the platform's account. The pack's channel, quantity and price are kept
    here as they were when the order was made."""

    __tablename__ = "message_credit_purchases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    pack_id = Column(Integer, ForeignKey("message_credit_packs.id", ondelete="SET NULL"), nullable=True)
    channel = Column(String(16), nullable=False)
    quantity = Column(Integer, nullable=False)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="INR", server_default="INR")
    status = Column(String(20), nullable=False, default=PurchaseStatus.CREATED, server_default=PurchaseStatus.CREATED)
    razorpay_order_id = Column(String(64), nullable=False, unique=True)
    razorpay_payment_id = Column(String(64), nullable=True, unique=True)
    payment_method = Column(String(40), nullable=True)
    paid_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Who started the checkout (the club's account).
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # A refund (an admin's, or one made in Razorpay's dashboard).
    refunded_paise = Column(Integer, nullable=False, default=0, server_default="0")
    #: What the admin asked to give back, while it is being asked.
    refund_amount_paise = Column(Integer, nullable=True)
    refund_reason = Column(String(300), nullable=True)
    razorpay_refund_id = Column(String(64), nullable=True)
    refund_requested_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    refunded_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    refunded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    refund_error = Column(String(300), nullable=True)

    __table_args__ = (
        CheckConstraint("channel IN ('email', 'whatsapp')", name="ck_message_credit_purchases_channel"),
        CheckConstraint(
            "status IN ('created', 'paid', 'refunding', 'refund_failed', 'refunded')",
            name="ck_message_credit_purchases_status",
        ),
        CheckConstraint(
            "quantity > 0 AND amount_paise > 0 AND refunded_paise >= 0 AND refunded_paise <= amount_paise",
            name="ck_message_credit_purchases_amounts",
        ),
        Index("ix_message_credit_purchases_club", "club_id", "created_at"),
        # The sweep's walk: checkouts a payment may still land on.
        Index(
            "ix_message_credit_purchases_open", "created_at",
            postgresql_where=text("status = 'created'"),
        ),
    )


class LotSource:
    PURCHASE = "purchase"
    GRANT = "grant"


class MessageCreditLots(AbstractSQLModel, TimestampsMixin):
    """Credits a club holds for one channel: `remaining` of `quantity`,
    until `expires_at`. Sends past the plan's monthly allowance take from
    the lot that expires first (credits.consume)."""

    __tablename__ = "message_credit_lots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(16), nullable=False)
    #: purchase | grant
    source = Column(String(16), nullable=False)
    quantity = Column(Integer, nullable=False)
    remaining = Column(Integer, nullable=False)
    bought_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    #: The purchase it came from: one lot per purchase, ever (unique).
    purchase_id = Column(
        Integer, ForeignKey("message_credit_purchases.id", ondelete="CASCADE"), nullable=True, unique=True,
    )
    #: A grant's reason and the admin who gave it.
    reason = Column(String(300), nullable=True)
    granted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: Its unused credits were taken back with a refund.
    refunded_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The club was told its credits run low (under 10% of this, the last pack bought).
    low_warned_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The club was told this lot expires within 30 days.
    expiry_warned_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("channel IN ('email', 'whatsapp')", name="ck_message_credit_lots_channel"),
        CheckConstraint("source IN ('purchase', 'grant')", name="ck_message_credit_lots_source"),
        CheckConstraint("quantity > 0 AND remaining >= 0 AND remaining <= quantity", name="ck_message_credit_lots_remaining"),
        # The sends' walk: a club's lots with credits left, by expiry.
        Index(
            "ix_message_credit_lots_live", "club_id", "channel", "expires_at",
            postgresql_where=text("remaining > 0"),
        ),
    )
