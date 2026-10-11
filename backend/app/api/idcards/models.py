"""ID cards: the design each event prints at the door, and every card printed.

A badge template resolves like a ticket's -- this event's design for the
role, then the event's default, then the club's -- through the same
resolver, so one design can serve the whole event or each role its own.
"""
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import JSON, UUID, Boolean, Column, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.api.templates_audience import TemplateAudience
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class ReprintPolicy(enum.Enum):
    anyone = "anyone"
    organiser = "organiser"
    never = "never"


class PrintSource(enum.Enum):
    checkin = "checkin"
    manual = "manual"
    batch = "batch"
    walkup = "walkup"
    kiosk = "kiosk"
    test = "test"


class BadgeTemplates(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "badge_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    audience = Column(Enum(TemplateAudience), nullable=False, default=TemplateAudience.event_default)
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)
    config = Column(JSON, nullable=False, default=dict)


class PrintJobs(AbstractSQLModel, TimestampsMixin):
    """A card that was put out, and from where. Rendering happens in the
    request, so a job is done or failed by the time it is recorded."""

    __tablename__ = "print_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id"), nullable=True, index=True)
    source = Column(Enum(PrintSource), nullable=False, default=PrintSource.manual)
    door_label = Column(String(40), nullable=True)
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # How many cards the PDF held: one, or a batch.
    cards = Column(Integer, nullable=False, default=1)
    design_version = Column(Integer, nullable=False, default=0)
    # The rule was open by the organiser for this one card.
    overrode = Column(Boolean, nullable=False, default=False)
    error = Column(String(400), nullable=True)

    registration = relationship("EventRegistrationsLink")
    by = relationship("Users")
