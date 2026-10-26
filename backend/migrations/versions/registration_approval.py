"""Registration approval: "Confirm each registration" (app/api/approvals,
docs/APPROVALS.md).

- events.requires_approval: boolean, not null, default false. The public
  form on such an event makes a request the club confirms or declines.
- waitlist_entries.via_request: boolean, not null, default false. A paid
  confirmation holds its place as an offer of the waiting-list machinery;
  this marks those offers, which the club's waiting list leaves out.
- registration_requests: new, one row per request.

Both columns are added with a constant default, which Postgres (11 and on)
writes into the catalogue without rewriting the table: a brief ACCESS
EXCLUSIVE lock on each, under lock_timeout 3s, so a busy table fails the
migration fast rather than queue every request behind it. The new table's
indexes are built on an empty table.

The foreign keys reference primary keys that earlier migrations made
(events, users, ticket_types and waitlist_entries in `ticketing`,
tracking_links in `add_tracking_links`, event_registrations_link's in
`add_missing_primary_keys`).

Downgrade drops the table and both columns: every request goes with it.

Revision ID: registration_approval
Revises: admin_ops
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "registration_approval"
down_revision = "admin_ops"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")
OPEN = "status IN ('pending', 'approved')"


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS requires_approval boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE waitlist_entries ADD COLUMN IF NOT EXISTS via_request boolean NOT NULL DEFAULT false")
    op.create_table(
        "registration_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("full_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(15), nullable=True),
        sa.Column("ticket_type_id", sa.Integer(), sa.ForeignKey("ticket_types.id", ondelete="SET NULL"), nullable=True),
        sa.Column("discount_code", sa.String(40), nullable=True),
        sa.Column("answers", postgresql.JSONB(), nullable=True),
        sa.Column("whatsapp_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("club_updates_whatsapp", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "tracking_link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tracking_links.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_by_name", sa.String(120), nullable=True),
        sa.Column("decline_note", sa.String(300), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "registration_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "waitlist_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("waitlist_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'declined', 'withdrawn', 'lapsed', 'closed')",
            name="ck_registration_requests_status",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_registration_requests_queue", "registration_requests", ["event_id", "status", "submitted_at", "id"],
        if_not_exists=True,
    )
    op.create_index(
        "uq_registration_requests_open_email", "registration_requests", ["event_id", sa.text("lower(btrim(email))")],
        unique=True, postgresql_where=sa.text(OPEN), if_not_exists=True,
    )
    op.create_index(
        "uq_registration_requests_open_user", "registration_requests", ["event_id", "user_id"],
        unique=True, postgresql_where=sa.text(f"{OPEN} AND user_id IS NOT NULL"), if_not_exists=True,
    )
    op.create_index("uq_registration_requests_token", "registration_requests", ["token"], unique=True, if_not_exists=True)
    op.create_index(
        "ix_registration_requests_user", "registration_requests", ["user_id", "submitted_at"],
        postgresql_where=sa.text("user_id IS NOT NULL"), if_not_exists=True,
    )
    op.create_index(
        "ix_registration_requests_hold", "registration_requests", ["waitlist_entry_id"],
        postgresql_where=sa.text("waitlist_entry_id IS NOT NULL"), if_not_exists=True,
    )
    op.create_index(
        "ix_registration_requests_pending", "registration_requests", ["event_id", "submitted_at", "id"],
        postgresql_where=sa.text("status = 'pending'"), if_not_exists=True,
    )


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP TABLE IF EXISTS registration_requests")
    op.execute("ALTER TABLE waitlist_entries DROP COLUMN IF EXISTS via_request")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS requires_approval")
