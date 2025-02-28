import uuid
from sqlalchemy import JSON, Column, Enum, Float, ForeignKey, String, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from enum import Enum as PyEnum

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class OrderStatus(PyEnum):
    created = "created"
    attempted = "attempted"
    paid = "paid"


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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    receipt = Column(String, nullable=False, index=True)
    razorpay_receipt = Column(String, nullable=True)
    razorpay_order_id = Column(String, nullable=False, unique=True)
    amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False, default="INR")
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.created)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(String, nullable=True)
    source = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)

    payment_logs = relationship("PaymentLogs", back_populates="order")


class PaymentLogs(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "payment_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id"), nullable=False
    )
    razorpay_payment_id = Column(String, nullable=False, unique=True, index=True)
    status = Column(Enum(PaymentStatus), nullable=False)
    amount = Column(Float, nullable=False)
    payment_method = Column(String, nullable=True)
    payment_details = Column(JSON, nullable=True)

    order = relationship("PaymentOrders", back_populates="payment_logs")


class RazorpayWebhookLogs(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "razorpay_webhook_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(String, nullable=False, index=True, unique=True)
    entity = Column(String, nullable=False)
    event = Column(String, nullable=False)
    signature = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
