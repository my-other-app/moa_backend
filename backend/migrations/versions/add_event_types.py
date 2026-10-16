"""The event types every club can pick from, present on every install.

Revision ID: add_event_types
Revises: add_club_team
"""
import sqlalchemy as sa
from alembic import op

revision = "add_event_types"
down_revision = "add_club_team"
branch_labels = None
depends_on = None

TYPES = ("Workshop", "Seminar", "Hackathon", "Competition", "Bootcamp", "Panel discussion", "Webinar", "Meetup", "Social", "TED-style talk")


def upgrade() -> None:
    # A type the product ships belongs to nobody.
    op.alter_column("event_categories", "created_by_id", existing_type=sa.Integer(), nullable=True)
    bind = op.get_bind()
    for name in TYPES:
        bind.execute(
            sa.text(
                "INSERT INTO event_categories (name, is_deleted, created_at, updated_at) "
                "SELECT :name, false, now(), now() "
                "WHERE NOT EXISTS (SELECT 1 FROM event_categories WHERE lower(name) = lower(:name) AND is_deleted = false)"
            ),
            {"name": name},
        )


def downgrade() -> None:
    op.alter_column("event_categories", "created_by_id", existing_type=sa.Integer(), nullable=False)
