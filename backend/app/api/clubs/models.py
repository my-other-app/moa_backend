# from geoalchemy2 import Geometry
from sqlalchemy import Boolean, Column, Enum, ForeignKey, Integer, String, JSON, Float
from sqlmodel import Field, SQLModel
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy.orm import relationship


class Clubs(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "clubs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    logo = Column(String, nullable=True)
    about = Column(String, nullable=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    # location = Column(Geometry("POINT"), nullable=True)
    location_name = Column(String, nullable=True)
    rating = Column(Float, nullable=False, default=0)
    total_ratings = Column(Integer, nullable=False, default=0)
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
    )

    user = relationship("Users", back_populates="club")
    org = relationship("Organizations", back_populates="clubs")


class ClubInterestsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "clubs_interests_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    club = relationship("Clubs")
    interest = relationship("Interests")


class ClubFollowersLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "clubs_followers_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_following = Column(Boolean, default=False, nullable=False)

    club = relationship("Clubs")
    user = relationship("Users")


class Notes(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    note = Column(String, nullable=False)

    club = relationship("Clubs")
    user = relationship("Users")
