"""Add deleted_at to event_speakers

Revision ID: add_deleted_at_to_event_speakers
Revises: add_event_tag_speakers
Create Date: 2026-01-25

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_deleted_at_to_event_speakers'
down_revision = 'add_event_tag_speakers'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('event_speakers', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('event_speakers', 'deleted_at')
