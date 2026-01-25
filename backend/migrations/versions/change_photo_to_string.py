"""Change photo column type to String

Revision ID: change_photo_to_string
Revises: add_deleted_at_to_event_speakers
Create Date: 2026-01-25

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'change_photo_to_string'
down_revision = 'add_deleted_at_to_event_speakers'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Change photo column type from JSON to String
    # Using 'using' clause to handle casting if necessary, though for JSON to String it might just stringify
    op.alter_column('event_speakers', 'photo',
               existing_type=sa.JSON(),
               type_=sa.String(),
               existing_nullable=True)


def downgrade() -> None:
    # Change photo column type back to JSON
    op.alter_column('event_speakers', 'photo',
               existing_type=sa.String(),
               type_=sa.JSON(),
               existing_nullable=True,
               postgresql_using='photo::json')
