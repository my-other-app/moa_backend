"""Discover at scale: each club's place kept on the club, a table for the
front page's slow numbers, and six indexes (docs/SCALING.md, fixes 1,
4 and 12; app/api/discover/places.py, stats.py and upcoming.py).

- clubs.place_lat / place_lng: the club's own point, else the middle of its
  in-person events' points (events not deleted, not drafts, not online, with
  a point). Kept from here on by triggers on clubs and events (the same SQL
  as app/api/discover/places.PLACE_SQL), and filled here for every club.
- discover_stats (name, value, computed_at): numbers the front page shows
  that cost a read of every event to count; "cities" is counted here once.
- ix_clubs_place (place_lat, place_lng) over the listed clubs with a place:
  "clubs near" as a box on an index.
- ix_clubs_user_id: the club an account owns, which every signed-in request
  loads with the user (app/core/auth); without it, a read of every club.
- ix_events_shown_created: what can be shown, by when it was made, for
  sort=newest (the site's feeds) to walk instead of sorting every event on.
- ix_events_shown_name_trgm, ix_events_shown_location_trgm: trigram indexes
  over what can be shown (the status clock moves what is over out of them),
  so the search's typo fallback rechecks those and not every event ever held.
- ix_events_long_ends_at: the events longer than 72 hours (the new limit on
  an event's length; any saved before it), by when they end, so "what is on"
  knows how far back to look while one of them still runs.

Lock-light, in three steps: the columns, the table and the triggers in one
short transaction (adding a nullable column without a default only touches
the catalog; creating the triggers holds writes to events and clubs for as
long as the transaction, milliseconds); then, each in its own transaction,
the backfill (an UPDATE of the clubs whose place it sets: one pass over the
events with a point), the count of cities (one pass over the events), and
the indexes, built CONCURRENTLY so writes go on meanwhile. Each index is
dropped first if a failed run left it invalid. Writes between the first step
and the backfill are already kept by the triggers; POST
/admin/discover/recompute-club-places (or scripts/recompute_club_places.py)
puts right anything a concurrent write and the backfill crossed on.

Downgrade drops the indexes (concurrently), the triggers and functions, the
table and the columns.

Revision ID: discover_scale
Revises: write_path
"""
import sqlalchemy as sa
from alembic import op

revision = "discover_scale"
down_revision = "write_path"
branch_labels = None
depends_on = None

ENDS_AT = "(timezone('UTC', timezone('UTC', event_datetime) + duration * INTERVAL '1 hour'))"

PLACE_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_club_events_place(cid integer, OUT lat double precision, OUT lng double precision)
    LANGUAGE plpgsql STABLE AS $$
    BEGIN
      SELECT avg(e.lat), avg(e.lng) INTO lat, lng FROM events e
      WHERE e.club_id = cid AND e.lat IS NOT NULL AND e.lng IS NOT NULL AND NOT e.is_deleted
        AND e.status <> 'draft' AND NOT coalesce(e.is_online, false);
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_refresh_club_place(cid integer) RETURNS void LANGUAGE plpgsql AS $$
    BEGIN
      IF cid IS NULL THEN
        RETURN;
      END IF;
      UPDATE clubs c SET place_lat = p.lat, place_lng = p.lng
      FROM moa_club_events_place(cid) p
      WHERE c.id = cid AND (c.lat IS NULL OR c.lng IS NULL)
        AND (c.place_lat IS DISTINCT FROM p.lat OR c.place_lng IS DISTINCT FROM p.lng);
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_club_place() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.lat IS NOT NULL AND NEW.lng IS NOT NULL THEN
        NEW.place_lat := NEW.lat;
        NEW.place_lng := NEW.lng;
      ELSE
        SELECT p.lat, p.lng INTO NEW.place_lat, NEW.place_lng FROM moa_club_events_place(NEW.id) p;
      END IF;
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_club_place_from_events() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM moa_refresh_club_place(OLD.club_id);
      END IF;
      IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND NEW.club_id IS DISTINCT FROM OLD.club_id) THEN
        PERFORM moa_refresh_club_place(NEW.club_id);
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_clubs_place BEFORE INSERT OR UPDATE OF lat, lng ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_club_place()
    """,
    """
    CREATE TRIGGER trg_events_club_place_insert AFTER INSERT ON events
    FOR EACH ROW WHEN (NEW.lat IS NOT NULL AND NEW.lng IS NOT NULL)
    EXECUTE FUNCTION moa_club_place_from_events()
    """,
    """
    CREATE TRIGGER trg_events_club_place_delete AFTER DELETE ON events
    FOR EACH ROW WHEN (OLD.lat IS NOT NULL AND OLD.lng IS NOT NULL)
    EXECUTE FUNCTION moa_club_place_from_events()
    """,
    """
    CREATE TRIGGER trg_events_club_place_update AFTER UPDATE OF lat, lng, is_online, is_deleted, status, club_id ON events
    FOR EACH ROW WHEN (
      ((OLD.lat IS NOT NULL AND OLD.lng IS NOT NULL) OR (NEW.lat IS NOT NULL AND NEW.lng IS NOT NULL))
      AND (OLD.lat IS DISTINCT FROM NEW.lat OR OLD.lng IS DISTINCT FROM NEW.lng
           OR coalesce(OLD.is_online, false) IS DISTINCT FROM coalesce(NEW.is_online, false)
           OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted
           OR (OLD.status = 'draft') IS DISTINCT FROM (NEW.status = 'draft')
           OR OLD.club_id IS DISTINCT FROM NEW.club_id)
    )
    EXECUTE FUNCTION moa_club_place_from_events()
    """,
)

BACKFILL = """
UPDATE clubs c SET place_lat = p.lat, place_lng = p.lng
FROM (
  SELECT k.id,
         CASE WHEN k.lat IS NOT NULL AND k.lng IS NOT NULL THEN k.lat ELSE ev.lat END AS lat,
         CASE WHEN k.lat IS NOT NULL AND k.lng IS NOT NULL THEN k.lng ELSE ev.lng END AS lng
  FROM clubs k
  LEFT JOIN (
    SELECT club_id, avg(lat) AS lat, avg(lng) AS lng FROM events
    WHERE lat IS NOT NULL AND lng IS NOT NULL AND NOT is_deleted AND status <> 'draft' AND NOT coalesce(is_online, false)
    GROUP BY club_id
  ) ev ON ev.club_id = k.id
) p
WHERE p.id = c.id AND (c.place_lat IS DISTINCT FROM p.lat OR c.place_lng IS DISTINCT FROM p.lng)
"""

CITIES = """
INSERT INTO discover_stats (name, value, computed_at)
SELECT 'cities', count(DISTINCT lower(trim(e.location_name))), now()
FROM events e JOIN clubs c ON c.id = e.club_id
WHERE NOT e.is_deleted AND e.status IN ('published', 'live', 'closed', 'archived') AND c.is_verified AND NOT c.is_deleted
  AND NOT coalesce(e.is_online, false) AND lower(trim(e.location_name)) IS NOT NULL AND lower(trim(e.location_name)) <> ''
ON CONFLICT (name) DO UPDATE SET value = excluded.value, computed_at = excluded.computed_at
"""

INDEXES = (
    # Every signed-in request loads the account's club by clubs.user_id (app.core.auth): a read of the whole table without it.
    ("ix_clubs_user_id", "clubs (user_id)"),
    ("ix_clubs_place", "clubs (place_lat, place_lng) WHERE is_verified AND NOT is_deleted AND place_lat IS NOT NULL AND place_lng IS NOT NULL"),
    ("ix_events_long_ends_at", f"events ({ENDS_AT}) WHERE NOT is_deleted AND duration > 72"),
    # sort=newest on the public lists (the feeds): what can be shown, the latest made first.
    ("ix_events_shown_created", "events (created_at, id) WHERE NOT is_deleted AND status IN ('published', 'live')"),
    # The search's typo fallback looks among what can be shown, not every event ever held.
    ("ix_events_shown_name_trgm", "events USING gin (name gin_trgm_ops) WHERE NOT is_deleted AND status IN ('published', 'live')"),
    ("ix_events_shown_location_trgm", "events USING gin (location_name gin_trgm_ops) WHERE NOT is_deleted AND status IN ('published', 'live')"),
)


def upgrade() -> None:
    op.add_column("clubs", sa.Column("place_lat", sa.Float(), nullable=True))
    op.add_column("clubs", sa.Column("place_lng", sa.Float(), nullable=True))
    op.create_table(
        "discover_stats",
        sa.Column("name", sa.String(40), primary_key=True),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    for sql in PLACE_SQL:
        op.execute(sql)

    with op.get_context().autocommit_block():
        op.execute(BACKFILL)
        # The backfill rewrote most clubs' rows: clear the old versions now
        # rather than leave every read of clubs twice the pages until autovacuum.
        op.execute("VACUUM (ANALYZE) clubs")
        op.execute(CITIES)
        for name, definition in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {definition}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    for sql in (
        "DROP TRIGGER IF EXISTS trg_events_club_place_update ON events",
        "DROP TRIGGER IF EXISTS trg_events_club_place_delete ON events",
        "DROP TRIGGER IF EXISTS trg_events_club_place_insert ON events",
        "DROP TRIGGER IF EXISTS trg_clubs_place ON clubs",
        "DROP FUNCTION IF EXISTS moa_club_place_from_events()",
        "DROP FUNCTION IF EXISTS moa_club_place()",
        "DROP FUNCTION IF EXISTS moa_refresh_club_place(integer)",
        "DROP FUNCTION IF EXISTS moa_club_events_place(integer)",
    ):
        op.execute(sql)
    op.drop_table("discover_stats")
    op.drop_column("clubs", "place_lng")
    op.drop_column("clubs", "place_lat")
