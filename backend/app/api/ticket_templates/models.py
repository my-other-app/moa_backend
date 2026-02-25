from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    JSON,
)
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from app.core.storage.fields import S3ImageField


class TicketTemplates(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "ticket_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
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
