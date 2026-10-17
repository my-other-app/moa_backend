from sqlalchemy import JSON, Column, Enum, ForeignKey, String, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from enum import Enum as PyEnum
import sqlalchemy as sa

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


class PaymentLogs(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "payment_logs"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id"), nullable=False
    )
    razorpay_payment_id = Column(String, nullable=False, unique=True, index=True)
    status = Column(Enum(PaymentStatus), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(String, nullable=True)
    payment_details = Column(JSON, nullable=True)

    order = relationship("PaymentOrders", back_populates="payment_logs")


class RazorpayWebhookLogs(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
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
