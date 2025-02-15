import enum
from sqlalchemy import Boolean, Column, Enum, ForeignKey, Integer, String
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy.orm import relationship
from app.db.base import AbstractSQLModel


class UserAvatarTypes(enum.Enum):
    emoji = "emoji"
    url = "url"


class UserAvatars(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "user_avatars"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(20), nullable=False)
    icon_type = Column(Enum(UserAvatarTypes), nullable=False)
    content = Column(String(200), nullable=False)


class Users(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), nullable=False, unique=True)
    full_name = Column(String(100), nullable=False)
    email = Column(String(100), nullable=False, unique=True)
    phone = Column(String(20), nullable=True, unique=True)
    whatsapp = Column(String(20), nullable=True, unique=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    password = Column(String(100), nullable=False)
    avatar_id = Column(Integer, ForeignKey("user_avatars.id"), nullable=True)
    profile_pic = Column(String, nullable=True)
    is_admin = Column(Boolean, nullable=False, default=False)

    org = relationship("Organizations")
    avatar = relationship("UserAvatars")

    def __repr__(self):
        return self.username


class UserInterests(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "user_interests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    user = relationship("Users")
    interest = relationship("Interests")
