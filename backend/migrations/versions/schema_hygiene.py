"""Schema hygiene: one live row per key on four tables, room on the events
rows for their counters, and the money tables' status words checked.

- Unique on the live rows only (built CONCURRENTLY):
  ux_club_users_link_live (club_id, user_id), ux_event_ratings_link_live
  (event_id, user_id), ux_club_socials_live (club_id) and
  ux_club_subscriptions_live (club_id), each WHERE NOT is_deleted. The
  unique keys there today include is_deleted, which lets a key have one
  deleted row and no second. They stay for now, and say the same about
  the live rows, so nothing already there can break these; the follow, pin
  and hide upserts move to the new key with this revision, and a later
  revision drops the old keys once this code is the one running.
- events fillfactor 85: a counter bump (going_count, paid_count, the
  follower and ticket-type counts' triggers) then usually finds room on
  the row's own page and does not write every index of the table again.
  A storage setting only: nothing is rewritten, pages already full get
  the room as they are next written.
- CHECK constraints on the words the money tables' status columns may
  hold, added NOT VALID: Postgres checks every row written from now on,
  and does not read the rows already there. A later revision validates
  them, after a count of rows outside the list is zero on production.
  event_store_orders.status, club_refunds.status, payment_refunds.kind,
  ledger_entries.kind (app/api/ledger/book.KINDS).

Every statement runs in an autocommit block: each one is its own short
transaction, under the migration guard's lock limit and retry
(migrations/env.py), so no lock is held past the statement that took it.
Each step is skipped when it is already done (an index left INVALID by a
build that was cut off is dropped and built again), so a run that stopped
part way can simply run again.

Downgrade drops the CHECKs and the indexes and puts the fillfactor back.

Revision ID: schema_hygiene
Revises: stability_indexes
"""
import sqlalchemy as sa
from alembic import op

revision = "schema_hygiene"
down_revision = "stability_indexes"
branch_labels = None
depends_on = None

#: (name, table, columns) of each index on the live rows this adds.
LIVE_KEYS = (
    ("ux_club_users_link_live", "club_users_link", "club_id, user_id"),
    ("ux_event_ratings_link_live", "event_ratings_link", "event_id, user_id"),
    ("ux_club_socials_live", "club_socials", "club_id"),
    ("ux_club_subscriptions_live", "club_subscriptions", "club_id"),
)

LEDGER_KINDS = (
    "capture", "refund", "apply", "unapply", "desk", "fulfil", "comp", "sub_charge", "sub_refund",
    "first_charge", "backfill", "repair",
)

#: (name, table, column, the words it may hold).
CHECKS = (
    ("ck_event_store_orders_status", "event_store_orders", "status", ("pending", "paid", "cancelled", "refunded", "oversold")),
    ("ck_club_refunds_status", "club_refunds", "status", ("pending", "processing", "succeeded", "failed")),
    ("ck_payment_refunds_kind", "payment_refunds", "kind", ("ticket", "store_order", "rest")),
    ("ck_ledger_entries_kind", "ledger_entries", "kind", LEDGER_KINDS),
)

FILLFACTOR = 85

_INDEX_STATE = sa.text(
    "SELECT i.indisvalid AND i.indisready FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = :name AND n.nspname = current_schema()"
)
_HAS_CONSTRAINT = sa.text(
    "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE c.conname = :name AND t.relname = :table AND n.nspname = current_schema()"
)


def check_sql(column: str, words) -> str:
    return f"{column} IN ({', '.join(repr(w) for w in words)})"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, table, columns in LIVE_KEYS:
            valid = bind.execute(_INDEX_STATE, {"name": name}).scalar()
            if valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns}) WHERE NOT is_deleted")

        # A storage parameter: SHARE UPDATE EXCLUSIVE, which no read or write waits for.
        op.execute(f"ALTER TABLE events SET (fillfactor = {FILLFACTOR})")

        # ADD CONSTRAINT ... NOT VALID is a catalog change, but its lock is
        # ACCESS EXCLUSIVE for that instant: wait for it briefly, and let the
        # guard try again, rather than queue the table's readers behind it.
        op.execute("SET lock_timeout = '2s'")
        for name, table, column, words in CHECKS:
            if bind.execute(_HAS_CONSTRAINT, {"name": name, "table": table}).first() is None:
                op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check_sql(column, words)}) NOT VALID")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '2s'")
        for name, table, _column, _words in reversed(CHECKS):
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute("RESET lock_timeout")
        op.execute("ALTER TABLE events RESET (fillfactor)")
        for name, _table, _columns in reversed(LIVE_KEYS):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
