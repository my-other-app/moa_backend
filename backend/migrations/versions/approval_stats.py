"""Confirm-each events: full numbers, and the loopholes closed
(docs/APPROVALS.md §7).

- events.high_demand_label: boolean, not null, default false. The club's
  "High demand" label for the public page, instead of any request numbers.
- registration_requests.closed_reason: varchar(24), null. Why a request is
  `closed`: registered_directly, event_ended, event_deleted or
  event_cancelled. A check constraint keeps it to those.
- ix_registration_requests_registration: (registration_id) WHERE NOT NULL.
  The registration's own deletion (the foreign key's SET NULL) and the
  figures that tell a confirmed registration from one added directly read it.

The requests already closed get their reason from their event: called off,
deleted, or else over (the only three ways a request closed before this).
registration_requests is new and small (weeks old), so that UPDATE and the
index build are short; neither touches a hot table.

events.high_demand_label is added with a constant default, which Postgres
(11 and on) writes into the catalogue without rewriting the table: a brief
ACCESS EXCLUSIVE lock, under lock_timeout 3s.

Downgrade drops the index, the constraint and both columns.

Revision ID: approval_stats
Revises: registration_approval
"""
import sqlalchemy as sa
from alembic import op

revision = "approval_stats"
down_revision = "registration_approval"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
REASONS = "('registered_directly', 'event_ended', 'event_deleted', 'event_cancelled')"

BACKFILL = """
UPDATE registration_requests r
SET closed_reason = CASE
    WHEN e.status = 'cancelled' THEN 'event_cancelled'
    WHEN e.is_deleted THEN 'event_deleted'
    ELSE 'event_ended'
END
FROM events e
WHERE e.id = r.event_id AND r.status = 'closed' AND r.closed_reason IS NULL
"""


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS high_demand_label boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE registration_requests ADD COLUMN IF NOT EXISTS closed_reason varchar(24)")
    op.execute(
        "ALTER TABLE registration_requests DROP CONSTRAINT IF EXISTS ck_registration_requests_closed_reason"
    )
    op.execute(
        "ALTER TABLE registration_requests ADD CONSTRAINT ck_registration_requests_closed_reason "
        f"CHECK (closed_reason IS NULL OR closed_reason IN {REASONS})"
    )
    op.execute(BACKFILL)
    op.create_index(
        "ix_registration_requests_registration", "registration_requests", ["registration_id"],
        postgresql_where=sa.text("registration_id IS NOT NULL"), if_not_exists=True,
    )


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP INDEX IF EXISTS ix_registration_requests_registration")
    op.execute("ALTER TABLE registration_requests DROP CONSTRAINT IF EXISTS ck_registration_requests_closed_reason")
    op.execute("ALTER TABLE registration_requests DROP COLUMN IF EXISTS closed_reason")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS high_demand_label")
