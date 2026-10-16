"""A club website's own tracking link: a kind on tracking links, one website link per club.

Revision ID: add_site_tracking
Revises: add_club_members
"""
import sqlalchemy as sa
from alembic import op

revision = "add_site_tracking"
down_revision = "add_club_members"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tracking_links", sa.Column("kind", sa.String(16), nullable=False, server_default="custom"))
    op.create_index(
        "uq_tracking_links_club_website", "tracking_links", ["club_id"], unique=True,
        postgresql_where=sa.text("kind = 'website' AND is_deleted = false"),
    )


def downgrade() -> None:
    op.drop_index("uq_tracking_links_club_website", table_name="tracking_links")
    op.drop_column("tracking_links", "kind")
