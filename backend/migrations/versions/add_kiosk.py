"""Kiosks, phone check-in sessions, the event's self check-in settings, and which kiosk made a check-in.

Revision ID: add_kiosk
Revises: add_id_cards
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_kiosk"
down_revision = "add_id_cards"
branch_labels = None
depends_on = None

VERIFY = postgresql.ENUM("name", "email", "phone", name="selfcheckinverify", create_type=False)


def upgrade() -> None:
    VERIFY.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "kiosk_devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("door_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("doors.id", ondelete="SET NULL"), nullable=True),
        sa.Column("door_label", sa.String(40), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(8), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_kiosk_devices_event_id", "kiosk_devices", ["event_id"])
    op.create_table(
        "self_check_in_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("kiosk_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("kiosk_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id"), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lookups", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("code_hash", sa.String(64), nullable=True),
        sa.Column("code_channel", sa.String(16), nullable=True),
        sa.Column("code_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("code_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code_resent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checked_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("print_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("printed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_self_check_in_sessions_event_id", "self_check_in_sessions", ["event_id"])
    op.create_index("ix_self_check_in_sessions_kiosk_id", "self_check_in_sessions", ["kiosk_id"])
    op.add_column("events", sa.Column("self_check_in_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("events", sa.Column("self_check_in_verify", VERIFY, nullable=False, server_default="email"))
    op.add_column("events", sa.Column("self_check_in_print", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("events", sa.Column("kiosk_pin", sa.String(12), nullable=True))
    op.add_column("check_ins", sa.Column("kiosk_device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("kiosk_devices.id", ondelete="SET NULL"), nullable=True))


def downgrade() -> None:
    op.drop_column("check_ins", "kiosk_device_id")
    for col in ("kiosk_pin", "self_check_in_print", "self_check_in_verify", "self_check_in_enabled"):
        op.drop_column("events", col)
    op.drop_table("self_check_in_sessions")
    op.drop_table("kiosk_devices")
    VERIFY.drop(op.get_bind(), checkfirst=True)
