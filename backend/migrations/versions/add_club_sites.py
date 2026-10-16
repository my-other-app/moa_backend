"""A club's website: its choices and the page as last published.

Revision ID: add_club_sites
Revises: add_competitions
"""
import sqlalchemy as sa
from alembic import op

revision = "add_club_sites"
down_revision = "add_competitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "club_sites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("template", sa.String(20), nullable=False, server_default="launchpad"),
        sa.Column("theme", sa.String(10), nullable=False, server_default="light"),
        sa.Column("font", sa.String(20), nullable=False, server_default="dm_sans"),
        sa.Column("headline", sa.String(120), nullable=True),
        sa.Column("subline", sa.String(240), nullable=True),
        sa.Column("sections", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settings_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_club_sites_club_id", "club_sites", ["club_id"])


def downgrade() -> None:
    op.drop_index("ix_club_sites_club_id", table_name="club_sites")
    op.drop_table("club_sites")
