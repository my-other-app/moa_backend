"""Club expenses: the ledger behind the Finance page.

Revision ID: add_club_expenses
Revises: add_tracking_links
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_club_expenses"
down_revision = "add_tracking_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "club_expenses",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_club_expenses_club_id", "club_expenses", ["club_id"])
    op.create_index("ix_club_expenses_event_id", "club_expenses", ["event_id"])
    op.create_index("ix_club_expenses_is_deleted", "club_expenses", ["is_deleted"])


def downgrade() -> None:
    op.drop_index("ix_club_expenses_is_deleted", table_name="club_expenses")
    op.drop_index("ix_club_expenses_event_id", table_name="club_expenses")
    op.drop_index("ix_club_expenses_club_id", table_name="club_expenses")
    op.drop_table("club_expenses")
