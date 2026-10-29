"""Clubs co-hosting another club's event (docs/COHOSTS.md).

- event_cohosts: one row per club invited to co-host an event: its status
  ('invited', 'accepted', 'declined', 'left', 'removed', 'cancelled',
  'expired'; a check constraint holds it to those), the host's note, who
  sent it (the account, and the team member when one did), when it was
  sent, lapses, was answered and ended, and when the co-host's followers
  were told about the event.
- uq_event_cohosts_live: at most one live row (invited or accepted) per
  event and club, whatever the races. ix_event_cohosts_event_id,
  ix_event_cohosts_club_id and ix_event_cohosts_club_status (a club's
  co-hosted events and pending invitations).

A new, empty table: nothing is backfilled and nothing existing is rewritten.
Its foreign keys take a brief SHARE ROW EXCLUSIVE lock on events, clubs,
users and club_members while the table is made, under lock_timeout 3s.

Downgrade drops the table and its indexes.

Revision ID: add_event_cohosts
Revises: review_emails
"""
import sqlalchemy as sa
from alembic import op

revision = "add_event_cohosts"
down_revision = "review_emails"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        "event_cohosts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="invited"),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("invited_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invited_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("followers_told_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('invited', 'accepted', 'declined', 'left', 'removed', 'cancelled', 'expired')",
            name="ck_event_cohosts_status",
        ),
        if_not_exists=True,
    )
    op.create_index("ix_event_cohosts_event_id", "event_cohosts", ["event_id"], if_not_exists=True)
    op.create_index("ix_event_cohosts_club_id", "event_cohosts", ["club_id"], if_not_exists=True)
    op.create_index("ix_event_cohosts_club_status", "event_cohosts", ["club_id", "status"], if_not_exists=True)
    op.create_index(
        "uq_event_cohosts_live", "event_cohosts", ["event_id", "club_id"], unique=True,
        postgresql_where=sa.text("status IN ('invited', 'accepted')"), if_not_exists=True,
    )


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP INDEX IF EXISTS uq_event_cohosts_live")
    op.execute("DROP INDEX IF EXISTS ix_event_cohosts_club_status")
    op.execute("DROP INDEX IF EXISTS ix_event_cohosts_club_id")
    op.execute("DROP INDEX IF EXISTS ix_event_cohosts_event_id")
    op.execute("DROP TABLE IF EXISTS event_cohosts")
