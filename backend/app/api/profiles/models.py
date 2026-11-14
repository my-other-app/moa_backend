"""Profiles and the Network: a person's public page, their QR code, and the
people they saved at events (feature-register/profiles-networking-contract.md).

- profile_links / profile_projects: the page's links and projects, in the
  person's order (`position`).
- profile_codes: what the QR opens (myotherapp.in/p/<token>). One live code
  a person; a replaced one keeps its row with `replaced_at` and answers 410.
- network_people: one person's save of another. One-way; a pair is mutual
  when both saved each other, kept on both rows in the transaction that
  saves or removes either (network.py, under the pair's lock).
- network_encounters: when and how they met: at most one per event (the
  partial unique index), any number without one (one a day, network.py).
- network_save_notices: the 10-minute batch of "X saved your profile"
  pushes, one open row per (person, event).
- network_recaps: the morning-after recap each person got for an event, so a
  second run of the job sends none (recap.py).
- released_handles: a handle given up by a rename or an account's own
  deletion, held HANDLE_HOLD (30 days) so old /u/ links (vCards, recap
  mails) never open someone else's page.

The person's switches and section levels are columns of user_profiles
(app/api/users/models.py).
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, Column, Date, ForeignKey, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB

from app.core.storage.fields import S3ImageField
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

#: A public profile's sections, each with its own level.
SECTIONS = ("links", "projects", "achievements", "interests", "org")
#: Who sees a section: anyone; people either side saved; both saved; only the owner.
LEVELS = ("everyone", "met", "mutual", "me")
#: Who sees the address and the number.
SHARE_LEVELS = ("nobody", "mutual", "met")
LINK_TYPES = ("linkedin", "github", "behance", "dribbble", "medium", "youtube", "x", "instagram", "website", "portfolio", "other")
METHODS = ("qr", "discovery", "link", "search")

MAX_LINKS = 10
#: How long a handle given up stays out of reach of anyone else.
HANDLE_HOLD_DAYS = 30
MAX_PROJECTS = 12
MAX_PROJECT_TAGS = 5
MAX_NETWORK_TAGS = 10
MAX_TAG_CHARS = 24

PROJECT_IMAGE_SIZES = {
    "thumbnail": {"width": 320, "height": 320},
    "medium": {"width": 800, "height": 800},
    "large": {"width": 1600, "height": 1600},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProfileLinks(AbstractSQLModel):
    __tablename__ = "profile_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(16), nullable=False)
    url = Column(String(500), nullable=False)
    label = Column(String(60), nullable=True)
    position = Column(SmallInteger, nullable=False, default=0, server_default="0")

    __table_args__ = (sa.Index("ix_profile_links_user_position", "user_id", "position"),)


class ProfileProjects(AbstractSQLModel):
    __tablename__ = "profile_projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(80), nullable=False)
    summary = Column(String(160), nullable=True)
    url = Column(String(500), nullable=True)
    # The existing image pipeline: any picture format in, stored upright,
    # without metadata, in three sizes beside the original. Private like a
    # profile photo: links signed for an hour.
    image_key = Column(S3ImageField(upload_to="users/projects/", variations=PROJECT_IMAGE_SIZES), nullable=True)
    tags = Column(JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb"))
    role = Column(String(60), nullable=True)
    #: The first of the month it was made in.
    month = Column(Date, nullable=True)
    position = Column(SmallInteger, nullable=False, default=0, server_default="0")
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))

    __table_args__ = (sa.Index("ix_profile_projects_user_position", "user_id", "position"),)


class ProfileCodes(AbstractSQLModel):
    __tablename__ = "profile_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(16), nullable=False, unique=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    replaced_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One live code a person: two first opens of My QR at once make one.
        sa.Index("ux_profile_codes_user_live", "user_id", unique=True, postgresql_where=sa.text("replaced_at IS NULL")),
    )


class NetworkPeople(AbstractSQLModel):
    __tablename__ = "network_people"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    person_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    note = Column(String(500), nullable=True)
    tags = Column(JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb"))
    mutual = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.UniqueConstraint("owner_id", "person_id", name="uq_network_people_owner_person"),
        CheckConstraint("owner_id <> person_id", name="ck_network_people_not_self"),
        # My network, newest first.
        sa.Index("ix_network_people_owner_created", "owner_id", sa.text("created_at DESC")),
        # Who saved me (saved-me, the reverse of a pair).
        sa.Index("ix_network_people_person", "person_id"),
        # Saves in the last 7 and 30 days (the admin's summary).
        sa.Index("ix_network_people_created", "created_at"),
    )


class NetworkEncounters(AbstractSQLModel):
    __tablename__ = "network_encounters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    network_person_id = Column(Integer, ForeignKey("network_people.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    method = Column(String(10), nullable=False)
    met_at = Column(TZAwareDateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("method IN ('qr', 'discovery', 'link', 'search')", name="ck_network_encounters_method"),
        # One encounter a pair per event: a second scan there is the same meeting.
        sa.Index(
            "ux_network_encounters_person_event", "network_person_id", "event_id",
            unique=True, postgresql_where=sa.text("event_id IS NOT NULL"),
        ),
        sa.Index("ix_network_encounters_person_met", "network_person_id", sa.text("met_at DESC")),
        # An event's encounters: the club's stats and the morning recap.
        sa.Index("ix_network_encounters_event_met", "event_id", "met_at", postgresql_where=sa.text("event_id IS NOT NULL")),
    )


class NetworkSaveNotices(AbstractSQLModel):
    __tablename__ = "network_save_notices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    person_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=True)
    #: Who saved them in this batch, in order, each once.
    saver_ids = Column(JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb"))
    due_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (
        # One open batch per (person, event), "no event" included: a save
        # upserts it (INSERT ... ON CONFLICT on this index).
        sa.Index(
            "ux_network_save_notices_open", "person_id", sa.text("COALESCE(event_id, 0)"),
            unique=True, postgresql_where=sa.text("sent_at IS NULL"),
        ),
    )


class NetworkRecaps(AbstractSQLModel):
    __tablename__ = "network_recaps"

    person_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    #: Claimed by the send: set once, by the first run.
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (sa.Index("ix_network_recaps_event", "event_id"),)


class ReleasedHandles(AbstractSQLModel):
    __tablename__ = "released_handles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: Lower case, as handles are matched.
    handle = Column(String(100), nullable=False)
    #: Who gave it up (they may take it back).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    released_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (sa.Index("ix_released_handles_handle_released", "handle", "released_at"),)
