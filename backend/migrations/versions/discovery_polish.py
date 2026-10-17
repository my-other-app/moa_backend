"""Discovery polish: where points come from, the venue geocoder's cache, the
index the calendar and the date-range search probe, and forgotten-password
codes.

- events.geo_source and clubs.geo_source ('manual' | 'link' | 'geocoded'):
  which of a pin, the map link or the venue's words gave the point. Every
  point there today was read from a map link, so it is marked 'link'.
- geocode_cache: what the geocoder said about some words (a point, or that it
  found none, asked again after 30 days).
- ix_events_span: a GiST index on each event as a closed range of instants,
  start to end. `span && [from, to)` finds what touches a stretch of days,
  past ones included. GREATEST keeps the range from being inverted whatever
  the duration, so no write can fail on it. Over every row, not partial: the
  planner only uses an expression index's statistics when the index is not
  partial, and without them it takes any stretch of days to hold a few
  hundred events and picks plans for that (RESULTS.md). The ANALYZE at the
  end collects those statistics now rather than at autovacuum's next pass.
- password_reset_codes: forgotten-password codes, per address (kept as a
  keyed hash), with the account they went to when there is one.

Plain CREATE INDEX: it locks writes to events while it builds, milliseconds
at today's size (37,000 events: RESULTS.md).

Revision ID: discovery_polish
Revises: discover_perf
"""
import sqlalchemy as sa
from alembic import op

revision = "discovery_polish"
down_revision = "discover_perf"
branch_labels = None
depends_on = None

ENDS_AT = "timezone('UTC', timezone('UTC', event_datetime) + duration * INTERVAL '1 hour')"
SPAN_INDEX = f"CREATE INDEX ix_events_span ON events USING gist (tstzrange(event_datetime, GREATEST(event_datetime, {ENDS_AT}), '[]'))"


def upgrade() -> None:
    for table in ("events", "clubs"):
        op.add_column(table, sa.Column("geo_source", sa.String(10), nullable=True))
        op.execute(f"UPDATE {table} SET geo_source = 'link' WHERE lat IS NOT NULL AND lng IS NOT NULL")

    op.create_table(
        "geocode_cache",
        sa.Column("query", sa.String(200), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("found", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("query"),
    )

    op.execute(SPAN_INDEX)

    op.create_table(
        "password_reset_codes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email_key", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_password_reset_codes_key_sent", "password_reset_codes", ["email_key", "sent_at"])
    op.create_index("ix_password_reset_codes_sent", "password_reset_codes", ["sent_at"])

    # The span index's statistics, for the planner, now.
    op.execute("ANALYZE events")


def downgrade() -> None:
    op.drop_index("ix_password_reset_codes_sent", table_name="password_reset_codes")
    op.drop_index("ix_password_reset_codes_key_sent", table_name="password_reset_codes")
    op.drop_table("password_reset_codes")
    op.execute("DROP INDEX IF EXISTS ix_events_span")
    op.drop_table("geocode_cache")
    for table in ("clubs", "events"):
        op.drop_column(table, "geo_source")
