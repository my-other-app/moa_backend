"""Designed emails, and every message sent to guests.

email_templates: a club's library (event_id null) and each event's copies
of the four journey stages plus custom emails. messages: one row per send,
with the design frozen; message_deliveries: one row per guest per channel.

Revision ID: add_emails
Revises: add_task_log_event
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_emails"
down_revision = "add_task_log_event"
branch_labels = None
depends_on = None

STAGE = postgresql.ENUM("INVITE", "PRE_EVENT", "REMINDER", "POST_EVENT", "CUSTOM", name="emailstage", create_type=False)
MESSAGE_KIND = postgresql.ENUM("BROADCAST", "TEMPLATE", "TEST", name="messagekind", create_type=False)
MESSAGE_STATUS = postgresql.ENUM("SENDING", "SENT", name="messagestatus", create_type=False)
# Shared with the deliveries table, made by add_deliveries.
CHANNEL = postgresql.ENUM("email", "whatsapp", name="deliverychannel", create_type=False)
STATUS = postgresql.ENUM("pending", "sent", "failed", "skipped", name="deliverystatus", create_type=False)
OUTCOME = postgresql.ENUM("delivered", "read", "undelivered", "bounced", "complained", name="deliveryoutcome", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for t in (STAGE, MESSAGE_KIND, MESSAGE_STATUS):
        t.create(bind, checkfirst=True)

    op.create_table(
        "email_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("stage", STAGE, nullable=False, server_default="CUSTOM"),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("design", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("attachments", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("audience", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("trigger", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("channels", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_email_templates_club_id", "email_templates", ["club_id"])
    op.create_index("ix_email_templates_event_id", "email_templates", ["event_id"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", MESSAGE_KIND, nullable=False, server_default="BROADCAST"),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("email_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("design", sa.JSON(), nullable=True),
        sa.Column("attachments", sa.JSON(), nullable=True),
        sa.Column("scheduled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("body", sa.String(4000), nullable=False, server_default=""),
        sa.Column("audience", sa.String(40), nullable=False, server_default="ALL"),
        sa.Column("roles", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("channels", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", MESSAGE_STATUS, nullable=False, server_default="SENDING"),
        sa.Column("recipients", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("event_id", "client_ref", name="uq_messages_event_client_ref"),
    )
    op.create_index("ix_messages_event_id", "messages", ["event_id"])
    op.create_index("ix_messages_template_id", "messages", ["template_id"])

    op.create_table(
        "message_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id"), nullable=False),
        sa.Column("channel", CHANNEL, nullable=False),
        sa.Column("status", STATUS, nullable=False, server_default="pending"),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("error", sa.String(400), nullable=True),
        sa.Column("outcome", OUTCOME, nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(400), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("message_id", "registration_id", "channel", name="uq_message_deliveries_message_registration_channel"),
    )
    op.create_index("ix_message_deliveries_message_id", "message_deliveries", ["message_id"])
    op.create_index("ix_message_deliveries_registration_id", "message_deliveries", ["registration_id"])


def downgrade() -> None:
    op.drop_table("message_deliveries")
    op.drop_table("messages")
    op.drop_table("email_templates")
    bind = op.get_bind()
    for t in (MESSAGE_STATUS, MESSAGE_KIND, STAGE):
        t.drop(bind, checkfirst=True)
