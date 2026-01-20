"""add event_id to notifications

Revision ID: add_event_id_notifications
Revises: 
Create Date: 2026-01-20

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_event_id_notifications'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add event_id column to notifications table
    op.add_column(
        'notifications',
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id'), nullable=True)
    )
    
    # Create index for faster queries by event
    op.create_index(
        'ix_notifications_event_id',
        'notifications',
        ['event_id']
    )


def downgrade() -> None:
    op.drop_index('ix_notifications_event_id', table_name='notifications')
    op.drop_column('notifications', 'event_id')
