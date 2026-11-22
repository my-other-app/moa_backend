"""Print stations: remote ID-card printing (feature-register/print-station/CONTRACT.md).

- kiosk_devices.role (NOT NULL, constant default 'kiosk': every existing
  device stays a kiosk; "printer" is a paired print station). A constant
  default does not rewrite the table on PG 11+.
- card_print_requests: a guest's card waiting for a print station, with its
  indexes (event, status, created_at) and (registration), and a partial
  unique index so a guest has at most one open (queued or printing) request.
  The table is new and empty, so its indexes are made with it.

Adding the column takes kiosk_devices' ACCESS EXCLUSIVE lock for as long as
the catalogue change takes (milliseconds); lock_timeout makes the migration
give up rather than queue behind a long transaction. Idempotent: running it
again after a partial run finishes the job.

Revision ID: print_stations
Revises: live_custom
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "print_stations"
down_revision = "live_custom"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def _at(name, nullable=True, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kw)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE kiosk_devices ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'kiosk'")
    op.execute("ALTER TABLE kiosk_devices DROP CONSTRAINT IF EXISTS ck_kiosk_devices_role")
    op.execute("ALTER TABLE kiosk_devices ADD CONSTRAINT ck_kiosk_devices_role CHECK (role IN ('kiosk', 'printer')) NOT VALID")
    op.execute("ALTER TABLE kiosk_devices VALIDATE CONSTRAINT ck_kiosk_devices_role")

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("card_print_requests"):
        op.create_table(
            "card_print_requests",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
            sa.Column("registration_id", UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id"), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("requested_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("requested_by_device_id", UUID(as_uuid=True), sa.ForeignKey("kiosk_devices.id", ondelete="SET NULL"), nullable=True),
            sa.Column("printer_id", UUID(as_uuid=True), sa.ForeignKey("kiosk_devices.id", ondelete="SET NULL"), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("claimed_by", UUID(as_uuid=True), sa.ForeignKey("kiosk_devices.id", ondelete="SET NULL"), nullable=True),
            _at("claimed_at"),
            _at("lease_until"),
            _at("printed_at"),
            sa.Column("override", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("error", sa.String(400), nullable=True),
            _at("created_at", False, server_default=NOW),
            _at("updated_at", False, server_default=NOW),
            sa.CheckConstraint("status IN ('queued', 'printing', 'printed', 'failed', 'cancelled')", name="ck_card_print_requests_status"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_card_print_requests_event_status ON card_print_requests (event_id, status, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_card_print_requests_registration ON card_print_requests (registration_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_card_print_requests_open ON card_print_requests (event_id, registration_id) WHERE status IN ('queued', 'printing')")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS card_print_requests")
    op.execute("ALTER TABLE kiosk_devices DROP CONSTRAINT IF EXISTS ck_kiosk_devices_role")
    op.execute("ALTER TABLE kiosk_devices DROP COLUMN IF EXISTS role")
