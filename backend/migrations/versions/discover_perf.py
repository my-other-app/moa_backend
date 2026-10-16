"""Discover at scale: counters kept by triggers, trigram search, and the
indexes the public lists, the For You feed and the app's lists walk.

- pg_trgm, for ILIKE '%…%' and word similarity on names and places.
- events.going_count and clubs.follower_count, each kept exact by a trigger
  on the rows it counts (registrations not deleted; follows that follow and
  are not deleted), in the writer's transaction, and filled here from those
  rows. The trigger is created before the fill, and both run in this
  migration's transaction: creating a trigger locks its table against
  writes until commit, so nothing can register or follow in between.
- Partial and expression indexes, each used by a query in
  app/api/discover/service.py, app/api/events/recommendations.py or
  app/api/events/service.py (scripts/bench/RESULTS.md has the plans).

Plain CREATE INDEX, not CONCURRENTLY: at today's size each builds in
milliseconds. On the benchmark's 37,000 events and 400,000 registrations
the whole upgrade takes a few seconds (RESULTS.md).

The downgrade leaves the pg_trgm extension in place: other things may come
to use it, and creating it again is a no-op.

Revision ID: discover_perf
Revises: add_apple_provider
"""
from alembic import op

revision = "discover_perf"
down_revision = "add_apple_provider"
branch_labels = None
depends_on = None

SHOWN = "NOT is_deleted AND status IN ('published', 'live')"
ENDS_AT = "(timezone('UTC', timezone('UTC', event_datetime) + duration * INTERVAL '1 hour'))"

INDEXES = (
    # Events: what can be shown, soonest first.
    f"CREATE INDEX ix_events_shown_start ON events (event_datetime, id) WHERE {SHOWN}",
    # When each event ends ("not over yet" as a range).
    f"CREATE INDEX ix_events_ends_at ON events ({ENDS_AT}) WHERE NOT is_deleted",
    "CREATE INDEX ix_events_club_start ON events (club_id, event_datetime) WHERE NOT is_deleted",
    "CREATE INDEX ix_events_category_start ON events (category_id, event_datetime) WHERE NOT is_deleted",
    f"CREATE INDEX ix_events_shown_point ON events (lat, lng) WHERE {SHOWN} AND lat IS NOT NULL AND lng IS NOT NULL",
    "CREATE INDEX ix_events_name_trgm ON events USING gin (name gin_trgm_ops) WHERE NOT is_deleted",
    "CREATE INDEX ix_events_location_trgm ON events USING gin (location_name gin_trgm_ops) WHERE NOT is_deleted",
    # Clubs.
    "CREATE INDEX ix_clubs_name_trgm ON clubs USING gin (name gin_trgm_ops) WHERE NOT is_deleted",
    "CREATE INDEX ix_clubs_tagline_trgm ON clubs USING gin (tagline gin_trgm_ops) WHERE NOT is_deleted",
    "CREATE INDEX ix_clubs_location_trgm ON clubs USING gin (location_name gin_trgm_ops) WHERE NOT is_deleted",
    # Registrations and follows: one person's.
    "CREATE INDEX ix_registrations_user ON event_registrations_link (user_id) WHERE NOT is_deleted",
    "CREATE INDEX ix_club_users_user ON club_users_link (user_id) WHERE NOT is_deleted",
    # Link tables.
    "CREATE INDEX ix_event_interests_event ON event_interests_link (event_id) WHERE NOT is_deleted",
    "CREATE INDEX ix_event_interests_interest ON event_interests_link (interest_id, event_id) WHERE NOT is_deleted",
    "CREATE INDEX ix_club_interests_club ON clubs_interests_link (club_id) WHERE NOT is_deleted",
    "CREATE INDEX ix_club_interests_interest ON clubs_interests_link (interest_id, club_id) WHERE NOT is_deleted",
    "CREATE INDEX ix_user_interests_user ON user_interests (user_id)",
    "CREATE INDEX ix_user_profiles_user ON user_profiles (user_id)",
    # The newest posts across every club.
    "CREATE INDEX ix_club_threads_newest ON club_threads (created_at DESC, id) WHERE NOT is_deleted",
)

GOING_COUNT = (
    """
    CREATE OR REPLACE FUNCTION moa_going_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.event_id = NEW.event_id AND OLD.is_deleted = NEW.is_deleted THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') AND NOT OLD.is_deleted THEN
        UPDATE events SET going_count = going_count - 1 WHERE id = OLD.event_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') AND NOT NEW.is_deleted THEN
        UPDATE events SET going_count = going_count + 1 WHERE id = NEW.event_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_registrations_going_count
    AFTER INSERT OR DELETE OR UPDATE OF event_id, is_deleted ON event_registrations_link
    FOR EACH ROW EXECUTE FUNCTION moa_going_count()
    """,
)

FOLLOWER_COUNT = (
    """
    CREATE OR REPLACE FUNCTION moa_follower_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.club_id = NEW.club_id AND OLD.is_following = NEW.is_following AND OLD.is_deleted = NEW.is_deleted THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.is_following AND NOT OLD.is_deleted THEN
        UPDATE clubs SET follower_count = follower_count - 1 WHERE id = OLD.club_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') AND NEW.is_following AND NOT NEW.is_deleted THEN
        UPDATE clubs SET follower_count = follower_count + 1 WHERE id = NEW.club_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_club_users_follower_count
    AFTER INSERT OR DELETE OR UPDATE OF club_id, is_following, is_deleted ON club_users_link
    FOR EACH ROW EXECUTE FUNCTION moa_follower_count()
    """,
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute("ALTER TABLE events ADD COLUMN going_count integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE clubs ADD COLUMN follower_count integer NOT NULL DEFAULT 0")
    for sql in GOING_COUNT + FOLLOWER_COUNT:
        op.execute(sql)
    op.execute(
        """
        UPDATE events e SET going_count = c.n
        FROM (SELECT event_id, count(*) AS n FROM event_registrations_link WHERE NOT is_deleted GROUP BY event_id) c
        WHERE c.event_id = e.id
        """
    )
    op.execute(
        """
        UPDATE clubs k SET follower_count = c.n
        FROM (SELECT club_id, count(*) AS n FROM club_users_link WHERE is_following AND NOT is_deleted GROUP BY club_id) c
        WHERE c.club_id = k.id
        """
    )

    for sql in INDEXES:
        op.execute(sql)


def downgrade() -> None:
    for sql in reversed(INDEXES):
        name = sql.split(" INDEX ", 1)[1].split(" ", 1)[0]
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute("DROP TRIGGER IF EXISTS trg_club_users_follower_count ON club_users_link")
    op.execute("DROP TRIGGER IF EXISTS trg_registrations_going_count ON event_registrations_link")
    op.execute("DROP FUNCTION IF EXISTS moa_follower_count()")
    op.execute("DROP FUNCTION IF EXISTS moa_going_count()")
    op.execute("ALTER TABLE clubs DROP COLUMN follower_count")
    op.execute("ALTER TABLE events DROP COLUMN going_count")
