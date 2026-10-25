"""The Activity page's search: who acted, by name, and a trigram index.

The audit log's list drew a search box the endpoint ignored. It now finds
rows by words in who acted (name or email), the summary, the record ("club
#12") and the reason given (app.api.audit.service.list_entries).

Schema:

- admin_audit_logs.actor_name (varchar(100), nullable): the actor's name
  when they acted, kept like actor_email so a row still says who after the
  account is gone, and so the search reads no other table. Filled from
  users for the rows already there (only where it is still empty, in
  batches; running it again changes nothing).
- ix_audit_search_trgm: a GIN trigram index on the one expression the
  search reads. Built CONCURRENTLY, in an autocommit block, so no admin
  action waits on it; one a failed run left INVALID is dropped and built
  again.

Downgrade drops the index and the column (the stored names go with it).

The SQL is written out here, not imported: what this migration did must not
change when the app's code does.

Revision ID: audit_search
Revises: pricing_adjust
"""
import sqlalchemy as sa
from alembic import op

revision = "audit_search"
down_revision = "pricing_adjust"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
INDEX = "ix_audit_search_trgm"
#: app.api.audit.models.search_sql() as it was when this was written.
SEARCH = (
    "(coalesce(actor_name::text, '') || ' ' || coalesce(actor_email::text, '') || ' ' || "
    "coalesce(summary::text, '') || ' ' || coalesce(entity_type::text, '') || ' #' || "
    "coalesce(entity_id::text, '') || ' ' || coalesce(details ->> 'reason', ''))"
)
BATCH = 5000


def _state(name: str):
    """None when there is no such index, else whether it is valid."""
    row = op.get_bind().execute(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND c.relnamespace = current_schema()::regnamespace"
        ),
        {"name": name},
    ).first()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE admin_audit_logs ADD COLUMN IF NOT EXISTS actor_name varchar(100)")

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        # The names of who acted so far, a batch at a time: only rows still
        # without one, whose account still has one.
        while True:
            done = bind.execute(sa.text(
                "UPDATE admin_audit_logs a SET actor_name = left(u.full_name, 100) FROM users u "
                "WHERE a.id IN ("
                "  SELECT x.id FROM admin_audit_logs x JOIN users y ON y.id = x.actor_id "
                "  WHERE x.actor_name IS NULL AND y.full_name IS NOT NULL AND y.full_name <> '' "
                f"  LIMIT {BATCH}"
                ") AND u.id = a.actor_id"
            )).rowcount
            if not done:
                break
        state = _state(INDEX)
        if state is False:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        if not state:
            op.execute(f"CREATE INDEX CONCURRENTLY {INDEX} ON admin_audit_logs USING gin ({SEARCH} gin_trgm_ops)")
        op.execute("ANALYZE admin_audit_logs")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE admin_audit_logs DROP COLUMN IF EXISTS actor_name")
