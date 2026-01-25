"""Add event_tag and event_speakers table

Revision ID: add_event_tag_speakers
Revises: 
Create Date: 2026-01-25

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_event_tag_speakers'
down_revision = 'add_event_id_notifications'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add event_tag column to events table
    op.add_column('events', sa.Column('event_tag', sa.String(50), nullable=True))
    
    # Create event_speakers table
    op.create_table(
        'event_speakers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('designation', sa.String(200), nullable=True),
        sa.Column('photo', sa.JSON(), nullable=True),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create index on event_id for faster lookups
    op.create_index('ix_event_speakers_event_id', 'event_speakers', ['event_id'])


def downgrade() -> None:
    # Drop event_speakers table
    op.drop_index('ix_event_speakers_event_id', table_name='event_speakers')
    op.drop_table('event_speakers')
    
    # Remove event_tag column from events table
    op.drop_column('events', 'event_tag')
