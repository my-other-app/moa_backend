"""A logo on each club partner.

Revision ID: add_partner_logos
Revises: add_security_signins
"""
import sqlalchemy as sa
from alembic import op

revision = "add_partner_logos"
down_revision = "add_security_signins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("club_partners", sa.Column("logo", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("club_partners", "logo")
