from sqlalchemy import (
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

    event = relationship("Events")
    user = relationship("Users")
    club = relationship("Clubs")
