"""Organisation search: organizations.search_text and its trigram index.

The 1,936 Kerala colleges just imported are only reachable through an
unfiltered GET /orgs/list. This gives them the same search every other table
here has (app.api.search.text): one column holding the name and the address,
lowercased, unaccented and with every run of punctuation turned into one
space, kept by a trigger, under one GIN trigram index.

- `moa_org_search_norm` / `moa_org_search_text`, and the trigger
  `trg_organizations_search_text` on name and address.
- organizations.search_text, backfilled.
- ix_organizations_search_trgm: the words of a query (LIKE) and a word with
  a typo in it (`%>`), both over this one index.
- ix_organizations_name: the picker's and the admin's list, both by name.
- pg_trgm, which discover_perf already created, and unaccent, which
  w2_search did: both IF NOT EXISTS, so this stands on its own.

Lock-safe, as w2_search is, and for the same reason (the Wave 1 deploy
stalled production behind migration locks):

- The column is nullable with no default: a catalogue change, no rewrite.
  The ALTER and the CREATE TRIGGER run under lock_timeout 3s, so a long
  query on the table makes this fail fast -- run it again -- rather than
  queue every request behind it.
- The backfill runs outside any long transaction: a statement a batch, each
  committed, each sized to take about 0.2 s, rows another writer holds
  skipped (SKIP LOCKED) and taken on a later pass. The trigger keeps every
  row written meanwhile right, so the backfill only ever has old rows to do.
- The indexes are built CONCURRENTLY in an autocommit block; one a failed
  run left INVALID is dropped and built again.

The SQL is written out here rather than imported: what this migration did
must not change when the app's code does.

Revision ID: org_search
Revises: add_event_cohosts
"""
import time

import sqlalchemy as sa
from alembic import op

revision = "org_search"
down_revision = "add_event_cohosts"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
#: Rows per backfill statement to start with, its bounds, and how long a
#: statement should take: the batch is resized after each so a statement
#: holds its rows about this long, however fast or loaded the database is.
BACKFILL_BATCH = 500
BACKFILL_MIN, BACKFILL_MAX = 20, 5000
BACKFILL_SECONDS = 0.2
#: Passes over the rows other writers held meanwhile.
BACKFILL_PASSES = 5

FUNCTIONS = (
    r"""
    CREATE OR REPLACE FUNCTION moa_org_search_norm(t text) RETURNS text
    LANGUAGE sql STABLE PARALLEL SAFE AS $$
      SELECT nullif(btrim(regexp_replace(moa_search_norm(t), '[^[:alnum:]]+', ' ', 'g')), '')
    $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_org_search_text(oname text, oaddr text) RETURNS text
    LANGUAGE sql STABLE PARALLEL SAFE AS $$
      SELECT moa_org_search_norm(concat_ws(' ', oname, oaddr))
    $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_orgs_search_text() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.search_text := moa_org_search_text(NEW.name, NEW.address);
      RETURN NEW;
    END $$
    """,
)

TRIGGER = r"""
    CREATE TRIGGER trg_organizations_search_text BEFORE INSERT OR UPDATE OF name, address ON organizations
    FOR EACH ROW EXECUTE FUNCTION moa_orgs_search_text()
    """

INDEXES = (
    # (name, definition after ON)
    ("ix_organizations_search_trgm", "organizations USING gin (search_text gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_organizations_name", "organizations (name) WHERE NOT is_deleted"),
)

#: What a row's column should be, and which rows still need it.
BACKFILL_ASSIGN = "search_text = moa_org_search_text(t.name, t.address)"
BACKFILL_NEEDS = "search_text IS NULL"

DROPPED_FUNCTIONS = (
    "moa_orgs_search_text()",
    "moa_org_search_text(text, text)",
    "moa_org_search_norm(text)",
)


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


def _build(name: str, definition: str) -> None:
    """CREATE INDEX CONCURRENTLY, once: a valid one is left alone, an invalid
    one (a build that failed) is dropped and built again."""
    state = _state(name)
    if state:
        return
    if state is False:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {definition}")


def _backfill(bind) -> None:
    """Every row's search_text, a batch of rows per statement, each committed
    (with synchronous_commit off: a batch lost to a crash is simply done
    again, its rows still need it); rows held by another writer skipped and
    tried again on a later pass."""
    batch = BACKFILL_BATCH
    left = 0
    for attempt in range(BACKFILL_PASSES):
        last = 0
        while True:
            ids = [r[0] for r in bind.execute(
                sa.text(
                    f"SELECT id FROM organizations WHERE id > :last AND ({BACKFILL_NEEDS}) ORDER BY id LIMIT :n"
                ),
                {"last": last, "n": batch},
            )]
            if not ids:
                break
            last = ids[-1]
            started = time.monotonic()
            bind.execute(sa.text("BEGIN"))
            bind.execute(sa.text(LOCK_TIMEOUT))
            bind.execute(sa.text("SET LOCAL synchronous_commit = off"))
            bind.execute(
                sa.text(
                    f"UPDATE organizations t SET {BACKFILL_ASSIGN} WHERE t.id IN ("
                    f"SELECT id FROM organizations WHERE id = ANY(:ids) AND ({BACKFILL_NEEDS}) FOR UPDATE SKIP LOCKED)"
                ),
                {"ids": ids},
            )
            bind.execute(sa.text("COMMIT"))
            took = max(time.monotonic() - started, 0.001)
            batch = int(min(max(batch * BACKFILL_SECONDS / took, batch / 2, BACKFILL_MIN), batch * 2, BACKFILL_MAX))
        left = bind.execute(sa.text(f"SELECT count(*) FROM organizations WHERE {BACKFILL_NEEDS}")).scalar()
        if not left:
            break
        time.sleep(1.0 * (attempt + 1))
    if left:
        print(f"org_search: {left} organizations rows were held by other writers throughout; the six-hourly search check fills them")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS search_text text")
    for sql in FUNCTIONS:
        op.execute(sql.strip())
    op.execute("DROP TRIGGER IF EXISTS trg_organizations_search_text ON organizations")
    op.execute(TRIGGER.strip())

    with op.get_context().autocommit_block():
        _backfill(op.get_bind())
        # The planner's statistics for the new column, so the new index is
        # chosen from the first request (ANALYZE samples, and blocks no one).
        op.execute("ANALYZE organizations (search_text, name)")
        for name, definition in INDEXES:
            _build(name, definition)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _definition in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP TRIGGER IF EXISTS trg_organizations_search_text ON organizations")
    for signature in DROPPED_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS search_text")
