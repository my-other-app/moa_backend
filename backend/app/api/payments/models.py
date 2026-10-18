from sqlalchemy import JSON, Column, Enum, ForeignKey, String, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from enum import Enum as PyEnum
import sqlalchemy as sa

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class OrderStatus(PyEnum):
    created = "created"
    attempted = "attempted"
    paid = "paid"
    refunded = "refunded"


class PaymentStatus(PyEnum):
    created = "created"
    authorized = "authorized"
    captured = "captured"
    refunded = "refunded"
    disputed = "disputed"
    pending = "pending"
    failed = "failed"


class PaymentOrders(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "payment_orders"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    receipt = Column(String, nullable=False, index=True)
    razorpay_receipt = Column(String, nullable=True)
    razorpay_order_id = Column(String, nullable=False, unique=True)
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String, nullable=False, default="INR")
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.created)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(String, nullable=True)
    source = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
    # The club's own Razorpay account the order was made on; null = the
    # platform's. Set once, at creation, and never changed: the payment is
    # verified and refunded through this account even after the club replaces
    # or disconnects it (app/api/payment_gateways).
    payment_gateway_id = Column(
        Integer, ForeignKey("club_payment_gateways.id"), nullable=True, index=True
    )

    payment_logs = relationship("PaymentLogs", back_populates="order")
    user = relationship("Users")
    payment_gateway = relationship("ClubPaymentGateways")

    __table_args__ = (
        # The reconciliation sweep's walk: orders a payment may still land on.
        sa.Index(
            "ix_payment_orders_open_created", "created_at",
            postgresql_where=sa.text("status IN ('created', 'attempted')"),
        ),
        # The repair sweep's walk: orders paid lately.
        sa.Index("ix_payment_orders_paid_updated", "updated_at", postgresql_where=sa.text("status = 'paid'")),
    )


class PaymentLogs(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "payment_logs"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id"), nullable=False, index=True
    )
    razorpay_payment_id = Column(String, nullable=False, unique=True, index=True)
    status = Column(Enum(PaymentStatus), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(String, nullable=True)
    payment_details = Column(JSON, nullable=True)
    #: Why this captured payment is being given back without anyone asking:
    #: "event_full" (the last place went while it was being paid) or
    #: "duplicate" (a second payment on an order already paid). Set, with
    #: `refund_requested_at`, in the transaction that finds it, before
    #: Razorpay is asked; the refund sweep retries until Razorpay has done it.
    refund_reason = Column(String(40), nullable=True)
    refund_requested_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    order = relationship("PaymentOrders", back_populates="payment_logs")

    __table_args__ = (
        # Finance's refunds: the few refunded payments, by order.
        sa.Index("ix_payment_logs_refunded_order", "order_id", postgresql_where=sa.text("status = 'refunded'")),
        # The refund sweep's walk: automatic refunds Razorpay has not confirmed yet.
        sa.Index(
            "ix_payment_logs_refund_due", "refund_requested_at",
            postgresql_where=sa.text("refund_requested_at IS NOT NULL AND status = 'captured'"),
        ),
    )


class RazorpayWebhookLogs(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """Every webhook delivery, once per event id: the idempotency gate.

    The row is written (and committed) before the event is applied, with
    `claimed_at`; `processed_at` is stamped only once applying it committed.
    A row without `processed_at` is retryable: Razorpay's next delivery of
    the same event takes it over once the claim is older than
    service.WEBHOOK_LEASE (a delivery that died mid-way), and applies it."""

    __tablename__ = "razorpay_webhook_logs"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    event_id = Column(String, nullable=False, index=True, unique=True)
    entity = Column(String, nullable=False)
    event = Column(String, nullable=False)
    signature = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
    #: When the delivery now applying it took it; cleared when applying failed.
    claimed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When applying it committed. Null: not applied yet, and retryable.
    processed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    #: Why the last attempt failed, or why the event was refused for good.
    last_error = Column(String(300), nullable=True)
