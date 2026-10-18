"""Write path at scale: timed emails by their due instant, one copy of each
journey email, registration lookups on indexes, a shared job schedule.

- scheduled_runs: one row per periodic job (the counter check's last run),
  so the schedule is shared by every worker instead of each having its own.
- email_templates.due_at, with the function and triggers that keep it (the
  same SQL as EMAIL_DUE_AT_SQL in app/api/emails/models.py), filled in for
  every existing row in batches of events. ix_email_templates_due serves the
  minute sweep.
- Journey emails copied more than once (the registration race, before
  uq_email_templates_*): the oldest copy of each stage is kept, on each event
  and in each club's library; the messages sent from a duplicate and any
  copy made from one point at the kept one; the duplicates are deleted. Then
  the unique indexes that stop it happening again.
- event_registrations_link: ix_registrations_event_email (the duplicate
  check), ix_registrations_event_created (the plan's monthly count, a paid
  event's places, the counter check), and the tracking link index made
  partial (most rows have no link).

Every index on events, registrations or email tables is built CONCURRENTLY,
outside the migration's transaction, so writes go on while it builds. One
left INVALID by a failed build is dropped and built again on the next run.

Downgrade drops what this adds and puts the full tracking link index back.
The duplicate journey emails it deleted are not restored.

Revision ID: write_path
Revises: club_domains
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "write_path"
down_revision = "club_domains"
branch_labels = None
depends_on = None

#: Events per backfill statement.
BACKFILL_BATCH = 20_000

DUE_AT_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_email_due_at(trig json, starts timestamptz, hours double precision)
    RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
    DECLARE
      mode text := trig->>'mode';
      mins text := trig->>'minutes';
    BEGIN
      IF starts IS NULL OR mode IS NULL OR mode NOT IN ('BEFORE_START', 'AFTER_END')
         OR mins IS NULL OR mins !~ '^[0-9]+(\\.0+)?$' OR mins::numeric = 0 THEN
        RETURN NULL;
      END IF;
      IF mode = 'BEFORE_START' THEN
        RETURN starts - make_interval(mins => mins::numeric::int);
      END IF;
      RETURN starts + coalesce(hours, 0) * interval '1 hour' + make_interval(mins => mins::numeric::int);
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_email_template_due() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      starts timestamptz;
      hours double precision;
    BEGIN
      IF NEW.event_id IS NULL THEN
        NEW.due_at := NULL;
        RETURN NEW;
      END IF;
      SELECT event_datetime, duration INTO starts, hours FROM events WHERE id = NEW.event_id;
      NEW.due_at := moa_email_due_at(NEW.trigger, starts, hours);
      RETURN NEW;
    END $$
    """,
    "DROP TRIGGER IF EXISTS trg_email_templates_due ON email_templates",
    """
    CREATE TRIGGER trg_email_templates_due
    BEFORE INSERT OR UPDATE OF trigger, event_id ON email_templates
    FOR EACH ROW EXECUTE FUNCTION moa_email_template_due()
    """,
    """
    CREATE OR REPLACE FUNCTION moa_event_email_due() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.event_datetime IS DISTINCT FROM OLD.event_datetime OR NEW.duration IS DISTINCT FROM OLD.duration THEN
        UPDATE email_templates SET due_at = moa_email_due_at(trigger, NEW.event_datetime, NEW.duration)
        WHERE event_id = NEW.id;
      END IF;
      RETURN NULL;
    END $$
    """,
    "DROP TRIGGER IF EXISTS trg_events_email_due ON events",
    """
    CREATE TRIGGER trg_events_email_due
    AFTER UPDATE OF event_datetime, duration ON events
    FOR EACH ROW EXECUTE FUNCTION moa_event_email_due()
    """,
)

# The duplicates of a journey stage: every copy but the oldest, with the
# oldest it gives way to. On events, then in the clubs' libraries.
_DUPLICATES = """
INSERT INTO _journey_duplicates (dup_id, keep_id)
SELECT id, keep_id FROM (
  SELECT id,
         first_value(id) OVER w AS keep_id,
         row_number() OVER w AS n
  FROM email_templates
  WHERE {where} AND stage <> 'CUSTOM'
  WINDOW w AS (PARTITION BY {key}, stage ORDER BY created_at, id)
) ranked
WHERE n > 1
"""

INDEXES = (
    # (name, table, definition after ON <table>)
    ("uq_email_templates_event_stage", "email_templates", "UNIQUE", "(event_id, stage) WHERE stage <> 'CUSTOM'"),
    ("uq_email_templates_library_stage", "email_templates", "UNIQUE", "(club_id, stage) WHERE event_id IS NULL AND stage <> 'CUSTOM'"),
    ("ix_email_templates_due", "email_templates", "", "(due_at) WHERE enabled AND due_at IS NOT NULL"),
    ("ix_registrations_event_email", "event_registrations_link", "", "(event_id, lower(btrim(email))) WHERE NOT is_deleted"),
    ("ix_registrations_event_created", "event_registrations_link", "", "(event_id, created_at) INCLUDE (is_paid) WHERE NOT is_deleted"),
)

TRACKING = "ix_event_registrations_link_tracking_link_id"


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


def _build(name: str, table: str, unique: str, definition: str) -> None:
    """CREATE [UNIQUE] INDEX CONCURRENTLY, once: a valid one is left alone, an
    invalid one (a build that failed) is dropped and built again. Inside
    an autocommit block."""
    state = _state(name)
    if state:
        return
    if state is False:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE {unique} INDEX CONCURRENTLY {name} ON {table} {definition}")


def _dedupe() -> int:
    """Delete the duplicate journey emails, re-pointing what refers to them.
    One transaction. Returns how many were deleted."""
    bind = op.get_bind()
    bind.execute(sa.text("CREATE TEMP TABLE IF NOT EXISTS _journey_duplicates (dup_id uuid PRIMARY KEY, keep_id uuid NOT NULL)"))
    bind.execute(sa.text("TRUNCATE _journey_duplicates"))
    bind.execute(sa.text(_DUPLICATES.format(where="event_id IS NOT NULL", key="event_id")))
    bind.execute(sa.text(_DUPLICATES.format(where="event_id IS NULL", key="club_id")))
    found = bind.execute(sa.text("SELECT count(*) FROM _journey_duplicates")).scalar() or 0
    if found:
        bind.execute(sa.text(
            "UPDATE messages m SET template_id = d.keep_id FROM _journey_duplicates d WHERE m.template_id = d.dup_id"
        ))
        bind.execute(sa.text(
            "UPDATE email_templates t SET source_id = d.keep_id FROM _journey_duplicates d WHERE t.source_id = d.dup_id"
        ))
        bind.execute(sa.text("DELETE FROM email_templates t USING _journey_duplicates d WHERE t.id = d.dup_id"))
    bind.execute(sa.text("DROP TABLE _journey_duplicates"))
    return int(found)


def upgrade() -> None:
    # Fast, and first: the columns and the functions the new code reads.
    op.create_table(
        "scheduled_runs",
        sa.Column("name", sa.String(60), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
    )
    op.add_column("email_templates", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    for sql in DUE_AT_SQL:
        op.execute(sql)

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        # Every timed template's instant, a batch of events per statement (the
        # triggers above keep rows written meanwhile right).
        lo, hi = bind.execute(sa.text(
            "SELECT min(event_id), max(event_id) FROM email_templates WHERE event_id IS NOT NULL"
        )).one()
        start = lo
        while start is not None and start <= hi:
            bind.execute(
                sa.text(
                    "UPDATE email_templates t SET due_at = moa_email_due_at(t.trigger, e.event_datetime, e.duration) "
                    "FROM events e WHERE e.id = t.event_id AND t.event_id BETWEEN :lo AND :hi "
                    "AND (t.trigger->>'mode') IN ('BEFORE_START', 'AFTER_END')"
                ),
                {"lo": start, "hi": start + BACKFILL_BATCH - 1},
            )
            start += BACKFILL_BATCH

        # One copy of each journey email, then the indexes that keep it so.
        # The duplicates are looked for again just before each unique index,
        # in case a copy was made since (the new code inserts with ON
        # CONFLICT DO NOTHING, which only refuses once the index is there).
        for name, table, unique, definition in INDEXES:
            if unique and not _state(name):
                bind.execute(sa.text("BEGIN"))
                try:
                    _dedupe()
                except Exception:
                    bind.execute(sa.text("ROLLBACK"))
                    raise
                bind.execute(sa.text("COMMIT"))
            _build(name, table, unique, definition)

        # The tracking link index, partial: built under a new name, then the
        # full one dropped and the new one given its name.
        if _state(TRACKING) is not None and not _partial(TRACKING):
            _build(f"{TRACKING}_p", "event_registrations_link", "", "(tracking_link_id) WHERE tracking_link_id IS NOT NULL")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {TRACKING}")
            op.execute(f"ALTER INDEX {TRACKING}_p RENAME TO {TRACKING}")
        elif _state(TRACKING) is None:
            _build(TRACKING, "event_registrations_link", "", "(tracking_link_id) WHERE tracking_link_id IS NOT NULL")


def _partial(name: str) -> bool:
    row = op.get_bind().execute(
        sa.text(
            "SELECT i.indpred IS NOT NULL FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND c.relnamespace = current_schema()::regnamespace"
        ),
        {"name": name},
    ).first()
    return bool(row and row[0])


def downgrade() -> None:
    with op.get_context().autocommit_block():
        if _partial(TRACKING):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {TRACKING}_full")
            op.execute(f"CREATE INDEX CONCURRENTLY {TRACKING}_full ON event_registrations_link (tracking_link_id)")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {TRACKING}")
            op.execute(f"ALTER INDEX {TRACKING}_full RENAME TO {TRACKING}")
        for name, _table, _unique, _definition in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute("DROP TRIGGER IF EXISTS trg_events_email_due ON events")
    op.execute("DROP TRIGGER IF EXISTS trg_email_templates_due ON email_templates")
    op.execute("DROP FUNCTION IF EXISTS moa_event_email_due()")
    op.execute("DROP FUNCTION IF EXISTS moa_email_template_due()")
    op.execute("DROP FUNCTION IF EXISTS moa_email_due_at(json, timestamptz, double precision)")
    op.drop_column("email_templates", "due_at")
    op.drop_table("scheduled_runs")
