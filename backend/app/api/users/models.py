import enum
import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
)
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy.orm import deferred, relationship
from app.db.base import AbstractSQLModel
from app.core.storage.fields import S3ImageField
from app.core.utils.db_fields import TZAwareDateTime


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


#: Providers that check the address before they hand it over. An account
#: made through one owns its address from the start.
ADDRESS_CHECKING_PROVIDERS = frozenset({SigninProviders.google.value, SigninProviders.apple.value})


class UserAvatars(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "user_avatars"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(20), nullable=False)
    # The stock avatars people choose from (an admin uploads them): the
    # same pictures for everyone, so public -- signed for a week, or on the
    # media CDN. A person's own photo (UserProfiles.profile_pic) is not.
    image = Column(
        S3ImageField(
            upload_to="avatars/",
            variations={
                "thumbnail": {"width": 150, "height": 150},
                "medium": {"width": 500, "height": 500},
                "large": {"width": 800, "height": 800},
            },
            public=True,
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
    # When the owner of `email` showed it is theirs: a code sent there was
    # typed back, or Google or Apple vouched for it. A password account's
    # address is only typed at sign-up, so until this is set nothing that
    # belongs to the address -- guests' tickets above all -- is given to it.
    email_verified_at = Column(TZAwareDateTime(timezone=True), nullable=True)
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
    # Encrypted (app.api.auth.totp) when APP_PAYMENT_KEYS_SECRET is set; a
    # secret saved before that, or without it, is the plain base32.
    totp_secret = Column(String(255), nullable=True)
    # The last 30-second step a code was accepted for: a code is good once.
    totp_last_step = Column(sa.BigInteger, nullable=True)
    totp_enabled = Column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    # An email when a device signs in that has not been seen for this account before.
    notify_new_device = Column(Boolean, nullable=False, default=True, server_default=sa.text("true"))
    # Whether others checked in at the same event see this person in People
    # Like You (app/api/events/networking.py). On unless they turn it off.
    networking_visible = Column(Boolean, nullable=False, default=True, server_default=sa.text("true"))
    # Hashed, never stored in the clear -- they are password equivalents.
    totp_recovery_codes = Column(sa.JSON, nullable=True)

    admin_role = Column(
        Enum(AdminRoles), nullable=False, default=AdminRoles.owner,
        server_default="owner",
    )
    # The name, username and address, lowercased and unaccented, for the
    # admin's search (app.api.search.text). Kept by a trigger; only SQL reads it.
    search_text = deferred(Column(sa.Text, nullable=True))

    profile = relationship("UserProfiles", uselist=False)
    club = relationship("Clubs", uselist=False)
    notifications = relationship(
        "Notifications", back_populates="user", foreign_keys="Notifications.user_id"
    )
    interests = relationship("Interests", secondary="user_interests", uselist=True)

    @property
    def email_verified(self) -> bool:
        """Whether the account has shown it owns its address.

        Google and Apple accounts always have: the provider checked before
        handing the address over. They carry the stamp too (set when made,
        and backfilled), but an account a build without the stamp made in
        the minute around a deploy is still theirs.
        """
        if self.email_verified_at is not None:
            return True
        return getattr(self.provider, "value", self.provider) in ADDRESS_CHECKING_PROVIDERS

    __table_args__ = (
        # One live account per address, and per phone, for each kind of
        # account -- except guests. The public form makes a fresh guest for
        # every sign-up without a sign-in, so one address can stand behind
        # several guests until they are merged into the app account it
        # belongs to. Deleted accounts are left out: they held the address for
        # ever, so a person who deleted their account could never sign up
        # again (a 500 from /user/register and /auth/google).
        sa.Index(
            "uq_users_email_user_type",
            "email",
            "user_type",
            unique=True,
            postgresql_where=sa.text("user_type <> 'guest' AND NOT is_deleted"),
        ),
        sa.Index(
            "uq_users_phone_user_type",
            "phone",
            "user_type",
            unique=True,
            postgresql_where=sa.text("user_type <> 'guest' AND NOT is_deleted"),
        ),
        # Addresses are matched trimmed and case-blind, and the merge looks
        # for guests this way on every app launch.
        sa.Index("ix_users_email_normalised", sa.func.lower(sa.func.btrim(email))),
        # The admin's user list: newest first, a page at a time by keyset.
        sa.Index(
            "ix_users_created_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
            postgresql_where=sa.text("NOT is_deleted"),
        ),
        # The admin's search: "contains" on the name, username and address,
        # all three in search_text (app.api.search.text), one trigram index.
        sa.Index("ix_users_search_trgm", "search_text", postgresql_using="gin", postgresql_ops={"search_text": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
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

    __table_args__ = (
        sa.Index("ix_user_profiles_user", "user_id"),
        # One live profile per account. Two requests making the first one at
        # once used to make two, and which one an account showed was then
        # whichever Postgres returned first.
        sa.Index("ux_user_profiles_user_live", "user_id", unique=True, postgresql_where=sa.text("NOT is_deleted")),
    )

    def __repr__(self):
        return self.username


class UserInterests(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "user_interests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    # user = relationship("Users")
    interest = relationship("Interests")

    __table_args__ = (
        sa.Index("ix_user_interests_user", "user_id"),
        # One row per interest a person picked: selecting is one INSERT ...
        # ON CONFLICT DO NOTHING against this.
        sa.Index("ux_user_interests_user_interest", "user_id", "interest_id", unique=True),
        # Who picked an interest: the new-event push to people whose
        # interests match reads it this way round.
        sa.Index("ix_user_interests_interest", "interest_id", "user_id"),
    )


class UserDeviceTokens(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """FCM tokens for push notifications: one row per device.

    A token names one install of the app, so it is unique, and belongs to
    whichever account last signed in there. It used to be unique per (user,
    platform), so a second Android phone replaced the first and only one of a
    person's devices ever got their pushes."""
    __tablename__ = "user_device_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    fcm_token = Column(String(512), nullable=False)
    platform = Column(String(20), nullable=False)  # ios / android
    # The app on this device subscribes to its person's FCM topics (it said
    # `topics: 1` when it registered the token). Broadcasts reach it by
    # topic once PUSH_TOPICS is on; a device without is sent them one by one.
    supports_topics = Column(Boolean, nullable=False, default=False, server_default=sa.false())

    user = relationship("Users")

    __table_args__ = (
        sa.Index("ux_user_device_tokens_token", "fcm_token", unique=True),
        sa.Index("ix_user_device_tokens_user_id", "user_id"),
    )
