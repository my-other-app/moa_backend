from sqlalchemy import (
    Enum,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    JSON,
)
from sqlalchemy.orm import relationship

from app.api.templates_audience import TemplateAudience
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from app.core.storage.fields import S3ImageField


class TicketTemplates(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "ticket_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)

    # Set when this design belongs to one event rather than the club's
    # library. An event starts out using the club default; the first edit
    # made on the event's own design page forks it here, after which changes
    # to the club template no longer reach it.
    event_id = Column(
        Integer,
        # use_alter: events points at templates and templates point back
        # at events, so the constraint has to be added after both tables
        # exist or create_all cannot order them.
        ForeignKey("events.id", use_alter=True, name="fk_ticket_templates_event_id"),
        nullable=True,
        index=True,
    )

    # Which role this variant is for. event_default is the one used when a
    # role has none of its own.
    audience = Column(
        Enum(TemplateAudience),
        nullable=False,
        default=TemplateAudience.event_default,
        server_default=TemplateAudience.event_default.value,
    )
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)

    # Uploaded images (S3)
    logo = Column(
        S3ImageField(upload_to="templates/tickets/logos/"),
        nullable=True,
    )
    secondary_logo = Column(
        S3ImageField(upload_to="templates/tickets/logos/"),
        nullable=True,
    )
    background_image = Column(
        S3ImageField(upload_to="templates/tickets/backgrounds/"),
        nullable=True,
    )

    # Style configuration JSON (all non-file options)
    config = Column(JSON, nullable=False, default=dict)

    club = relationship("Clubs")
    # The event this design was forked for, when it belongs to one. Named
    # explicitly because events also point back at templates they were
    # assigned, so the join is ambiguous otherwise.
    event = relationship("Events", foreign_keys=[event_id])
