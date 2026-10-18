import uuid

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
    club_thread = "club_thread"


class NotificationStatus(PyEnum):
    read = "read"
    unread = "unread"


class Notifications(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "notifications"

    # Made in Python, so the ORM can write a batch of these in one statement:
    # with the SQL default it had to fetch each new key back, one INSERT per row.
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    type = Column(String(50), nullable=False)
    from_club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Indexed since add_event_id_notifications (ix_notifications_event_id); declared here too.
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)  # Link to relevant event
    status = Column(
        Enum(NotificationStatus), nullable=False, default=NotificationStatus.unread
    )
    data = Column(JSON, nullable=True)  # Additional data like certificate_id, etc.

    # Relationships
    user = relationship("Users", back_populates="notifications", foreign_keys=[user_id])
    from_club = relationship("Clubs", foreign_keys=[from_club_id])
    from_user = relationship("Users", foreign_keys=[from_user_id])
    event = relationship("Events", foreign_keys=[event_id])

    __table_args__ = (
        # The inbox, newest first, and its unread count: both by person. Without
        # these every inbox read, count and mark-all-read walked the whole table.
        sa.Index(
            "ix_notifications_user_created",
            "user_id",
            sa.text("created_at DESC"),
            postgresql_where=sa.text("NOT is_deleted"),
        ),
        sa.Index(
            "ix_notifications_user_unread",
            "user_id",
            postgresql_where=sa.text("status = 'unread' AND NOT is_deleted"),
        ),
    )
