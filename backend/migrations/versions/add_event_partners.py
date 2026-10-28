"""An event's own sponsors and partners (docs/EVENT_PARTNERS.md).

- event_partners: one row per sponsor or partner of an event. `kind` is
  'sponsor' or 'partner' (a check constraint holds it to those); a name, an
  optional label ("Title sponsor") and https link, a logo (a key under
  events/partners/), its `position` within its kind, and the club partner it
  was copied from, if any. Soft-deleted like the club's partners.
- ix_event_partners_event_id and ix_event_partners_is_deleted, as the model
  declares them; ix_event_partners_event_live, (event_id, kind, position)
  WHERE NOT is_deleted, for the page's read.

A new, empty table: nothing is backfilled and nothing existing is rewritten.
Its foreign keys take a brief SHARE ROW EXCLUSIVE lock on events and
club_partners while the table is made, under lock_timeout 3s.

Downgrade drops the table and its indexes.

Revision ID: add_event_partners
Revises: approval_stats
"""
import sqlalchemy as sa
from alembic import op

revision = "add_event_partners"
down_revision = "approval_stats"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        "event_partners",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("label", sa.String(60), nullable=True),
        sa.Column("link", sa.String(500), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("club_partner_id", sa.Integer(), sa.ForeignKey("club_partners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("logo", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind IN ('sponsor', 'partner')", name="ck_event_partners_kind"),
        if_not_exists=True,
    )
    op.create_index("ix_event_partners_event_id", "event_partners", ["event_id"], if_not_exists=True)
    op.create_index("ix_event_partners_is_deleted", "event_partners", ["is_deleted"], if_not_exists=True)
    op.create_index(
        "ix_event_partners_event_live", "event_partners", ["event_id", "kind", "position"],
        postgresql_where=sa.text("NOT is_deleted"), if_not_exists=True,
    )


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP INDEX IF EXISTS ix_event_partners_event_live")
    op.execute("DROP INDEX IF EXISTS ix_event_partners_is_deleted")
    op.execute("DROP INDEX IF EXISTS ix_event_partners_event_id")
    op.execute("DROP TABLE IF EXISTS event_partners")
