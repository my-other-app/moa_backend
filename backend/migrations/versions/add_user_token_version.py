"""add_user_token_version

Add a token_version column to users, used to invalidate previously issued
access/refresh tokens when a password is changed or reset. Existing rows
default to 0, matching the default embedded in tokens minted before this
change, so no one is force-logged-out by the migration itself.

Revision ID: add_user_token_version
Revises: add_template_images
"""
from alembic import op
import sqlalchemy as sa


revision = 'add_user_token_version'
down_revision = 'add_template_images'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('users', 'token_version')
