"""Designed emails, and every message sent from one or typed by hand.

Templates live in two places: a club's library holds the defaults every
new event starts from; each event holds its own copies, edited freely
without touching the library. A send freezes the design and the audience
into a message with one delivery row per guest, so editing the template
afterwards never changes an email already on its way.
"""
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import JSON, UUID, Boolean, Column, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.api.deliveries.models import DeliveryChannel, DeliveryOutcome, DeliveryStatus
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class EmailStage(enum.Enum):
    INVITE = "INVITE"
    PRE_EVENT = "PRE_EVENT"
    REMINDER = "REMINDER"
    POST_EVENT = "POST_EVENT"
    CUSTOM = "CUSTOM"


class EmailTemplates(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "email_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False, index=True)
    # Null: a library template the club's new events start from.
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    stage = Column(Enum(EmailStage), nullable=False, default=EmailStage.CUSTOM)
    name = Column(String(80), nullable=False)
    subject = Column(String(200), nullable=False)
    # { theme, blocks } -- see app.core.mail.schema.EmailDesign.
    design = Column(JSON, nullable=False, default=dict)
    attachments = Column(JSON, nullable=False, default=dict)
    audience = Column(JSON, nullable=False, default=dict)
    trigger = Column(JSON, nullable=False, default=dict)
    channels = Column(JSON, nullable=False, default=list)
    enabled = Column(Boolean, nullable=False, default=True)
    version = Column(Integer, nullable=False, default=1)
    # The library template this event copy came from, if any.
    source_id = Column(UUID(as_uuid=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class MessageKind(enum.Enum):
    BROADCAST = "BROADCAST"
    TEMPLATE = "TEMPLATE"
    TEST = "TEST"


class MessageStatus(enum.Enum):
    SENDING = "SENDING"
    SENT = "SENT"


class Messages(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("event_id", "client_ref", name="uq_messages_event_client_ref"),)

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    # One per composed message, from the browser: a double submit sends once.
    client_ref = Column(UUID(as_uuid=True), nullable=True)
    kind = Column(Enum(MessageKind), nullable=False, default=MessageKind.BROADCAST)
    # The template this send came from, with its design frozen at send time.
    template_id = Column(UUID(as_uuid=True), ForeignKey("email_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    design = Column(JSON, nullable=True)
    attachments = Column(JSON, nullable=True)
    # The timer's send for a template: at most one per template.
    scheduled = Column(Boolean, nullable=False, default=False)
    subject = Column(String(200), nullable=True)
    body = Column(String(4000), nullable=False, default="")
    audience = Column(String(40), nullable=False, default="ALL")
    roles = Column(JSON, nullable=False, default=list)
    channels = Column(JSON, nullable=False, default=list)
    status = Column(Enum(MessageStatus), nullable=False, default=MessageStatus.SENDING)
    recipients = Column(Integer, nullable=False, default=0)
    sent_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    event = relationship("Events")
    deliveries = relationship("MessageDeliveries", back_populates="message", cascade="all, delete-orphan")


class MessageDeliveries(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "message_deliveries"
    __table_args__ = (
        UniqueConstraint("message_id", "registration_id", "channel", name="uq_message_deliveries_message_registration_channel"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id"), nullable=False, index=True)
    channel = Column(Enum(DeliveryChannel), nullable=False)
    status = Column(Enum(DeliveryStatus), nullable=False, default=DeliveryStatus.pending)
    provider_id = Column(String(200), nullable=True)
    error = Column(String(400), nullable=True)
    outcome = Column(Enum(DeliveryOutcome), nullable=True)
    outcome_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    reason = Column(String(400), nullable=True)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    message = relationship("Messages", back_populates="deliveries")
    registration = relationship("EventRegistrationsLink")
