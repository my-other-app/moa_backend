# from geoalchemy2 import Geometry
import sqlalchemy as sa
from sqlalchemy import (
    DDL,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Float,
    UniqueConstraint,
)
from sqlalchemy import event as sa_event
from sqlmodel import Field, SQLModel
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

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
            public=True,
        ),
        nullable=True,
    )
    about = Column(String, nullable=True)
    # One line under the name on the club page and every event; the about is the long form.
    tagline = Column(String(120), nullable=True)
    cover = Column(
        S3ImageField(
            upload_to="clubs/covers/",
            variations={
                "medium": {"width": 1200, "height": 400},
                "large": {"width": 1800, "height": 600},
            },
            public=True,
        ),
        nullable=True,
    )
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    # location = Column(Geometry("POINT"), nullable=True)
    location_name = Column(String, nullable=True)
    location_link = Column(String, nullable=True)
    # Read from location_link when it is a map link, found from location_name
    # by the geocoder when it is not, or pinned by hand on the dashboard's map;
    # a club without any is placed by its events.
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    # 'manual' > 'link' > 'geocoded' (app.api.discover.geo.apply_place).
    geo_source = Column(String(10), nullable=True)
    rating = Column(Float, nullable=False, default=0)
    total_ratings = Column(Integer, nullable=False, default=0)
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
    )
    contact_phone = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    # Brand colours designs and emails fall back to when a template sets none; hex, like a design tool.
    brand_primary = Column(String(7), nullable=True)
    brand_secondary = Column(String(7), nullable=True)
    initial_password = Column(String, nullable=True)
    is_verified = Column(Boolean, nullable=False, default=False)
    # The plan key and cycle ('monthly' | 'yearly') chosen on the sign-up form,
    # for the admin approving it and for the dashboard to preselect afterwards.
    # A request, not a subscription: nothing is granted or charged by it.
    requested_plan = Column(String(40), nullable=True)
    requested_cycle = Column(String(10), nullable=True)
    # How many follow the club: kept by a trigger on club_users_link in the
    # same transaction as every follow, unfollow or delete there (see
    # FOLLOWER_COUNT_SQL below), so lists sort by it without counting.
    follower_count = Column(Integer, nullable=False, default=0, server_default="0")

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

    __table_args__ = (
        sa.Index("ix_clubs_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_clubs_tagline_trgm", "tagline", postgresql_using="gin", postgresql_ops={"tagline": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_clubs_location_trgm", "location_name", postgresql_using="gin", postgresql_ops={"location_name": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
    )


class ClubInterestsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "clubs_interests_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    club = relationship("Clubs")
    interest = relationship("Interests")

    __table_args__ = (
        sa.Index("ix_club_interests_club", "club_id", postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_club_interests_interest", "interest_id", "club_id", postgresql_where=sa.text("NOT is_deleted")),
    )


class ClubUsersLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "club_users_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_following = Column(Boolean, nullable=False, default=False)
    is_pinned = Column(Boolean, nullable=False, default=False)
    is_hidden = Column(Boolean, nullable=False, default=False)
    # The tracking link whose code the follow carried, when it came through one.
    tracking_link_id = Column(PG_UUID(as_uuid=True), ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True, index=True)

    club = relationship("Clubs", back_populates="followers")
    user = relationship("Users")

    __table_args__ = (
        UniqueConstraint("club_id", "user_id", "is_deleted"),
        # One person's clubs: following, pinned, hidden (the app's feeds).
        sa.Index("ix_club_users_user", "user_id", postgresql_where=sa.text("NOT is_deleted")),
    )


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


class ClubTeamMembers(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """The people who run the club, shown on the club page: a name, a role, a photo, and where to find them."""

    __tablename__ = "club_team_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    role = Column(String(80), nullable=True)
    instagram = Column(String(255), nullable=True)
    linkedin = Column(String(255), nullable=True)
    x = Column(String(255), nullable=True)
    website = Column(String(255), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    photo = Column(
        S3ImageField(upload_to="clubs/team/", variations={"thumbnail": {"width": 150, "height": 150}, "medium": {"width": 400, "height": 400}}, public=True),
        nullable=True,
    )


class ClubPartners(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """Year-round partners shown on the club page; event sponsors live on the event."""

    __tablename__ = "club_partners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    role = Column(String(80), nullable=True)
    since = Column(Integer, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    logo = Column(
        S3ImageField(upload_to="clubs/partners/", variations={"thumbnail": {"width": 150, "height": 150}, "medium": {"width": 400, "height": 400}}, public=True),
        nullable=True,
    )


# ─── clubs.follower_count, kept by the database ───────────────────────────
#
# Counts rows that are following and not deleted. A trigger, so a follow
# from the app, a tracking link, an admin tool or raw SQL is counted the same
# way, in the writer's transaction. The migration discover_perf installs the
# same function and trigger.

FOLLOWER_COUNT_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_follower_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.club_id = NEW.club_id AND OLD.is_following = NEW.is_following AND OLD.is_deleted = NEW.is_deleted THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.is_following AND NOT OLD.is_deleted THEN
        UPDATE clubs SET follower_count = follower_count - 1 WHERE id = OLD.club_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') AND NEW.is_following AND NOT NEW.is_deleted THEN
        UPDATE clubs SET follower_count = follower_count + 1 WHERE id = NEW.club_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_club_users_follower_count
    AFTER INSERT OR DELETE OR UPDATE OF club_id, is_following, is_deleted ON club_users_link
    FOR EACH ROW EXECUTE FUNCTION moa_follower_count()
    """,
)

for _sql in FOLLOWER_COUNT_SQL:
    sa_event.listen(ClubUsersLink.__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql"))
