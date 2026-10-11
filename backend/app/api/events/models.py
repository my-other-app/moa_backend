from datetime import datetime, timezone
from pydantic import ConfigDict
from sqlalchemy import (
    ARRAY,
    UUID,
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
    JSON,
    Float,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
import sqlalchemy as sa

from app.api.templates_audience import RegistrationRole
from app.api.users.models import UserAvatarTypes
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from app.core.storage.fields import S3FileField, S3ImageField
from app.core.utils.db_fields import TZAwareDateTime


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
    event_datetime = Column(TZAwareDateTime(timezone=True), nullable=False)
    has_fee = Column(Boolean, nullable=False, default=False)
    reg_fee = Column(Numeric(10, 2), nullable=True)
    duration = Column(Float, nullable=False)
    about = Column(String, nullable=True)
    location_name = Column(String, nullable=True)
    location_link = Column(String, nullable=True)
    has_prize = Column(Boolean, nullable=False, default=False)
    prize_amount = Column(Numeric(10, 2), nullable=True)
    contact_name = Column(String, nullable=True)
    contact_phone = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    url = Column(String, nullable=True)
    is_online = Column(Boolean, nullable=True)
    reg_startdate = Column(
        TZAwareDateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    reg_enddate = Column(TZAwareDateTime(timezone=True), nullable=True)
    max_participants = Column(Integer, nullable=True)
    additional_details = Column(ARRAY(JSON), nullable=True)
    event_guidelines = Column(String, nullable=True)
    page_views = Column(Integer, nullable=False, default=0)
    event_tag = Column(String(50), nullable=True)  # Tag like "Free", "Paid", "Featured", etc.

    category_id = Column(Integer, ForeignKey("event_categories.id"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    ticket_template_id = Column(Integer, ForeignKey("ticket_templates.id"), nullable=True)
    certificate_template_id = Column(Integer, ForeignKey("certificate_templates.id"), nullable=True)

    category = relationship("EventCategories")
    club = relationship("Clubs")
    # foreign_keys is required now that the relationship runs both ways:
    # an event points at a club template it was assigned, and an event's
    # own forked design points back at the event. Without naming the column,
    # SQLAlchemy cannot tell which of the two joins is meant.
    ticket_template = relationship(
        "TicketTemplates", foreign_keys=[ticket_template_id]
    )
    certificate_template = relationship(
        "CertificateTemplates", foreign_keys=[certificate_template_id]
    )
    registrations = relationship("EventRegistrationsLink", back_populates="event")
    interests = relationship(
        "Interests",
        secondary="event_interests_link",
        back_populates="events",
        uselist=True,
    )
    files = relationship("EventFiles", back_populates="event")
    ratings = relationship("EventRatingsLink", back_populates="event")
    speakers = relationship("EventSpeakers", back_populates="event", order_by="EventSpeakers.display_order")

    model_config = ConfigDict(from_attributes=True)

    @property
    def status(self) -> str:
        if getattr(self, "is_deleted", False):
            return "cancelled"
        return "published"


class EventSpeakers(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """Speakers and guests for events."""
    __tablename__ = "event_speakers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    name = Column(String(100), nullable=False)
    designation = Column(String(200), nullable=True)
    photo = Column(
        S3ImageField(
            upload_to="events/speakers/",
            variations={
                "thumbnail": {"width": 100, "height": 100},
                "medium": {"width": 200, "height": 200},
            },
        ),
        nullable=True,
    )
    display_order = Column(Integer, nullable=False, default=0)

    event = relationship("Events", back_populates="speakers")


class EventFiles(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_files"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
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


class EventPageViews(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_page_views"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_agent = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    event = relationship("Events")
    user = relationship("Users")


class EventInterestsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_interests_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    # event = relationship("Events")
    # interest = relationship("Interests")


class EventRegistrationsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_registrations_link"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    full_name = Column(String(100), nullable=False)
    email = Column(String(100), nullable=False)
    phone = Column(String(15), nullable=True)

    ticket_id = Column(String(60), nullable=False, unique=True, index=True)

    # What this person is at the event, which picks the ticket and
    # certificate variant they are issued. Everyone is an attendee unless
    # the organiser says otherwise.
    role = Column(
        Enum(RegistrationRole),
        nullable=False,
        default=RegistrationRole.attendee,
        server_default=RegistrationRole.attendee.value,
    )
    is_attended = Column(Boolean, nullable=False, default=False)
    attended_on = Column(TZAwareDateTime(timezone=True), nullable=True)
    # Set when the organiser revokes the ticket: the door refuses it by name
    # from the next scan, and nothing is sent for it until it is reinstated.
    ticket_revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # A certificate is issued by the organiser and can be taken back. Null
    # issued_at with is_attended keeps the certificate attended guests
    # always had, so turning issuing on removes nothing.
    certificate_issued_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    certificate_revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    certificate_design_version = Column(Integer, nullable=False, default=0, server_default="0")
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=True)

    is_won = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=True)

    is_paid = Column(Boolean, nullable=False, default=False)
    paid_amount = Column(Numeric(10, 2), nullable=False, default=0)
    actual_amount = Column(Numeric(10, 2), nullable=False, default=0)
    payment_receipt = Column(String, nullable=True)

    additional_details = Column(JSON, nullable=True)

    event = relationship("Events", back_populates="registrations")
    user = relationship("Users")
    volunteer = relationship("Volunteer")

    __table_args__ = (UniqueConstraint("event_id", "user_id", "is_deleted"),)

    @property
    def institution(self) -> str | None:
        if self.additional_details and isinstance(self.additional_details, dict):
            return (
                self.additional_details.get("College Name")
                or self.additional_details.get("Institution")
                or self.additional_details.get("college")
            )
        return None

    @property
    def name(self) -> str:
        return self.full_name


class EventRatingsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_ratings_link"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rating = Column(Float, nullable=False)
    review = Column(String, nullable=True)

    event = relationship("Events", back_populates="ratings")
    user = relationship("Users")

    __table_args__ = (UniqueConstraint("event_id", "user_id", "is_deleted"),)
