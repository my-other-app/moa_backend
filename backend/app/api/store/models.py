"""The event store: merchandise for sale, picked at registration and paid with the ticket."""
import uuid

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, Column, ForeignKey, Integer, Numeric, String, UUID, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class EventStoreItems(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_store_items"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(String(300), nullable=True)
    photo_key = Column(String(300), nullable=True)
    price = Column(Numeric(10, 2), nullable=False, default=0)
    # Stock on the item when it has no sizes; on each variant when it does.
    stock = Column(Integer, nullable=True)
    sold_count = Column(Integer, nullable=False, default=0)
    # Registration roles that may buy it (null = everyone) and that get it free.
    available_roles = Column(JSON, nullable=True)
    free_roles = Column(JSON, nullable=False, default=list)
    on_sale = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)

    variants = relationship("EventStoreVariants", cascade="all, delete-orphan", order_by="EventStoreVariants.sort_order", lazy="selectin")


class EventStoreVariants(AbstractSQLModel):
    __tablename__ = "event_store_variants"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    item_id = Column(UUID(as_uuid=True), ForeignKey("event_store_items.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String(40), nullable=False)
    stock = Column(Integer, nullable=False, default=0)
    sold_count = Column(Integer, nullable=False, default=0)
    sort_order = Column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("item_id", "label", name="uq_event_store_variants_item_label"),)


class EventStoreOrders(AbstractSQLModel, TimestampsMixin):
    """What one registration bought: pending until the ticket's payment captures, then paid."""

    __tablename__ = "event_store_orders"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(12), nullable=False, default="pending")  # pending | paid | cancelled | refunded
    total = Column(Numeric(10, 2), nullable=False, default=0)
    paid_at = Column(TimestampsMixin.created_at.type, nullable=True)
    handed_over_at = Column(TimestampsMixin.created_at.type, nullable=True)
    handed_over_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    lines = relationship("EventStoreOrderLines", cascade="all, delete-orphan", lazy="selectin")


class EventStoreOrderLines(AbstractSQLModel):
    __tablename__ = "event_store_order_lines"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("event_store_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(UUID(as_uuid=True), ForeignKey("event_store_items.id", ondelete="CASCADE"), nullable=False)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("event_store_variants.id", ondelete="SET NULL"), nullable=True)
    item_name = Column(String(120), nullable=False)
    variant_label = Column(String(40), nullable=True)
    qty = Column(Integer, nullable=False, default=1)
    unit_price = Column(Numeric(10, 2), nullable=False, default=0)
    line_total = Column(Numeric(10, 2), nullable=False, default=0)
    is_free = Column(Boolean, nullable=False, default=False)
