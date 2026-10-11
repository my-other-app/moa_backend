"""Deliveries: what was sent to whom, on which channel, and what became of it.

Tickets were emailed on registration and nothing recorded whether the mail
went, so "who never got their ticket" could not be answered. One row per
registration, per kind of document, per channel; the worker upserts it.

A registration also gains the time its ticket was revoked, so the door can
refuse a lost or transferred ticket by name.

Revision ID: add_deliveries
Revises: add_event_template_variants
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_deliveries"
down_revision = "add_missing_primary_keys"
branch_labels = None
depends_on = None

KIND = postgresql.ENUM("ticket", "certificate", name="deliverykind", create_type=False)
CHANNEL = postgresql.ENUM("email", "whatsapp", name="deliverychannel", create_type=False)
STATUS = postgresql.ENUM("pending", "sent", "failed", "skipped", name="deliverystatus", create_type=False)
OUTCOME = postgresql.ENUM(
    "delivered", "read", "undelivered", "bounced", "complained", name="deliveryoutcome", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    for t in (KIND, CHANNEL, STATUS, OUTCOME):
        t.create(bind, checkfirst=True)

    op.create_table(
        "deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column(
            "registration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("event_registrations_link.id"),
            nullable=False,
        ),
        sa.Column("kind", KIND, nullable=False),
        sa.Column("channel", CHANNEL, nullable=False),
        sa.Column("status", STATUS, nullable=False, server_default="pending"),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("error", sa.String(400), nullable=True),
        sa.Column("outcome", OUTCOME, nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(400), nullable=True),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("design_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("registration_id", "kind", "channel", name="uq_deliveries_registration_kind_channel"),
    )
    op.create_index("ix_deliveries_event_id", "deliveries", ["event_id"])
    op.create_index("ix_deliveries_registration_id", "deliveries", ["registration_id"])

    op.add_column(
        "event_registrations_link",
        sa.Column("ticket_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("event_registrations_link", "ticket_revoked_at")
    op.drop_index("ix_deliveries_registration_id", table_name="deliveries")
    op.drop_index("ix_deliveries_event_id", table_name="deliveries")
    op.drop_table("deliveries")
    bind = op.get_bind()
    for t in (OUTCOME, STATUS, CHANNEL, KIND):
        t.drop(bind, checkfirst=True)
