"""The job queue: `jobs`, and `job_workers`, its runners' heartbeats.

- jobs: one row per piece of work set off after a request (a mail, a push
  fan-out, a retention run...), written in the same transaction as the change
  it is about and run once that commits (app/core/jobs.py). Indexes:
  - ix_jobs_claim (name, priority, run_at, id) over unfinished rows: the
    claim's FOR UPDATE SKIP LOCKED walk, per name and priority, each a range
    that ends at the first row not yet due;
  - uq_jobs_dedupe_key, unique over unfinished rows: enqueue's ON CONFLICT
    DO NOTHING for a job already waiting;
  - ix_jobs_finished_at and ix_jobs_dead: the maintenance pass's deletes and
    the admin's dead letters.
- job_workers: a row per running runner, its heartbeat every 15 seconds.

Both tables are new and empty, so their indexes are built with them, in the
migration's transaction; nothing else is touched and no lock is taken on a
table in use. `after_commit_jobs` (the outbox this replaces) is left in place:
a process from before this release may still write to it until it is
restarted, and the jobs' maintenance pass moves what it holds into `jobs`
(app.core.after_commit.adopt_legacy). A later migration drops it once it
stays empty.

Downgrade drops the two tables; jobs not yet run are lost with them.

Revision ID: w2_platform
Revises: topic_placeholders
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "w2_platform"
down_revision = "topic_placeholders"
branch_labels = None
depends_on = None

UNFINISHED = "finished_at IS NULL AND NOT dead"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("locked_by", sa.String(120), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("dead", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()")),
        sa.CheckConstraint("priority BETWEEN -2 AND 2", name="ck_jobs_priority"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["name", "priority", "run_at", "id"], postgresql_where=sa.text(UNFINISHED))
    # A queue: every job is an insert and two updates, so dead row versions
    # pile up fast and every claim walks past their index entries until a
    # vacuum. Vacuumed every few thousand of them rather than at a fraction
    # of a table that keeps days of finished rows (the visibility map lets
    # it skip those pages, which no longer change).
    op.execute(
        "ALTER TABLE jobs SET (autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 2000, "
        "autovacuum_analyze_scale_factor = 0, autovacuum_analyze_threshold = 5000)"
    )
    op.create_index("uq_jobs_dedupe_key", "jobs", ["dedupe_key"], unique=True, postgresql_where=sa.text(f"dedupe_key IS NOT NULL AND {UNFINISHED}"))
    op.create_index("ix_jobs_finished_at", "jobs", ["finished_at"], postgresql_where=sa.text("finished_at IS NOT NULL"))
    op.create_index("ix_jobs_dead", "jobs", ["updated_at"], postgresql_where=sa.text("dead"))
    op.create_table(
        "job_workers",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("host", sa.String(200), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("sweeps", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("running", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("handled", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failed", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_table("job_workers")
    op.drop_index("ix_jobs_dead", table_name="jobs")
    op.drop_index("ix_jobs_finished_at", table_name="jobs")
    op.drop_index("uq_jobs_dedupe_key", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
