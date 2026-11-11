"""The speakers directory (app/api/speakers, docs/SPEAKERS.md,
feature-register/speakers-contract.md).

New tables:

- speaker_profiles: one person who has spoken at an event (name, headline,
  photo, bio, links, city, topics), active, hidden or merged into another,
  and who claimed it. A GIN trigram index on name_key for the name search
  (pg_trgm, which discover_perf created; created here too if missing).
- speaker_contacts: one club's email, phone and notes for a profile.
- speaker_merge_candidates: possible duplicates waiting for an admin.
- speaker_history: every change to a profile.
- speaker_follows: app users following a speaker.
- speaker_claims: invite links to claim a profile (the token's hash only).
- agenda_item_speakers: an agenda session's speakers, from its event's
  speaker rows (the session's free-text `speaker` stays).

Changed: event_speakers gains speaker_profile_id (nullable, FK, SET NULL)
and talk_title, both NULL for every row until the backfill
(`speakers.backfill`) links them -- nothing shown changes.

Lock-safe: event_speakers is small and not a hot table; adding two nullable
columns is a catalog change, the FK's SHARE ROW EXCLUSIVE is brief, under
lock_timeout 3s, and its index is built on a column of NULLs. The new tables
are empty. The foreign keys to users, clubs, events' agenda_items take the
same brief lock.

Downgrade drops the two columns and the seven tables: the profiles,
contacts, merges, follows and claims go with them; every event keeps its
own speakers as before.

Revision ID: speakers_directory
Revises: review_resends
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "speakers_directory"
down_revision = "review_resends"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")


def _stamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        "speaker_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(140), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("name_key", sa.String(120), nullable=False),
        sa.Column("headline", sa.String(200), nullable=True),
        sa.Column("bio", sa.String(600), nullable=True),
        sa.Column("photo", sa.String(), nullable=True),
        sa.Column("links", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("topics", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'::integer[]")),
        sa.Column("status", sa.String(8), nullable=False, server_default="active"),
        sa.Column("merged_into_id", sa.Integer(), sa.ForeignKey("speaker_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("claimed_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("slug", name="speaker_profiles_slug_key"),
        sa.CheckConstraint("status IN ('active', 'hidden', 'merged')", name="ck_speaker_profiles_status"),
        sa.CheckConstraint("status <> 'merged' OR merged_into_id IS NOT NULL", name="ck_speaker_profiles_merged_into"),
        if_not_exists=True,
    )
    op.create_index(
        "ux_speaker_profiles_claimed_by", "speaker_profiles", ["claimed_by_user_id"], unique=True,
        postgresql_where=sa.text("claimed_by_user_id IS NOT NULL"), if_not_exists=True,
    )
    op.create_index("ix_speaker_profiles_name_key", "speaker_profiles", ["name_key"], if_not_exists=True)
    op.create_index("ix_speaker_profiles_club", "speaker_profiles", ["created_by_club_id"], if_not_exists=True)
    op.create_index(
        "ix_speaker_profiles_name_trgm", "speaker_profiles", ["name_key"], postgresql_using="gin",
        postgresql_ops={"name_key": "gin_trgm_ops"}, if_not_exists=True,
    )

    op.create_table(
        "speaker_contacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("consent_by_name", sa.String(120), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("profile_id", "club_id", name="uq_speaker_contacts_profile_club"),
        if_not_exists=True,
    )
    op.create_index("ix_speaker_contacts_club", "speaker_contacts", ["club_id"], if_not_exists=True)

    op.create_table(
        "speaker_merge_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("a_id", sa.Integer(), sa.ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("b_id", sa.Integer(), sa.ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("a_id", "b_id", name="uq_speaker_merge_candidates_pair"),
        sa.CheckConstraint("a_id < b_id", name="ck_speaker_merge_candidates_order"),
        sa.CheckConstraint("status IN ('open', 'merged', 'dismissed')", name="ck_speaker_merge_candidates_status"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_speaker_merge_candidates_open", "speaker_merge_candidates", ["score"],
        postgresql_where=sa.text("status = 'open'"), if_not_exists=True,
    )
    op.create_index("ix_speaker_merge_candidates_b", "speaker_merge_candidates", ["b_id"], if_not_exists=True)

    op.create_table(
        "speaker_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_kind", sa.String(10), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_name", sa.String(120), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        if_not_exists=True,
    )
    op.create_index("ix_speaker_history_profile", "speaker_history", ["profile_id", "created_at"], if_not_exists=True)

    op.create_table(
        "speaker_follows",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("user_id", "profile_id", name="uq_speaker_follows_user_profile"),
        if_not_exists=True,
    )
    op.create_index("ix_speaker_follows_profile", "speaker_follows", ["profile_id"], if_not_exists=True)

    op.create_table(
        "speaker_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("speaker_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("token_hash", name="speaker_claims_token_hash_key"),
        if_not_exists=True,
    )
    op.create_index("ix_speaker_claims_profile", "speaker_claims", ["profile_id"], if_not_exists=True)

    # The event's own speaker rows: which profile each is, and the talk's title.
    op.execute(
        "ALTER TABLE event_speakers ADD COLUMN IF NOT EXISTS speaker_profile_id integer "
        "REFERENCES speaker_profiles (id) ON DELETE SET NULL"
    )
    op.execute("ALTER TABLE event_speakers ADD COLUMN IF NOT EXISTS talk_title varchar(200)")
    op.create_index(
        "ix_event_speakers_profile", "event_speakers", ["speaker_profile_id"],
        postgresql_where=sa.text("speaker_profile_id IS NOT NULL"), if_not_exists=True,
    )

    op.create_table(
        "agenda_item_speakers",
        sa.Column("agenda_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agenda_items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("event_speaker_id", sa.Integer(), sa.ForeignKey("event_speakers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        if_not_exists=True,
    )
    op.create_index("ix_agenda_item_speakers_speaker", "agenda_item_speakers", ["event_speaker_id"], if_not_exists=True)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP TABLE IF EXISTS agenda_item_speakers")
    op.execute("DROP INDEX IF EXISTS ix_event_speakers_profile")
    op.execute("ALTER TABLE event_speakers DROP COLUMN IF EXISTS talk_title")
    op.execute("ALTER TABLE event_speakers DROP COLUMN IF EXISTS speaker_profile_id")
    for table in ("speaker_claims", "speaker_follows", "speaker_history", "speaker_merge_candidates", "speaker_contacts", "speaker_profiles"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
