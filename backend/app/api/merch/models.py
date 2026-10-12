"""Merchandise: what an event hands out, and every handover.

An item keeps its own stock or has sized variants, never both. A guest's
claim is resolved from their own record (role, or a guest field), never
trusted from the client. A handover is unique per guest and item: nobody
gets two shirts by qualifying for two sizes.
"""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import UUID, Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class MerchItems(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "merch_items"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    photo_key = Column(String(300), nullable=True)
    # Null when variants exist; stock then lives per variant.
    stock = Column(Integer, nullable=True)
    collected_count = Column(Integer, nullable=False, default=0, server_default="0")
    eligibility_field = Column(String(80), nullable=True)
    eligibility_value = Column(String(80), nullable=True)
    variant_field = Column(String(80), nullable=True)
    # Swag: who sponsored it, how it is given out, and pieces counted out by hand at the door.
    sponsor = Column(String(120), nullable=True)
    handout_rule = Column(String(40), nullable=True)
    manual_handed_out = Column(Integer, nullable=False, default=0)

    variants = relationship("MerchVariants", back_populates="item", cascade="all, delete-orphan", order_by="MerchVariants.created_at")


class MerchVariants(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "merch_variants"
    __table_args__ = (UniqueConstraint("item_id", "label", name="uq_merch_variants_item_label"),)

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    item_id = Column(UUID(as_uuid=True), ForeignKey("merch_items.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String(40), nullable=False)
    stock = Column(Integer, nullable=False)
    collected_count = Column(Integer, nullable=False, default=0, server_default="0")

    item = relationship("MerchItems", back_populates="variants")


class MerchCollections(AbstractSQLModel, TimestampsMixin):
    """A handover. The door is nullable because a desk that sends none is
    still a desk doing its job; the log says the door is unknown rather than
    guessing."""

    __tablename__ = "merch_collections"
    __table_args__ = (UniqueConstraint("registration_id", "item_id", name="uq_merch_collections_registration_item"),)

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id"), nullable=False, index=True)
    item_id = Column(UUID(as_uuid=True), ForeignKey("merch_items.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("merch_variants.id", ondelete="SET NULL"), nullable=True)
    door_label = Column(String(40), nullable=True)
    door_id = Column(UUID(as_uuid=True), ForeignKey("doors.id", ondelete="SET NULL"), nullable=True, index=True)
    collected_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    collected_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    registration = relationship("EventRegistrationsLink")
    item = relationship("MerchItems")
    variant = relationship("MerchVariants")
    door = relationship("Doors")
    by = relationship("Users")
