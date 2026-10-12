"""Club profile extras: a tagline, a cover banner, and year-round partners.

Revision ID: add_club_profile_extras
Revises: add_club_expenses
"""
import sqlalchemy as sa
from alembic import op

revision = "add_club_profile_extras"
down_revision = "add_club_expenses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clubs", sa.Column("tagline", sa.String(120), nullable=True))
    op.add_column("clubs", sa.Column("cover", sa.String(), nullable=True))
    op.create_table(
        "club_partners",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(80), nullable=True),
        sa.Column("since", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_club_partners_club_id", "club_partners", ["club_id"])
    op.create_index("ix_club_partners_is_deleted", "club_partners", ["is_deleted"])


def downgrade() -> None:
    op.drop_index("ix_club_partners_is_deleted", table_name="club_partners")
    op.drop_index("ix_club_partners_club_id", table_name="club_partners")
    op.drop_table("club_partners")
    op.drop_column("clubs", "cover")
    op.drop_column("clubs", "tagline")
