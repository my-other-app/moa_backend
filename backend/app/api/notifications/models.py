from sqlalchemy import JSON, UUID, Column, Enum, ForeignKey, Integer, String
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin

from sqlalchemy.orm import relationship
import sqlalchemy as sa
from enum import Enum as PyEnum


class NotificationType(PyEnum):
    certificate_generated = "certificate_generated"
    event_started = "event_started"
    event_invited = "event_invited"
    event_checkin = "event_checkin"
    event_registered = "event_registered"


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
    status = Column(
        Enum(NotificationStatus), nullable=False, default=NotificationStatus.unread
    )
    data = Column(JSON, nullable=True)

    user = relationship("Users", back_populates="notifications", foreign_keys=[user_id])
    from_club = relationship("Clubs", foreign_keys=[from_club_id])
    from_user = relationship("Users", foreign_keys=[from_user_id])
    # user = relationship("Users", back_populates="notifications")
