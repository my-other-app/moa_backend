"""Security: sign-in attempts, cached places for addresses, and the new-device email switch.

Revision ID: add_security_signins
Revises: add_club_profile_extras
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_security_signins"
down_revision = "add_club_profile_extras"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("notify_new_device", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        "sign_in_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("identifier", sa.String(120), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("result", sa.String(30), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("place", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sign_in_attempts_user_id", "sign_in_attempts", ["user_id"])
    op.create_index("ix_sign_in_attempts_identifier", "sign_in_attempts", ["identifier"])
    op.create_index("ix_sign_in_attempts_ip_address", "sign_in_attempts", ["ip_address"])
    op.create_index("ix_sign_in_attempts_user_created", "sign_in_attempts", ["user_id", "created_at"])
    op.create_table(
        "ip_places",
        sa.Column("ip_address", sa.String(45), nullable=False),
        sa.Column("place", sa.String(120), nullable=True),
        sa.Column("looked_up_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ip_address"),
    )


def downgrade() -> None:
    op.drop_table("ip_places")
    op.drop_index("ix_sign_in_attempts_user_created", table_name="sign_in_attempts")
    op.drop_index("ix_sign_in_attempts_ip_address", table_name="sign_in_attempts")
    op.drop_index("ix_sign_in_attempts_identifier", table_name="sign_in_attempts")
    op.drop_index("ix_sign_in_attempts_user_id", table_name="sign_in_attempts")
    op.drop_table("sign_in_attempts")
    op.drop_column("users", "notify_new_device")
