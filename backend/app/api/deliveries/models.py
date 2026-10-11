"""What was sent to whom, on which channel, and what became of it.

A ticket emailed and never recorded is a ticket nobody can say was sent.
One row per registration, per kind of document, per channel: the worker
upserts it as it goes, so "send tickets" can be clicked twice without
anyone getting two, and the list can say who was reached.
"""
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import UUID, Column, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class DeliveryKind(enum.Enum):
    ticket = "ticket"
    certificate = "certificate"


class DeliveryChannel(enum.Enum):
    email = "email"
    # No provider yet. The column knows the channel exists so the day one is
    # wired in, nothing already recorded has to change shape.
    whatsapp = "whatsapp"


class DeliveryStatus(enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    # No address on this channel, or nothing to send: a fact about the
    # guest, not a failure.
    skipped = "skipped"


class DeliveryOutcome(enum.Enum):
    """What the provider's own webhook said became of it, after we sent it."""

    delivered = "delivered"
    read = "read"
    undelivered = "undelivered"
    bounced = "bounced"
    complained = "complained"


class Deliveries(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint(
            "registration_id", "kind", "channel", name="uq_deliveries_registration_kind_channel"
        ),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    registration_id = Column(
        UUID(as_uuid=True),
        ForeignKey("event_registrations_link.id"),
        nullable=False,
        index=True,
    )
    kind = Column(Enum(DeliveryKind), nullable=False)
    channel = Column(Enum(DeliveryChannel), nullable=False)
    status = Column(Enum(DeliveryStatus), nullable=False, default=DeliveryStatus.pending)
    # The provider's id for the message -- SES's MessageId -- so a bounce
    # notification can be matched back to the row.
    provider_id = Column(String(200), nullable=True)
    # Why it failed, or why it was skipped, in words a desk can read.
    error = Column(String(400), nullable=True)
    outcome = Column(Enum(DeliveryOutcome), nullable=True)
    outcome_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    reason = Column(String(400), nullable=True)
    reason_code = Column(String(80), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # The layered design's version the document carried when it went out.
    design_version = Column(Integer, nullable=False, default=0)

    registration = relationship("EventRegistrationsLink")
