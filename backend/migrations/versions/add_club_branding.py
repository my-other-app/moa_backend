"""Brand colours on the club, which designs and emails fall back to.

Revision ID: add_club_branding
Revises: add_delivery_outcomes
"""

import sqlalchemy as sa
from alembic import op

revision = "add_club_branding"
down_revision = "add_delivery_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clubs", sa.Column("brand_primary", sa.String(7), nullable=True))
    op.add_column("clubs", sa.Column("brand_secondary", sa.String(7), nullable=True))


def downgrade() -> None:
    op.drop_column("clubs", "brand_secondary")
    op.drop_column("clubs", "brand_primary")
