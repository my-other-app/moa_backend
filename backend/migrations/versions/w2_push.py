"""Push by FCM topics, and broadcasts as one shared inbox row.

- notification_broadcasts: one row per broadcast (a club's post or new
  event, a new event by topic, an announcement to everyone), whoever it
  reaches; push_id unique.
- notification_broadcast_targets: the audiences a shared broadcast is read
  through ("club:12", "topic:5"), with ix_..._feed (target, created_at DESC,
  broadcast_id DESC) INCLUDE (except_club_id): a person's inbox is one short
  range of it per club they follow and topic they picked.
- notification_broadcast_states: what one person did with one broadcast
  (read, dismissed), written only when they did it.
- notification_settings: a person's switches (clubs, topics, platform) and
  read_through, written only when they change one or read everything.
- club_users_link.is_muted (NOT NULL DEFAULT false) and .followed_at (NULL).
- user_device_tokens.supports_topics (NOT NULL DEFAULT false).

The four tables are new and empty: their indexes are built with them, in the
migration's transaction. The three columns go onto tables in use: a column
with a constant default (or none) is a change to the catalogue only since
Postgres 11 -- no rewrite, no scan -- but ALTER TABLE still waits for an
ACCESS EXCLUSIVE lock, and every query on the table queues behind that wait.
lock_timeout bounds it at three seconds: on a busy table the migration fails
and is run again rather than holding up requests. Nothing is backfilled:
followed_at is NULL for follows from before, and created_at stands in.

Downgrade drops the columns and the tables; broadcasts written in between
leave the inbox with them (the per-recipient rows written while INBOX_SHARED
was off are untouched).

Revision ID: w2_push
Revises: w2_platform
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "w2_push"
down_revision = "w2_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.create_table(
        "notification_broadcasts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("push_id", sa.String(64), nullable=False, unique=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("from_club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=True),
        sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("audience", postgresql.JSONB(), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notification_broadcasts_type_created", "notification_broadcasts", ["type", "created_at"])
    op.create_table(
        "notification_broadcast_targets",
        sa.Column("broadcast_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("notification_broadcasts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("target", sa.String(40), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("except_club_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_notification_broadcast_targets_feed", "notification_broadcast_targets",
        ["target", sa.text("created_at DESC"), sa.text("broadcast_id DESC")], postgresql_include=["except_club_id"],
    )
    op.create_table(
        "notification_broadcast_states",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("broadcast_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("notification_broadcasts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_notification_broadcast_states_broadcast", "notification_broadcast_states", ["broadcast_id"])
    op.create_table(
        "notification_settings",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("clubs", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("topics", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("platform", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("read_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()")),
    )
    # Catalogue-only changes (a constant default, or none), each behind the lock_timeout above.
    op.add_column("club_users_link", sa.Column("is_muted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("club_users_link", sa.Column("followed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_device_tokens", sa.Column("supports_topics", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_column("user_device_tokens", "supports_topics")
    op.drop_column("club_users_link", "followed_at")
    op.drop_column("club_users_link", "is_muted")
    op.drop_table("notification_settings")
    op.drop_index("ix_notification_broadcast_states_broadcast", table_name="notification_broadcast_states")
    op.drop_table("notification_broadcast_states")
    op.drop_index("ix_notification_broadcast_targets_feed", table_name="notification_broadcast_targets")
    op.drop_table("notification_broadcast_targets")
    op.drop_index("ix_notification_broadcasts_type_created", table_name="notification_broadcasts")
    op.drop_table("notification_broadcasts")
