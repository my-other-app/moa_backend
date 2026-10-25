"""What was sent to whom, on which channel, and what became of it.

A ticket emailed and never recorded is a ticket nobody can say was sent.
One row per registration, per kind of document, per channel: "send tickets"
upserts it (ON CONFLICT), a worker claims it (pending -> sending, one
worker only) before sending, so the button can be clicked twice without
anyone getting two, and the list can say who was reached.

The rows are the outbox: nothing else holds the queue. A row left pending,
or claimed and never finished, is taken up again by the resumer in the
minute sweep (app/api/deliveries/pipeline.py).
"""
import enum
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import UUID, Column, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class DeliveryKind(enum.Enum):
    ticket = "ticket"
    certificate = "certificate"
    # The day-before reminder, on WhatsApp only, to the guests who asked for
    # it (deliveries.service.queue_reminders): one row a guest, ever.
    reminder = "reminder"


class DeliveryChannel(enum.Enum):
    email = "email"
    # Only when the server has the WhatsApp keys (app/core/whatsapp.py), and
    # only for a guest who ticked "Send my ticket on WhatsApp".
    whatsapp = "whatsapp"


class DeliveryStatus(enum.Enum):
    pending = "pending"
    # Claimed by a worker (claimed_at): being rendered and sent right now.
    sending = "sending"
    sent = "sent"
    failed = "failed"
    # No address on this channel, or nothing to send: a fact about the
    # guest, not a failure.
    skipped = "skipped"
    # Tried MAIL_MAX_ATTEMPTS times while the provider was down or slow, and
    # given up on; the admins were told. The dead-letter queue: "Retry
    # failed" sends these again.
    dead = "dead"


#: Waiting for a worker, or with one now.
UNFINISHED = (DeliveryStatus.pending, DeliveryStatus.sending)

#: How a status reads outside (the desk, the analytics, a report): a row
#: being sent is still pending, and one given up on has failed -- its error
#: says it was given up on. The statuses the outside knows are the others.
SHOWN = {DeliveryStatus.sending: DeliveryStatus.pending, DeliveryStatus.dead: DeliveryStatus.failed}
SHOWN_STATUSES = tuple(s for s in DeliveryStatus if s not in SHOWN)


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
        # Every SES or Resend notification looks its send up by this.
        sa.Index("ix_deliveries_provider_id", "provider_id", postgresql_where=sa.text("provider_id IS NOT NULL")),
        # The resumer's minute look at what is waiting or stuck.
        sa.Index("ix_deliveries_unfinished", "event_id", "kind", postgresql_where=sa.text("status IN ('pending', 'sending')")),
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
    # The provider's id for the message -- SES's MessageId, Resend's id,
    # WhatsApp's wamid -- so a provider's notice can be matched back to the row.
    provider_id = Column(String(200), nullable=True)
    # Why it failed, or why it was skipped, in words a desk can read.
    error = Column(String(400), nullable=True)
    outcome = Column(Enum(DeliveryOutcome), nullable=True)
    outcome_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    reason = Column(String(400), nullable=True)
    reason_code = Column(String(80), nullable=True)
    # Every time a worker took it, ever (what the desk sees).
    attempts = Column(Integer, nullable=False, default=0)
    # Tries since it was last queued: what backoff and giving up count.
    tries = Column(Integer, nullable=False, default=0, server_default="0")
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # The layered design's version the document carried when it went out.
    design_version = Column(Integer, nullable=False, default=0)
    # When it was last queued: with the id, the idempotency key a retry
    # sends again under, so Resend takes one copy however often it is tried.
    queued_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # When a worker took it (status sending). A claim older than ten minutes
    # is a worker that died; the resumer puts the row back.
    claimed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # Not before this: the backoff after the provider said "not now".
    next_attempt_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # A WhatsApp send's number, as its last attempt went: "club" (the club's
    # own, app/api/club_whatsapp) or "platform"; and, from the platform's
    # when the club wanted its own, why (Meta's code, or ours, and words).
    via = Column(String(8), nullable=True)
    fallback_code = Column(String(40), nullable=True)
    fallback_reason = Column(String(300), nullable=True)

    registration = relationship("EventRegistrationsLink")


class DeliveryAttempts(AbstractSQLModel):
    """Each mail a provider took, by the id it gave it.

    A send again (a guest's ticket once more) gives the delivery row a new
    provider id; the bounce of the first send, arriving late, still finds its
    attempt here -- and through it the address it went to -- where it used to
    match nothing at all."""

    __tablename__ = "delivery_attempts"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    provider = Column(String(20), nullable=True)
    provider_id = Column(String(200), nullable=False)
    delivery_id = Column(UUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=True, index=True)
    message_delivery_id = Column(UUID(as_uuid=True), ForeignKey("message_deliveries.id", ondelete="CASCADE"), nullable=True, index=True)
    # Lower case: the address this one went to, which may since have changed.
    address = Column(String(320), nullable=True)
    outcome = Column(Enum(DeliveryOutcome), nullable=True)
    outcome_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    reason = Column(String(400), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    # A WhatsApp attempt's number -- "club" or "platform" -- and, when the club
    # wanted its own and this went from the platform's, why (Deliveries.via).
    via = Column(String(8), nullable=True)
    fallback_code = Column(String(40), nullable=True)
    fallback_reason = Column(String(300), nullable=True)

    __table_args__ = (
        UniqueConstraint("provider_id", name="uq_delivery_attempts_provider_id"),
        # This month's fallbacks, for the club and the admins.
        sa.Index("ix_delivery_attempts_fallback", "created_at", postgresql_where=sa.text("fallback_code IS NOT NULL")),
    )


# Kept with the models so the registry (and Alembic, and create_all) knows them.
from app.core.mail.suppression import EmailSuppressions  # noqa: E402,F401
