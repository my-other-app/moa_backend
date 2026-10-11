"""Doors, check-ins, volunteer switches and the event's door settings.

Revision ID: add_doors_check_ins
Revises: add_emails
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_doors_check_ins"
down_revision = "add_emails"
branch_labels = None
depends_on = None

DOOR_KIND = postgresql.ENUM("door", "desk", "kiosk", name="doorkind", create_type=False)
DOOR_MODE = postgresql.ENUM("open", "auto", "closed", name="doormode", create_type=False)
VIA = postgresql.ENUM("gate", "kiosk", "walkup", "offline_sync", "desk", "phone", "app", name="checkinvia", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for t in (DOOR_KIND, DOOR_MODE, VIA):
        t.create(bind, checkfirst=True)

    op.create_table(
        "doors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("kind", DOOR_KIND, nullable=False, server_default="door"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("link_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("link_prefix", sa.String(8), nullable=True),
        sa.Column("link_can_walkup", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("link_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("event_id", "name", name="uq_doors_event_name"),
    )
    op.create_index("ix_doors_event_id", "doors", ["event_id"])

    op.create_table(
        "check_ins",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id"), nullable=False, unique=True),
        sa.Column("door_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("doors.id", ondelete="SET NULL"), nullable=True),
        sa.Column("door_label", sa.String(40), nullable=True),
        sa.Column("checked_in_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("via", VIA, nullable=False, server_default="gate"),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_ref", sa.String(64), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_check_ins_event_id", "check_ins", ["event_id"])
    op.create_index("ix_check_ins_door_id", "check_ins", ["door_id"])
    op.create_index("ix_check_ins_event_checked_in_at", "check_ins", ["event_id", "checked_in_at"])

    # Guests already marked present keep that: one row each, admitted "by hand".
    op.execute(
        """
        INSERT INTO check_ins (event_id, registration_id, via, checked_in_at, created_at, updated_at)
        SELECT event_id, id, 'desk', COALESCE(attended_on, updated_at, now()), now(), now()
        FROM event_registrations_link
        WHERE is_attended = true AND is_deleted = false
        """
    )

    op.add_column("volunteers", sa.Column("can_check_in", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("volunteers", sa.Column("can_walkup", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("volunteers", sa.Column("can_merch", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("volunteers", sa.Column("door_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("doors.id", ondelete="SET NULL"), nullable=True))

    op.add_column("events", sa.Column("door_mode", DOOR_MODE, nullable=False, server_default="open"))
    op.add_column("events", sa.Column("close_after_minutes", sa.Integer(), nullable=False, server_default="240"))
    op.add_column("events", sa.Column("spot_registration_checks_in", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("events", sa.Column("capture_contact_at_gate", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("events", sa.Column("require_contact_at_gate", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for col in ("require_contact_at_gate", "capture_contact_at_gate", "spot_registration_checks_in", "close_after_minutes", "door_mode"):
        op.drop_column("events", col)
    for col in ("door_id", "can_merch", "can_walkup", "can_check_in"):
        op.drop_column("volunteers", col)
    op.drop_table("check_ins")
    op.drop_table("doors")
    bind = op.get_bind()
    for t in (VIA, DOOR_MODE, DOOR_KIND):
        t.drop(bind, checkfirst=True)
