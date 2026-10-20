"""The store: a club's merchandise for sale, offered on its events, picked at
registration and paid with the ticket.

An item belongs to the club (`club_id`). Each event offers the items it
picks (`EventStoreOffers`), so three or four events on at once sell from one
pile of t-shirts: the stock and its counts are on the item (or its sizes),
shared by every event that offers it. What each event sold is in its orders,
which keep their `event_id`."""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, CheckConstraint, Column, ForeignKey, Integer, Numeric, String, UUID, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class EventStoreItems(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_store_items"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    #: The club whose catalogue it is in; only that club's events may offer it.
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    #: The event it was first made for, if any. A record only: which events
    #: sell it is EventStoreOffers, and nothing scopes by this.
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String(120), nullable=False)
    description = Column(String(300), nullable=True)
    photo_key = Column(String(300), nullable=True)
    price = Column(Numeric(10, 2), nullable=False, default=0)
    # Stock on the item when it has no sizes; on each variant when it does.
    stock = Column(Integer, nullable=True)
    sold_count = Column(Integer, nullable=False, default=0)
    #: Pieces held for pending orders (store.service: reserved when picked,
    #: sold when the payment captures, given back when the order is dropped
    #: or its hold runs out). sold + reserved never passes stock.
    reserved_count = Column(Integer, nullable=False, default=0, server_default="0")
    # Registration roles that may buy it (null = everyone) and that get it free.
    available_roles = Column(JSON, nullable=True)
    free_roles = Column(JSON, nullable=False, default=list)
    on_sale = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)

    variants = relationship("EventStoreVariants", cascade="all, delete-orphan", order_by="EventStoreVariants.sort_order", lazy="selectin")

    __table_args__ = (
        CheckConstraint("stock IS NULL OR sold_count <= stock", name="ck_event_store_items_sold_within_stock"),
        CheckConstraint("sold_count >= 0 AND reserved_count >= 0", name="ck_event_store_items_counts_non_negative"),
    )


class EventStoreOffers(AbstractSQLModel):
    """An event offering one of its club's items, in the event's own order.
    Taking an item off an event deletes the row; orders already placed for
    it are not touched."""

    __tablename__ = "event_store_offers"

    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    item_id = Column(UUID(as_uuid=True), ForeignKey("event_store_items.id", ondelete="CASCADE"), primary_key=True, index=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=sa.func.now())


class EventStoreVariants(AbstractSQLModel):
    __tablename__ = "event_store_variants"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    item_id = Column(UUID(as_uuid=True), ForeignKey("event_store_items.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String(40), nullable=False)
    stock = Column(Integer, nullable=False, default=0)
    sold_count = Column(Integer, nullable=False, default=0)
    reserved_count = Column(Integer, nullable=False, default=0, server_default="0")
    sort_order = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("item_id", "label", name="uq_event_store_variants_item_label"),
        CheckConstraint("sold_count <= stock", name="ck_event_store_variants_sold_within_stock"),
        CheckConstraint("sold_count >= 0 AND reserved_count >= 0", name="ck_event_store_variants_counts_non_negative"),
    )


class EventStoreOrders(AbstractSQLModel, TimestampsMixin):
    """What one registration bought: pending until the ticket's payment captures, then paid.

    While pending its lines are held on the shelf (`held_until` set: they are
    counted in the items' reserved_count) until the hold runs out, the order
    is dropped, or the payment converts them to sold. A payment that lands
    when the pieces are gone leaves it `oversold`, for the organiser to
    settle: hand something over anyway, or refund."""

    __tablename__ = "event_store_orders"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(12), nullable=False, default="pending")  # pending | paid | cancelled | refunded | oversold
    total = Column(Numeric(10, 2), nullable=False, default=0)
    paid_at = Column(TimestampsMixin.created_at.type, nullable=True)
    handed_over_at = Column(TimestampsMixin.created_at.type, nullable=True)
    handed_over_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: Until when the pending order's lines are held on the shelf; null when
    #: nothing is held (paid, dropped, or the hold was given back).
    held_until = Column(TZAwareDateTime(timezone=True), nullable=True)

    lines = relationship("EventStoreOrderLines", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        # The hold sweep's walk: only orders still holding something.
        sa.Index("ix_event_store_orders_held_until", "held_until", postgresql_where=sa.text("held_until IS NOT NULL")),
    )


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
