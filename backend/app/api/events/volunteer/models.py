import sqlalchemy as sa
from sqlalchemy import (
    UUID,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
)
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy.orm import relationship


class Volunteer(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "volunteers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(100), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    is_approved = Column(Boolean, nullable=False, default=False)
    # What they may do on the gate console, and the door they are expected on.
    can_check_in = Column(Boolean, nullable=False, default=True)
    can_walkup = Column(Boolean, nullable=False, default=False)
    can_merch = Column(Boolean, nullable=False, default=False)
    door_id = Column(UUID(as_uuid=True), ForeignKey("doors.id", ondelete="SET NULL"), nullable=True)

    event = relationship("Events")
    user = relationship("Users")
    club = relationship("Clubs")
    door = relationship("Doors")

    __table_args__ = (
        # By person: every gate request from a signed-in volunteer finds their row by it.
        sa.Index("ix_volunteers_user", "user_id", postgresql_where=sa.text("NOT is_deleted")),
        # An event's team, and the host's and co-hosts' club-wide volunteers.
        sa.Index("ix_volunteers_event", "event_id", postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_volunteers_club", "club_id", postgresql_where=sa.text("NOT is_deleted")),
    )
