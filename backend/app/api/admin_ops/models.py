"""The admin tools' own tables (migration admin_ops).

- `platform_settings`: the runtime settings an owner changes from the portal
  (app.api.admin_ops.runtime); each change is also an admin audit entry.
- `admin_invites`: an admin added by email, with the set-password link's
  hashed token; also how the two-factor grace tells a new admin from one who
  was already there (app.api.admin_ops.two_factor).
- `club_payouts`: money MyOtherApp paid a club out of the ticket and store
  sales its own Razorpay account took for that club (app.api.admin_ops.payouts).
- `payment_fees`: Razorpay's fee on each of those payments, read from Razorpay.
- `payment_refunds`: refunds an admin made of part of a payment -- some of the
  ticket, or one store order (app.api.admin_ops.refunds).
- `payment_disputes`: Razorpay disputes and chargebacks, from the platform
  webhook (app.api.admin_ops.disputes).

No foreign key from the money tables to payment_logs or events: like the
ledger, a record of money outlives the rows it names.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import JSON, BigInteger, Boolean, Column, Date, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlatformSettings(AbstractSQLModel):
    __tablename__ = "platform_settings"

    #: One of runtime.SPECS' keys.
    key = Column(String(64), primary_key=True)
    #: The value, as JSON (a bool, a string, a timestamp's ISO text).
    value = Column(JSON, nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))


class AdminInvites(AbstractSQLModel):
    __tablename__ = "admin_invites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    email = Column(String(320), nullable=False)
    role = Column(String(20), nullable=False)
    #: SHA-256 of the set-password link's token; the token itself is only mailed.
    token_hash = Column(String(64), nullable=False, unique=True)
    invited_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: Whether the account existed before (promoted) or was made for the invite.
    promoted = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    accepted_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)


class ClubPayouts(AbstractSQLModel):
    __tablename__ = "club_payouts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    paid_on = Column(Date, nullable=False)
    #: The bank transfer's UTR, a cheque number: whatever finds it again.
    reference = Column(String(120), nullable=False)
    note = Column(String(500), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    #: A payout recorded by mistake is voided, never deleted: its row stays, with why.
    voided_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    voided_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    void_reason = Column(String(300), nullable=True)

    __table_args__ = (sa.CheckConstraint("amount > 0", name="ck_club_payouts_amount_positive"),)


class PaymentFees(AbstractSQLModel):
    __tablename__ = "payment_fees"

    razorpay_payment_id = Column(String(64), primary_key=True)
    #: Razorpay's fee, GST included (its `fee`), and the GST in it (`tax`).
    fee_paise = Column(BigInteger, nullable=False)
    tax_paise = Column(BigInteger, nullable=False, default=0, server_default="0")
    fetched_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))


class PaymentRefunds(AbstractSQLModel):
    __tablename__ = "payment_refunds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    payment_log_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    razorpay_payment_id = Column(String(64), nullable=False, index=True)
    #: Razorpay's refund id; null only while it was being asked.
    razorpay_refund_id = Column(String(64), nullable=True, unique=True)
    #: "ticket" (part of the ticket), "store_order" (one order), "rest" (what was left, in full).
    kind = Column(String(20), nullable=False)
    store_order_id = Column(UUID(as_uuid=True), nullable=True)
    registration_id = Column(UUID(as_uuid=True), nullable=True)
    club_id = Column(Integer, nullable=True)
    amount = Column(Numeric(10, 2), nullable=False)
    reason = Column(String(300), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (sa.CheckConstraint("amount > 0", name="ck_payment_refunds_amount_positive"),)


class PaymentDisputes(AbstractSQLModel):
    __tablename__ = "payment_disputes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    razorpay_dispute_id = Column(String(64), nullable=False, unique=True)
    razorpay_payment_id = Column(String(64), nullable=True, index=True)
    #: The ticket payment it is about, or the subscription charge; neither when it is not ours.
    payment_log_id = Column(UUID(as_uuid=True), nullable=True)
    subscription_charge_id = Column(Integer, nullable=True)
    club_id = Column(Integer, nullable=True, index=True)
    event_id = Column(Integer, nullable=True)
    amount_paise = Column(BigInteger, nullable=False, default=0, server_default="0")
    #: What Razorpay took from the platform's balance while it is decided.
    amount_deducted_paise = Column(BigInteger, nullable=False, default=0, server_default="0")
    currency = Column(String(3), nullable=False, default="INR", server_default="INR")
    reason_code = Column(String(80), nullable=True)
    reason_description = Column(String(500), nullable=True)
    #: Razorpay's phase: retrieval, chargeback, pre_arbitration, arbitration, fraud.
    phase = Column(String(40), nullable=True)
    #: open, under_review, won, lost, closed (and action_required, Razorpay's newer word).
    status = Column(String(30), nullable=False, default="open", server_default="open")
    #: The deadline for the evidence.
    respond_by = Column(TZAwareDateTime(timezone=True), nullable=True)
    opened_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The last webhook event that changed it.
    last_event = Column(String(60), nullable=True)
    #: The admins' notes: [{at, by_id, by_name, text}], oldest first.
    notes = Column(JSON, nullable=False, default=list)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (
        Index("ix_payment_disputes_open", "respond_by", postgresql_where=sa.text("status IN ('open', 'under_review', 'action_required')")),
    )


__all__ = ["PlatformSettings", "AdminInvites", "ClubPayouts", "PaymentFees", "PaymentRefunds", "PaymentDisputes"]
