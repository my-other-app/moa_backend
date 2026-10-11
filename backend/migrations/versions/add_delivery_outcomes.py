"""Addresses that bounced, on the registration, so later sends leave them alone.

Revision ID: add_delivery_outcomes
Revises: add_closeout
"""

import sqlalchemy as sa
from alembic import op

revision = "add_delivery_outcomes"
down_revision = "add_closeout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event_registrations_link", sa.Column("email_bounced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("event_registrations_link", sa.Column("email_bounce_reason", sa.String(300), nullable=True))
    op.add_column("event_registrations_link", sa.Column("wa_bounced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("event_registrations_link", sa.Column("wa_bounce_reason", sa.String(300), nullable=True))


def downgrade() -> None:
    for col in ("wa_bounce_reason", "wa_bounced_at", "email_bounce_reason", "email_bounced_at"):
        op.drop_column("event_registrations_link", col)
