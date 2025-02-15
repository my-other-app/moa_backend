from sqlalchemy import Boolean, Column, Enum, Integer, String
from sqlmodel import Field, SQLModel
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
import enum


class OrgTypes(enum.Enum):
    school = "school"
    university = "university"
    college = "college"
    company = "company"
    other = "other"


class Organizations(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    type = Column(Enum(OrgTypes), nullable=False)
    address = Column(String(200), nullable=True)
    phone = Column(String(20), nullable=True)
    email = Column(String(100), nullable=True)
    website = Column(String(100), nullable=True)
    logo = Column(String(100), nullable=True)
    is_verified = Column(Boolean, default=False, nullable=False)
