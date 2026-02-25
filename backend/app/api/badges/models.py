import enum
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Float,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class BadgeType(enum.Enum):
    user = "user"
    club = "club"


class BadgeDefinitions(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "badge_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(50), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    description = Column(String(255), nullable=False)
    emoji = Column(String(10), nullable=False)
    badge_type = Column(Enum(BadgeType), nullable=False)
    trigger_metric = Column(String(50), nullable=False)  # e.g. "events_attended", "events_hosted"
    threshold = Column(Float, nullable=False, default=1)


class UserBadges(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "user_badges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    badge_id = Column(Integer, ForeignKey("badge_definitions.id"), nullable=False)
    awarded_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user = relationship("Users")
    badge = relationship("BadgeDefinitions")

    __table_args__ = (
        UniqueConstraint("user_id", "badge_id", name="uq_user_badge"),
    )


class ClubBadges(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_badges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    badge_id = Column(Integer, ForeignKey("badge_definitions.id"), nullable=False)
    awarded_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    club = relationship("Clubs")
    badge = relationship("BadgeDefinitions")

    __table_args__ = (
        UniqueConstraint("club_id", "badge_id", name="uq_club_badge"),
    )
