"""WhatsApp: the guest's consent, and the day-before reminder.

- event_registrations_link.whatsapp_opt_in (NOT NULL DEFAULT false): the
  guest ticked "Send my ticket and event reminders on WhatsApp" on the
  registration form. False for every registration already made -- consent
  is never assumed -- and for every new one that does not tick it.
- event_registrations_link.whatsapp_opt_in_at (timestamptz, nullable): when.
- ix_registrations_whatsapp_opt_in on (event_id), where whatsapp_opt_in and
  not deleted: the reminder sweep's minute look at the guests of the events
  starting within a day reads these alone. Empty at first, since nobody
  has opted in yet.
- deliverykind gains 'reminder': the day-before reminder is a delivery row
  like a ticket's, one per registration (the unique registration, kind,
  channel key), so a second sweep sends nobody a second one.

Both columns are a change to the catalogue only (a constant default, a
nullable column): no rewrite, no scan. ALTER TABLE still waits for its lock
on a hot table, so lock_timeout bounds the wait and env.py tries again. The
enum value is added in the transaction (Postgres 12+) and not used in it.
The index is built CONCURRENTLY after the commit, any invalid leftover of a
failed run dropped first, so registrations go on throughout.

Downgrade drops the index and the columns, deletes the reminder rows (their
attempts go with them, ON DELETE CASCADE) and builds deliverykind again
without 'reminder' -- Postgres cannot drop one value from an enum.

Revision ID: whatsapp_opt_in
Revises: certificate_numbers
"""
import sqlalchemy as sa
from alembic import op

revision = "whatsapp_opt_in"
down_revision = "certificate_numbers"
branch_labels = None
depends_on = None

INDEX = "ix_registrations_whatsapp_opt_in"


def upgrade() -> None:
    # A hot table: wait for its lock briefly, and let env.py try again, rather than queue every request behind it.
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column(
        "event_registrations_link",
        sa.Column("whatsapp_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("event_registrations_link", sa.Column("whatsapp_opt_in_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("ALTER TYPE deliverykind ADD VALUE IF NOT EXISTS 'reminder'")
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {INDEX} ON event_registrations_link (event_id) "
            "WHERE whatsapp_opt_in AND NOT is_deleted"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("DELETE FROM deliveries WHERE kind = 'reminder'")
    op.execute("ALTER TYPE deliverykind RENAME TO deliverykind_old")
    op.execute("CREATE TYPE deliverykind AS ENUM ('ticket', 'certificate')")
    op.execute("ALTER TABLE deliveries ALTER COLUMN kind TYPE deliverykind USING kind::text::deliverykind")
    op.execute("DROP TYPE deliverykind_old")
    op.drop_column("event_registrations_link", "whatsapp_opt_in_at")
    op.drop_column("event_registrations_link", "whatsapp_opt_in")
