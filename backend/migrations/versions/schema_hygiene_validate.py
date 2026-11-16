"""The second half of schema_hygiene, for once production's rows are known
to fit: the money tables' CHECKs validated, and the old unique keys that
include is_deleted dropped.

Deploy it only after schema_hygiene is live (its code is the one running:
follow, pin and hide upsert against the live-row key) and the count of
rows outside each CHECK's words is 0 on production.

- VALIDATE CONSTRAINT for ck_event_store_orders_status,
  ck_club_refunds_status, ck_payment_refunds_kind, ck_ledger_entries_kind.
  It reads the table under SHARE UPDATE EXCLUSIVE, which no read or write
  waits for. A row outside a CHECK's words stops the migration there,
  Postgres naming the constraint, before any key is dropped.
- DROP CONSTRAINT for the four keys with is_deleted in them, which allowed
  one deleted row per key and no second: club_users_link (club_id,
  user_id, is_deleted), event_ratings_link (event_id, user_id,
  is_deleted), club_socials (club_id, is_deleted), club_subscriptions
  (club_id, is_deleted). Each only while its live-row index from
  schema_hygiene is there and valid; that index says the same about the
  live rows and allows any number of deleted ones. ACCESS EXCLUSIVE for
  an instant (2 s lock limit, the guard tries again).

Each statement is its own transaction (an autocommit block), and each step
is a no-op when already done, so a run cut short can run again.

Downgrade builds the old keys again (CONCURRENTLY, then attached as
constraints); it fails if two deleted rows now share one. The CHECKs stay
validated.

Revision ID: schema_hygiene_validate
Revises: schema_hygiene
"""
import sqlalchemy as sa
from alembic import op

revision = "schema_hygiene_validate"
down_revision = "schema_hygiene"
branch_labels = None
depends_on = None

#: (table, CHECK constraint) validated here; added NOT VALID by schema_hygiene.
CHECKS = (
    ("event_store_orders", "ck_event_store_orders_status"),
    ("club_refunds", "ck_club_refunds_status"),
    ("payment_refunds", "ck_payment_refunds_kind"),
    ("ledger_entries", "ck_ledger_entries_kind"),
)

#: (table, old key, its columns, the live-row index that replaces it). Older
#: databases named the ratings key event_ratings_link_event_id_user_id_is_deleted_key
#: (fix_event_ratings_unique renamed it); either name goes.
OLD_KEYS = (
    ("club_users_link", ("club_users_link_club_id_user_id_is_deleted_key",), "club_id, user_id, is_deleted", "ux_club_users_link_live"),
    (
        "event_ratings_link",
        ("uq_event_ratings_event_user_is_deleted", "event_ratings_link_event_id_user_id_is_deleted_key"),
        "event_id, user_id, is_deleted",
        "ux_event_ratings_link_live",
    ),
    ("club_socials", ("club_socials_club_id_is_deleted_key",), "club_id, is_deleted", "ux_club_socials_live"),
    ("club_subscriptions", ("uq_club_subscription_live",), "club_id, is_deleted", "ux_club_subscriptions_live"),
)

_INDEX_VALID = sa.text(
    "SELECT i.indisvalid AND i.indisready FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = :name AND n.nspname = current_schema()"
)
_HAS_CONSTRAINT = sa.text(
    "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE c.conname = :name AND t.relname = :table AND n.nspname = current_schema()"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for table, name in CHECKS:
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        for table, _names, _columns, live in OLD_KEYS:
            if not bind.execute(_INDEX_VALID, {"name": live}).scalar():
                raise RuntimeError(f"{live} is missing or not valid: {table}'s old unique key is kept (run schema_hygiene first)")
        op.execute("SET lock_timeout = '2s'")
        for table, names, _columns, _live in OLD_KEYS:
            for name in names:
                op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for table, names, columns, _live in OLD_KEYS:
            name = names[0]
            if bind.execute(_HAS_CONSTRAINT, {"name": name, "table": table}).first() is not None:
                continue
            if bind.execute(_INDEX_VALID, {"name": name}).scalar() is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns})")
            op.execute("SET lock_timeout = '2s'")
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE USING INDEX {name}")
            op.execute("RESET lock_timeout")
