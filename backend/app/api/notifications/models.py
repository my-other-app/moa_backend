from sqlalchemy import JSON, UUID, Column, Enum, ForeignKey, Integer, String
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin

from sqlalchemy.orm import relationship
import sqlalchemy as sa
from enum import Enum as PyEnum


class NotificationType(PyEnum):
    """All notification types supported by the system."""
    # Event-related
    certificate_generated = "certificate_generated"  # Certificate ready for download
    event_started = "event_started"                  # Event is starting soon
    event_invited = "event_invited"                  # User invited to event
    event_checkin = "event_checkin"                  # Check-in confirmation
    event_registered = "event_registered"            # Registration confirmation
    event_reminder = "event_reminder"                # Reminder before event
    
    # Club-related
    new_event = "new_event"                          # Followed club posted new event
    nearby_event = "nearby_event"                    # Event matching user interests
    club_announcement = "club_announcement"          # Club sent custom message
    
    # Volunteer-related
    volunteer_added = "volunteer_added"              # User added as volunteer
    volunteer_removed = "volunteer_removed"          # User removed as volunteer


class NotificationStatus(PyEnum):
    read = "read"
    unread = "unread"


class Notifications(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "notifications"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    type = Column(String(50), nullable=False)
    from_club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True)  # Link to relevant event
    status = Column(
        Enum(NotificationStatus), nullable=False, default=NotificationStatus.unread
    )
    data = Column(JSON, nullable=True)  # Additional data like certificate_id, etc.

    # Relationships
    user = relationship("Users", back_populates="notifications", foreign_keys=[user_id])
    from_club = relationship("Clubs", foreign_keys=[from_club_id])
    from_user = relationship("Users", foreign_keys=[from_user_id])
    event = relationship("Events", foreign_keys=[event_id])

