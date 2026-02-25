"""Add ticket_templates table and ticket_template_id to events

Revision ID: add_ticket_templates
Revises: add_ngo_community_orgs
Create Date: 2026-02-25

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_ticket_templates'
down_revision = 'add_ngo_community_orgs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create ticket_templates table
    op.create_table(
        'ticket_templates',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('club_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('config', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['club_id'], ['clubs.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # Add ticket_template_id FK to events table
    op.add_column('events', sa.Column('ticket_template_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_events_ticket_template_id',
        'events', 'ticket_templates',
        ['ticket_template_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_events_ticket_template_id', 'events', type_='foreignkey')
    op.drop_column('events', 'ticket_template_id')
    op.drop_table('ticket_templates')
