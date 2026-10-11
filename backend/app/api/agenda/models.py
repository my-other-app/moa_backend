"""The run of show: sessions with a time, a speaker and a room, and the
programme design that draws them on a kiosk screen or an A4 page.

An agenda template resolves like a badge's, through the same resolver, so
a club can keep one programme look for every event and an event can have
its own.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy import JSON, UUID, Boolean, Column, ForeignKey, Integer, String, Enum
from sqlalchemy.orm import relationship

from app.api.templates_audience import TemplateAudience
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class AgendaItems(AbstractSQLModel, TimestampsMixin):
    """One block of the run of show. Ordered by start time; ties keep the order
    they were added so parallel tracks stay put."""

    __tablename__ = "agenda_items"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    starts_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    ends_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    title = Column(String(160), nullable=False)
    description = Column(String(1000), nullable=True)
    speaker = Column(String(120), nullable=True)
    speaker_photo_key = Column(String(300), nullable=True)
    location = Column(String(120), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)

    event = relationship("Events")


class AgendaTemplates(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """The programme design: a layered layout under config["layout"]."""

    __tablename__ = "agenda_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    audience = Column(Enum(TemplateAudience), nullable=False, default=TemplateAudience.event_default)
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)
    config = Column(JSON, nullable=False, default=dict)
