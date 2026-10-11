"""Closing an event: the report that sums it up, the clock that moves its
status, and the retention job that scrubs the guest list once the report is
kept.

Registrations here belong to the guests' own accounts (their tickets and
certificates stay in the app), so retention scrubs what the event gathered
beyond that: phones captured at the door, answers to the event's own
fields, delivery logs, phone check-in sessions, and walk-up records made at
the gate. A club that keeps contacts gets a copy first.
"""
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import ARRAY, JSON, UUID, Boolean, Column, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class EventStatus(enum.Enum):
    draft = "draft"
    published = "published"
    live = "live"
    closed = "closed"
    archived = "archived"


class RetentionMode(enum.Enum):
    keep = "keep"
    delete = "delete"


class RetentionState(enum.Enum):
    pending = "pending"
    report_sent = "report_sent"
    deleted = "deleted"
    failed = "failed"


# Statuses in which the door accepts check-ins.
DOOR_OPEN_STATUSES = (EventStatus.published, EventStatus.live)
# Statuses in which event settings are read-only (the status itself stays editable).
LOCKED_STATUSES = (EventStatus.closed, EventStatus.archived)

STATUS_NOTE = {
    EventStatus.draft: "Refuses check-ins",
    EventStatus.published: "Tickets can go out",
    EventStatus.live: "Door is open",
    EventStatus.closed: "Locks settings",
    EventStatus.archived: "Hidden from lists",
}


class CloseoutReports(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "closeout_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    pdf_key = Column(String(300), nullable=False)
    filename = Column(String(160), nullable=False)
    figures = Column(JSON, nullable=False, default=dict)
    generated_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    generated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    delivered_to = Column(ARRAY(String), nullable=False, default=list)

    by = relationship("Users")


class RetentionJobs(AbstractSQLModel, TimestampsMixin):
    """PENDING → REPORT_SENT → DELETED, plus FAILED. Forward only."""

    __tablename__ = "retention_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, unique=True)
    state = Column(Enum(RetentionState), nullable=False, default=RetentionState.pending)
    run_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(400), nullable=True)
    last_refusal = Column(String(40), nullable=True)
    refused_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    report_id = Column(UUID(as_uuid=True), ForeignKey("closeout_reports.id", ondelete="SET NULL"), nullable=True)
    deleted_counts = Column(JSON, nullable=True)
    completed_at = Column(TZAwareDateTime(timezone=True), nullable=True)


class ArchivedContacts(AbstractSQLModel, TimestampsMixin):
    """The four fields a club asked to keep, copied out as the list is scrubbed."""

    __tablename__ = "archived_contacts"
    __table_args__ = (UniqueConstraint("event_id", "registration_id", name="uq_archived_contacts_event_registration"),)

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(String(100), nullable=False)
    email = Column(String(100), nullable=True)
    phone = Column(String(15), nullable=True)
    role = Column(String(20), nullable=False, default="attendee")
    attended = Column(Boolean, nullable=False, default=False)
    event_name = Column(String(100), nullable=False)
    event_ended_at = Column(TZAwareDateTime(timezone=True), nullable=True)
