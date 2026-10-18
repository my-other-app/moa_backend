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

from app.core.utils.db_fields import TZAwareDateTime
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
    # When the map link was last followed for a point (discover.geo.missing).
    link_checked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
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
    # Where "clubs near" puts the club: its own point (lat, lng) when it has
    # one, else the middle of its in-person events' points; null with neither.
    # Kept by triggers on clubs and events in the writer's own transaction
    # (app.api.discover.places), so a box on ix_clubs_place finds the clubs
    # near a point without averaging every club's events.
    place_lat = Column(Float, nullable=True)
    place_lng = Column(Float, nullable=True)
    # Whether the public may see the club and what it runs: verified, not
    # deleted, and its organisation (if any) not blocked. Kept by triggers on
    # clubs and organizations in the writer's transaction (LISTED_SQL below),
    # so every public rule is one column and a block on an organisation
    # reaches each of its clubs at once. The database writes it; an insert or
    # update through the ORM reads it back (eager_defaults, RETURNING), but a
    # block on the organisation changes it behind any loaded copy, so the
    # rules ask for it in a WHERE rather than off an object.
    listed = Column(Boolean, nullable=False, server_default=sa.false(), server_onupdate=sa.FetchedValue())
    __mapper_args__ = {"eager_defaults": True}

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

    @property
    def topics(self) -> list[dict]:
        """The topics of its interests, when loaded with their categories (app.api.interests.topics.loaded_topics)."""
        from app.api.interests.topics import loaded_topics

        return loaded_topics(self)

    __table_args__ = (
        sa.Index("ix_clubs_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_clubs_tagline_trgm", "tagline", postgresql_using="gin", postgresql_ops={"tagline": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_clubs_location_trgm", "location_name", postgresql_using="gin", postgresql_ops={"location_name": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
        # The club an account owns: every signed-in request loads it with the
        # user (app.core.auth), which read the whole table without this.
        sa.Index("ix_clubs_user_id", "user_id"),
        # "Clubs near": the listed clubs that have a place, by latitude then longitude.
        sa.Index(
            "ix_clubs_place", "place_lat", "place_lng",
            postgresql_where=sa.text("is_verified AND NOT is_deleted AND place_lat IS NOT NULL AND place_lng IS NOT NULL"),
        ),
        # The listed clubs, most followed first, then by name as Python
        # orders it, then id: the app's club list and discover's "popular"
        # walk it and stop at the page instead of sorting every club.
        # The clubs whose map link may still give a point, least recently tried first (geo.missing).
        sa.Index(
            "ix_clubs_link_unchecked", sa.text("link_checked_at NULLS FIRST"), "id",
            postgresql_where=sa.text("location_link IS NOT NULL AND location_link <> '' AND NOT is_deleted AND (lat IS NULL OR geo_source = 'geocoded')"),
        ),
        sa.Index(
            "ix_clubs_listed_popular", sa.text("follower_count DESC"), sa.text('lower(name) COLLATE "C"'), "id",
            postgresql_where=sa.text("listed"),
        ),
        # The listed clubs, newest first: the app's club list (its default order) and discover's sort=newest.
        sa.Index("ix_clubs_listed_newest", sa.text("created_at DESC"), sa.text("id DESC"), postgresql_where=sa.text("listed")),
        # The few clubs that are not listed: "not one of these" is a hashed
        # list read from this index, where a join would look up each event's club.
        sa.Index("ix_clubs_unlisted", "id", postgresql_where=sa.text("NOT listed")),
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
        # One live row per person and club: follow, pin and hide write it with
        # ON CONFLICT (club_id, user_id, is_deleted), and "is following" is a
        # probe of it.
        UniqueConstraint("club_id", "user_id", "is_deleted"),
        # One person's clubs: following, pinned, hidden (the app's feeds).
        sa.Index("ix_club_users_user", "user_id", postgresql_where=sa.text("NOT is_deleted")),
        # A club's followers, newest first, a page at a time (keyset on created_at, id).
        sa.Index(
            "ix_club_users_followers", "club_id", sa.text("created_at DESC"), sa.text("id DESC"),
            postgresql_where=sa.text("is_following AND NOT is_deleted"),
        ),
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


# ─── clubs.listed, kept by the database ───────────────────────────────────
#
# Verified, not deleted, and not under a blocked organisation. On clubs, a
# BEFORE trigger works it out on every insert and on every change to what it
# rests on (or to `listed` itself, so a hand edit is put right); on
# organizations, a change of `is_blocked` touches the organisation's clubs so
# their own trigger works it out again. The migration discover_integrity
# installs the same functions and triggers, and fills the column.

LISTED_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_club_listed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.listed := coalesce(NEW.is_verified, false) AND NOT coalesce(NEW.is_deleted, false)
        AND (NEW.org_id IS NULL OR NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = NEW.org_id AND o.is_blocked));
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_org_listed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      UPDATE clubs SET listed = listed WHERE org_id = NEW.id;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_clubs_listed BEFORE INSERT OR UPDATE OF is_verified, is_deleted, org_id, listed ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_club_listed()
    """,
    """
    CREATE TRIGGER trg_organizations_listed AFTER UPDATE OF is_blocked ON organizations
    FOR EACH ROW WHEN (OLD.is_blocked IS DISTINCT FROM NEW.is_blocked)
    EXECUTE FUNCTION moa_org_listed()
    """,
)

# The organizations table is created before clubs (clubs.org_id refers to it).
for _sql in LISTED_SQL:
    sa_event.listen(Clubs.__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql"))
