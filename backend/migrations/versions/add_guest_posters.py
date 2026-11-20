""""I'm attending" posters: three new tables, nothing existing touched.

- poster_settings: an event's switch (off), its formats, its moments' words.
- poster_designs: an event's poster variants, a layout v2 per format.
- guest_posters: one guest's photo, framing, share link, renders and counts.

New, empty tables made with their indexes in one transaction: no existing
table is rewritten, locked for long or given a column. The foreign keys to
events, event_registrations_link, tracking_links and users take a SHARE ROW
EXCLUSIVE lock on those tables only for as long as the CREATE TABLE takes
(a few milliseconds on an empty table), the same as any new table with a
foreign key; nothing is validated against existing rows.

Revision ID: add_guest_posters
Revises: stability_indexes
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "add_guest_posters"
down_revision = "stability_indexes"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "poster_settings",
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("formats", JSONB(), nullable=False, server_default=sa.text("'[\"square\", \"portrait\", \"story\"]'::jsonb")),
        sa.Column("moments", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_table(
        "poster_designs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant", sa.String(40), nullable=False),
        sa.Column("layouts", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starter", sa.String(80), nullable=True),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("event_id", "variant", name="uq_poster_designs_event_variant"),
    )
    op.create_table(
        "guest_posters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tracking_link_id", UUID(as_uuid=True), sa.ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True),
        sa.Column("photo_key", sa.String(300), nullable=True),
        sa.Column("photo_sha", sa.String(64), nullable=True),
        sa.Column("photo_width", sa.Integer(), nullable=True),
        sa.Column("photo_height", sa.Integer(), nullable=True),
        sa.Column("photo_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("framing", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("renders", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("made_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("render_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("downloads", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shares", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("copies", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("registration_id", name="uq_guest_posters_registration"),
    )
    op.create_index("ix_guest_posters_event", "guest_posters", ["event_id"])
    op.create_index(
        "ix_guest_posters_tracking_link", "guest_posters", ["tracking_link_id"],
        postgresql_where=sa.text("tracking_link_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_guest_posters_tracking_link", table_name="guest_posters")
    op.drop_index("ix_guest_posters_event", table_name="guest_posters")
    op.drop_table("guest_posters")
    op.drop_table("poster_designs")
    op.drop_table("poster_settings")
