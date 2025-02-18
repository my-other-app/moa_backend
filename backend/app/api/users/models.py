import enum
from sqlalchemy import (
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy.orm import relationship
from app.db.base import AbstractSQLModel


class UserAvatarTypes(enum.Enum):
    emoji = "emoji"
    url = "url"


class UserTypes(enum.Enum):
    app_user = "app_user"
    club = "club"
    admin = "admin"


class UserAvatars(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "user_avatars"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(20), nullable=False)
    icon_type = Column(Enum(UserAvatarTypes), nullable=False)
    content = Column(String(200), nullable=False)


class Users(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    full_name = Column(String(100), nullable=False)
    username = Column(String(100), nullable=False, unique=True)
    email = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=True)
    password = Column(String(100), nullable=False)
    user_type = Column(Enum(UserTypes), nullable=False, default=UserTypes.app_user)

    user_profiles = relationship("UserProfiles", uselist=False)
    club = relationship("Clubs", uselist=False)

    __table_args__ = (
        UniqueConstraint(
            "email",
            "user_type",
        ),
        UniqueConstraint("phone", "user_type"),
    )


class UserProfiles(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    whatsapp = Column(String(20), nullable=True, unique=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    avatar_id = Column(Integer, ForeignKey("user_avatars.id"), nullable=True)
    profile_pic = Column(String, nullable=True)

    user = relationship("Users", back_populates="user_profiles")
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
