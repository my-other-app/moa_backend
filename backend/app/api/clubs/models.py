# from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Float,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy.orm import relationship

from app.core.storage.fields import S3ImageField


class Clubs(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "clubs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(120), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    logo = Column(
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
    about = Column(String, nullable=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    # location = Column(Geometry("POINT"), nullable=True)
    location_name = Column(String, nullable=True)
    location_link = Column(String, nullable=True)
    rating = Column(Float, nullable=False, default=0)
    total_ratings = Column(Integer, nullable=False, default=0)
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
    )
    contact_phone = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    initial_password = Column(String, nullable=True)
    is_verified = Column(Boolean, nullable=False, default=False)

    user = relationship("Users", back_populates="club")
    org = relationship("Organizations", back_populates="clubs")
    followers = relationship("ClubUsersLink", back_populates="club")
    interests = relationship(
        "Interests",
        secondary="clubs_interests_link",
        back_populates="clubs",
    )
    events = relationship("Events", back_populates="club")
    notes = relationship("Notes", back_populates="club")
    socials = relationship("ClubSocials", back_populates="club", uselist=False)


class ClubInterestsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "clubs_interests_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    club = relationship("Clubs")
    interest = relationship("Interests")


class ClubUsersLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "club_users_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_following = Column(Boolean, nullable=False, default=False)
    is_pinned = Column(Boolean, nullable=False, default=False)
    is_hidden = Column(Boolean, nullable=False, default=False)

    club = relationship("Clubs", back_populates="followers")
    user = relationship("Users")

    __table_args__ = (UniqueConstraint("club_id", "user_id", "is_deleted"),)


class Notes(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    note = Column(String, nullable=False)

    club = relationship("Clubs")
    user = relationship("Users")


class ClubSocials(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "club_socials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    instagram = Column(String(255), nullable=True)
    linkedin = Column(String(255), nullable=True)
    youtube = Column(String(255), nullable=True)
    website = Column(String(255), nullable=True)

    club = relationship("Clubs", back_populates="socials")

    __table_args__ = (UniqueConstraint("club_id", "is_deleted"),)
