"""Closing an event: the status clock on events, closeout reports, retention jobs, archived contacts.

Revision ID: add_closeout
Revises: add_merch
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_closeout"
down_revision = "add_merch"
branch_labels = None
depends_on = None

STATUS = postgresql.ENUM("draft", "published", "live", "closed", "archived", name="eventstatus", create_type=False)
MODE = postgresql.ENUM("keep", "delete", name="retentionmode", create_type=False)
STATE = postgresql.ENUM("pending", "report_sent", "deleted", "failed", name="retentionstate", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    STATUS.create(bind, checkfirst=True)
    MODE.create(bind, checkfirst=True)
    STATE.create(bind, checkfirst=True)

    op.add_column("events", sa.Column("status", STATUS, nullable=False, server_default="published"))
    op.add_column("events", sa.Column("status_clock", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("events", sa.Column("retention_mode", MODE, nullable=False, server_default="keep"))
    op.add_column("events", sa.Column("retention_hours", sa.Integer(), nullable=False, server_default="72"))
    op.add_column("events", sa.Column("report_recipients", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"))
    op.add_column("events", sa.Column("keep_contacts", sa.Boolean(), nullable=False, server_default=sa.true()))

    op.create_table(
        "closeout_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("pdf_key", sa.String(300), nullable=False),
        sa.Column("filename", sa.String(160), nullable=False),
        sa.Column("figures", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("delivered_to", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_closeout_reports_event_id", "closeout_reports", ["event_id"])

    op.create_table(
        "retention_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False, unique=True),
        sa.Column("state", STATE, nullable=False, server_default="pending"),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(400), nullable=True),
        sa.Column("last_refusal", sa.String(40), nullable=True),
        sa.Column("refused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("closeout_reports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("deleted_counts", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_retention_jobs_state_run_at", "retention_jobs", ["state", "run_at"])

    op.create_table(
        "archived_contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(100), nullable=True),
        sa.Column("phone", sa.String(15), nullable=True),
        sa.Column("role", sa.String(20), nullable=False, server_default="attendee"),
        sa.Column("attended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("event_name", sa.String(100), nullable=False),
        sa.Column("event_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("event_id", "registration_id", name="uq_archived_contacts_event_registration"),
    )
    op.create_index("ix_archived_contacts_club_id", "archived_contacts", ["club_id"])
    op.create_index("ix_archived_contacts_event_id", "archived_contacts", ["event_id"])


def downgrade() -> None:
    op.drop_table("archived_contacts")
    op.drop_table("retention_jobs")
    op.drop_table("closeout_reports")
    for col in ("keep_contacts", "report_recipients", "retention_hours", "retention_mode", "status_clock", "status"):
        op.drop_column("events", col)
    bind = op.get_bind()
    STATE.drop(bind, checkfirst=True)
    MODE.drop(bind, checkfirst=True)
    STATUS.drop(bind, checkfirst=True)
