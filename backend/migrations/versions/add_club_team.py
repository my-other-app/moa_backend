"""The club's team: the people who run it, shown on the club page.

Revision ID: add_club_team
Revises: add_ledger_income
"""
import sqlalchemy as sa
from alembic import op

revision = "add_club_team"
down_revision = "add_ledger_income"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "club_team_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=True),
        sa.Column("instagram", sa.String(length=255), nullable=True),
        sa.Column("linkedin", sa.String(length=255), nullable=True),
        sa.Column("x", sa.String(length=255), nullable=True),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("photo", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_club_team_members_club_id", "club_team_members", ["club_id"])
    op.create_index("ix_club_team_members_is_deleted", "club_team_members", ["is_deleted"])


def downgrade() -> None:
    op.drop_index("ix_club_team_members_is_deleted", table_name="club_team_members")
    op.drop_index("ix_club_team_members_club_id", table_name="club_team_members")
    op.drop_table("club_team_members")
