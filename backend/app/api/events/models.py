from datetime import datetime, timezone
from pydantic import ConfigDict
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
    UniqueConstraint,
)
from app.api.users.models import UserAvatarTypes
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy.orm import relationship

from app.core.storage.fields import S3FileField, S3ImageField


class EventCategories(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    icon = Column(String, nullable=True)
    icon_type = Column(Enum(UserAvatarTypes))
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    created_by = relationship("Users")


class Events(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(120), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    poster = Column(
        S3ImageField(
            upload_to="clubs/logos/",
            variations={
                "thumbnail": {"width": 150, "height": 150},
                "medium": {"width": 500, "height": 500},
                "large": {"width": 800, "height": 800},
            },
        ),
        nullable=True,
    )
    images = Column(ARRAY(String), nullable=False, default=[])
    event_datetime = Column(DateTime(timezone=True), nullable=False)
    has_fee = Column(Boolean, nullable=False, default=False)
    reg_fee = Column(Float, nullable=True)
    duration = Column(Float, nullable=False)
    about = Column(String, nullable=True)
    location_name = Column(String, nullable=True)
    location_link = Column(String, nullable=True)
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
    max_participants = Column(Integer, nullable=True)
    additional_details = Column(ARRAY(JSON), nullable=True)
    event_guidelines = Column(String, nullable=True)

    category_id = Column(Integer, ForeignKey("event_categories.id"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)

    category = relationship("EventCategories")
    club = relationship("Clubs")
    registrations = relationship("EventRegistrationsLink", back_populates="event")
    interests = relationship(
        "Interests",
        secondary="event_interests_link",
        back_populates="events",
        uselist=True,
    )
    files = relationship("EventFiles", back_populates="event")
    ratings = relationship("EventRatingsLink", back_populates="event")

    model_config = ConfigDict(from_attributes=True)


class EventFiles(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    file = Column(
        S3FileField(
            upload_to="events/files/",
            allowed_extensions=[
                "pdf",
                "doc",
                "docx",
                "ppt",
                "pptx",
                "xls",
                "xlsx",
                "txt",
                "png",
                "jpg",
                "jpeg",
                "gif",
            ],
        ),
        nullable=False,
    )
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)

    event = relationship("Events", back_populates="files")


class EventInterestsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_interests_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    # event = relationship("Events")
    # interest = relationship("Interests")


class EventRegistrationsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_registrations_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    full_name = Column(String(100), nullable=False)
    email = Column(String(100), nullable=False)
    phone = Column(String(15), nullable=True)

    ticket_id = Column(String(60), nullable=False, unique=True, index=True)

    is_attended = Column(Boolean, nullable=False, default=False)
    attended_on = Column(DateTime(timezone=True), nullable=True)

    is_won = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=True)

    is_paid = Column(Boolean, nullable=False, default=False)
    paid_amount = Column(Float, nullable=False, default=0)
    actual_amount = Column(Float, nullable=False, default=0)
    payment_receipt = Column(String, nullable=True)

    additional_details = Column(JSON, nullable=True)

    event = relationship("Events", back_populates="registrations")
    user = relationship("Users")

    __table_args__ = (UniqueConstraint("event_id", "user_id", "is_deleted"),)


class EventRatingsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_ratings_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rating = Column(Float, nullable=False)
    review = Column(String, nullable=True)

    event = relationship("Events", back_populates="ratings")
    user = relationship("Users")

    __table_args__ = (UniqueConstraint("event_id", "user_id", "is_deleted"),)
