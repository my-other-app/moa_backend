"""add_template_image_columns

Add S3ImageField columns to ticket_templates and certificate_templates tables
for file uploads (logo, secondary_logo, background_image, signatures).

Revision ID: add_template_images
Revises: add_certificate_templates
"""
from alembic import op
import sqlalchemy as sa


revision = 'add_template_images'
down_revision = 'add_certificate_templates'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ticket templates — add image columns
    op.add_column('ticket_templates', sa.Column('logo', sa.Text(), nullable=True))
    op.add_column('ticket_templates', sa.Column('secondary_logo', sa.Text(), nullable=True))
    op.add_column('ticket_templates', sa.Column('background_image', sa.Text(), nullable=True))

    # Certificate templates — add image columns
    op.add_column('certificate_templates', sa.Column('logo', sa.Text(), nullable=True))
    op.add_column('certificate_templates', sa.Column('secondary_logo', sa.Text(), nullable=True))
    op.add_column('certificate_templates', sa.Column('background_image', sa.Text(), nullable=True))
    op.add_column('certificate_templates', sa.Column('signature_1', sa.Text(), nullable=True))
    op.add_column('certificate_templates', sa.Column('signature_2', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('certificate_templates', 'signature_2')
    op.drop_column('certificate_templates', 'signature_1')
    op.drop_column('certificate_templates', 'background_image')
    op.drop_column('certificate_templates', 'secondary_logo')
    op.drop_column('certificate_templates', 'logo')

    op.drop_column('ticket_templates', 'background_image')
    op.drop_column('ticket_templates', 'secondary_logo')
    op.drop_column('ticket_templates', 'logo')
