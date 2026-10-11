"""Certificates are issued, not implied.

A certificate used to be whatever an attended guest downloaded. The
organiser now issues them -- to everyone who checked in, by default -- and
can take one back; a registration records when each happened and which
design version it was issued with. Null issued_at with an attendance keeps
the download attended guests always had, so nothing disappears on upgrade.

Revision ID: add_certificates
Revises: add_deliveries
"""

import sqlalchemy as sa
from alembic import op

revision = "add_certificates"
down_revision = "add_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event_registrations_link", sa.Column("certificate_issued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("event_registrations_link", sa.Column("certificate_revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "event_registrations_link",
        sa.Column("certificate_design_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("event_registrations_link", "certificate_design_version")
    op.drop_column("event_registrations_link", "certificate_revoked_at")
    op.drop_column("event_registrations_link", "certificate_issued_at")
