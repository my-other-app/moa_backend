"""The live room made the organiser's: a theme per event, rounds of quizzes
with their own settings and question kinds, teams, polls with settings and
two new kinds, the big screen's controls, and the pictures they use.

All of it additive, and safe while the room is in use:

- two new tables: live_themes (an event's theme and screen controls) and
  live_media (the pictures);
- new columns on live_quizzes, live_quiz_questions, live_quiz_players,
  live_quiz_answers, live_polls and live_poll_responses, each nullable or
  with a constant default (no table is rewritten; PG 11+ keeps a constant
  default in the catalogue);
- two values added to the pollkind type (ranking, text), outside a
  transaction;
- an event may hold several quizzes: the unique constraint on
  live_quizzes.event_id goes, after ix_live_quizzes_event_order (the
  event's rounds in their order) and uq_live_quizzes_one_live (at most one
  live per event) are built;
- the board's index gains the players' saved time (the faster first among
  equal scores): ix_live_quiz_players_rank is built and then the old
  ix_live_quiz_players_board dropped, only once the new one is valid.

Every index is built and dropped CONCURRENTLY in an autocommit block, after
dropping one of its name left INVALID by a build cut off, so running this
again finishes the job. The DDL that takes a table's lock (the columns, the
constraint) runs under lock_timeout 3s: it waits behind nothing for long.

Downgrade puts the unique constraint back (only possible while every event
has one quiz), the board's old index, and drops what this added. Enum
values cannot be dropped from a type in Postgres; they stay, unused.

Revision ID: live_custom
Revises: add_guest_posters
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "live_custom"
down_revision = "add_guest_posters"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"

#: (table, column) added, each nullable or with a constant default.
COLUMNS = (
    ("live_quizzes", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")),
    ("live_quizzes", sa.Column("settings", sa.JSON(), nullable=True)),
    ("live_quizzes", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True)),
    ("live_quizzes", sa.Column("revealed_at", sa.DateTime(timezone=True), nullable=True)),
    ("live_quizzes", sa.Column("asked", sa.JSON(), nullable=True)),
    ("live_quiz_questions", sa.Column("kind", sa.String(16), nullable=True)),
    ("live_quiz_questions", sa.Column("settings", sa.JSON(), nullable=True)),
    ("live_quiz_players", sa.Column("person", sa.String(64), nullable=True)),
    ("live_quiz_players", sa.Column("team", sa.String(16), nullable=True)),
    ("live_quiz_players", sa.Column("saved_ms", sa.BigInteger(), nullable=False, server_default="0")),
    ("live_quiz_players", sa.Column("streak", sa.Integer(), nullable=False, server_default="0")),
    ("live_quiz_players", sa.Column("streak_at", UUID(as_uuid=True), nullable=True)),
    ("live_quiz_answers", sa.Column("response", sa.JSON(), nullable=True)),
    ("live_polls", sa.Column("settings", sa.JSON(), nullable=True)),
    ("live_polls", sa.Column("closes_at", sa.DateTime(timezone=True), nullable=True)),
    ("live_poll_responses", sa.Column("status", sa.String(10), nullable=True)),
    ("live_poll_responses", sa.Column("author_name", sa.String(60), nullable=True)),
)

#: (name, CREATE ... body) of each index this builds.
INDEXES = (
    ("ix_live_quizzes_event_order", "INDEX CONCURRENTLY IF NOT EXISTS ix_live_quizzes_event_order ON live_quizzes (event_id, sort_order)"),
    ("uq_live_quizzes_one_live", "UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_live_quizzes_one_live ON live_quizzes (event_id) WHERE status = 'live'"),
    (
        "ix_live_quiz_players_rank",
        "INDEX CONCURRENTLY IF NOT EXISTS ix_live_quiz_players_rank ON live_quiz_players (quiz_id, score DESC, saved_ms DESC, created_at)",
    ),
    ("ix_live_media_event", "INDEX CONCURRENTLY IF NOT EXISTS ix_live_media_event ON live_media (event_id, created_at)"),
)

_VALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = :name AND n.nspname = current_schema() AND i.indisvalid AND i.indisready"
)
_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = :name AND n.nspname = current_schema() AND NOT (i.indisvalid AND i.indisready)"
)
_HAS_COLUMN = sa.text("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c")
_HAS_TABLE = sa.text("SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :t")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(LOCK_TIMEOUT)
    if bind.execute(_HAS_TABLE, {"t": "live_themes"}).first() is None:
        op.create_table(
            "live_themes",
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("theme", sa.JSON(), nullable=True),
            sa.Column("screen", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if bind.execute(_HAS_TABLE, {"t": "live_media"}).first() is None:
        op.create_table(
            "live_media",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("image", sa.String(), nullable=False),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    for table, column in COLUMNS:
        if bind.execute(_HAS_COLUMN, {"t": table, "c": column.name}).first() is None:
            op.add_column(table, column)
    # The new kinds of poll, and the indexes: outside the transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE pollkind ADD VALUE IF NOT EXISTS 'ranking'")
        op.execute("ALTER TYPE pollkind ADD VALUE IF NOT EXISTS 'text'")
        for name, body in INDEXES:
            if bind.execute(_INVALID, {"name": name}).first() is not None:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE {body}")
        # Several quizzes per event, once the indexes that replace the
        # constraint are in place; one statement, a moment's lock.
        if bind.execute(_VALID, {"name": "uq_live_quizzes_one_live"}).first() is not None:
            op.execute("SET lock_timeout = '3s'")
            op.execute("ALTER TABLE live_quizzes DROP CONSTRAINT IF EXISTS live_quizzes_event_id_key")
            op.execute("RESET lock_timeout")
        if bind.execute(_VALID, {"name": "ix_live_quiz_players_rank"}).first() is not None:
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_live_quiz_players_board")


def downgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_live_quiz_players_board")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_live_quiz_players_board ON live_quiz_players (quiz_id, score DESC, created_at)")
        # Back to one quiz an event: only possible while no event has two.
        op.execute("SET lock_timeout = '3s'")
        op.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'live_quizzes_event_id_key') THEN "
            "ALTER TABLE live_quizzes ADD CONSTRAINT live_quizzes_event_id_key UNIQUE (event_id); END IF; END $$"
        )
        op.execute("RESET lock_timeout")
        for name, _body in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(LOCK_TIMEOUT)
    for table, column in reversed(COLUMNS):
        if bind.execute(_HAS_COLUMN, {"t": table, "c": column.name}).first() is not None:
            op.drop_column(table, column.name)
    op.execute("DROP TABLE IF EXISTS live_media")
    op.execute("DROP TABLE IF EXISTS live_themes")
