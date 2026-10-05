"""Two-factor authentication for admin accounts.

Revision ID: add_two_factor
Revises: add_user_sessions
"""

import sqlalchemy as sa
from alembic import op

revision = "add_two_factor"
down_revision = "add_user_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret", sa.String(length=64), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "totp_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Hashed, never plaintext -- a recovery code is a password equivalent.
    op.add_column("users", sa.Column("totp_recovery_codes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "totp_recovery_codes")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")
