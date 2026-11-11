"""The speakers directory (docs/SPEAKERS.md, feature-register/speakers-contract.md).

One profile per person who has spoken at an event on MyOtherApp. Each event
keeps its own copy of what its page shows (event_speakers: name, designation,
photo, bio, link) and points at the profile it belongs to
(event_speakers.speaker_profile_id); an agenda session points at the event's
speakers (agenda_item_speakers), its old free-text `speaker` kept.

- speaker_profiles: the person, as the public sees them (name, headline,
  photo, bio, links, city, topics), whether an admin hid it or merged it
  into another, and who claimed it.
- speaker_contacts: an email, a phone and notes, one row per profile and
  club: the club that entered them sees them, and admins (never the notes);
  no other club, ever.
- speaker_merge_candidates: two profiles that may be one person, waiting for
  an admin (the backfill and new profiles put them here).
- speaker_history: every change to a profile, by whom, before and after.
- speaker_follows: app users following a speaker (a push when they are on a
  newly published event).
- speaker_claims: the invite links a speaker claims their profile with (only
  the token's hash is kept).
"""
import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Column, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from app.core.storage.fields import S3ImageField
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

ACTIVE, HIDDEN, MERGED = "active", "hidden", "merged"
STATUSES = (ACTIVE, HIDDEN, MERGED)
OPEN, DISMISSED = "open", "dismissed"
CANDIDATE_STATUSES = (OPEN, MERGED, DISMISSED)

#: The photo's sizes: the event copies' own, in the event copies' folder, so a
#: photo copied between a profile and an event copy keeps its sizes both ways
#: (S3ImageField.has_variants reads the folder off the key).
PHOTO_FOLDER = "events/speakers/"
PHOTO_SIZES = {
    "thumbnail": {"width": 100, "height": 100},
    "medium": {"width": 200, "height": 200},
}

NOW = sa.text("now()")


class SpeakerProfiles(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "speaker_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(140), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    #: The name as matching reads it (speakers.names.name_key): lower case, no
    #: titles, no punctuation. Two event rows with the same key are the same
    #: name.
    name_key = Column(String(120), nullable=False)
    #: Role · organisation, as an event's `designation`.
    headline = Column(String(200), nullable=True)
    bio = Column(String(600), nullable=True)
    photo = Column(S3ImageField(upload_to=PHOTO_FOLDER, variations=PHOTO_SIZES, public=True), nullable=True)
    #: {website, linkedin, x, github, other}: each a web address or absent.
    links = Column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    city = Column(String(100), nullable=True)
    #: Topic ids (interest_categories) from the topic tree.
    topics = Column(ARRAY(Integer), nullable=False, default=list, server_default=sa.text("'{}'::integer[]"))
    status = Column(String(8), nullable=False, default=ACTIVE, server_default=ACTIVE)
    merged_into_id = Column(Integer, ForeignKey("speaker_profiles.id", ondelete="SET NULL"), nullable=True)
    claimed_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    claimed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by_club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True)
    #: Set when an admin removed it on the speaker's request: hidden, contacts gone.
    removed_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'hidden', 'merged')", name="ck_speaker_profiles_status"),
        CheckConstraint("status <> 'merged' OR merged_into_id IS NOT NULL", name="ck_speaker_profiles_merged_into"),
        # One profile a person claims.
        sa.Index("ux_speaker_profiles_claimed_by", "claimed_by_user_id", unique=True, postgresql_where=sa.text("claimed_by_user_id IS NOT NULL")),
        sa.Index("ix_speaker_profiles_name_key", "name_key"),
        sa.Index("ix_speaker_profiles_club", "created_by_club_id"),
        # The picker's and the admin's name search (pg_trgm: LIKE '%…%' and `%`), on the matching key.
        sa.Index("ix_speaker_profiles_name_trgm", "name_key", postgresql_using="gin", postgresql_ops={"name_key": "gin_trgm_ops"}),
    )


class SpeakerContacts(AbstractSQLModel, TimestampsMixin):
    """How one club reaches one speaker. Private to that club (and admins)."""

    __tablename__ = "speaker_contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(254), nullable=True)
    phone = Column(String(20), nullable=True)
    #: The club's own notes ("needs an HDMI projector"): that club only, not admins.
    notes = Column(String(1000), nullable=True)
    #: When the club ticked that the speaker agreed to their email or phone being kept.
    consent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    consent_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: Who ticked it, in words (the team member's name or email, or the club's account).
    consent_by_name = Column(String(120), nullable=True)

    __table_args__ = (
        UniqueConstraint("profile_id", "club_id", name="uq_speaker_contacts_profile_club"),
        sa.Index("ix_speaker_contacts_club", "club_id"),
    )


class SpeakerMergeCandidates(AbstractSQLModel, TimestampsMixin):
    """Two profiles that may be one person. a_id < b_id, one row a pair."""

    __tablename__ = "speaker_merge_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    a_id = Column(Integer, ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False)
    b_id = Column(Integer, ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False)
    #: same_name_other_club, same_link_other_name, similar_name.
    reason = Column(String(40), nullable=False)
    score = Column(Float, nullable=False, default=0.0, server_default="0")
    status = Column(String(10), nullable=False, default=OPEN, server_default=OPEN)
    decided_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("a_id", "b_id", name="uq_speaker_merge_candidates_pair"),
        CheckConstraint("a_id < b_id", name="ck_speaker_merge_candidates_order"),
        CheckConstraint("status IN ('open', 'merged', 'dismissed')", name="ck_speaker_merge_candidates_status"),
        sa.Index("ix_speaker_merge_candidates_open", "score", postgresql_where=sa.text("status = 'open'")),
        sa.Index("ix_speaker_merge_candidates_b", "b_id"),
    )


class SpeakerHistory(AbstractSQLModel):
    """One change to a profile. Never rewritten; a merge's line is what its
    unmerge reads back."""

    __tablename__ = "speaker_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False)
    #: admin, club, speaker or system.
    actor_kind = Column(String(10), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True)
    actor_name = Column(String(120), nullable=True)
    action = Column(String(40), nullable=False)
    before = Column(JSONB, nullable=True)
    after = Column(JSONB, nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())

    __table_args__ = (sa.Index("ix_speaker_history_profile", "profile_id", "created_at"),)


class SpeakerFollows(AbstractSQLModel):
    __tablename__ = "speaker_follows"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    profile_id = Column(Integer, ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "profile_id", name="uq_speaker_follows_user_profile"),
        sa.Index("ix_speaker_follows_profile", "profile_id"),
    )


class SpeakerClaims(AbstractSQLModel):
    """An invite to claim a profile: the link's token is never kept, only its sha256."""

    __tablename__ = "speaker_claims"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    email = Column(String(254), nullable=False)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    used_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    used_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: A newer invite for the same profile replaces this one.
    revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    invited_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())

    __table_args__ = (sa.Index("ix_speaker_claims_profile", "profile_id"),)


class AgendaItemSpeakers(AbstractSQLModel):
    """An agenda session's speakers: the event's own speaker rows, in order.
    Gone with the session or the speaker row (both cascade)."""

    __tablename__ = "agenda_item_speakers"

    agenda_item_id = Column(UUID(as_uuid=True), ForeignKey("agenda_items.id", ondelete="CASCADE"), primary_key=True)
    event_speaker_id = Column(Integer, ForeignKey("event_speakers.id", ondelete="CASCADE"), primary_key=True)
    position = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (sa.Index("ix_agenda_item_speakers_speaker", "event_speaker_id"),)
