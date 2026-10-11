"""Tracking links: the links a club shares, the clicks on them, and the sign-up or follow each brought.

Revision ID: add_tracking_links
Revises: add_admission_numbers
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_tracking_links"
down_revision = "add_admission_numbers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tracking_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("code", sa.String(12), nullable=False),
        sa.Column("utm_source", sa.String(60), nullable=False),
        sa.Column("utm_medium", sa.String(60), nullable=False),
        sa.Column("utm_campaign", sa.String(80), nullable=True),
        sa.Column("utm_content", sa.String(80), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_tracking_links_code"),
    )
    op.create_index("ix_tracking_links_is_deleted", "tracking_links", ["is_deleted"])
    op.create_index("ix_tracking_links_club_id", "tracking_links", ["club_id"])
    op.create_index("ix_tracking_links_event_id", "tracking_links", ["event_id"])
    op.create_index("ix_tracking_links_code", "tracking_links", ["code"])

    op.create_table(
        "tracking_clicks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tracking_links.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visitor", sa.String(64), nullable=True),
        sa.Column("referrer", sa.String(300), nullable=True),
        sa.Column("user_agent", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tracking_clicks_link_id", "tracking_clicks", ["link_id"])
    op.create_index("ix_tracking_clicks_link_created", "tracking_clicks", ["link_id", "created_at"])

    op.add_column("event_registrations_link", sa.Column("tracking_link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_event_registrations_link_tracking_link_id", "event_registrations_link", ["tracking_link_id"])
    op.add_column("club_users_link", sa.Column("tracking_link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_club_users_link_tracking_link_id", "club_users_link", ["tracking_link_id"])


def downgrade() -> None:
    op.drop_index("ix_club_users_link_tracking_link_id", table_name="club_users_link")
    op.drop_column("club_users_link", "tracking_link_id")
    op.drop_index("ix_event_registrations_link_tracking_link_id", table_name="event_registrations_link")
    op.drop_column("event_registrations_link", "tracking_link_id")
    op.drop_index("ix_tracking_clicks_link_created", table_name="tracking_clicks")
    op.drop_index("ix_tracking_clicks_link_id", table_name="tracking_clicks")
    op.drop_table("tracking_clicks")
    op.drop_index("ix_tracking_links_code", table_name="tracking_links")
    op.drop_index("ix_tracking_links_event_id", table_name="tracking_links")
    op.drop_index("ix_tracking_links_club_id", table_name="tracking_links")
    op.drop_index("ix_tracking_links_is_deleted", table_name="tracking_links")
    op.drop_table("tracking_links")
