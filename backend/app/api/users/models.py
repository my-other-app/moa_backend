import enum
import sqlalchemy as sa
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
    guest = "guest"
    club = "club"
    admin = "admin"


class AdminRoles(enum.Enum):
    """
    What an admin may do.

    There was one `admin` user_type and nothing else, so anyone who needed to
    look at the dashboard could delete clubs, refund payments and impersonate
    users. These narrow that without a second auth system: `user_type` still
    gates who is an admin at all, this gates what kind.
    """

    # Read anything, change nothing. For analysts and new joiners.
    viewer = "viewer"
    # Day-to-day moderation: approve, suspend, award, announce. No money, no
    # deletion of established records, no impersonation.
    support = "support"
    # Everything, including refunds, deletion and impersonation.
    owner = "owner"


class SigninProviders(enum.Enum):
    google = "google"
    apple = "apple"
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
    token_version = Column(Integer, nullable=False, default=0)
    # Suspension, as distinct from deletion. The only lever an admin had was
    # soft-delete, which is permanent in practice and removes the account
    # rather than restricting it. A suspended user keeps their data and can be
    # reinstated; the auth dependency refuses them while the flag is set.
    is_active = Column(Boolean, nullable=False, default=True, server_default=sa.text("true"))
    suspended_reason = Column(String(255), nullable=True)
    # Only meaningful when user_type is admin. Defaults to owner so existing
    # admins keep the access they have today -- a migration that silently
    # demoted everyone would lock the platform out of itself.
    # Two-factor, for admins. A stolen admin password is otherwise the whole
    # chain: impersonation, refunds and deletion all sit behind it.
    totp_secret = Column(String(64), nullable=True)
    totp_enabled = Column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    # Hashed, never stored in the clear -- they are password equivalents.
    totp_recovery_codes = Column(sa.JSON, nullable=True)

    admin_role = Column(
        Enum(AdminRoles), nullable=False, default=AdminRoles.owner,
        server_default="owner",
    )

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

    # user = relationship("Users")
    interest = relationship("Interests")


class UserDeviceTokens(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """Store FCM tokens for push notifications per user/device."""
    __tablename__ = "user_device_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    fcm_token = Column(String(512), nullable=False)
    platform = Column(String(20), nullable=False)  # ios / android

    user = relationship("Users")
    
    __table_args__ = (
        UniqueConstraint("user_id", "platform", name="uq_user_platform"),
    )

