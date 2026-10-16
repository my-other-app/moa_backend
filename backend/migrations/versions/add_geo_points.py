"""Coordinates on events and clubs, read from their map links, for "near you".

Revision ID: add_geo_points
Revises: add_site_tracking
"""
import sqlalchemy as sa
from alembic import op

revision = "add_geo_points"
down_revision = "add_site_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("events", "clubs"):
        op.add_column(table, sa.Column("lat", sa.Float(), nullable=True))
        op.add_column(table, sa.Column("lng", sa.Float(), nullable=True))


def downgrade() -> None:
    for table in ("clubs", "events"):
        op.drop_column(table, "lng")
        op.drop_column(table, "lat")
