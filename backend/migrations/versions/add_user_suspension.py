"""User suspension, as distinct from deletion.

Revision ID: add_user_suspension
Revises: add_admin_audit_logs
"""

import sqlalchemy as sa
from alembic import op

revision = "add_user_suspension"
down_revision = "add_admin_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so existing rows become active rather than NULL, which
    # the NOT NULL would reject.
    op.add_column(
        "users",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "users", sa.Column("suspended_reason", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "suspended_reason")
    op.drop_column("users", "is_active")
