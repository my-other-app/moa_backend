"""The public event page: who from the club's team organises an event, and a
bio and a link for each speaker.

Revision ID: add_event_page
Revises: add_geo_points
"""
import sqlalchemy as sa
from alembic import op

revision = "add_event_page"
down_revision = "add_geo_points"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_team_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("club_team_members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("event_id", "member_id", name="uq_event_team_members_event_member"),
    )
    op.create_index("ix_event_team_members_event_id", "event_team_members", ["event_id"])
    op.add_column("event_speakers", sa.Column("bio", sa.String(length=600), nullable=True))
    op.add_column("event_speakers", sa.Column("link", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("event_speakers", "link")
    op.drop_column("event_speakers", "bio")
    op.drop_index("ix_event_team_members_event_id", table_name="event_team_members")
    op.drop_table("event_team_members")
