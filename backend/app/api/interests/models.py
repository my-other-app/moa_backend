import enum

from sqlalchemy import Column, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class InterestIconType(enum.Enum):
    emoji = "emoji"
    url = "url"


class InterestCategory(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "interest_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    icon = Column(String, nullable=True)
    icon_type = Column(Enum(InterestIconType), nullable=True)


class Interests(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "interests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    icon = Column(String, nullable=True)
    icon_type = Column(Enum(InterestIconType), nullable=True)
    category_id = Column(Integer, ForeignKey("interest_categories.id"), nullable=False)

    category = relationship("InterestCategory")
