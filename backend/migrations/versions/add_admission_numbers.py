"""Admission numbers: the format on the event, the number on the registration.

Revision ID: add_admission_numbers
Revises: add_club_branding
"""

import sqlalchemy as sa
from alembic import op

revision = "add_admission_numbers"
down_revision = "add_club_branding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("admission_prefix", sa.String(12), nullable=True))
    op.add_column("events", sa.Column("admission_digits", sa.Integer(), nullable=False, server_default="4"))
    op.add_column("events", sa.Column("admission_include_code", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("events", sa.Column("admission_seq", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("event_registrations_link", sa.Column("admission_no", sa.String(40), nullable=True))
    op.create_index("ix_event_registrations_link_admission_no", "event_registrations_link", ["event_id", "admission_no"])


def downgrade() -> None:
    op.drop_index("ix_event_registrations_link_admission_no", table_name="event_registrations_link")
    op.drop_column("event_registrations_link", "admission_no")
    for col in ("admission_seq", "admission_include_code", "admission_digits", "admission_prefix"):
        op.drop_column("events", col)
