"""Add certificate templates and event FK

Revision ID: add_certificate_templates
Revises: add_badge_system
Create Date: 2026-02-25

"""
from typing import Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'add_certificate_templates'
down_revision = 'add_badge_system'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        'certificate_templates',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('club_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('config', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['club_id'], ['clubs.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.add_column(
        'events',
        sa.Column('certificate_template_id', sa.Integer(), sa.ForeignKey('certificate_templates.id'), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('events', 'certificate_template_id')
    op.drop_table('certificate_templates')
