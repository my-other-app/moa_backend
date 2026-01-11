"""add user device tokens table

Revision ID: a1b2c3d4e5f6
Revises: fix_event_ratings_unique
Create Date: 2026-01-12 04:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'fix_event_ratings_unique'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create user_device_tokens table for storing FCM tokens
    op.create_table(
        'user_device_tokens',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('fcm_token', sa.String(length=512), nullable=False),
        sa.Column('platform', sa.String(length=20), nullable=False),
        sa.Column('device_id', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'platform', name='uq_user_device_platform')
    )
    
    # Create index for faster lookups
    op.create_index('ix_user_device_tokens_user_id', 'user_device_tokens', ['user_id'])
    op.create_index('ix_user_device_tokens_fcm_token', 'user_device_tokens', ['fcm_token'])
    
    # Create notification_logs table for tracking sent notifications
    op.create_table(
        'notification_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('notification_type', sa.String(length=50), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='sent'),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('club_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ),
        sa.ForeignKeyConstraint(['club_id'], ['clubs.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for analytics queries
    op.create_index('ix_notification_logs_user_id', 'notification_logs', ['user_id'])
    op.create_index('ix_notification_logs_type', 'notification_logs', ['notification_type'])
    op.create_index('ix_notification_logs_created_at', 'notification_logs', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_notification_logs_created_at', table_name='notification_logs')
    op.drop_index('ix_notification_logs_type', table_name='notification_logs')
    op.drop_index('ix_notification_logs_user_id', table_name='notification_logs')
    op.drop_table('notification_logs')
    
    op.drop_index('ix_user_device_tokens_fcm_token', table_name='user_device_tokens')
    op.drop_index('ix_user_device_tokens_user_id', table_name='user_device_tokens')
    op.drop_table('user_device_tokens')
