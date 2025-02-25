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
from app.core.storage.fields import S3ImageField


class UserAvatarTypes(enum.Enum):
    emoji = "emoji"
    url = "url"


class UserTypes(enum.Enum):
    app_user = "app_user"
    club = "club"
    admin = "admin"


class SigninProviders(enum.Enum):
    google = "google"
    email = "email"


class UserAvatars(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "user_avatars"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(20), nullable=False)
    image = Column(
        S3ImageField(
            upload_to="avatars/",
            variations={
                "thumbnail": {"width": 150, "height": 150},
                "medium": {"width": 500, "height": 500},
                "large": {"width": 800, "height": 800},
            },
        ),
        nullable=False,
    )


class Users(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    full_name = Column(String(100), nullable=False)
    username = Column(String(100), nullable=False, unique=True)
    email = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=True)
    password = Column(String(100), nullable=True)
    provider = Column(Enum(SigninProviders), nullable=True)
    user_type = Column(Enum(UserTypes), nullable=False, default=UserTypes.app_user)

    profile = relationship("UserProfiles", uselist=False)
    club = relationship("Clubs", uselist=False)
    notifications = relationship(
        "Notifications", back_populates="user", foreign_keys="Notifications.user_id"
    )
    interests = relationship("Interests", secondary="user_interests", uselist=True)

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
    full_name = Column(String(100), nullable=False)
    whatsapp = Column(String(20), nullable=True, unique=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    avatar_id = Column(Integer, ForeignKey("user_avatars.id"), nullable=True)
    profile_pic = Column(
        S3ImageField(
            upload_to="users/profile_pic/",
            variations={
                "thumbnail": {"width": 150, "height": 150},
                "medium": {"width": 500, "height": 500},
                "large": {"width": 800, "height": 800},
            },
        ),
        nullable=True,
    )

    user = relationship("Users", back_populates="profile")
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
