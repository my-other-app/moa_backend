"""Email verification: users.email_verified_at, and the codes sent to prove it.

A password account's address was only ever typed, so the guest merge left
those accounts out. Now a code sent to the address and typed back stamps
email_verified_at, and stamped accounts take part like Google and Apple ones.

Google and Apple checked the address before handing it over, so their app
accounts are stamped here. Password accounts stay unstamped until their
owners verify; nobody is signed out or locked out by this.

`provider` is compared as text: the chain created its enum with 'google' and
'email' only, and a schema built from it has no 'apple' label to compare to.

Revision ID: add_email_verification
Revises: allow_repeat_guests
"""
import sqlalchemy as sa
from alembic import op

revision = "add_email_verification"
down_revision = "allow_repeat_guests"
branch_labels = None
depends_on = None

BACKFILL = """
UPDATE users
SET email_verified_at = now()
WHERE email_verified_at IS NULL
  AND user_type = 'app_user'
  AND provider::text IN ('google', 'apple')
"""


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(BACKFILL)
    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(100), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_verification_codes_user_sent", "email_verification_codes", ["user_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_email_verification_codes_user_sent", table_name="email_verification_codes")
    op.drop_table("email_verification_codes")
    op.drop_column("users", "email_verified_at")
