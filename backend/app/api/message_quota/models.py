"""What clubs send, counted by the month, and what the extra costs.

Four tables:

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
- `message_statements`: one per club and closed month: what was used, the
  limit, the extra and what it costs, and what became of the charge -- a
  Razorpay add-on on the club's subscription (its id kept, so it is never
  added twice), a statement for an admin to invoice by hand, or nothing.
- `club_message_quotas`: an admin's own limits and prices for one club on
  Enterprise (custom: unlimited unless set).
"""

from datetime import datetime, timezone

from sqlalchemy import (
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
    #: Sends held back because the plan has no way to pay for more (Free),
    #: or past the plan's promotional WhatsApp cap.
    blocked = Column(Integer, nullable=False, default=0, server_default="0")
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

    ALL = (NOTHING_DUE, PENDING, CHARGING, CHARGED, FAILED, UNCONFIRMED, GAVE_UP, MANUAL, NOT_BILLABLE, WAIVED)


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
            "'gave_up', 'manual', 'not_billable', 'waived')",
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
    unlimited) and the price of an extra one (null: the plan's)."""

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

