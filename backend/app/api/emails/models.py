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
from sqlalchemy import DDL, JSON, UUID, Boolean, Column, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import event as sa_event
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
    # When a timed journey email (BEFORE_START / AFTER_END) comes due: the
    # event's start or end moved by the trigger's minutes. Null for anything
    # else (library templates, the invite, custom emails, manual sends).
    # Written by the database (EMAIL_DUE_AT_SQL below), on every insert, every
    # change of the trigger and every change of the event's start or length,
    # so no write path can leave it stale. The minute sweep reads only the
    # templates whose instant has come (ix_email_templates_due).
    due_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.Index("ix_email_templates_due", "due_at", postgresql_where=sa.text("enabled AND due_at IS NOT NULL")),
        # One of each journey email per event, and per club library: two
        # requests copying them at once insert, the second does nothing
        # (ON CONFLICT), and both read back the same four.
        sa.Index("uq_email_templates_event_stage", "event_id", "stage", unique=True, postgresql_where=sa.text("stage <> 'CUSTOM'")),
        sa.Index(
            "uq_email_templates_library_stage", "club_id", "stage", unique=True,
            postgresql_where=sa.text("event_id IS NULL AND stage <> 'CUSTOM'"),
        ),
    )


class MessageKind(enum.Enum):
    BROADCAST = "BROADCAST"
    TEMPLATE = "TEMPLATE"
    TEST = "TEST"


class MessageStatus(enum.Enum):
    SENDING = "SENDING"
    SENT = "SENT"


class Messages(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("event_id", "client_ref", name="uq_messages_event_client_ref"),
        # The resumer's minute look for messages not yet finished.
        sa.Index("ix_messages_sending", "created_at", postgresql_where=sa.text("status = 'SENDING'")),
    )

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
        # Every SES or Resend notification looks its send up by this.
        sa.Index("ix_message_deliveries_provider_id", "provider_id", postgresql_where=sa.text("provider_id IS NOT NULL")),
        # A message's workers claim from what is left of it, not from all of it.
        sa.Index("ix_message_deliveries_unfinished", "message_id", postgresql_where=sa.text("status IN ('pending', 'sending')")),
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
    # Times a worker took it; backoff and giving up count these.
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    # When a worker took it (status sending); see Deliveries.claimed_at.
    claimed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # Not before this: the backoff after the provider said "not now".
    next_attempt_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    message = relationship("Messages", back_populates="deliveries")
    registration = relationship("EventRegistrationsLink")


# ─── email_templates.due_at, kept by the database ─────────────────────────
#
# moa_email_due_at mirrors emails.service.due_at exactly: BEFORE_START is the
# start less the minutes, AFTER_END the end (start + duration hours) plus
# them; anything else, or no minutes, has no instant. Triggers keep the
# column right whoever writes: a template inserted or its trigger or event
# changed, and an event whose start or length changes. The migration
# write_path installs the same and fills in every existing row.

EMAIL_DUE_AT_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_email_due_at(trig json, starts timestamptz, hours double precision)
    RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
    DECLARE
      mode text := trig->>'mode';
      mins text := trig->>'minutes';
    BEGIN
      IF starts IS NULL OR mode IS NULL OR mode NOT IN ('BEFORE_START', 'AFTER_END')
         OR mins IS NULL OR mins !~ '^[0-9]+(\.0+)?$' OR mins::numeric = 0 THEN
        RETURN NULL;
      END IF;
      IF mode = 'BEFORE_START' THEN
        RETURN starts - make_interval(mins => mins::numeric::int);
      END IF;
      RETURN starts + coalesce(hours, 0) * interval '1 hour' + make_interval(mins => mins::numeric::int);
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_email_template_due() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      starts timestamptz;
      hours double precision;
    BEGIN
      IF NEW.event_id IS NULL THEN
        NEW.due_at := NULL;
        RETURN NEW;
      END IF;
      SELECT event_datetime, duration INTO starts, hours FROM events WHERE id = NEW.event_id;
      NEW.due_at := moa_email_due_at(NEW.trigger, starts, hours);
      RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER trg_email_templates_due
    BEFORE INSERT OR UPDATE OF trigger, event_id ON email_templates
    FOR EACH ROW EXECUTE FUNCTION moa_email_template_due()
    """,
    """
    CREATE OR REPLACE FUNCTION moa_event_email_due() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.event_datetime IS DISTINCT FROM OLD.event_datetime OR NEW.duration IS DISTINCT FROM OLD.duration THEN
        UPDATE email_templates SET due_at = moa_email_due_at(trigger, NEW.event_datetime, NEW.duration)
        WHERE event_id = NEW.id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_events_email_due
    AFTER UPDATE OF event_datetime, duration ON events
    FOR EACH ROW EXECUTE FUNCTION moa_event_email_due()
    """,
)

# On email_templates' creation: events exists by then (the foreign key orders them).
for _sql in EMAIL_DUE_AT_SQL:
    sa_event.listen(EmailTemplates.__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql"))
