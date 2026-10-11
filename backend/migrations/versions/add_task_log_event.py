"""An import remembers which event it was for.

Background task logs had no link to an event, so an organiser could not be
shown the history of their own imports.

Revision ID: add_task_log_event
Revises: add_certificates
"""

import sqlalchemy as sa
from alembic import op

revision = "add_task_log_event"
down_revision = "add_certificates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("background_task_logs", sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True))
    op.create_index("ix_background_task_logs_event_id", "background_task_logs", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_background_task_logs_event_id", table_name="background_task_logs")
    op.drop_column("background_task_logs", "event_id")
