from sqlalchemy import Boolean, Column, Enum, Integer, String
from sqlmodel import Field, SQLModel
from sqlalchemy.orm import relationship
import enum

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from app.core.storage.fields import S3ImageField


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
    logo = Column(
        S3ImageField(
            upload_to="/organizations/logos/",
            variations={
                "thumbnail": {"width": 150, "height": 150},
                "medium": {"width": 500, "height": 500},
                "large": {"width": 800, "height": 800},
            },
        ),
        nullable=True,
    )
    is_verified = Column(Boolean, default=False, nullable=False)

    clubs = relationship("Clubs", back_populates="org")
