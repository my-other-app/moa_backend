from datetime import datetime, timezone
from geoalchemy2 import Geometry
from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    JSON,
    Float,
)
from app.api.users.models import UserAvatarTypes
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
import enum
from sqlalchemy.orm import relationship


class EventCategories(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(20), nullable=False)
    icon = Column(String, nullable=True)
    icon_type = Column(Enum(UserAvatarTypes))
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    created_by = relationship("Users")


class Events(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    poster = Column(String, nullable=True)
    images = Column(ARRAY(String), nullable=False, default=[])
    event_datetime = Column(DateTime(timezone=True), nullable=False)
    has_fee = Column(Boolean, nullable=False, default=False)
    reg_fee = Column(Float, nullable=True)
    duration = Column(Float, nullable=False)
    about = Column(String, nullable=True)
    # location = Column(Geometry("POINT"), nullable=True)
    location_name = Column(String, nullable=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    has_prize = Column(Boolean, nullable=False, default=False)
    prize_amount = Column(Float, nullable=True)
    contact_phone = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    url = Column(String, nullable=True)
    is_online = Column(Boolean, nullable=True)
    reg_startdate = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    reg_enddate = Column(DateTime(timezone=True), nullable=True)
    category_id = Column(Integer, ForeignKey("event_categories.id"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    created_by = relationship("Users")
    category = relationship("EventCategories")
    org = relationship("Organizations")
    club = relationship("Clubs")

    # socials = Column(JSON, nullable=False, default=[])

    # def set_location(self, longitude, latitude):
    #     self.location = f"POINT({longitude} {latitude})"


class EventInterestsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_interests_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    event = relationship("Events")
    interest = relationship("Interests")


class EventParticipantsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_participants_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    event = relationship("Events")
    user = relationship("Users")
