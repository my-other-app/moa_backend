"""“Can't come? Give your ticket back” (app/api/ticket_cancel,
feature-register/ticket-self-cancel-contract.md).

- events.allow_self_cancel: whether the event's guests may give a free
  ticket back by the link in their emails. On (true) for every event, as the
  contract asks; a club turns it off per event.
- ticket_cancellations: one row a ticket given back that way (who, when,
  why), kept after the registration is taken off the list.

Lock-safe: events is a hot table, so lock_timeout 3s comes first; the new
column is NOT NULL with a constant default, which Postgres 11+ writes in
the catalog only (no rewrite, no scan). The new table is empty; its foreign
keys take a brief SHARE ROW EXCLUSIVE on event_registrations_link, events
and users.

Downgrade drops the table and the column: the record of tickets given back
goes; the registrations taken off the list stay off it, as the club's own
removals do.

Revision ID: ticket_self_cancel
Revises: review_resends
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ticket_self_cancel"
down_revision = "review_resends"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS allow_self_cancel boolean NOT NULL DEFAULT true")
    op.create_table(
        "ticket_cancellations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "registration_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("full_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(100), nullable=False),
        sa.Column("ticket_id", sa.String(60), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("registration_id", name="ticket_cancellations_registration_id_key"),
        if_not_exists=True,
    )
    op.create_index("ix_ticket_cancellations_event", "ticket_cancellations", ["event_id", "cancelled_at"], if_not_exists=True)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP TABLE IF EXISTS ticket_cancellations")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS allow_self_cancel")
